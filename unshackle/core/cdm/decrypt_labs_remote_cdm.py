from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional

import requests

from unshackle.core import __version__
from unshackle.core.cdm.http_cdm import HttpCdm, HttpCdmExceptions
from unshackle.core.vaults import Vaults

DecryptLabsRemoteCDMExceptions = HttpCdmExceptions


class DecryptLabsRemoteCDM(HttpCdm):
    """The ``HttpCdm`` adapter for the Decrypt Labs KeyXtractor API; L1 and L2 devices always ask for cached keys first."""

    privacy_cert_devices = ("L1", "L2")
    label = "DecryptLabs"

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
        **kwargs: Any,
    ) -> None:
        """
        Initialise Decrypt Labs Remote CDM for Widevine and PlayReady schemes.

        Args:
            secret: Decrypt Labs API key (matches config format)
            host: Decrypt Labs API host URL (matches config format)
            device_name: DRM system device name. Widevine takes ChromeCDM, L1, L2 or L3, and PlayReady takes SL2 or SL3
            service_name: Service tag for content key caching and vault operations
            vaults: Vaults instance for local content key caching
            device_type: Device type (CHROME, ANDROID, PLAYREADY) - for compatibility
            system_id: System ID - for compatibility
            security_level: Security level - for compatibility
        """
        _ = kwargs

        super().__init__(
            host,
            device_name,
            device_type,
            service_name,
            vaults,
            {"decrypt-labs-api-key": secret, "User-Agent": f"unshackle-decrypt-labs-cdm/{__version__}"},
        )
        if self._is_playready:
            self.system_id = system_id or 0
            self.security_level = security_level or (2000 if device_name == "SL2" else 3000)
        else:
            self.system_id = system_id or 26830
            self.security_level = security_level or 3

    def _request_challenge(
        self,
        session: Dict[str, Any],
        body: Dict[str, Any],
        license_type: str,
        privacy_mode: bool,
    ) -> bytes:
        """Ask for cached keys first, and for a licence challenge when they miss a required KID.

        An L1 or L2 device always asks for cached keys, as the API optimises those; another device asks
        only on the first try of the CDM session.
        """
        already_tried_cache = session.get("tried_cache", False)
        request_data = {
            **body,
            "get_cached_keys_if_exists": self.device_name in ("L1", "L2") or not already_tried_cache,
        }

        data = self._post("/get-request", request_data, "API request failed")

        if data.get("message") != "success":
            error_msg = self._api_error(data.get("message", "Unknown error"), data)
            if "service_certificate is required" in str(data) and not session["service_certificate"]:
                error_msg += " (No service certificate was provided to the CDM session)"

            raise requests.RequestException(f"API error: {error_msg}")

        message_type = data.get("message_type")

        if message_type == "cached-keys" or "cached_keys" in data:
            session["tried_cache"] = True
            cached_keys = self.parse_cached_keys(data.get("cached_keys", []))
            if self._take_cached_keys(session, cached_keys):
                return b""

            response = self._http_session.post(
                f"{self.host}/get-request", json={**request_data, "get_cached_keys_if_exists": False}, timeout=30
            )
            if response.status_code == 200:
                data = response.json()
                if data.get("message") == "success" and "challenge" in data:
                    return self._keep_challenge(session, base64.b64decode(data["challenge"]), data["session_id"])

            session["keys"] = cached_keys + session.get("vault_keys", [])
            return b""

        if message_type == "license-request" or "challenge" in data:
            return self._keep_challenge(session, base64.b64decode(data["challenge"]), data["session_id"])

        if already_tried_cache:
            return b""

        unexpected = f"Unexpected API response format: message_type={message_type}, available_fields={list(data)}"
        raise requests.RequestException(self._api_error(f"API response: {data['message']} - {unexpected}", data))

    def _request_keys(self, session: Dict[str, Any], body: Dict[str, Any]) -> List[Dict[str, Any]]:
        data = self._post("/decrypt-response", body, "License decrypt failed")

        if data.get("message") != "success":
            error_msg = self._api_error(data.get("message", "Unknown error"), data)
            raise requests.RequestException(f"License decrypt error: {error_msg}")

        return self.parse_keys_response(data)

    @staticmethod
    def _api_error(message: str, data: Dict[str, Any]) -> str:
        """The message with the details and the error the API added to its answer."""
        if "details" in data:
            message += f" - Details: {data['details']}"
        if "Error" in data:
            message += f" - Error: {data['Error']}"
        return message

    def parse_cached_keys(self, cached_keys_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Parse cached keys from API response.

        Args:
            cached_keys_data: List of cached content key objects from API

        Returns:
            List of content key dictionaries
        """
        keys = []

        try:
            if cached_keys_data and isinstance(cached_keys_data, list):
                for key_data in cached_keys_data:
                    if "kid" in key_data and "key" in key_data:
                        keys.append({"kid": key_data["kid"], "key": key_data["key"], "type": "CONTENT"})
        # non-dict entries in the API payload raise TypeError on membership/index
        except TypeError:
            pass
        return keys

    def parse_keys_response(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
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


__all__ = ["DecryptLabsRemoteCDM", "DecryptLabsRemoteCDMExceptions"]
