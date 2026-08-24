#!/usr/bin/env python3
"""
Localhost fault-injection HTTP server for downloader reliability benchmarking.

Serves deterministic pseudo-random segment payloads (seeded per URL path, so the
bytes are reproducible and any output file can be hashed against them to prove
integrity) and applies a configurable *fault profile* to those responses. Stall
and reset faults are decided deterministically from a printed seed keyed on
(path, attempt), so a retry can recover instead of failing forever; rate-limit
windows are wall-clock relative to run start, so their timing is not seed-bound.

Fault profiles:
    stall        sleep N seconds mid-body on X% of (path, attempt)s
    reset        RST the connection mid-body on X% of (path, attempt)s
    rate-limit   reply 429 + Retry-After during recurring throttle windows
                 (window_open seconds serving / window_closed seconds throttled)
    tail-slow    heavily throttle the last K segments' bodies
    throttle-cut throttle every body and close it (FIN) after N seconds or N bytes
    flaky-first  fail attempt 1 per path (503), succeed on retry
    loopback     no faults; payloads served from memory (overhead ceiling)

Range requests are honored (206 + Content-Range) so byte-range / hedge / tail-boost
download paths exercise correctly, and an unsatisfiable resume gets a 416.

Standalone:
    uv run python scripts/fault_server.py --profile rate-limit --segments 32
    uv run python scripts/fault_server.py --profile stall --stall-secs 4 --port 8080
    uv run python scripts/fault_server.py --profile throttle-cut --segments 1 --seg-size 71303168

Importable: build a ``FaultServer``, serve it on a thread, and hash output against
``segment_payload(path, size)``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import socket
import struct
import threading
import time
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

MIB = 1024 * 1024
DEFAULT_SEED = 1729


def segment_payload(path: str, size: int) -> bytes:
    """Deterministic pseudo-random body for ``path``.

    Pure and seed-stable: the bench regenerates this to byte-compare every output
    file, so it must never depend on the fault seed, request order, or attempt.
    """
    seed = int.from_bytes(hashlib.sha256(path.encode("utf-8")).digest()[:8], "big")
    return random.Random(seed).randbytes(size)


@dataclass
class FaultProfile:
    """A named set of deterministic response faults. Presets live in ``PROFILES``."""

    name: str
    stall_pct: float = 0.0  # % of (path, attempt)s that stall mid-body
    stall_secs: float = 3.0  # seconds to hold when a stall fires
    reset_pct: float = 0.0  # % of (path, attempt)s that RST mid-body
    rate_limit: bool = False  # 429 + Retry-After during closed windows
    retry_after: int = 1  # Retry-After seconds advertised on a 429
    window_open: float = 5.0  # seconds serving normally before a throttle window
    window_closed: float = 5.0  # seconds replying 429 during a throttle window
    body_rate_kib: int = 0  # throttle every segment body to this KiB/s (0 = unthrottled)
    tail_slow: int = 0  # throttle the last K segments' bodies
    tail_rate_kib: int = 256  # throttle rate for tail-slow segments (KiB/s)
    flaky_first: bool = False  # fail attempt 1 per path, succeed after
    flaky_status: int = 503  # status returned by a flaky-first failure
    cut_secs: float = 0.0  # close the body cleanly (FIN) after this long (0 = never)
    cut_bytes: int = 0  # close the body cleanly (FIN) after this many bytes (0 = never)


PROFILES: dict[str, FaultProfile] = {
    "loopback": FaultProfile("loopback"),
    "stall": FaultProfile("stall", stall_pct=25.0, stall_secs=3.0),
    "reset": FaultProfile("reset", reset_pct=25.0),
    # short windows + a body throttle so a fast loopback batch actually spans several throttle
    # windows and trips the 429 + Retry-After retry path (a real 900 MB/s batch would otherwise
    # finish inside the first open window). Standalone use can widen the windows via overrides.
    "rate-limit": FaultProfile(
        "rate-limit", rate_limit=True, retry_after=1, window_open=1.0, window_closed=1.0, body_rate_kib=2048
    ),
    "tail-slow": FaultProfile("tail-slow", tail_slow=4, tail_rate_kib=256),
    "flaky-first": FaultProfile("flaky-first", flaky_first=True, flaky_status=503),
    # a CDN that throttles one flow and drops it on a timer: every connection delivers bytes
    # and then ends short, so each retry makes progress but no single attempt finishes. Not
    # seed-gated like stall and reset, because the real host cuts every attempt, not a share.
    "throttle-cut": FaultProfile("throttle-cut", body_rate_kib=480, cut_secs=6.5, cut_bytes=5 * MIB // 2),
}


class FaultServer(ThreadingHTTPServer):
    """Threaded loopback server serving seeded payloads under a ``FaultProfile``.

    Counters (total requests, per-status, per-path attempts) are the request-amplification
    signal the bench reads: a retry/hedge storm against a throttled host shows up as
    requests >> segment count. ``reset_counters`` re-baselines between benchmark runs.
    """

    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 128  # default 5 drops SYNs past 5 concurrent connects -> phantom 1s retransmit tail

    def __init__(
        self,
        profile: FaultProfile,
        seg_size: int,
        total_segments: int,
        seed: int = DEFAULT_SEED,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.profile = profile
        self.seg_size = seg_size
        self.total_segments = total_segments
        self.seed = seed
        self.stall_event = threading.Event()  # set at teardown to release stalled handlers
        self._lock = threading.Lock()
        self._request_count = 0
        self._status_counts: dict[int, int] = {}
        self._attempts: dict[str, int] = {}
        self._window_origin = time.monotonic()
        # precompute the known segment bodies so serving is a dict lookup (no per-request
        # generation), making the loopback profile a true python-overhead ceiling
        self._payloads: dict[str, bytes] = {
            f"/seg/{i}": segment_payload(f"/seg/{i}", seg_size) for i in range(total_segments)
        }
        super().__init__((host, port), FaultHandler)

    def handle_error(self, request: Any, client_address: Any) -> None:  # silence expected fault noise
        pass

    def payload(self, path: str) -> bytes:
        cached = self._payloads.get(path)
        if cached is not None:
            return cached
        with self._lock:  # unknown path (standalone use): generate + cache once
            body = self._payloads.get(path)
            if body is None:
                body = segment_payload(path, self.seg_size)
                self._payloads[path] = body
            return body

    def note_request(self) -> None:
        with self._lock:
            self._request_count += 1

    def note_status(self, code: int) -> None:
        with self._lock:
            self._status_counts[code] = self._status_counts.get(code, 0) + 1

    def note_attempt(self, path: str) -> int:
        with self._lock:
            n = self._attempts.get(path, 0) + 1
            self._attempts[path] = n
            return n

    def in_throttle_window(self) -> bool:
        p = self.profile
        period = p.window_open + p.window_closed
        if period <= 0:
            return False
        phase = (time.monotonic() - self._window_origin) % period
        return phase >= p.window_open

    def decides(self, kind: str, path: str, attempt: int, pct: float) -> bool:
        """Deterministic per-(path, attempt) fault gate: fires for ``pct`` percent of them."""
        if pct <= 0:
            return False
        digest = hashlib.sha256(f"{self.seed}:{kind}:{path}:{attempt}".encode("utf-8")).digest()
        return (int.from_bytes(digest[:4], "big") / 0xFFFFFFFF) * 100.0 < pct

    def reset_counters(self) -> None:
        with self._lock:
            self._request_count = 0
            self._status_counts.clear()
            self._attempts.clear()
        self._window_origin = time.monotonic()  # realign throttle windows to this run's start

    def stats(self) -> dict[str, Any]:
        with self._lock:
            max_attempts = max(self._attempts.values(), default=0)
            return {
                "requests": self._request_count,
                "status_counts": dict(sorted(self._status_counts.items())),
                "max_attempts": max_attempts,
            }


class FaultHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_: Any) -> None:  # silence per-request logging
        pass

    def _parse_range(self, total: int) -> Optional[tuple[int, int]]:
        header = self.headers.get("Range")
        if not header or not header.startswith("bytes="):
            return None
        start_s, _, end_s = header[len("bytes=") :].partition("-")
        if not start_s:  # RFC 7233 suffix range: bytes=-N means the last N bytes
            return max(0, total - int(end_s)), total - 1
        start = int(start_s)
        end = int(end_s) if end_s else total - 1
        if start > end:  # inverted range: report unsatisfiable so _serve answers 416
            return total, total - 1
        return start, min(end, total - 1)

    def do_GET(self) -> None:
        server: FaultServer = self.server  # type: ignore[assignment]
        server.note_request()
        if not self.path.startswith("/seg/"):
            self.send_error(404)
            return
        try:
            idx = int(self.path.rsplit("/", 1)[-1])
        except ValueError:
            self.send_error(404)
            return

        attempt = server.note_attempt(self.path)
        prof = server.profile

        if prof.flaky_first and attempt == 1:  # first attempt per path always fails, retry recovers
            self._send_status(prof.flaky_status)
            return
        if prof.rate_limit and server.in_throttle_window():  # recurring throttle window
            self._send_429(prof.retry_after)
            return

        rate = prof.body_rate_kib
        if prof.tail_slow and idx >= server.total_segments - prof.tail_slow:
            rate = prof.tail_rate_kib
        stall = server.decides("stall", self.path, attempt, prof.stall_pct)
        reset = server.decides("reset", self.path, attempt, prof.reset_pct)
        self._serve(
            server.payload(self.path),
            stall=stall,
            reset=reset,
            rate_kib=rate,
            cut_secs=prof.cut_secs,
            cut_bytes=prof.cut_bytes,
        )

    def _send_status(self, code: int) -> None:
        server: FaultServer = self.server  # type: ignore[assignment]
        server.note_status(code)
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_429(self, retry_after: int) -> None:
        server: FaultServer = self.server  # type: ignore[assignment]
        server.note_status(429)
        self.send_response(429)
        self.send_header("Retry-After", str(retry_after))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve(
        self,
        full: bytes,
        stall: bool = False,
        reset: bool = False,
        rate_kib: int = 0,
        cut_secs: float = 0.0,
        cut_bytes: int = 0,
    ) -> None:
        server: FaultServer = self.server  # type: ignore[assignment]
        rng = self._parse_range(len(full))
        if rng is not None and rng[0] >= len(full):  # unsatisfiable resume: a real CDN answers 416
            server.note_status(416)
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{len(full)}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if rng is not None:
            start, end = rng
            body = full[start : end + 1]
            server.note_status(206)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(full)}")
        else:
            body = full
            server.note_status(200)
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()

        try:
            if reset:  # send half, then abort the TCP connection (RST via SO_LINGER=0)
                half = max(1, len(body) // 2)
                self.wfile.write(body[:half])
                self.wfile.flush()
                sock = self.connection
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                sock.close()
                self.close_connection = True
            elif stall:  # send half, hold (a client READ_TIMEOUT shorter than stall_secs will retry), then finish
                half = max(1, len(body) // 2)
                self.wfile.write(body[:half])
                self.wfile.flush()
                server.stall_event.wait(server.profile.stall_secs)
                self.wfile.write(body[half:])
                self.wfile.flush()
            else:
                self._write_body(body, rate_kib, cut_secs, cut_bytes)
        except (BrokenPipeError, ConnectionError, OSError):
            self.close_connection = True

    def _cut(self) -> None:
        """End the body early with a clean half-close.

        FIN rather than RST: the client reads fewer bytes than Content-Length promised and
        raises IncompleteRead, which is the resumable short read a throttling CDN produces.
        An RST (see the reset fault) is a different error class on the client side.
        """
        try:
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        self.close_connection = True

    def _write_body(self, body: bytes, rate_kib: int, cut_secs: float = 0.0, cut_bytes: int = 0) -> None:
        if rate_kib <= 0 and cut_secs <= 0 and cut_bytes <= 0:
            self.wfile.write(body)
            return
        chunk = 64 * 1024  # throttle: fixed-size chunks paced to rate_kib KiB/s
        per_chunk = chunk / (rate_kib * 1024) if rate_kib > 0 else 0.0
        deadline = time.monotonic() + cut_secs if cut_secs > 0 else None
        sent = 0
        for off in range(0, len(body), chunk):
            size = min(chunk, len(body) - off)
            if cut_bytes > 0:
                size = min(size, cut_bytes - sent)
            self.wfile.write(body[off : off + size])
            self.wfile.flush()
            sent += size
            if cut_bytes > 0 and sent >= cut_bytes:
                self._cut()
                return
            if deadline is not None and time.monotonic() >= deadline:
                self._cut()
                return
            if per_chunk:
                time.sleep(per_chunk)


def resolve_profile(name: str, overrides: Optional[dict[str, Any]] = None) -> FaultProfile:
    """Return a copy of the named preset with any non-None ``overrides`` applied."""
    if name not in PROFILES:
        raise KeyError(f"unknown profile {name!r}; choices: {', '.join(sorted(PROFILES))}")
    base = asdict(PROFILES[name])
    for key, value in (overrides or {}).items():
        if value is not None and key in base:
            base[key] = value
    return FaultProfile(**base)


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=0, help="Listen port (0 = auto).")
    p.add_argument("--profile", choices=sorted(PROFILES), default="loopback", help="Fault profile to apply.")
    p.add_argument("--segments", type=int, default=32, help="Number of /seg/i payloads to precompute.")
    p.add_argument("--seg-size", type=int, default=2 * MIB, help="Bytes per segment payload.")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED, help="Fault RNG seed (printed for reproducibility).")
    # profile knobs (override the chosen preset when given)
    p.add_argument("--stall-pct", type=float, help="Override: %% of (path, attempt)s that stall.")
    p.add_argument("--stall-secs", type=float, help="Override: seconds a stall holds.")
    p.add_argument("--reset-pct", type=float, help="Override: %% of (path, attempt)s that RST.")
    p.add_argument("--retry-after", type=int, help="Override: Retry-After seconds on a 429.")
    p.add_argument("--window-open", type=float, help="Override: seconds serving before a throttle window.")
    p.add_argument("--window-closed", type=float, help="Override: seconds replying 429 during a window.")
    p.add_argument("--body-rate-kib", type=int, help="Override: throttle every body to this KiB/s (0 = off).")
    p.add_argument("--tail-slow", type=int, help="Override: throttle the last K segments.")
    p.add_argument("--tail-rate-kib", type=int, help="Override: tail-slow throttle rate (KiB/s).")
    p.add_argument("--cut-secs", type=float, help="Override: close each body (FIN) after this long (0 = never).")
    p.add_argument("--cut-bytes", type=int, help="Override: close each body (FIN) after this many bytes (0 = never).")
    return p


def main() -> None:
    args = _build_arg_parser().parse_args()
    overrides = {
        "stall_pct": args.stall_pct,
        "stall_secs": args.stall_secs,
        "reset_pct": args.reset_pct,
        "retry_after": args.retry_after,
        "window_open": args.window_open,
        "window_closed": args.window_closed,
        "body_rate_kib": args.body_rate_kib,
        "tail_slow": args.tail_slow,
        "tail_rate_kib": args.tail_rate_kib,
        "cut_secs": args.cut_secs,
        "cut_bytes": args.cut_bytes,
    }
    profile = resolve_profile(args.profile, overrides)
    server = FaultServer(profile, args.seg_size, args.segments, args.seed, port=args.port)
    host, port = server.server_address[0], server.server_address[1]
    config = {
        "listen": f"http://{host}:{port}",
        "seed": args.seed,
        "segments": args.segments,
        "seg_size": args.seg_size,
        "base_url": f"http://{host}:{port}/seg/{{i}}",
        "profile": asdict(profile),
    }
    print(json.dumps(config, indent=2), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stall_event.set()
        server.shutdown()


if __name__ == "__main__":
    main()
