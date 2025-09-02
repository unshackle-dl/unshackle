from __future__ import annotations

import base64
import secrets
from typing import Any, Dict, List, Optional, Union
from uuid import UUID

import requests
from pywidevine.device import DeviceTypes
from requests import Session

from unshackle.core.vaults import Vaults


class MockCertificateChain:
    """Mock certificate chain for PlayReady compatibility."""

    def __init__(self, name: str):
        self._name = name

    def get_name(self) -> str:
        return self._name


class Key:
    """Key object compatible with pywidevine."""

    def __init__(self, kid: str, key: str, type_: str = "CONTENT"):
        if isinstance(kid, str):
            clean_kid = kid.replace("-", "")
            if len(clean_kid) == 32:
                self.kid = UUID(hex=clean_kid)
            else:
                self.kid = UUID(hex=clean_kid.ljust(32, "0"))
        else:
            self.kid = kid

        if isinstance(key, str):
            self.key = bytes.fromhex(key)
        else:
            self.key = key

        self.type = type_


class DecryptLabsRemoteCDMExceptions:
    """Exception classes for compatibility with pywidevine CDM."""

    class InvalidSession(Exception):
        """Raised when session ID is invalid."""

    class TooManySessions(Exception):
        """Raised when session limit is reached."""

    class InvalidInitData(Exception):
        """Raised when PSSH/init data is invalid."""

    class InvalidLicenseType(Exception):
        """Raised when license type is invalid."""

    class InvalidLicenseMessage(Exception):
        """Raised when license message is invalid."""

    class InvalidContext(Exception):
        """Raised when session has no context data."""

    class SignatureMismatch(Exception):
        """Raised when signature verification fails."""


class DecryptLabsRemoteCDM:
    """
    Decrypt Labs Remote CDM implementation with intelligent caching system.

    This class provides a drop-in replacement for pywidevine's local CDM using
    Decrypt Labs' KeyXtractor API service, enhanced with smart caching logic
    that minimizes unnecessary license requests.

    Key Features:
    - Compatible with both Widevine and PlayReady DRM schemes
    - Intelligent caching that compares required vs. available keys
    - Automatic key combination for mixed cache/license scenarios
    - Seamless fallback to license requests when keys are missing

    Intelligent Caching System:
    1. DRM classes (PlayReady/Widevine) provide required KIDs via set_required_kids()
    2. get_license_challenge() first checks for cached keys
    3. If cached keys satisfy requirements, returns empty challenge (no license needed)
    4. If keys are missing, makes targeted license request for remaining keys
    5. parse_license() combines cached and license keys intelligently
    """

    service_certificate_challenge = b"\x08\x04"

    def __init__(
        self,
        secret: str,
        host: str = "https://keyxtractor.decryptlabs.com",
        device_name: str = "ChromeCDM",
        service_name: Optional[str] = None,
        vaults: Optional[Vaults] = None,
        device_type: Optional[str] = None,
        system_id: Optional[int] = None,
        security_level: Optional[int] = None,
        **kwargs,
    ):
        """
        Initialize Decrypt Labs Remote CDM for Widevine and PlayReady schemes.

        Args:
            secret: Decrypt Labs API key (matches config format)
            host: Decrypt Labs API host URL (matches config format)
            device_name: DRM scheme (ChromeCDM, L1, L2 for Widevine; SL2, SL3 for PlayReady)
            service_name: Service name for key caching and vault operations
            vaults: Vaults instance for local key caching
            device_type: Device type (CHROME, ANDROID, PLAYREADY) - for compatibility
            system_id: System ID - for compatibility
            security_level: Security level - for compatibility
        """
        _ = kwargs

        self.secret = secret
        self.host = host.rstrip("/")
        self.device_name = device_name
        self.service_name = service_name or ""
        self.vaults = vaults
        self.uch = self.host != "https://keyxtractor.decryptlabs.com"

        self._device_type_str = device_type
        if device_type:
            self.device_type = self._get_device_type_enum(device_type)

        self._is_playready = (device_type and device_type.upper() == "PLAYREADY") or (device_name in ["SL2", "SL3"])

        if self._is_playready:
            self.system_id = system_id or 0
            self.security_level = security_level or (2000 if device_name == "SL2" else 3000)
        else:
            self.system_id = system_id or 26830
            self.security_level = security_level or 3

        self._sessions: Dict[bytes, Dict[str, Any]] = {}
        self._pssh_b64 = None
        self._required_kids: Optional[List[str]] = None
        self._http_session = Session()
        self._http_session.headers.update(
            {
                "decrypt-labs-api-key": self.secret,
                "Content-Type": "application/json",
                "User-Agent": "unshackle-decrypt-labs-cdm/1.0",
            }
        )

    def _get_device_type_enum(self, device_type: str):
        """Convert device type string to enum for compatibility."""
        device_type_upper = device_type.upper()
        if device_type_upper == "ANDROID":
            return DeviceTypes.ANDROID
        elif device_type_upper == "CHROME":
            return DeviceTypes.CHROME
        else:
            return DeviceTypes.CHROME

    @property
    def is_playready(self) -> bool:
        """Check if this CDM is in PlayReady mode."""
        return self._is_playready

    @property
    def certificate_chain(self) -> MockCertificateChain:
        """Mock certificate chain for PlayReady compatibility."""
        return MockCertificateChain(f"{self.device_name}_Remote")

    def set_pssh_b64(self, pssh_b64: str) -> None:
        """Store base64-encoded PSSH data for PlayReady compatibility."""
        self._pssh_b64 = pssh_b64

    def set_required_kids(self, kids: List[Union[str, UUID]]) -> None:
        """
        Set the required Key IDs for intelligent caching decisions.

        This method enables the CDM to make smart decisions about when to request
        additional keys via license challenges. When cached keys are available,
        the CDM will compare them against the required KIDs to determine if a
        license request is still needed for missing keys.

        Args:
            kids: List of required Key IDs as UUIDs or hex strings

        Note:
            Should be called by DRM classes (PlayReady/Widevine) before making
            license challenge requests to enable optimal caching behavior.
        """
        self._required_kids = []
        for kid in kids:
            if isinstance(kid, UUID):
                self._required_kids.append(str(kid).replace("-", "").lower())
            else:
                self._required_kids.append(str(kid).replace("-", "").lower())

    def _generate_session_id(self) -> bytes:
        """Generate a unique session ID."""
        return secrets.token_bytes(16)

    def _get_init_data_from_pssh(self, pssh: Any) -> str:
        """Extract init data from various PSSH formats."""
        if self.is_playready and self._pssh_b64:
            return self._pssh_b64

        if hasattr(pssh, "dumps"):
            dumps_result = pssh.dumps()

            if isinstance(dumps_result, str):
                try:
                    base64.b64decode(dumps_result)
                    return dumps_result
                except Exception:
                    return base64.b64encode(dumps_result.encode("utf-8")).decode("utf-8")
            else:
                return base64.b64encode(dumps_result).decode("utf-8")
        elif hasattr(pssh, "raw"):
            raw_data = pssh.raw
            if isinstance(raw_data, str):
                raw_data = raw_data.encode("utf-8")
            return base64.b64encode(raw_data).decode("utf-8")
        elif hasattr(pssh, "__class__") and "WrmHeader" in pssh.__class__.__name__:
            if self.is_playready:
                raise ValueError("PlayReady WRM header received but no PSSH B64 was set via set_pssh_b64()")

            if hasattr(pssh, "raw_bytes"):
                return base64.b64encode(pssh.raw_bytes).decode("utf-8")
            elif hasattr(pssh, "bytes"):
                return base64.b64encode(pssh.bytes).decode("utf-8")
            else:
                raise ValueError(f"Cannot extract PSSH data from WRM header type: {type(pssh)}")
        else:
            raise ValueError(f"Unsupported PSSH type: {type(pssh)}")

    def open(self) -> bytes:
        """
        Open a new CDM session.

        Returns:
            Session identifier as bytes
        """
        session_id = self._generate_session_id()
        self._sessions[session_id] = {
            "service_certificate": None,
            "keys": [],
            "pssh": None,
            "challenge": None,
            "decrypt_labs_session_id": None,
        }
        return session_id

    def close(self, session_id: bytes) -> None:
        """
        Close a CDM session.

        Args:
            session_id: Session identifier

        Raises:
            ValueError: If session ID is invalid
        """
        if session_id not in self._sessions:
            raise DecryptLabsRemoteCDMExceptions.InvalidSession(f"Invalid session ID: {session_id.hex()}")

        del self._sessions[session_id]

    def get_service_certificate(self, session_id: bytes) -> Optional[bytes]:
        """
        Get the service certificate for a session.

        Args:
            session_id: Session identifier

        Returns:
            Service certificate if set, None otherwise

        Raises:
            ValueError: If session ID is invalid
        """
        if session_id not in self._sessions:
            raise DecryptLabsRemoteCDMExceptions.InvalidSession(f"Invalid session ID: {session_id.hex()}")

        return self._sessions[session_id]["service_certificate"]

    def set_service_certificate(self, session_id: bytes, certificate: Optional[Union[bytes, str]]) -> str:
        """
        Set the service certificate for a session.

        Args:
            session_id: Session identifier
            certificate: Service certificate (bytes or base64 string)

        Returns:
            Certificate status message

        Raises:
            ValueError: If session ID is invalid
        """
        if session_id not in self._sessions:
            raise DecryptLabsRemoteCDMExceptions.InvalidSession(f"Invalid session ID: {session_id.hex()}")

        if certificate is None:
            self._sessions[session_id]["service_certificate"] = None
            return "Removed"

        if isinstance(certificate, str):
            certificate = base64.b64decode(certificate)

        self._sessions[session_id]["service_certificate"] = certificate
        return "Successfully set Service Certificate"

    def has_cached_keys(self, session_id: bytes) -> bool:
        """
        Check if cached keys are available for the session.

        Args:
            session_id: Session identifier

        Returns:
            True if cached keys are available

        Raises:
            ValueError: If session ID is invalid
        """
        if session_id not in self._sessions:
            raise DecryptLabsRemoteCDMExceptions.InvalidSession(f"Invalid session ID: {session_id.hex()}")

        session = self._sessions[session_id]
        session_keys = session.get("keys", [])
        return len(session_keys) > 0

    def get_license_challenge(
        self, session_id: bytes, pssh_or_wrm: Any, license_type: str = "STREAMING", privacy_mode: bool = True
    ) -> bytes:
        """
        Generate a license challenge using Decrypt Labs API with intelligent caching.

        This method implements smart caching logic that:
        1. First attempts to retrieve cached keys from the API
        2. If required KIDs are set, compares cached keys against requirements
        3. Only makes a license request if keys are missing
        4. Returns empty challenge if all required keys are cached

        The intelligent caching works as follows:
        - With required KIDs set: Only requests license for missing keys
        - Without required KIDs: Returns any available cached keys
        - For PlayReady: Combines cached keys with license keys seamlessly

        Args:
            session_id: Session identifier
            pssh_or_wrm: PSSH object or WRM header (for PlayReady compatibility)
            license_type: Type of license (STREAMING, OFFLINE, AUTOMATIC) - for compatibility only
            privacy_mode: Whether to use privacy mode - for compatibility only

        Returns:
            License challenge as bytes, or empty bytes if cached keys satisfy requirements

        Raises:
            InvalidSession: If session ID is invalid
            requests.RequestException: If API request fails

        Note:
            Call set_required_kids() before this method for optimal caching behavior.
        """
        _ = license_type, privacy_mode

        if session_id not in self._sessions:
            raise DecryptLabsRemoteCDMExceptions.InvalidSession(f"Invalid session ID: {session_id.hex()}")

        session = self._sessions[session_id]

        session["pssh"] = pssh_or_wrm
        init_data = self._get_init_data_from_pssh(pssh_or_wrm)
        already_tried_cache = session.get("tried_cache", False)

        request_data = {
            "scheme": self.device_name,
            "init_data": init_data,
            "get_cached_keys_if_exists": not already_tried_cache,
        }

        if self.device_name in ["L1", "L2", "SL2", "SL3"] and self.service_name:
            request_data["service"] = self.service_name

        if session["service_certificate"]:
            request_data["service_certificate"] = base64.b64encode(session["service_certificate"]).decode("utf-8")

        response = self._http_session.post(f"{self.host}/get-request", json=request_data, timeout=30)

        if response.status_code != 200:
            raise requests.RequestException(f"API request failed: {response.status_code} {response.text}")

        data = response.json()

        if data.get("message") != "success":
            error_msg = data.get("message", "Unknown error")
            if "details" in data:
                error_msg += f" - Details: {data['details']}"
            if "error" in data:
                error_msg += f" - Error: {data['error']}"

            if "service_certificate is required" in str(data) and not session["service_certificate"]:
                error_msg += " (No service certificate was provided to the CDM session)"

            raise requests.RequestException(f"API error: {error_msg}")

        message_type = data.get("message_type")

        if message_type == "cached-keys" or "cached_keys" in data:
            """
            Handle cached keys response from API.

            When the API returns cached keys, we need to determine if they satisfy
            our requirements or if we need to make an additional license request
            for missing keys.
            """
            cached_keys = data.get("cached_keys", [])
            parsed_keys = self._parse_cached_keys(cached_keys)
            session["keys"] = parsed_keys
            session["tried_cache"] = True

            if self._required_kids:
                cached_kids = set()
                for key in parsed_keys:
                    if isinstance(key, dict) and "kid" in key:
                        cached_kids.add(key["kid"].replace("-", "").lower())

                required_kids = set(self._required_kids)
                missing_kids = required_kids - cached_kids

                if missing_kids:
                    session["cached_keys"] = parsed_keys
                    request_data["get_cached_keys_if_exists"] = False
                    response = self._http_session.post(f"{self.host}/get-request", json=request_data, timeout=30)
                    if response.status_code == 200:
                        data = response.json()
                        if data.get("message") == "success" and "challenge" in data:
                            challenge = base64.b64decode(data["challenge"])
                            session["challenge"] = challenge
                            session["decrypt_labs_session_id"] = data["session_id"]
                            return challenge

                    return b""
                else:
                    return b""
            else:
                return b""

        if message_type == "license-request" or "challenge" in data:
            challenge = base64.b64decode(data["challenge"])
            session["challenge"] = challenge
            session["decrypt_labs_session_id"] = data["session_id"]
            return challenge

        error_msg = f"Unexpected API response format. message_type={message_type}, available_fields={list(data.keys())}"
        if data.get("message"):
            error_msg = f"API response: {data['message']} - {error_msg}"
        if "details" in data:
            error_msg += f" - Details: {data['details']}"
        if "error" in data:
            error_msg += f" - Error: {data['error']}"

        if already_tried_cache and data.get("message") == "success":
            return b""

        raise requests.RequestException(error_msg)

    def parse_license(self, session_id: bytes, license_message: Union[bytes, str]) -> None:
        """
        Parse license response using Decrypt Labs API with intelligent key combination.

        For PlayReady content with partial cached keys, this method intelligently
        combines the cached keys with newly obtained license keys, avoiding
        duplicates while ensuring all required keys are available.

        The key combination process:
        1. Extracts keys from the license response
        2. If cached keys exist (PlayReady), combines them with license keys
        3. Removes duplicate keys by comparing normalized KIDs
        4. Updates the session with the complete key set

        Args:
            session_id: Session identifier
            license_message: License response from license server

        Raises:
            ValueError: If session ID is invalid or no challenge available
            requests.RequestException: If API request fails
        """
        if session_id not in self._sessions:
            raise DecryptLabsRemoteCDMExceptions.InvalidSession(f"Invalid session ID: {session_id.hex()}")

        session = self._sessions[session_id]

        if session["keys"] and not (self.is_playready and "cached_keys" in session):
            return

        if not session.get("challenge") or not session.get("decrypt_labs_session_id"):
            raise ValueError("No challenge available - call get_license_challenge first")

        if isinstance(license_message, str):
            if self.is_playready and license_message.strip().startswith("<?xml"):
                license_message = license_message.encode("utf-8")
            else:
                try:
                    license_message = base64.b64decode(license_message)
                except Exception:
                    license_message = license_message.encode("utf-8")

        pssh = session["pssh"]
        init_data = self._get_init_data_from_pssh(pssh)

        license_request_b64 = base64.b64encode(session["challenge"]).decode("utf-8")
        license_response_b64 = base64.b64encode(license_message).decode("utf-8")

        request_data = {
            "scheme": self.device_name,
            "session_id": session["decrypt_labs_session_id"],
            "init_data": init_data,
            "license_request": license_request_b64,
            "license_response": license_response_b64,
        }

        response = self._http_session.post(f"{self.host}/decrypt-response", json=request_data, timeout=30)

        if response.status_code != 200:
            raise requests.RequestException(f"License decrypt failed: {response.status_code} {response.text}")

        data = response.json()

        if data.get("message") != "success":
            error_msg = data.get("message", "Unknown error")
            if "error" in data:
                error_msg += f" - Error: {data['error']}"
            if "details" in data:
                error_msg += f" - Details: {data['details']}"
            raise requests.RequestException(f"License decrypt error: {error_msg}")

        license_keys = self._parse_keys_response(data)

        if self.is_playready and "cached_keys" in session:
            """
            Combine cached keys with license keys for PlayReady content.

            This ensures we have both the cached keys (obtained earlier) and
            any additional keys from the license response, without duplicates.
            """
            cached_keys = session.get("cached_keys", [])
            all_keys = list(cached_keys)

            for license_key in license_keys:
                already_exists = False
                license_kid = None
                if isinstance(license_key, dict) and "kid" in license_key:
                    license_kid = license_key["kid"].replace("-", "").lower()
                elif hasattr(license_key, "kid"):
                    license_kid = str(license_key.kid).replace("-", "").lower()
                elif hasattr(license_key, "key_id"):
                    license_kid = str(license_key.key_id).replace("-", "").lower()

                if license_kid:
                    for cached_key in cached_keys:
                        cached_kid = None
                        if isinstance(cached_key, dict) and "kid" in cached_key:
                            cached_kid = cached_key["kid"].replace("-", "").lower()
                        elif hasattr(cached_key, "kid"):
                            cached_kid = str(cached_key.kid).replace("-", "").lower()
                        elif hasattr(cached_key, "key_id"):
                            cached_kid = str(cached_key.key_id).replace("-", "").lower()

                        if cached_kid == license_kid:
                            already_exists = True
                            break

                if not already_exists:
                    all_keys.append(license_key)

            session["keys"] = all_keys
        else:
            session["keys"] = license_keys

        if self.vaults and session["keys"]:
            key_dict = {UUID(hex=key["kid"]): key["key"] for key in session["keys"] if key["type"] == "CONTENT"}
            self.vaults.add_keys(key_dict)

    def get_keys(self, session_id: bytes, type_: Optional[str] = None) -> List[Key]:
        """
        Get keys from the session.

        Args:
            session_id: Session identifier
            type_: Optional key type filter (CONTENT, SIGNING, etc.)

        Returns:
            List of Key objects

        Raises:
            InvalidSession: If session ID is invalid
        """
        if session_id not in self._sessions:
            raise DecryptLabsRemoteCDMExceptions.InvalidSession(f"Invalid session ID: {session_id.hex()}")

        key_dicts = self._sessions[session_id]["keys"]
        keys = [Key(kid=k["kid"], key=k["key"], type_=k["type"]) for k in key_dicts]

        if type_:
            keys = [key for key in keys if key.type == type_]

        return keys

    def _parse_cached_keys(self, cached_keys_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Parse cached keys from API response.

        Args:
            cached_keys_data: List of cached key objects from API

        Returns:
            List of key dictionaries
        """
        keys = []

        try:
            if cached_keys_data and isinstance(cached_keys_data, list):
                for key_data in cached_keys_data:
                    if "kid" in key_data and "key" in key_data:
                        keys.append({"kid": key_data["kid"], "key": key_data["key"], "type": "CONTENT"})
        except Exception:
            pass
        return keys

    def _parse_keys_response(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Parse keys from decrypt response."""
        keys = []

        if "keys" in data and isinstance(data["keys"], str):
            keys_string = data["keys"]

            for line in keys_string.split("\n"):
                line = line.strip()
                if line.startswith("--key "):
                    key_part = line[6:]
                    if ":" in key_part:
                        kid, key = key_part.split(":", 1)
                        keys.append({"kid": kid.strip(), "key": key.strip(), "type": "CONTENT"})
        elif "keys" in data and isinstance(data["keys"], list):
            for key_data in data["keys"]:
                keys.append(
                    {"kid": key_data.get("kid"), "key": key_data.get("key"), "type": key_data.get("type", "CONTENT")}
                )

        return keys


__all__ = ["DecryptLabsRemoteCDM"]
