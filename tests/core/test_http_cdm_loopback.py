"""Each HTTP CDM adapter licenses a title end to end against a loopback stub of its API.

The stub answers the request shapes the adapter sends, so the test covers the wire form of each
adapter through ``Widevine.get_content_keys`` and ``PlayReady.get_content_keys``.
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Iterator, List
from uuid import UUID

import pytest
from pyplayready.system.pssh import PSSH as PlayReadyPssh
from pywidevine.pssh import PSSH as WidevinePssh

from unshackle.core.cdm.custom_remote_cdm import CustomRemoteCDM
from unshackle.core.cdm.decrypt_labs_remote_cdm import DecryptLabsRemoteCDM
from unshackle.core.drm import PlayReady, Widevine

pytestmark = pytest.mark.unit

KID = UUID("00112233445566778899aabbccddeeff")
KEY = "0123456789abcdef0123456789abcdef"
CHALLENGE = b"loopback challenge"
LICENCE = b"loopback licence"
PR_LICENCE = "<License>loopback</License>"

Answer = Callable[[Dict[str, Any]], Dict[str, Any]]


class Api:
    """A loopback HTTP API: one answer per path, every request recorded."""

    def __init__(self) -> None:
        self.answers: Dict[str, Answer] = {}
        self.requests: List[tuple[str, Dict[str, str], Dict[str, Any]]] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                api.requests.append((self.path, dict(self.headers), body))
                answer = json.dumps(api.answers[self.path](body)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

            def log_message(self, *_: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture
def api() -> Iterator[Api]:
    stub = Api()
    thread = threading.Thread(target=stub.server.serve_forever, daemon=True)
    thread.start()
    yield stub
    stub.server.shutdown()
    stub.server.server_close()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def widevine_drm() -> Widevine:
    return Widevine(pssh=WidevinePssh.new(system_id=WidevinePssh.SystemId.Widevine, key_ids=[KID]), kid=KID)


def playready_drm() -> PlayReady:
    kid_xml = f'<KID ALGID="AESCTR" VALUE="{b64(KID.bytes_le)}"></KID>'
    header = (
        '<WRMHEADER xmlns="http://schemas.microsoft.com/DRM/2007/03/PlayReadyHeader" version="4.3.0.0">'
        f"<DATA><PROTECTINFO><KIDS>{kid_xml}</KIDS></PROTECTINFO></DATA></WRMHEADER>"
    ).encode("utf-16-le")
    pro = PlayReadyPssh.PlayreadyHeader.build(
        {"length": 10 + len(header), "records": [{"type": 1, "length": len(header), "data": header}]}
    )
    return PlayReady.from_pssh_b64(b64(pro))


def licence_for(expected: Any) -> Callable[..., Any]:
    def licence(*, challenge: Any, **_: Any) -> Any:
        assert challenge == CHALLENGE
        return expected

    return licence


def decrypt_labs_answers(api: Api, keys: Any) -> None:
    api.answers["/get-request"] = lambda body: {
        "message": "success",
        "message_type": "license-request",
        "challenge": b64(CHALLENGE),
        "session_id": "remote-1",
    }
    api.answers["/decrypt-response"] = lambda body: {"message": "success", "keys": keys}


def test_decrypt_labs_widevine(api: Api) -> None:
    decrypt_labs_answers(api, f"--key {KID.hex}:{KEY}\n")
    drm = widevine_drm()
    cdm = DecryptLabsRemoteCDM(secret="s3cret", host=api.url, device_name="L3", service_name="EXAMPLE")

    drm.get_content_keys(cdm, certificate=lambda **_: None, licence=licence_for(LICENCE))

    assert drm.content_keys == {KID: KEY}
    (path, headers, challenge_req), (_, _, keys_req) = api.requests
    assert headers["decrypt-labs-api-key"] == "s3cret"
    assert challenge_req == {
        "scheme": "L3",
        "init_data": drm.pssh.dumps(),
        "get_cached_keys_if_exists": True,
        "service": "EXAMPLE",
    }
    assert keys_req["session_id"] == "remote-1"
    assert keys_req["license_request"] == b64(CHALLENGE)
    assert keys_req["license_response"] == b64(LICENCE)


def test_decrypt_labs_playready(api: Api) -> None:
    decrypt_labs_answers(api, [{"kid": KID.hex, "key": KEY, "type": "CONTENT"}])
    drm = playready_drm()
    cdm = DecryptLabsRemoteCDM(secret="s3cret", host=api.url, device_name="SL3")

    drm.get_content_keys(cdm, certificate=lambda **_: None, licence=licence_for(PR_LICENCE))

    assert drm.content_keys == {KID: KEY}
    (_, _, challenge_req), (_, _, keys_req) = api.requests
    assert challenge_req["init_data"] == drm.pssh_b64
    assert keys_req["init_data"] == drm.pssh_b64
    assert base64.b64decode(keys_req["license_response"]) == PR_LICENCE.encode()


def test_decrypt_labs_cached_keys_need_no_licence(api: Api) -> None:
    api.answers["/get-request"] = lambda body: {
        "message": "success",
        "message_type": "cached-keys",
        "cached_keys": [{"kid": KID.hex, "key": KEY}],
    }
    drm = widevine_drm()
    cdm = DecryptLabsRemoteCDM(secret="s", host=api.url, device_name="L3")

    drm.get_content_keys(cdm, certificate=lambda **_: None, licence=licence_for(None))

    assert drm.content_keys == {KID: KEY}
    assert [path for path, _, _ in api.requests] == ["/get-request"]


KID_B = UUID("ffeeddccbbaa99887766554433221100")
KEY_B = "fedcba9876543210fedcba9876543210"


@pytest.mark.parametrize("second_answer", ["challenge", "refused"])
def test_decrypt_labs_partial_cache_asks_for_a_licence(api: Api, second_answer: str) -> None:
    """Cached keys that miss a KID make an L1 device ask again without the cache, then merge both sets."""
    answers = iter(
        [
            {"message": "success", "message_type": "cached-keys", "cached_keys": [{"kid": KID.hex, "key": KEY}]},
            {"message": "success", "challenge": b64(CHALLENGE), "session_id": "remote-1"}
            if second_answer == "challenge"
            else {"message": "refused"},
        ]
    )
    api.answers["/get-request"] = lambda body: next(answers)
    api.answers["/decrypt-response"] = lambda body: {"message": "success", "keys": f"--key {KID_B.hex}:{KEY_B}"}
    cdm = DecryptLabsRemoteCDM(secret="s", host=api.url, device_name="L1")
    session_id = cdm.open()
    cdm.set_required_kids([KID, KID_B], session_id=session_id)

    challenge = cdm.get_license_challenge(session_id, widevine_drm().pssh)
    if challenge:
        cdm.parse_license(session_id, LICENCE)

    keys = {k.kid: k.key.hex() for k in cdm.get_keys(session_id, "CONTENT")}
    requests = [(path, body.get("get_cached_keys_if_exists")) for path, _, body in api.requests]
    assert requests[:2] == [("/get-request", True), ("/get-request", False)]
    if second_answer == "challenge":
        assert challenge == CHALLENGE
        assert keys == {KID: KEY, KID_B: KEY_B}
    else:
        assert challenge == b""
        assert keys == {KID: KEY}


CUSTOM_CONFIG: Dict[str, Any] = {
    "auth": {"type": "header", "header_name": "x-api-key", "key": "s3cret"},
    "endpoints": {"get_request": "/challenge", "decrypt_response": {"path": "/keys"}},
    "request_mapping": {"get_request": {"param_names": {"init_data": "pssh"}}},
    "response_mapping": {
        "get_request": {
            "fields": {"challenge": "data.challenge", "session_id": "data.session", "status": "status"},
            "success_conditions": ["status == ok"],
        },
        "decrypt_response": {"fields": {"keys": "data.keys"}, "key_fields": {"kid": "key_id", "key": "value"}},
    },
}


def custom_answers(api: Api) -> None:
    api.answers["/challenge"] = lambda body: {
        "status": "ok",
        "data": {"challenge": b64(CHALLENGE), "session": "remote-1"},
    }
    api.answers["/keys"] = lambda body: {"data": {"keys": [{"key_id": KID.hex, "value": KEY}]}}


@pytest.mark.parametrize(
    ("device", "drm_of", "licence"),
    [
        ({"name": "L3", "type": "CHROME"}, widevine_drm, LICENCE),
        ({"name": "SL3000", "type": "PLAYREADY"}, playready_drm, PR_LICENCE),
    ],
)
def test_custom_api(api: Api, device: Dict[str, Any], drm_of: Callable[[], Any], licence: Any) -> None:
    custom_answers(api)
    drm = drm_of()
    cdm = CustomRemoteCDM(host=api.url, device=device, **CUSTOM_CONFIG)

    drm.get_content_keys(cdm, certificate=lambda **_: None, licence=licence_for(licence))

    assert drm.content_keys == {KID: KEY}
    (path, headers, challenge_req), (keys_path, _, keys_req) = api.requests
    assert (path, keys_path) == ("/challenge", "/keys")
    assert headers["x-api-key"] == "s3cret"
    assert challenge_req["scheme"] == device["name"]
    assert "init_data" not in challenge_req and challenge_req["pssh"]
    assert keys_req["session_id"] == "remote-1"
    assert keys_req["license_request"] == b64(CHALLENGE)
