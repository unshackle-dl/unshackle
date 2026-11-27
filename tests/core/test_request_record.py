"""The debug_requests http_request entry in the JSONL debug log, for both HTTP session types."""

from __future__ import annotations

import copy
import importlib
import json
import logging
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import m3u8
import pytest
import requests as rq
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from unshackle.core import service as service_module
from unshackle.core import session as http_session
from unshackle.core.config import Config, config
from unshackle.core.manifests import DASH, HLS
from unshackle.core.service import Service
from unshackle.core.session import MaxRetriesError, RecordedSession, RnetSession, record_requests, unrecorded
from unshackle.core.tracks.track import Track
from unshackle.core.utilities import close_debug_logger, init_debug_logger
from unshackle.core.utils import bitrate
from unshackle.core.utils.xml import load_xml

dl = importlib.import_module("unshackle.core.downloaders.requests")

pytestmark = pytest.mark.unit

BODY = b"x" * 64


class Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self) -> None:
        self.hits: list[str] = []
        self.docs: dict[str, str] = {}
        self.types: dict[str, str] = {}
        self.lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), Handler)
        self.host = f"127.0.0.1:{self.server_address[1]}"
        self.url = f"http://{self.host}"

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: Server

    def log_message(self, *_: Any) -> None:
        pass

    def reply(self) -> None:
        if length := int(self.headers.get("Content-Length") or 0):
            self.rfile.read(length)
        path = self.path.partition("?")[0]
        with self.server.lock:
            first = path not in self.server.hits
            self.server.hits.append(path)
        if path == "/old":
            self.send_response(302)
            self.send_header("Location", "/new")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/busy" and first:
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        ranged = bool(self.headers.get("Range")) and not path.startswith("/plain")
        body = self.server.docs[path].encode() if path in self.server.docs else BODY[:1] if ranged else BODY
        if path.startswith("/chunked"):
            self.send_response(200)
            self.send_header("Content-Type", self.server.types.get(path, "text/plain"))
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n")
            return
        self.send_response(206 if ranged else 200)
        if ranged:
            self.send_header("Content-Range", f"bytes 0-0/{len(BODY)}")
        if path in self.server.types:
            self.send_header("Content-Type", self.server.types[path])
        if not (path.startswith("/plain") and self.command == "HEAD"):
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = do_POST = do_PATCH = do_HEAD = reply


@pytest.fixture
def server() -> Iterator[Server]:
    srv = Server()
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def debug_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setattr(config, "debug_requests", True)
    path = tmp_path / "debug.jsonl"
    init_debug_logger(log_path=path, enabled=True)
    yield path
    close_debug_logger()


def records(path: Path) -> list[dict]:
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [e for e in lines if e.get("operation") == "http_request"]


def requests_session() -> rq.Session:
    session = rq.Session()
    adapter = HTTPAdapter(max_retries=Retry(total=2, backoff_factor=0, status_forcelist=[503]))
    session.mount("http://", adapter)
    record_requests(session)
    return session


def rnet_session() -> RnetSession:
    session = RnetSession(backoff_factor=0)
    session.get_sleep_time = lambda *_: 0  # type: ignore[method-assign]
    return session


def closed_port() -> str:
    srv = Server()
    srv.server_close()
    return srv.url


SESSIONS = [pytest.param(requests_session, id="requests"), pytest.param(rnet_session, id="rnet")]


@pytest.mark.parametrize("make_session", SESSIONS)
def test_the_entry_keeps_host_path_and_query_as_received(server, debug_log, make_session):
    make_session().get(f"{server.url}/api/{JWT}/items;hmac=a1b2?id=5&access_token=SECRET123")

    (entry,) = records(debug_log)
    assert entry["request"] == {
        "method": "GET",
        "host": server.host,
        "path": f"/api/{JWT}/items;hmac=a1b2",
        "query": "id=5&access_token=SECRET123",
        "attempt": 1,
    }
    assert entry["response"] == {"status": 200, "size": len(BODY)}
    assert entry["duration_ms"] >= 0
    assert entry["failure"] is None


@pytest.mark.parametrize("make_session", SESSIONS)
def test_a_retried_request_writes_one_entry_per_attempt(server, debug_log, make_session):
    make_session().get(f"{server.url}/busy")

    entries = records(debug_log)
    assert [(e["request"]["attempt"], e["response"]["status"]) for e in entries] == [(1, 503), (2, 200)]


@pytest.mark.parametrize(
    ("make_session", "kwargs", "error"),
    [
        pytest.param(requests_session, {}, rq.ConnectionError, id="requests"),
        pytest.param(rnet_session, {"max_retries": 0}, MaxRetriesError, id="rnet"),
    ],
)
def test_a_failed_attempt_names_the_failure(debug_log, make_session, kwargs, error):
    with pytest.raises(error):
        make_session().get(f"{closed_port()}/gone", **kwargs)

    (entry,) = records(debug_log)
    assert entry["failure"]
    assert entry["response"]["status"] is None
    assert entry["request"]["path"] == "/gone"


def test_a_redirect_writes_one_entry_per_hop(server, debug_log):
    requests_session().get(f"{server.url}/old")

    assert [(e["request"]["path"], e["response"]["status"]) for e in records(debug_log)] == [
        ("/old", 302),
        ("/new", 200),
    ]


def test_an_rnet_method_outside_the_retry_methods_is_recorded(server, debug_log):
    RnetSession(allowed_methods={"GET"}).patch(f"{server.url}/edit")

    (entry,) = records(debug_log)
    assert (entry["request"]["method"], entry["request"]["path"], entry["response"]["status"]) == (
        "PATCH",
        "/edit",
        200,
    )


@pytest.mark.parametrize("make_session", SESSIONS)
def test_segment_and_probe_requests_are_not_recorded(server, debug_log, tmp_path, make_session):
    session = make_session()
    list(dl.download(url=f"{server.url}/segment", save_path=tmp_path / "seg.mp4", session=session))
    dl.probe_ranged(f"{server.url}/probe", session)
    session.get(f"{server.url}/api")

    assert server.hits.count("/segment") == 1 and server.hits.count("/probe") == 1
    assert [e["request"]["path"] for e in records(debug_log)] == ["/api"]


@pytest.mark.parametrize("built_with_debug_requests", [True, False])
@pytest.mark.parametrize("make_session", SESSIONS)
def test_a_debug_run_without_debug_requests_records_nothing(
    server, debug_log, monkeypatch, make_session, built_with_debug_requests
):
    monkeypatch.setattr(config, "debug_requests", built_with_debug_requests)
    session = make_session()
    monkeypatch.setattr(config, "debug_requests", False)
    session.get(f"{server.url}/api")
    session.get(f"{server.url}/busy")

    assert server.hits.count("/api") == 1
    assert records(debug_log) == []
    assert "session_start" in debug_log.read_text(encoding="utf-8")


def test_record_requests_leaves_the_session_alone_without_debug_requests(monkeypatch):
    monkeypatch.setattr(config, "debug_requests", False)
    session = rq.Session()
    record_requests(session)

    assert type(session) is rq.Session


def test_a_recorded_adapter_reads_through_and_copies(debug_log):
    session = requests_session()
    adapter = session.get_adapter("http://h")

    assert adapter.max_retries.total == 2
    assert copy.copy(adapter).max_retries.total == 2


def test_the_service_session_records_only_with_debug_requests(monkeypatch):
    class Stop(Exception):
        pass

    stub = type("Stub", (Service,), dict.fromkeys(Service.__abstractmethods__, lambda *_: None))
    ctx = SimpleNamespace(obj=SimpleNamespace(config={}))
    monkeypatch.setattr(service_module, "Cacher", lambda *_: (_ for _ in ()).throw(Stop()))

    for enabled in (True, False):
        monkeypatch.setattr(config, "debug_requests", enabled)
        instance = object.__new__(stub)
        with pytest.raises(Stop):
            Service.__init__(instance, ctx)  # type: ignore[arg-type]
        assert isinstance(instance.session, RecordedSession) is enabled
        assert type(instance.session).__name__ == "TimeoutSession"


JWT = "eyJhbGciOi.eyJzdWIi.c2ln"


def test_the_rnet_console_line_keeps_the_query(server, debug_log, caplog):
    with caplog.at_level("DEBUG"):
        rnet_session().get(f"{server.url}/{JWT}/pts_0.mp4?sig=S2")

    (line,) = [r.getMessage() for r in caplog.records if " 200 " in r.getMessage()]
    assert f'"GET /{JWT}/pts_0.mp4?sig=S2 ' in line


def body_of(
    server: Server, debug_log: Path, make_session: Any, path: str, kind: str | None, doc: str, **kw: Any
) -> dict:
    server.docs[path] = doc
    if kind:
        server.types[path] = kind
    make_session().get(f"{server.url}{path}", **kw)
    (entry,) = records(debug_log)
    return entry["response"]


@pytest.mark.parametrize("make_session", SESSIONS)
def test_a_json_body_is_stored_as_an_object_as_received(server, debug_log, make_session):
    doc = json.dumps({"items": [{"lang": "xx-ZZ"}], "access_token": "T1", "key": "K1", "file": f"{Path.home()}/a"})
    response = body_of(server, debug_log, make_session, "/api/json", "application/json; charset=utf-8", doc)

    assert response["body"] == {"items": [{"lang": "xx-ZZ"}], "access_token": "T1", "key": "K1", "file": "~/a"}
    assert str(Path.home()) not in debug_log.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("path", "kind", "doc"),
    [
        pytest.param("/a.txt", "text/plain", "error inside a 200", id="text"),
        pytest.param("/bad-json", "application/json", "{not json", id="bad-json"),
        pytest.param("/m.xml", "application/xml", "<a/>", id="xml"),
    ],
)
@pytest.mark.parametrize("make_session", SESSIONS)
def test_a_text_body_is_stored_as_a_string(server, debug_log, make_session, path, kind, doc):
    assert body_of(server, debug_log, make_session, path, kind, doc)["body"] == doc


@pytest.mark.parametrize(
    ("path", "kind", "kw"),
    [
        pytest.param("/seg.bin", "application/octet-stream", {}, id="binary"),
        pytest.param("/seg.m3u8", "video/mp2t", {}, id="media-type"),
        pytest.param("/no-type", None, {}, id="no-type"),
        pytest.param("/api/stream", "application/json", {"stream": True}, id="stream"),
        pytest.param("/events", "text/event-stream", {}, id="event-stream"),
        pytest.param("/v/manifest.mpd", "application/dash+xml", {}, id="mpd"),
        pytest.param("/v/manifest.mpd", "text/xml", {}, id="mpd-suffix"),
        pytest.param("/master", "application/vnd.apple.mpegurl", {}, id="m3u8"),
        pytest.param("/v.m3u8", "text/plain", {}, id="m3u8-suffix"),
        pytest.param("/a.ism/Manifest", "application/vnd.ms-sstr+xml", {}, id="ism"),
        pytest.param("/a.ism/Manifest", "text/xml", {}, id="ism-suffix"),
    ],
)
@pytest.mark.parametrize("make_session", SESSIONS)
def test_no_body_for_binary_or_streamed_responses(server, debug_log, make_session, path, kind, kw):
    assert "body" not in body_of(server, debug_log, make_session, path, kind, '{"a": 1}', **kw)


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        pytest.param(
            {"json": {"q": "query Me", "variables": {"id": 1}}}, {"q": "query Me", "variables": {"id": 1}}, id="json"
        ),
        pytest.param({"data": {"a": "1", "b": "x y"}}, "a=1&b=x+y", id="form"),
        pytest.param({"data": "<Envelope/>", "headers": {"Content-Type": "text/xml"}}, "<Envelope/>", id="xml"),
    ],
)
@pytest.mark.parametrize("make_session", SESSIONS)
def test_a_text_request_body_is_recorded_as_sent(server, debug_log, make_session, kw, expected):
    make_session().post(f"{server.url}/api/send", **kw)

    assert records(debug_log)[-1]["request"]["body"] == expected


@pytest.mark.parametrize(
    "kw",
    [
        pytest.param({"data": b"\x08\x04\x12", "headers": {"Content-Type": "application/octet-stream"}}, id="binary"),
        pytest.param({"data": b"\x08\x04\x12"}, id="no-type"),
        pytest.param({}, id="none"),
    ],
)
@pytest.mark.parametrize("make_session", SESSIONS)
def test_no_request_body_for_a_binary_or_empty_request(server, debug_log, make_session, kw):
    make_session().post(f"{server.url}/api/send", **kw)

    assert "body" not in records(debug_log)[-1]["request"]


@pytest.mark.parametrize("make_session", SESSIONS)
def test_a_server_process_records_no_request_body(server, debug_log, make_session, monkeypatch):
    monkeypatch.setattr(http_session, "record_bodies", False)
    make_session().post(f"{server.url}/api/send", json={"a": 1})

    assert "body" not in records(debug_log)[-1]["request"]


@pytest.mark.parametrize("make_session", SESSIONS)
def test_the_size_comes_from_the_body_without_a_content_length(server, debug_log, make_session):
    server.docs["/chunked"] = '{"a": 1}'
    server.types["/chunked"] = "application/json"
    make_session().get(f"{server.url}/chunked")

    assert records(debug_log)[-1]["response"]["size"] == len(server.docs["/chunked"])


@pytest.mark.parametrize("make_session", SESSIONS)
def test_a_json_reply_at_a_manifest_path_keeps_its_body(server, debug_log, make_session):
    assert body_of(server, debug_log, make_session, "/playback/manifest", "application/json", '{"a": 1}')["body"] == {
        "a": 1
    }


@pytest.mark.parametrize("make_session", SESSIONS)
def test_a_recorded_body_stays_readable_by_the_caller(server, debug_log, make_session):
    server.docs["/api/read"] = '{"a": 1}'
    server.types["/api/read"] = "application/json"
    session = make_session()

    assert session.get(f"{server.url}/api/read").json() == {"a": 1}
    assert b"".join(session.get(f"{server.url}/api/read").iter_content(3)) == b'{"a": 1}'
    with session.get(f"{server.url}/api/read", stream=True) as streamed:
        assert b"".join(streamed.iter_content(3)) == b'{"a": 1}'


@pytest.mark.parametrize("kind", ["application/json", "text/plain"])
@pytest.mark.parametrize("make_session", SESSIONS)
def test_a_body_over_the_limit_is_cut_and_marked(server, debug_log, monkeypatch, make_session, kind):
    monkeypatch.setattr(config, "debug_requests_body_limit", 5)
    response = body_of(server, debug_log, make_session, "/api/long", kind, '{"xyzwvu": 1}')

    assert (response["body"], response["body_size"], response["body_cut"]) == ('{"xyz', 13, True)


def test_a_failed_body_read_is_recorded_and_left_to_the_caller(debug_log):
    class Broken:
        status_code = 200
        headers = {"content-type": "application/json"}

        @property
        def content(self) -> bytes:
            raise rq.exceptions.ChunkedEncodingError("cut short")

    response = Broken()
    http_session.record_request("GET", "http://h/api", 1, response=response, body=True)

    (entry,) = records(debug_log)
    assert entry["response"]["body_error"].startswith("ChunkedEncodingError")


@pytest.mark.parametrize("make_session", SESSIONS)
def test_a_body_too_deep_for_the_log_never_breaks_the_caller(server, debug_log, make_session):
    deep = "[" * 5000 + "]" * 5000
    server.docs["/api/deep"] = deep
    server.types["/api/deep"] = "application/json"

    assert make_session().get(f"{server.url}/api/deep").text == deep


@pytest.mark.parametrize(("value", "limit"), [(None, 0), ("5", 5), (-5, 0)])
def test_the_body_limit_is_a_byte_count(value, limit):
    assert Config(debug_requests_body_limit=value).debug_requests_body_limit == limit


def serve_starts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from aiohttp import web
    from click.testing import CliRunner

    from unshackle.commands.serve import serve

    monkeypatch.setattr(web, "run_app", lambda *a, **kw: None)
    monkeypatch.setattr(config, "serve", {})
    assert CliRunner().invoke(serve, ["--api-only", "--no-key"]).exit_code == 0


def worker_starts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from unshackle.core.api import download_worker

    monkeypatch.setattr(download_worker, "perform_download", lambda *a, **kw: [])
    monkeypatch.setattr(download_worker, "relay_prompts", lambda *a: None)
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"job_id": "j", "service": "EXAMPLE", "title_id": "t"}), encoding="utf-8")
    assert download_worker.main(["w", str(payload), str(tmp_path / "result.json")]) == 0


@pytest.mark.parametrize("start", [serve_starts, worker_starts], ids=["serve", "job-worker"])
@pytest.mark.parametrize("make_session", SESSIONS)
def test_a_server_process_records_no_body(server, debug_log, monkeypatch, tmp_path, make_session, start):
    start(monkeypatch, tmp_path)
    response = body_of(server, debug_log, make_session, "/api/json", "application/json", '{"a": 1}')

    assert http_session.record_bodies is False
    assert response == {"status": 200, "size": 8}


def test_serve_keeps_request_lines_out_of_the_dashboard_log(monkeypatch, tmp_path, caplog):
    from unshackle.core.api.stats import ring

    serve_starts(monkeypatch, tmp_path)
    with caplog.at_level(logging.DEBUG):
        for name in ("RnetSession", "urllib3.connectionpool", "EXAMPLE"):
            logging.getLogger(name).debug(f"EXAMPLE {name} line ?sig=S1")
        logging.getLogger("RnetSession").warning("EXAMPLE retry warning")

    lines = [r["msg"] for r in ring.since(0) if r["msg"].startswith("EXAMPLE")]
    assert lines == ["EXAMPLE EXAMPLE line ?sig=S1", "EXAMPLE retry warning"]


def paths(path: Path) -> list[str]:
    return [e["request"]["path"] for e in records(path)]


@pytest.mark.parametrize("make_session", SESSIONS)
def test_hls_media_probes_are_not_recorded_but_the_playlist_is(server, debug_log, make_session):
    session = make_session()
    server.docs["/v.m3u8"] = '#EXTM3U\n#EXT-X-MAP:URI="init.mp4",BYTERANGE="1@0"\n#EXTINF:4,\nseg.ts\n#EXT-X-ENDLIST\n'
    HLS.probe_ts_info(f"{server.url}/v.m3u8", session)
    playlist = m3u8.loads(server.docs["/v.m3u8"], uri=f"{server.url}/v.m3u8")
    HLS.fetch_init_section(session, playlist.segments[0].init_section, 0)
    HLS.get_track_kid_from_init(playlist, SimpleNamespace(get_key_id=lambda _: None), session)  # type: ignore[arg-type]

    assert (server.hits.count("/seg.ts"), server.hits.count("/init.mp4")) == (1, 2)
    assert paths(debug_log) == ["/v.m3u8"]
    assert "body" not in records(debug_log)[0]["response"]


@pytest.mark.parametrize("make_session", SESSIONS)
def test_dash_init_reads_are_not_recorded_but_the_manifest_is(server, debug_log, monkeypatch, make_session):
    monkeypatch.setattr(Track, "get_key_id", lambda *_: None)
    session = make_session()
    server.docs["/manifest.mpd"] = f"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT2S">
  <BaseURL>{server.url}/</BaseURL>
  <Period id="0" duration="PT2S">
    <AdaptationSet contentType="video" mimeType="video/mp4" lang="en">
      <Representation id="t" codecs="avc1.640028" bandwidth="1000000">
        <SegmentTemplate timescale="1" duration="1" startNumber="1" initialization="init.mp4" media="$Number$.mp4"/>
      </Representation>
      <Representation id="b" codecs="avc1.640028" bandwidth="900000">
        <BaseURL>file.mp4</BaseURL>
        <SegmentBase indexRange="0-0"><Initialization range="0-0"/></SegmentBase>
      </Representation>
      <Representation id="l" codecs="avc1.640028" bandwidth="800000">
        <SegmentList duration="1"><Initialization sourceURL="list.mp4"/><SegmentURL media="s1.mp4"/></SegmentList>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>"""
    DASH.from_url(f"{server.url}/manifest.mpd", session)
    tree = load_xml(server.docs["/manifest.mpd"].encode())
    period = tree.find("Period")
    for rep in period.iter("Representation"):
        track = Track(url=f"{server.url}/manifest.mpd", language="en")
        DASH.get_period_segments(
            period, period.find("AdaptationSet"), rep, tree, track, track.url, session, window=(0, 1)
        )

    assert [server.hits.count(p) for p in ("/init.mp4", "/file.mp4", "/list.mp4")] == [1, 2, 1]
    assert paths(debug_log) == ["/manifest.mpd"]


@pytest.mark.parametrize("make_session", SESSIONS)
def test_bitrate_probes_are_not_recorded(server, debug_log, make_session):
    session = make_session()
    bitrate.ranged_get(f"{server.url}/a.mp4", session, "bytes=0-0")
    bitrate.probe_size(bitrate.Segment(f"{server.url}/plain-b.mp4", None, None, 0.0), session)

    assert server.hits == ["/a.mp4", "/plain-b.mp4", "/plain-b.mp4"]
    assert records(debug_log) == []


@pytest.mark.parametrize("make_session", SESSIONS)
def test_track_init_and_sample_probes_are_not_recorded(server, debug_log, make_session):
    session = make_session()
    track = Track(url=f"{server.url}/c.mp4", language="en")
    track.get_init_segment(session=session)
    track.sample_byte_range(session)
    Track(url=f"{server.url}/plain-c.mp4", language="en").get_init_segment(session=session)

    assert (server.hits.count("/c.mp4"), server.hits.count("/plain-c.mp4")) == (4, 3)
    assert records(debug_log) == []


def test_an_unrecorded_probe_keeps_the_session_attributes(server, debug_log):
    class Own(rq.Session):
        def __init__(self) -> None:
            super().__init__()
            self.token = "T"

        def request(self, *args: Any, **kwargs: Any) -> rq.Response:
            kwargs["headers"] = {"X-Token": self.token}
            return super().request(*args, **kwargs)

    session = Own()
    record_requests(session)
    unrecorded(session, "get", f"{server.url}/probe")
    dl.probe_ranged(f"{server.url}/segment", session)
    session.get(f"{server.url}/api")

    assert paths(debug_log) == ["/api"]
