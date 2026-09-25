"""A remote client's own CDM, used by a server-side service through the session's auth prompt bridge.

The device never leaves the client. The server sends a ``cdm_call`` and the client runs it on its
local CDM: ``challenge`` makes a licence challenge, ``keys`` parses a licence and returns its
non-content keys, and ``close`` closes the CDM session. The client never returns content keys, so a server
cannot use it to license titles.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import uuid
from typing import Any, Callable, Optional, Protocol, Union
from uuid import UUID

from pywidevine import DeviceTypes
from pywidevine.key import Key
from pywidevine.pssh import PSSH

from unshackle.core.cdm.detect import is_playready_cdm

RELAYED_KEY_TYPES = frozenset({"OPERATOR_SESSION", "SIGNING"})


class CdmCallBridge(Protocol):
    def request_cdm(self, call: dict[str, Any]) -> dict[str, Any]: ...


class ClientDeviceCdm:
    """The server's stand-in for a remote client's CDM: the client's device facts, with its calls relayed.

    ``open`` and ``set_service_certificate`` stay local, so one challenge-and-licence exchange costs two
    round trips. The client answers only while one of its requests runs service code (login included).
    """

    def __init__(
        self,
        bridge: CdmCallBridge,
        is_playready: bool,
        server_device: Optional[Callable[[], Any]] = None,
        security_level: Optional[int] = None,
        system_id: Optional[int] = None,
        device_type: Optional[DeviceTypes] = None,
    ) -> None:
        self.bridge = bridge
        self.is_playready = is_playready
        self.server_device = server_device
        self.lent = False
        if security_level is not None:
            self.security_level = security_level
        if system_id is not None:
            self.system_id = system_id
        if device_type is not None:
            self.device_type = device_type
        self._certificates: dict[bytes, Optional[str]] = {}
        self._client_sessions: dict[bytes, str] = {}
        self._keys: dict[bytes, list[Key]] = {}

    def lend_server_device(self) -> Any:
        """The server's own device, for a service that builds its remote session on the server's identity.

        The remote session then licenses nothing live: its content keys come only from a vault. The
        loader also gives the service the server's own config back, so the two identities never mix.
        """
        if self.server_device is None:
            raise ValueError("This server has no device to lend")
        self.lent = True
        return self.server_device()

    def open(self) -> bytes:
        session_id = uuid.uuid4().bytes
        self._certificates[session_id] = None
        return session_id

    def close(self, session_id: bytes) -> None:
        """Close the CDM session, and the client's CDM session behind it, as the client's CDM holds only a few.

        A client too old to close one refuses the call, and it then closes the CDM session when its request ends.
        """
        self._certificates.pop(session_id, None)
        self._keys.pop(session_id, None)
        client_session = self._client_sessions.pop(session_id, None)
        if client_session is not None:
            with contextlib.suppress(Exception):
                self.bridge.request_cdm({"op": "close", "session": client_session})

    def set_service_certificate(self, session_id: bytes, certificate: Optional[Union[bytes, str]]) -> None:
        if isinstance(certificate, bytes):
            certificate = base64.b64encode(certificate).decode()
        self._certificates[session_id] = certificate

    def get_license_challenge(
        self,
        session_id: bytes,
        pssh_or_wrm: Any,
        license_type: str = "STREAMING",
        privacy_mode: bool = True,
        *_: Any,
        **__: Any,
    ) -> Union[bytes, str]:
        """Have the client's CDM make a challenge. Widevine returns bytes, PlayReady the challenge XML."""
        if self.is_playready:
            init_data = pssh_or_wrm if isinstance(pssh_or_wrm, str) else pssh_or_wrm.dumps()
        else:
            init_data = (pssh_or_wrm if isinstance(pssh_or_wrm, PSSH) else PSSH(pssh_or_wrm)).dumps()
        answer = self.bridge.request_cdm(
            {
                "op": "challenge",
                "drm": "playready" if self.is_playready else "widevine",
                "init_data": init_data,
                "license_type": license_type,
                "privacy_mode": privacy_mode,
                "service_certificate": self._certificates.get(session_id),
            }
        )
        self._client_sessions[session_id] = str(answer["session"])
        challenge = base64.b64decode(answer["challenge"])
        return challenge.decode("utf-8") if self.is_playready else challenge

    def parse_license(self, session_id: bytes, license_message: Union[bytes, str]) -> None:
        client_session = self._client_sessions.get(session_id)
        if client_session is None:
            raise ValueError("No client CDM challenge was made in this session")
        if isinstance(license_message, str):
            license_message = (
                license_message.encode("utf-8") if self.is_playready else base64.b64decode(license_message)
            )
        answer = self.bridge.request_cdm(
            {"op": "keys", "session": client_session, "license": base64.b64encode(license_message).decode()}
        )
        self._keys[session_id] = [
            Key(k["type"], UUID(hex=k["kid"]), bytes.fromhex(k["key"]), list(k.get("permissions") or []))
            for k in answer.get("keys") or []
        ]

    def get_keys(self, session_id: bytes, type_: Optional[str] = None) -> list[Key]:
        keys = self._keys.get(session_id, [])
        return [k for k in keys if type_ is None or k.type == type_]


def answer_cdm_call(cdm: Any, call: dict[str, Any], sessions: dict[str, bytes]) -> dict[str, Any]:
    """Run one relayed call on this machine's CDM and return the answer the server gets.

    ``sessions`` maps the refs handed to the server to open local CDM sessions; the caller closes them.
    A refused or failed call answers ``{"error": ...}`` so the server's login stops with the reason.
    """
    if cdm is None:
        return {"error": "no local CDM is loaded"}
    playready = is_playready_cdm(cdm)
    try:
        op = call.get("op")
        if op == "challenge":
            if (call.get("drm") == "playready") != playready:
                return {"error": f"the local CDM is not a {call.get('drm')} device"}
            session_id = cdm.open()
            ref = uuid.uuid4().hex
            sessions[ref] = session_id
            if playready:
                challenge: Union[bytes, str] = cdm.get_license_challenge(session_id, str(call["init_data"]))
                challenge_bytes = challenge.encode("utf-8") if isinstance(challenge, str) else challenge
            else:
                if call.get("service_certificate"):
                    cdm.set_service_certificate(session_id, call["service_certificate"])
                challenge_bytes = cdm.get_license_challenge(
                    session_id,
                    PSSH(call["init_data"]),
                    str(call.get("license_type") or "STREAMING"),
                    bool(call.get("privacy_mode", True)),
                )
            return {"session": ref, "challenge": base64.b64encode(challenge_bytes).decode()}
        if op == "keys":
            if playready:
                return {"error": "a PlayReady licence carries only content keys, so they stay on this machine"}
            session_id = sessions[str(call["session"])]
            cdm.parse_license(session_id, base64.b64decode(call["license"]))
            return {
                "keys": [
                    {"kid": k.kid.hex, "type": k.type, "key": k.key.hex(), "permissions": list(k.permissions)}
                    for k in cdm.get_keys(session_id)
                    if k.type in RELAYED_KEY_TYPES
                ]
            }
        if op == "close":
            cdm.close(sessions.pop(str(call["session"])))
            return {}
        return {"error": f"unknown CDM call {op!r}"}
    except (KeyError, ValueError, TypeError, binascii.Error) as e:
        return {"error": f"malformed CDM call: {e}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
