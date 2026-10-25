"""Reverse tunnel: a remote client's own network as the egress of its server-side service.

The client holds a WebSocket open on ``/api/tunnel`` before it creates its remote session. The server gives the
service a SOCKS5 proxy on loopback, and sends every connection that proxy accepts to the client, which
dials it from its own connection and relays the bytes. Only the client dials. TLS stays end to end between
the server and the service, so the client sees only ciphertext.
"""

from __future__ import annotations

import asyncio
import ipaddress
import itertools
import json
import logging
import secrets
import socket
import struct
import threading
from typing import Any, Optional

import aiohttp
from aiohttp import WSMsgType, web

log = logging.getLogger("tunnel")

OPEN, OK, DATA, CLOSE = range(4)
HEADER = struct.Struct("!IB")  # stream id, frame kind
HEARTBEAT = 20
GRACE = 60
RELINK_WAIT = 10
OPEN_TIMEOUT = 15
FAKE_IP_POOLS = (ipaddress.ip_network("198.18.0.0/15"), ipaddress.ip_network("2001:2::/48"))
MAX_TUNNELS = 4
MAX_STREAMS = 256
CHUNK = 65536
BACKLOG = 64
MAX_MESSAGE = 4 * CHUNK
ROUTE = "/api/tunnel"
CHECK_ROUTE = "/api/tunnel/check"


def frame(sid: int, kind: int, data: bytes = b"") -> bytes:
    return HEADER.pack(sid, kind) + data


def new_queue() -> asyncio.Queue[Optional[bytes]]:
    return asyncio.Queue(maxsize=BACKLOG + 1)  # the extra slot always takes the end marker


class Link:
    """One WebSocket that carries many TCP connections, each with its own id."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.lock = asyncio.Lock()
        self.streams: dict[int, asyncio.Queue[Optional[bytes]]] = {}

    async def send(self, sid: int, kind: int, data: bytes = b"") -> None:
        async with self.lock:
            await self.ws.send_bytes(frame(sid, kind, data))

    async def pump(self, sid: int, reader: asyncio.StreamReader) -> None:
        """Copy a socket into the WebSocket until either side closes."""
        try:
            while chunk := await reader.read(CHUNK):
                await self.send(sid, DATA, chunk)
        except (ConnectionError, RuntimeError):
            pass
        finally:
            await self.close_stream(sid)  # the peer does not echo CLOSE, so this also ends our drain

    async def drain(self, q: asyncio.Queue[Optional[bytes]], writer: asyncio.StreamWriter) -> None:
        """Copy the bytes that the peer sends for one connection into a socket, then close the socket."""
        try:
            while (chunk := await q.get()) is not None:
                writer.write(chunk)
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()

    def end(self, sid: int) -> bool:
        """End one connection's drain; False when the connection was already gone."""
        if (q := self.streams.pop(sid, None)) is None:
            return False
        q.put_nowait(None)
        return True

    async def close_stream(self, sid: int) -> None:
        """End one connection here and tell the peer."""
        if self.end(sid) and not self.ws.closed:
            await self.send(sid, CLOSE)

    async def feed(self, sid: int, body: bytes) -> None:
        """Queue bytes for one connection; drop a connection whose socket has stopped reading them."""
        q = self.streams.get(sid)
        if q is None:
            return
        if q.qsize() < BACKLOG:
            q.put_nowait(body)
        else:
            await self.close_stream(sid)

    def end_all(self) -> None:
        for sid in list(self.streams):
            self.end(sid)

    async def relay(
        self, sid: int, q: asyncio.Queue[Optional[bytes]], reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await asyncio.gather(self.pump(sid, reader), self.drain(q, writer))


TUNNELS: dict[str, "Tunnel"] = {}


class Tunnel:
    """The server side of one client's tunnel: a loopback SOCKS5 listener and the client's current link."""

    def __init__(self, owner: Optional[str]) -> None:
        self.id = secrets.token_urlsafe(16)
        self.secret = secrets.token_urlsafe(24)
        self.owner = owner
        self.link: Optional[Link] = None
        self.pending: dict[int, asyncio.Future[bool]] = {}
        self.sids = itertools.count(1)
        self.listener: Optional[asyncio.Server] = None
        self.expiry: Optional[asyncio.TimerHandle] = None
        self.linked = asyncio.Event()
        self.closing: Optional[asyncio.Task[None]] = None

    @property
    def port(self) -> int:
        assert self.listener is not None
        return int(self.listener.sockets[0].getsockname()[1])

    @property
    def proxy_uri(self) -> str:
        # without credentials, another API key could use this port as a raw socks5h://127.0.0.1 proxy
        return f"socks5h://{self.id}:{self.secret}@127.0.0.1:{self.port}"

    async def start(self) -> None:
        self.listener = await asyncio.start_server(self.accept, "127.0.0.1", 0)
        TUNNELS[self.id] = self
        log.info(f"Tunnel {self.id[:6]} opened on 127.0.0.1:{self.port}")

    async def close(self) -> None:
        if TUNNELS.pop(self.id, None):
            log.info(f"Tunnel {self.id[:6]} closed")
        if self.expiry:
            self.expiry.cancel()
        if self.listener:
            self.listener.close()
        await self.detach()

    async def detach(self) -> None:
        link, self.link = self.link, None
        self.linked.clear()
        for fut in self.pending.values():
            fut.cancel()
        self.pending.clear()
        if link:
            link.end_all()
            await link.ws.close()

    async def on_frame(self, link: Link, data: bytes) -> None:
        """Act on a frame from the client. An OPEN from the client is ignored: the server never dials for it."""
        if len(data) < HEADER.size:
            return
        sid, kind = HEADER.unpack_from(data)
        if kind == OK and (fut := self.pending.pop(sid, None)) and not fut.done():
            fut.set_result(True)
        elif kind == DATA:
            await link.feed(sid, data[HEADER.size :])
        elif kind == CLOSE:
            if (fut := self.pending.pop(sid, None)) and not fut.done():
                fut.set_result(False)
            link.end(sid)

    async def wait_link(self) -> Optional[Link]:
        """The current link, waiting ``RELINK_WAIT`` seconds for a dropped one to come back."""
        if not self.link:
            try:
                await asyncio.wait_for(self.linked.wait(), RELINK_WAIT)
            except asyncio.TimeoutError:
                pass
        return self.link

    async def accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Take one SOCKS5 CONNECT with this tunnel's username and password, and open it on the client."""

        def refuse(answer: bytes) -> None:
            writer.write(answer)
            writer.close()

        def reply(code: int) -> bytes:
            return bytes((5, code, 0, 1)) + bytes(6)

        try:
            version, methods = await reader.readexactly(2)
            if version != 5 or 2 not in await reader.readexactly(methods):
                return refuse(b"\x05\xff")
            writer.write(b"\x05\x02")
            auth_version = (await reader.readexactly(1))[0]
            user = await reader.readexactly((await reader.readexactly(1))[0])
            password = await reader.readexactly((await reader.readexactly(1))[0])
            if not (
                auth_version == 1
                and secrets.compare_digest(user, self.id.encode())
                and secrets.compare_digest(password, self.secret.encode())
            ):
                return refuse(b"\x01\x01")
            writer.write(b"\x01\x00")
            _, command, _, address_type = await reader.readexactly(4)
            if address_type == 1:
                host = socket.inet_ntop(socket.AF_INET, await reader.readexactly(4))
            elif address_type == 4:
                host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
            elif address_type == 3:
                host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
            else:
                return refuse(reply(8))  # address type not supported
            port = await reader.readexactly(2)
        except (asyncio.IncompleteReadError, ConnectionError, UnicodeDecodeError):
            writer.close()
            return
        if command != 1:
            return refuse(reply(7))  # command not supported
        link = await self.wait_link()
        if not link:
            return refuse(reply(5))
        sid = next(self.sids)
        fut = self.pending[sid] = asyncio.get_running_loop().create_future()
        q = link.streams[sid] = new_queue()
        ok = False
        try:
            await link.send(sid, OPEN, port + host.encode())
            ok = await asyncio.wait_for(fut, OPEN_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.CancelledError, ConnectionError, RuntimeError):
            pass
        if not ok:
            self.pending.pop(sid, None)
            await link.close_stream(sid)  # a client still dialling would relay into a dead stream
            return refuse(reply(5))  # connection refused
        writer.write(reply(0))
        await link.relay(sid, q, reader, writer)

    async def serve(self, ws: web.WebSocketResponse) -> None:
        """Make ``ws`` the tunnel's link until it closes, then wait ``GRACE`` seconds for a reconnect."""
        if self.expiry:
            self.expiry.cancel()
            self.expiry = None
        await self.detach()
        link = self.link = Link(ws)
        try:
            await ws.send_str(json.dumps({"id": self.id}))
            self.linked.set()
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    await self.on_frame(link, msg.data)
        finally:
            if self.link is link:
                self.link = None
                self.linked.clear()
                link.end_all()
                if self.id in TUNNELS:
                    self.expiry = asyncio.get_running_loop().call_later(GRACE, self.expire)

    def expire(self) -> None:
        self.closing = asyncio.ensure_future(self.close())  # held, so the loop cannot drop the task


def tunnel_proxy(tunnel_id: str, owner: Optional[str]) -> Optional[str]:
    """The loopback proxy URI for the tunnel ``tunnel_id`` that the caller owns, or None."""
    tunnel = TUNNELS.get(tunnel_id)
    return tunnel.proxy_uri if tunnel and tunnel.owner == owner else None


async def close_tunnels(_: web.Application) -> None:
    for tunnel in list(TUNNELS.values()):
        await tunnel.close()


def parse_ip(address: str) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return ipaddress.ip_address(address.partition("%")[0])
    except ValueError:
        return None


def is_public_ip(address: str) -> bool:
    return (ip := parse_ip(address)) is not None and ip.is_global


def is_fake_ip(address: str) -> bool:
    """True for an address from a pool that fake-IP DNS in a TUN-mode VPN or proxy hands out for a name."""
    return (ip := parse_ip(address)) is not None and any(ip in pool for pool in FAKE_IP_POOLS)


async def connect_public(host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to the first public address of ``host``. The server picks the targets, so a name that resolves
    only to the client's own machine or LAN is refused. A fake IP passes only when it comes from a name lookup:
    the TUN maps it back to the real host, which the client cannot check."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    resolved = [str(info[4][0]) for info in infos]
    from_name = parse_ip(host) is None
    addresses = [a for a in resolved if is_public_ip(a) or (from_name and is_fake_ip(a))]
    if not addresses:
        raise PermissionError(f"{host} resolves only to local addresses ({', '.join(resolved)})")
    error: OSError = OSError(f"could not connect to {host}")
    for address in addresses:
        try:
            return await asyncio.wait_for(asyncio.open_connection(address, port), 10)
        except (OSError, asyncio.TimeoutError) as e:
            error = OSError(str(e))
    raise error


class TunnelClient:
    """The client side: a background thread that keeps the tunnel's WebSocket open and dials for the server."""

    def __init__(self, server_url: str, headers: dict[str, str]) -> None:
        self.url = server_url.rstrip("/") + ROUTE
        self.headers = headers
        self.id: Optional[str] = None
        self.error: Optional[BaseException] = None
        self.lost = False
        self.ready = threading.Event()
        self.relays: set[asyncio.Task[None]] = set()
        self.refused: set[str] = set()
        self.thread = threading.Thread(target=lambda: asyncio.run(self.run()), name="tunnel", daemon=True)
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.task: Optional[asyncio.Task[None]] = None

    def start(self, timeout: float = 20) -> str:
        """Open the tunnel and return its id."""
        self.thread.start()
        if not self.ready.wait(timeout) or not self.id:
            self.stop()  # or the thread opens the tunnel later and nobody uses it
            raise ConnectionError(f"Could not open a tunnel to the server: {self.error or 'timed out'}")
        return self.id

    def stop(self) -> None:
        """Close the tunnel and wait for its connections to end, so none is left pending at exit."""
        if self.loop and self.task and self.thread.is_alive():
            self.loop.call_soon_threadsafe(self.task.cancel)
            self.thread.join(5)

    async def run(self) -> None:
        self.loop, self.task = asyncio.get_running_loop(), asyncio.current_task()
        try:
            await self.connect()
        except asyncio.CancelledError:
            pass
        finally:
            for t in self.relays:
                t.cancel()
            await asyncio.gather(*self.relays, return_exceptions=True)

    async def connect(self) -> None:
        backoff = 1
        async with aiohttp.ClientSession(headers=self.headers) as http:
            while True:
                try:
                    url = f"{self.url}?id={self.id}" if self.id else self.url
                    async with http.ws_connect(url, heartbeat=HEARTBEAT, max_msg_size=MAX_MESSAGE) as ws:
                        hello = await ws.receive_json(timeout=15)
                        self.id = hello["id"]
                        self.ready.set()
                        backoff = 1
                        await self.serve(Link(ws))
                except aiohttp.WSServerHandshakeError as e:
                    self.error = e
                    # a 404 means the tunnel expired or the server has no tunnel route; a 5xx is a proxy in between
                    if not self.id or e.status < 500:
                        if self.id:
                            self.lost = True
                            log.warning(f"The server closed the tunnel (HTTP {e.status}), so its requests now fail")
                        self.ready.set()
                        return
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError) as e:
                    self.error = e
                    if not self.id:
                        self.ready.set()
                        return
                log.warning(f"The tunnel to the server dropped, reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def serve(self, link: Link) -> None:
        try:
            async for msg in link.ws:
                if msg.type != WSMsgType.BINARY or len(msg.data) < HEADER.size:
                    continue
                sid, kind = HEADER.unpack_from(msg.data)
                body = msg.data[HEADER.size :]
                if kind == OPEN:
                    if len(link.streams) >= MAX_STREAMS or sid in link.streams or len(body) < 3:
                        await link.send(sid, CLOSE)
                        continue
                    link.streams[sid] = new_queue()
                    host = body[2:].decode(errors="replace")
                    task = asyncio.ensure_future(self.open(link, sid, host, int.from_bytes(body[:2], "big")))
                    self.relays.add(task)
                    task.add_done_callback(self.relays.discard)
                elif kind == DATA:
                    await link.feed(sid, body)
                elif kind == CLOSE:
                    link.end(sid)
        finally:
            link.end_all()

    async def open(self, link: Link, sid: int, host: str, port: int) -> None:
        try:
            reader, writer = await connect_public(host, port)
        except PermissionError as e:
            if host not in self.refused:  # the server retries, so warn once per host
                self.refused.add(host)
                log.warning(f"Tunnel refused {e}")
            await link.close_stream(sid)
            return
        except (OSError, ValueError, asyncio.TimeoutError) as e:  # the idna codec raises ValueError on a bad hostname
            log.debug(f"Tunnel dial to {host}:{port} failed: {e}")
            await link.close_stream(sid)
            return
        if (q := link.streams.get(sid)) is None:  # the server gave up while we dialled
            writer.close()
            return
        await link.send(sid, OK)
        await link.relay(sid, q, reader, writer)
