"""The shared part of the CDMs that drive a device behind an HTTP API.

``HttpCdm`` keeps the CDM sessions, the PSSH and required KIDs of each CDM session, the content keys from
the vaults and from the API cache, and the merge of those content keys with the licence keys. An adapter sends
its own API's requests in ``_request_challenge`` and ``_request_keys``.
"""

from __future__ import annotations

import base64
import secrets
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Union
from uuid import UUID

from pyplayready.system.wrmheader import WRMHeader
from pywidevine.cdm import Cdm as WidevineCdm
from pywidevine.device import DeviceTypes
from pywidevine.key import Key
from requests import RequestException, Session

from unshackle.core.cdm.loader import system_of
from unshackle.core.vault import is_null_key

if TYPE_CHECKING:
    from unshackle.core.vaults import Vaults


def kid_uuid(kid: Union[str, UUID]) -> UUID:
    """A KID as a UUID; zeros on the right fill a short hex KID."""
    if isinstance(kid, UUID):
        return kid
    clean = kid.replace("-", "")
    return UUID(hex=clean.ljust(32, "0"))


def kid_hex(kid: Union[str, UUID]) -> str:
    """A KID as lower-case hex without dashes, the form the required KIDs use."""
    return str(kid).replace("-", "").lower()


def b64_or_utf8(text: str) -> bytes:
    """Decode base64 text; text that is not base64 goes out as its UTF-8 bytes."""
    try:
        return base64.b64decode(text)
    except ValueError:
        return text.encode("utf-8")


class MockCertificateChain:
    """Mock certificate chain for PlayReady compatibility."""

    def __init__(self, name: str) -> None:
        self._name = name

    def get_name(self) -> str:
        return self._name


class HttpCdmExceptions:
    """Exception classes for compatibility with pywidevine CDM."""

    class InvalidSession(Exception):
        """Raised when session ID is invalid."""

    class TooManySessions(Exception):
        """Raised when the CDM sessions reach the limit."""

    class InvalidInitData(Exception):
        """Raised when PSSH/init data is invalid."""

    class InvalidLicenseType(Exception):
        """Raised when license type is invalid."""

    class InvalidLicenseMessage(Exception):
        """Raised when license message is invalid."""

    class InvalidContext(Exception):
        """Raised when the CDM session has no context data."""

    class SignatureMismatch(Exception):
        """Raised when signature verification fails."""


class HttpCdm(ABC):
    """A Widevine or PlayReady CDM whose device sits behind an HTTP API.

    The DRM classes give each CDM session its PSSH (``set_pssh_b64``) and the KIDs they need
    (``set_required_kids``). A challenge is empty when the vaults or the API cache already hold
    every required key, and ``has_cached_keys`` then says so.
    """

    service_certificate_challenge = b"\x08\x04"
    is_remote_cdm = True
    chain_suffix = "Remote"
    privacy_cert_devices: tuple[str, ...] = ("L1",)
    label: Optional[str] = None

    def __init__(
        self,
        host: str,
        device_name: str,
        device_type: Optional[str],
        service_name: Optional[str],
        vaults: Optional[Vaults],
        headers: Dict[str, str],
    ) -> None:
        self.host = host.rstrip("/")
        self.device_name = device_name
        self.service_name = service_name or ""
        self.vaults = vaults
        self.use_vaults = True
        self._is_playready = system_of(device_type, device_name) == "playready"
        if device_type:
            self.device_type = self.get_device_type_enum(device_type)
        self._sessions: Dict[bytes, Dict[str, Any]] = {}
        self._http_session = Session()
        self._http_session.headers.update({"Content-Type": "application/json", **headers})

    def get_device_type_enum(self, device_type: str) -> DeviceTypes:
        """Convert device type string to enum for compatibility."""
        return DeviceTypes.ANDROID if device_type.upper() == "ANDROID" else DeviceTypes.CHROME

    @property
    def is_playready(self) -> bool:
        """Return True if this CDM is in PlayReady mode."""
        return self._is_playready

    @property
    def certificate_chain(self) -> MockCertificateChain:
        """Mock certificate chain for PlayReady compatibility."""
        return MockCertificateChain(f"{self.device_name}_{self.chain_suffix}")

    def set_pssh_b64(self, pssh_b64: str, session_id: bytes) -> None:
        """Store the base64 PSSH that a PlayReady CDM session sends as its init data."""
        self._session(session_id)["pssh_b64"] = pssh_b64

    def set_required_kids(self, kids: Sequence[Union[str, UUID]], session_id: bytes) -> None:
        """Store the KIDs a CDM session needs, so that content keys from the vaults or the API cache can skip the licence."""
        self._session(session_id)["required_kids"] = [kid_hex(kid) for kid in kids]

    def generate_session_id(self) -> bytes:
        """Make a unique session ID."""
        return secrets.token_bytes(16)

    def get_init_data_from_pssh(self, pssh: Any, pssh_b64: Optional[str] = None) -> str:
        """Extract init data from various PSSH formats.

        PlayReady sends ``pssh_b64`` when it is set. Without it, a WRM header (object or XML text) goes
        out as a one-record PlayReady Object, as the remote APIs take a PRO, not a bare header.
        """
        if self.is_playready:
            if pssh_b64:
                return pssh_b64
            if isinstance(pssh, str) and pssh.lstrip().startswith("<"):
                pssh = WRMHeader(pssh)
            if isinstance(pssh, WRMHeader):
                from unshackle.core.drm.playready import PlayReady

                return PlayReady.pro_b64_for(pssh)
            if isinstance(pssh, str):
                return pssh

        if hasattr(pssh, "dumps"):
            dumps_result = pssh.dumps()

            if isinstance(dumps_result, str):
                try:
                    base64.b64decode(dumps_result)
                    return dumps_result
                except ValueError:
                    return base64.b64encode(dumps_result.encode("utf-8")).decode("utf-8")
            else:
                return base64.b64encode(dumps_result).decode("utf-8")
        elif hasattr(pssh, "raw"):
            raw_data = pssh.raw
            if isinstance(raw_data, str):
                raw_data = raw_data.encode("utf-8")
            return base64.b64encode(raw_data).decode("utf-8")
        else:
            raise ValueError(f"Unsupported PSSH type: {type(pssh)}")

    def _session(self, session_id: bytes) -> Dict[str, Any]:
        if session_id not in self._sessions:
            raise HttpCdmExceptions.InvalidSession(f"Invalid session ID: {session_id.hex()}")
        return self._sessions[session_id]

    def open(self) -> bytes:
        """Open a new CDM session and return its ID."""
        session_id = self.generate_session_id()
        self._sessions[session_id] = {
            "service_certificate": None,
            "keys": [],
            "init_data": None,
            "pssh_b64": None,
            "challenge": None,
            "remote_session_id": None,
            "cached_keys": None,
        }
        return session_id

    def close(self, session_id: bytes) -> None:
        """Close a CDM session; raises InvalidSession for an unknown ID."""
        self._session(session_id)
        del self._sessions[session_id]

    def get_service_certificate(self, session_id: bytes) -> Optional[bytes]:
        """The service certificate of a CDM session, or None."""
        return self._session(session_id)["service_certificate"]

    def set_service_certificate(self, session_id: bytes, certificate: Optional[Union[bytes, str]]) -> str:
        """Set the service certificate (bytes or base64) of a CDM session and return a status message.

        With no certificate, a Widevine device in ``privacy_cert_devices`` gets the common privacy certificate.
        """
        session = self._session(session_id)
        if certificate is None:
            if not self._is_playready and self.device_name in self.privacy_cert_devices:
                session["service_certificate"] = base64.b64decode(WidevineCdm.common_privacy_cert)
                return f"Using default Widevine common privacy certificate for {self.device_name}"
            session["service_certificate"] = None
            return "No certificate set (not required for this device type)"

        if isinstance(certificate, str):
            certificate = base64.b64decode(certificate)

        session["service_certificate"] = certificate
        return "Successfully set Service Certificate"

    def has_cached_keys(self, session_id: bytes) -> bool:
        """True when the CDM session holds keys."""
        return bool(self._session(session_id).get("keys"))

    def get_license_challenge(
        self, session_id: bytes, pssh_or_wrm: Any, license_type: str = "STREAMING", privacy_mode: bool = True
    ) -> bytes:
        """Make a licence challenge, or return empty bytes when the vaults or the API cache hold every required key.

        Raises InvalidSession for an unknown ID, and ``requests.RequestException`` when the API fails.
        """
        session = self._session(session_id)
        session["init_data"] = self.get_init_data_from_pssh(pssh_or_wrm, session.get("pssh_b64"))
        required = session.get("required_kids")
        if self._keys_from_vaults(session, required):
            return b""
        body: Dict[str, Any] = {"scheme": self.device_name, "init_data": session["init_data"]}
        if self.service_name:
            body["service"] = self.service_name
        if session["service_certificate"]:
            body["service_certificate"] = base64.b64encode(session["service_certificate"]).decode("utf-8")
        return self._request_challenge(session, body, license_type, privacy_mode)

    def _keys_from_vaults(self, session: Dict[str, Any], required: Optional[List[str]]) -> bool:
        """Look up the required KIDs in the vaults; True when they hold every one."""
        if not (self.use_vaults and self.vaults and required):
            return False
        vault_keys = []
        for kid in required:
            try:
                key, _ = self.vaults.get_key(kid_uuid(kid))
            except (ValueError, TypeError):
                continue
            if not is_null_key(key):
                vault_keys.append({"kid": kid, "key": key, "type": "CONTENT"})
        if not vault_keys:
            return False
        if set(required) <= {k["kid"] for k in vault_keys}:
            session["keys"] = vault_keys
            return True
        session["vault_keys"] = vault_keys
        return False

    def _take_cached_keys(self, session: Dict[str, Any], cached_keys: List[Dict[str, Any]]) -> bool:
        """Keep the keys the API cache returned; True when they and the vault keys cover every required KID.

        Otherwise the cached keys wait in the CDM session for ``parse_license`` to merge with the licence keys.
        """
        required = session.get("required_kids")
        available = [*cached_keys, *session.get("vault_keys", [])]
        have = {kid_hex(k["kid"]) for k in available}
        if required and set(required) - have:
            session["cached_keys"] = cached_keys
            return False
        session["keys"] = available
        return True

    def _keep_challenge(self, session: Dict[str, Any], challenge: bytes, remote_session_id: Any) -> bytes:
        session["challenge"] = challenge
        session["remote_session_id"] = remote_session_id
        return challenge

    def _post(self, path: str, body: Dict[str, Any], failure: str, timeout: float = 30) -> Dict[str, Any]:
        """POST ``body`` to ``path`` on the host and return the JSON answer; raises on a status other than 200."""
        response = self._http_session.post(f"{self.host}{path}", json=body, timeout=timeout)
        if response.status_code != 200:
            raise RequestException(f"{failure}: {response.status_code} {response.text}")
        return response.json()

    @abstractmethod
    def _request_challenge(
        self,
        session: Dict[str, Any],
        body: Dict[str, Any],
        license_type: str,
        privacy_mode: bool,
    ) -> bytes:
        """Ask the API for a challenge or cached keys; the adapter's wire form.

        ``body`` holds the scheme, the init data, and the service and service certificate when set.
        """

    @abstractmethod
    def _request_keys(self, session: Dict[str, Any], body: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Send the licence to the API and return the keys it holds; the adapter's wire form."""

    def parse_license(self, session_id: bytes, license_message: Union[bytes, str]) -> None:
        """Have the API read a licence, then merge its keys with the vault and cached keys of the CDM session.

        A content key from the vaults or the API cache wins over a licence key with the same KID. A CDM session
        that already holds its final keys and has no cached keys left to merge makes this a no-op.
        """
        session = self._session(session_id)

        if session["keys"] and "cached_keys" not in session:
            return

        if not session.get("challenge") or not session.get("remote_session_id"):
            raise ValueError("No challenge available - call get_license_challenge first")

        if isinstance(license_message, str):
            if self.is_playready and license_message.lstrip().startswith("<"):
                license_message = license_message.encode("utf-8")
            else:
                license_message = b64_or_utf8(license_message)

        license_keys = self._request_keys(
            session,
            {
                "scheme": self.device_name,
                "session_id": session["remote_session_id"],
                "init_data": session["init_data"],
                "license_request": base64.b64encode(session["challenge"]).decode("utf-8"),
                "license_response": base64.b64encode(license_message).decode("utf-8"),
            },
        )

        all_keys = [*session.get("vault_keys", []), *(session.get("cached_keys") or [])]
        have = {kid_hex(k["kid"]) for k in all_keys}
        for key in license_keys:
            kid = kid_hex(key["kid"])
            if kid not in have:
                have.add(kid)
                all_keys.append(key)

        session["keys"] = all_keys
        session.pop("cached_keys", None)
        session.pop("vault_keys", None)

        if self.use_vaults and self.vaults and all_keys:
            key_dict: dict[UUID | str, str] = {}
            for key in all_keys:
                if key["type"] == "CONTENT":
                    try:
                        key_dict[kid_uuid(key["kid"])] = key["key"]
                    except (ValueError, TypeError):
                        continue
            if key_dict:
                self.vaults.add_keys(key_dict)

    def get_keys(self, session_id: bytes, type_: Optional[str] = None) -> List[Key]:
        """The keys of a CDM session; only the keys of type ``type_`` when it is set."""
        keys = [
            Key(
                k.get("type", "CONTENT"),
                kid_uuid(k["kid"]),
                bytes.fromhex(k["key"].replace(":", "")) if isinstance(k["key"], str) else k["key"],
                k.get("permissions"),
            )
            for k in self._session(session_id)["keys"]
        ]
        return [key for key in keys if key.type == type_] if type_ else keys
