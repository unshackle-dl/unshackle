from __future__ import annotations

import base64
import secrets
from typing import Any, Dict, List, Optional, Union
from uuid import UUID

import requests
from pywidevine.cdm import Cdm as WidevineCdm
from pywidevine.device import DeviceTypes
from requests import Session

from unshackle.core import __version__
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


class CustomRemoteCDMExceptions:
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


class CustomRemoteCDM:
    """
    Highly Configurable Custom Remote CDM implementation.

    This class provides a maximally flexible CDM interface that can adapt to
    ANY CDM API format through YAML configuration alone. It's designed to support
    both current and future CDM providers without requiring code changes.

    Key Features:
    - Fully configuration-driven behavior (all logic controlled via YAML)
    - Pluggable authentication strategies (header, body, bearer, basic, custom)
    - Flexible endpoint configuration (custom paths, methods, timeouts)
    - Advanced parameter mapping (rename, add static, conditional, nested)
    - Powerful response parsing (deep field access, type detection, transforms)
    - Transform engine (base64, hex, JSON, custom key formats)
    - Condition evaluation (response type detection, success validation)
    - Compatible with both Widevine and PlayReady DRM schemes
    - Vault integration for intelligent key caching

    Configuration Philosophy:
    - 90% of new CDM providers: YAML config only
    - 9% of cases: Add new transform type (minimal code)
    - 1% of cases: Add new auth strategy (minimal code)
    - 0% need to modify core request/response logic

    The class is designed to handle diverse API patterns including:
    - Different authentication mechanisms (headers vs body vs tokens)
    - Custom endpoint paths and HTTP methods
    - Parameter name variations (scheme vs device, init_data vs pssh)
    - Nested JSON structures in requests/responses
    - Various key formats (JSON objects, colon-separated strings, etc.)
    - Different response success indicators and error messages
    - Conditional parameters based on device type or other factors
    """

    service_certificate_challenge = b"\x08\x04"

    def __init__(
        self,
        host: str,
        service_name: Optional[str] = None,
        vaults: Optional[Vaults] = None,
        device: Optional[Dict[str, Any]] = None,
        auth: Optional[Dict[str, Any]] = None,
        endpoints: Optional[Dict[str, Any]] = None,
        request_mapping: Optional[Dict[str, Any]] = None,
        response_mapping: Optional[Dict[str, Any]] = None,
        caching: Optional[Dict[str, Any]] = None,
        legacy: Optional[Dict[str, Any]] = None,
        timeout: int = 30,
        **kwargs,
    ):
        """
        Initialize Custom Remote CDM with highly configurable options.

        Args:
            host: Base URL for the CDM API
            service_name: Service name for key caching and vault operations
            vaults: Vaults instance for local key caching
            device: Device configuration (name, type, system_id, security_level)
            auth: Authentication configuration (type, credentials, headers)
            endpoints: Endpoint configuration (paths, methods, timeouts)
            request_mapping: Request transformation rules (param names, static params, transforms)
            response_mapping: Response parsing rules (field locations, type detection, success conditions)
            caching: Caching configuration (enabled, use_vaults, etc.)
            legacy: Legacy mode configuration
            timeout: Default request timeout in seconds
            **kwargs: Additional configuration options for future extensibility
        """
        self.host = host.rstrip("/")
        self.service_name = service_name or ""
        self.vaults = vaults
        self.timeout = timeout

        # Device configuration
        device = device or {}
        self.device_name = device.get("name", "ChromeCDM")
        self.device_type_str = device.get("type", "CHROME")
        self.system_id = device.get("system_id", 26830)
        self.security_level = device.get("security_level", 3)

        # Determine if this is a PlayReady CDM
        self._is_playready = self.device_type_str.upper() == "PLAYREADY" or self.device_name in ["SL2", "SL3"]

        # Get device type enum for compatibility
        if self.device_type_str:
            self.device_type = self._get_device_type_enum(self.device_type_str)

        # Authentication configuration
        self.auth_config = auth or {"type": "header", "header_name": "Authorization", "key": ""}

        # Endpoints configuration with defaults
        endpoints = endpoints or {}
        self.endpoints = {
            "get_request": {
                "path": endpoints.get("get_request", {}).get("path", "/get-challenge")
                if isinstance(endpoints.get("get_request"), dict)
                else endpoints.get("get_request", "/get-challenge"),
                "method": (
                    endpoints.get("get_request", {}).get("method", "POST")
                    if isinstance(endpoints.get("get_request"), dict)
                    else "POST"
                ),
                "timeout": (
                    endpoints.get("get_request", {}).get("timeout", self.timeout)
                    if isinstance(endpoints.get("get_request"), dict)
                    else self.timeout
                ),
            },
            "decrypt_response": {
                "path": endpoints.get("decrypt_response", {}).get("path", "/get-keys")
                if isinstance(endpoints.get("decrypt_response"), dict)
                else endpoints.get("decrypt_response", "/get-keys"),
                "method": (
                    endpoints.get("decrypt_response", {}).get("method", "POST")
                    if isinstance(endpoints.get("decrypt_response"), dict)
                    else "POST"
                ),
                "timeout": (
                    endpoints.get("decrypt_response", {}).get("timeout", self.timeout)
                    if isinstance(endpoints.get("decrypt_response"), dict)
                    else self.timeout
                ),
            },
        }

        # Request mapping configuration
        self.request_mapping = request_mapping or {}

        # Response mapping configuration
        self.response_mapping = response_mapping or {}

        # Caching configuration
        caching = caching or {}
        self.caching_enabled = caching.get("enabled", True)
        self.use_vaults = caching.get("use_vaults", True) and self.vaults is not None
        self.check_cached_first = caching.get("check_cached_first", True)

        # Legacy configuration
        self.legacy_config = legacy or {"enabled": False}

        # Session management
        self._sessions: Dict[bytes, Dict[str, Any]] = {}
        self._pssh_b64 = None
        self._required_kids: Optional[List[str]] = None

        # HTTP session setup
        self._http_session = Session()
        self._http_session.headers.update(
            {"Content-Type": "application/json", "User-Agent": f"unshackle-custom-cdm/{__version__}"}
        )

        # Apply custom headers from auth config
        custom_headers = self.auth_config.get("custom_headers", {})
        if custom_headers:
            self._http_session.headers.update(custom_headers)

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
        return MockCertificateChain(f"{self.device_name}_Custom_Remote")

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

    def _get_nested_field(self, data: Dict[str, Any], field_path: str, default: Any = None) -> Any:
        """
        Get a nested field from a dictionary using dot notation.

        Args:
            data: Dictionary to extract field from
            field_path: Field path using dot notation (e.g., "data.cached_keys")
            default: Default value if field not found

        Returns:
            Field value or default

        Examples:
            _get_nested_field({"data": {"keys": [1,2,3]}}, "data.keys") -> [1,2,3]
            _get_nested_field({"message": "success"}, "message") -> "success"
        """
        if not field_path:
            return default

        keys = field_path.split(".")
        current = data

        for key in keys:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return default

        return current

    def _apply_transform(self, value: Any, transform_type: str) -> Any:
        """
        Apply a transformation to a value.

        Args:
            value: Value to transform
            transform_type: Type of transformation to apply

        Returns:
            Transformed value

        Supported transforms:
            - base64_encode: Encode bytes/string to base64
            - base64_decode: Decode base64 string to bytes
            - hex_encode: Encode bytes to hex string
            - hex_decode: Decode hex string to bytes
            - json_stringify: Convert object to JSON string
            - json_parse: Parse JSON string to object
            - parse_key_string: Parse "kid:key" format strings
        """
        if transform_type == "base64_encode":
            if isinstance(value, str):
                value = value.encode("utf-8")
            return base64.b64encode(value).decode("utf-8")

        elif transform_type == "base64_decode":
            if isinstance(value, str):
                return base64.b64decode(value)
            return value

        elif transform_type == "hex_encode":
            if isinstance(value, bytes):
                return value.hex()
            elif isinstance(value, str):
                return value.encode("utf-8").hex()
            return value

        elif transform_type == "hex_decode":
            if isinstance(value, str):
                return bytes.fromhex(value)
            return value

        elif transform_type == "json_stringify":
            import json

            return json.dumps(value)

        elif transform_type == "json_parse":
            import json

            if isinstance(value, str):
                return json.loads(value)
            return value

        elif transform_type == "parse_key_string":
            # Handle key formats like "kid:key" or "--key kid:key"
            if isinstance(value, str):
                keys = []
                for line in value.split("\n"):
                    line = line.strip()
                    if line.startswith("--key "):
                        line = line[6:]
                    if ":" in line:
                        kid, key = line.split(":", 1)
                        keys.append({"kid": kid.strip(), "key": key.strip(), "type": "CONTENT"})
                return keys
            return value

        # Unknown transform type - return value unchanged
        return value

    def _evaluate_condition(self, condition: str, context: Dict[str, Any]) -> bool:
        """
        Evaluate a simple condition against a context.

        Args:
            condition: Condition string (e.g., "message == 'success'")
            context: Context dictionary with values to check

        Returns:
            True if condition is met, False otherwise

        Supported conditions:
            - "field == value": Equality check
            - "field != value": Inequality check
            - "field == null": Null check
            - "field != null": Not null check
            - "field exists": Existence check
        """
        condition = condition.strip()

        # Check for existence
        if " exists" in condition:
            field = condition.replace(" exists", "").strip()
            return self._get_nested_field(context, field) is not None

        # Check for null comparisons
        if " == null" in condition:
            field = condition.replace(" == null", "").strip()
            return self._get_nested_field(context, field) is None

        if " != null" in condition:
            field = condition.replace(" != null", "").strip()
            return self._get_nested_field(context, field) is not None

        # Check for equality
        if " == " in condition:
            parts = condition.split(" == ", 1)
            field = parts[0].strip()
            expected_value = parts[1].strip().strip("'\"")
            actual_value = self._get_nested_field(context, field)
            return str(actual_value) == expected_value

        # Check for inequality
        if " != " in condition:
            parts = condition.split(" != ", 1)
            field = parts[0].strip()
            expected_value = parts[1].strip().strip("'\"")
            actual_value = self._get_nested_field(context, field)
            return str(actual_value) != expected_value

        # Unknown condition format - return False
        return False

    def _build_request_params(
        self, endpoint_name: str, base_params: Dict[str, Any], session: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Build request parameters with mapping and transformations.

        Args:
            endpoint_name: Name of the endpoint (e.g., "get_request", "decrypt_response")
            base_params: Base parameters to transform
            session: Optional session data for context

        Returns:
            Transformed parameters dictionary

        This method applies the following transformations in order:
        1. Parameter name mappings (rename parameters)
        2. Static parameters (add fixed values)
        3. Conditional parameters (add based on conditions)
        4. Parameter transforms (apply data transformations)
        5. Nested parameter structure (create nested objects)
        6. Parameter exclusions (remove unwanted params)
        """
        # Get mapping config for this endpoint
        mapping_config = self.request_mapping.get(endpoint_name, {})

        # Start with base parameters
        params = base_params.copy()

        # 1. Apply parameter name mappings
        param_names = mapping_config.get("param_names", {})
        if param_names:
            renamed_params = {}
            for old_name, new_name in param_names.items():
                if old_name in params:
                    renamed_params[new_name] = params.pop(old_name)
            params.update(renamed_params)

        # 2. Add static parameters
        static_params = mapping_config.get("static_params", {})
        if static_params:
            params.update(static_params)

        # 3. Add conditional parameters
        conditional_params = mapping_config.get("conditional_params", [])
        for condition_block in conditional_params:
            condition = condition_block.get("condition", "")
            # Create context for condition evaluation
            context = {
                "device_type": self.device_type_str,
                "device_name": self.device_name,
                "is_playready": self._is_playready,
            }
            if session:
                context.update(session)

            if self._evaluate_condition(condition, context):
                params.update(condition_block.get("params", {}))

        # 4. Apply parameter transforms
        transforms = mapping_config.get("transforms", [])
        for transform in transforms:
            param_name = transform.get("param")
            transform_type = transform.get("type")
            if param_name in params:
                params[param_name] = self._apply_transform(params[param_name], transform_type)

        # 5. Handle nested parameter structure
        nested_params = mapping_config.get("nested_params", {})
        if nested_params:
            for parent_key, child_keys in nested_params.items():
                nested_obj = {}
                for child_key in child_keys:
                    if child_key in params:
                        nested_obj[child_key] = params.pop(child_key)
                if nested_obj:
                    params[parent_key] = nested_obj

        # 6. Exclude unwanted parameters
        exclude_params = mapping_config.get("exclude_params", [])
        for param_name in exclude_params:
            params.pop(param_name, None)

        return params

    def _apply_authentication(self, session: Session) -> None:
        """
        Apply authentication to the HTTP session based on auth configuration.

        Args:
            session: requests.Session to apply authentication to

        Supported auth types:
            - header: Add authentication header (e.g., x-api-key, Authorization)
            - body: Authentication will be added to request body (handled in request building)
            - bearer: Add Bearer token to Authorization header
            - basic: Add HTTP Basic authentication
            - query: Authentication will be added to query string (handled in request)
        """
        auth_type = self.auth_config.get("type", "header")

        if auth_type == "header":
            header_name = self.auth_config.get("header_name", "Authorization")
            key = self.auth_config.get("key", "")
            if key:
                session.headers[header_name] = key

        elif auth_type == "bearer":
            token = self.auth_config.get("bearer_token") or self.auth_config.get("key", "")
            if token:
                session.headers["Authorization"] = f"Bearer {token}"

        elif auth_type == "basic":
            username = self.auth_config.get("username", "")
            password = self.auth_config.get("password", "")
            if username and password:
                from requests.auth import HTTPBasicAuth

                session.auth = HTTPBasicAuth(username, password)

    def _parse_response_data(self, endpoint_name: str, response_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parse response data based on response mapping configuration.

        Args:
            endpoint_name: Name of the endpoint (e.g., "get_request", "decrypt_response")
            response_data: Raw response data from API

        Returns:
            Parsed response with standardized field names

        This method extracts fields from the response using the response_mapping
        configuration, handling nested fields, type detection, and transformations.
        """
        # Get mapping config for this endpoint
        mapping_config = self.response_mapping.get(endpoint_name, {})

        # Extract fields based on mapping
        fields_config = mapping_config.get("fields", {})
        parsed = {}

        for standard_name, field_path in fields_config.items():
            value = self._get_nested_field(response_data, field_path)
            if value is not None:
                parsed[standard_name] = value

        # Apply response transforms
        transforms = mapping_config.get("transforms", [])
        for transform in transforms:
            field_name = transform.get("field")
            transform_type = transform.get("type")
            if field_name in parsed:
                parsed[field_name] = self._apply_transform(parsed[field_name], transform_type)

        # Determine response type
        response_types = mapping_config.get("response_types", [])
        for response_type_config in response_types:
            condition = response_type_config.get("condition", "")
            if self._evaluate_condition(condition, parsed):
                parsed["_response_type"] = response_type_config.get("type")
                break

        # Check success conditions
        success_conditions = mapping_config.get("success_conditions", [])
        is_success = True
        if success_conditions:
            is_success = all(self._evaluate_condition(cond, parsed) for cond in success_conditions)
        parsed["_is_success"] = is_success

        # Extract error messages if not successful
        if not is_success:
            error_fields = mapping_config.get("error_fields", ["error", "message", "details"])
            error_messages = []
            for error_field in error_fields:
                error_msg = self._get_nested_field(response_data, error_field)
                if error_msg and error_msg not in error_messages:
                    error_messages.append(str(error_msg))
            parsed["_error_message"] = " - ".join(error_messages) if error_messages else "Unknown error"

        return parsed

    def _parse_keys_from_response(self, endpoint_name: str, response_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Parse keys from response data using key field mapping.

        Args:
            endpoint_name: Name of the endpoint
            response_data: Parsed response data

        Returns:
            List of key dictionaries with standardized format
        """
        mapping_config = self.response_mapping.get(endpoint_name, {})
        key_fields = mapping_config.get("key_fields", {"kid": "kid", "key": "key", "type": "type"})

        keys = []
        keys_data = response_data.get("keys", [])

        if isinstance(keys_data, list):
            for key_obj in keys_data:
                if isinstance(key_obj, dict):
                    kid = key_obj.get(key_fields.get("kid", "kid"))
                    key = key_obj.get(key_fields.get("key", "key"))
                    key_type = key_obj.get(key_fields.get("type", "type"), "CONTENT")

                    if kid and key:
                        keys.append({"kid": str(kid), "key": str(key), "type": str(key_type)})

        # Handle string format keys (e.g., "kid:key" format)
        elif isinstance(keys_data, str):
            keys = self._apply_transform(keys_data, "parse_key_string")

        return keys

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
            "remote_session_id": None,
            "tried_cache": False,
            "cached_keys": None,
        }
        return session_id

    def close(self, session_id: bytes) -> None:
        """
        Close a CDM session and perform comprehensive cleanup.

        Args:
            session_id: Session identifier

        Raises:
            ValueError: If session ID is invalid
        """
        if session_id not in self._sessions:
            raise CustomRemoteCDMExceptions.InvalidSession(f"Invalid session ID: {session_id.hex()}")

        session = self._sessions[session_id]
        session.clear()
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
            raise CustomRemoteCDMExceptions.InvalidSession(f"Invalid session ID: {session_id.hex()}")

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
            raise CustomRemoteCDMExceptions.InvalidSession(f"Invalid session ID: {session_id.hex()}")

        if certificate is None:
            if not self._is_playready and self.device_name == "L1":
                certificate = WidevineCdm.common_privacy_cert
                self._sessions[session_id]["service_certificate"] = base64.b64decode(certificate)
                return "Using default Widevine common privacy certificate for L1"
            else:
                self._sessions[session_id]["service_certificate"] = None
                return "No certificate set (not required for this device type)"

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
            raise CustomRemoteCDMExceptions.InvalidSession(f"Invalid session ID: {session_id.hex()}")

        session = self._sessions[session_id]
        session_keys = session.get("keys", [])
        return len(session_keys) > 0

    def get_license_challenge(
        self, session_id: bytes, pssh_or_wrm: Any, license_type: str = "STREAMING", privacy_mode: bool = True
    ) -> bytes:
        """
        Generate a license challenge using the custom CDM API.

        This method implements intelligent caching logic that checks vaults first,
        then attempts to retrieve cached keys from the API, and only makes a
        license request if keys are missing.

        Args:
            session_id: Session identifier
            pssh_or_wrm: PSSH object or WRM header (for PlayReady compatibility)
            license_type: Type of license (STREAMING, OFFLINE, AUTOMATIC) - for compatibility only
            privacy_mode: Whether to use privacy mode - for compatibility only

        Returns:
            License challenge as bytes, or empty bytes if available keys satisfy requirements

        Raises:
            InvalidSession: If session ID is invalid
            requests.RequestException: If API request fails
        """
        _ = license_type, privacy_mode

        if session_id not in self._sessions:
            raise CustomRemoteCDMExceptions.InvalidSession(f"Invalid session ID: {session_id.hex()}")

        session = self._sessions[session_id]
        session["pssh"] = pssh_or_wrm
        init_data = self._get_init_data_from_pssh(pssh_or_wrm)

        # Check vaults for cached keys first
        if self.use_vaults and self._required_kids:
            vault_keys = []
            for kid_str in self._required_kids:
                try:
                    clean_kid = kid_str.replace("-", "")
                    if len(clean_kid) == 32:
                        kid_uuid = UUID(hex=clean_kid)
                    else:
                        kid_uuid = UUID(hex=clean_kid.ljust(32, "0"))
                    key, _ = self.vaults.get_key(kid_uuid)
                    if key and key.count("0") != len(key):
                        vault_keys.append({"kid": kid_str, "key": key, "type": "CONTENT"})
                except (ValueError, TypeError):
                    continue

            if vault_keys:
                vault_kids = set(k["kid"] for k in vault_keys)
                required_kids = set(self._required_kids)

                if required_kids.issubset(vault_kids):
                    session["keys"] = vault_keys
                    return b""
                else:
                    session["vault_keys"] = vault_keys

        # Build request parameters
        base_params = {
            "scheme": self.device_name,
            "init_data": init_data,
        }

        if self.service_name:
            base_params["service"] = self.service_name

        if session["service_certificate"]:
            base_params["service_certificate"] = base64.b64encode(session["service_certificate"]).decode("utf-8")

        # Transform parameters based on configuration
        request_params = self._build_request_params("get_request", base_params, session)

        # Apply authentication
        self._apply_authentication(self._http_session)

        # Make API request
        endpoint_config = self.endpoints["get_request"]
        url = f"{self.host}{endpoint_config['path']}"
        timeout = endpoint_config["timeout"]

        response = self._http_session.post(url, json=request_params, timeout=timeout)

        if response.status_code != 200:
            raise requests.RequestException(f"API request failed: {response.status_code} {response.text}")

        # Parse response
        response_data = response.json()
        parsed_response = self._parse_response_data("get_request", response_data)

        # Check if request was successful
        if not parsed_response.get("_is_success", False):
            error_msg = parsed_response.get("_error_message", "Unknown error")
            raise requests.RequestException(f"API error: {error_msg}")

        # Determine response type
        response_type = parsed_response.get("_response_type")

        # Handle cached keys response
        if response_type == "cached_keys" or "cached_keys" in parsed_response:
            cached_keys = self._parse_keys_from_response("get_request", parsed_response)

            all_available_keys = list(cached_keys)
            if "vault_keys" in session:
                all_available_keys.extend(session["vault_keys"])

            session["tried_cache"] = True

            # Check if we have all required keys
            if self._required_kids:
                available_kids = set()
                for key in all_available_keys:
                    if isinstance(key, dict) and "kid" in key:
                        available_kids.add(key["kid"].replace("-", "").lower())

                required_kids = set(self._required_kids)
                missing_kids = required_kids - available_kids

                if missing_kids:
                    # Store cached keys separately - don't populate session["keys"] yet
                    # This allows parse_license() to properly combine cached + license keys
                    session["cached_keys"] = cached_keys
                else:
                    # All required keys are available from cache
                    session["keys"] = all_available_keys
                    return b""
            else:
                # No required KIDs specified - return cached keys
                session["keys"] = all_available_keys
                return b""

        # Handle license request response or fetch license if keys missing
        challenge = parsed_response.get("challenge")
        remote_session_id = parsed_response.get("session_id")

        if challenge and remote_session_id:
            # Decode challenge if it's base64
            if isinstance(challenge, str):
                try:
                    challenge = base64.b64decode(challenge)
                except Exception:
                    challenge = challenge.encode("utf-8")

            session["challenge"] = challenge
            session["remote_session_id"] = remote_session_id
            return challenge

        # If we have some keys but not all, return empty to skip license parsing
        if session.get("keys"):
            return b""

        raise requests.RequestException("API response did not contain challenge or cached keys")

    def parse_license(self, session_id: bytes, license_message: Union[bytes, str]) -> None:
        """
        Parse license response using the custom CDM API.

        This method intelligently combines cached keys with newly obtained license keys,
        avoiding duplicates while ensuring all required keys are available.

        Args:
            session_id: Session identifier
            license_message: License response from license server

        Raises:
            ValueError: If session ID is invalid or no challenge available
            requests.RequestException: If API request fails
        """
        if session_id not in self._sessions:
            raise CustomRemoteCDMExceptions.InvalidSession(f"Invalid session ID: {session_id.hex()}")

        session = self._sessions[session_id]

        # Skip parsing if we already have final keys (no cached keys to combine)
        # If cached_keys exist (Widevine or PlayReady), we need to combine them with license keys
        if session["keys"] and "cached_keys" not in session:
            return

        # Ensure we have a challenge and session ID
        if not session.get("challenge") or not session.get("remote_session_id"):
            raise ValueError("No challenge available - call get_license_challenge first")

        # Prepare license message
        if isinstance(license_message, str):
            if self.is_playready and license_message.strip().startswith("<?xml"):
                license_message = license_message.encode("utf-8")
            else:
                try:
                    license_message = base64.b64decode(license_message)
                except Exception:
                    license_message = license_message.encode("utf-8")

        # Build request parameters
        pssh = session["pssh"]
        init_data = self._get_init_data_from_pssh(pssh)
        license_request_b64 = base64.b64encode(session["challenge"]).decode("utf-8")
        license_response_b64 = base64.b64encode(license_message).decode("utf-8")

        base_params = {
            "scheme": self.device_name,
            "session_id": session["remote_session_id"],
            "init_data": init_data,
            "license_request": license_request_b64,
            "license_response": license_response_b64,
        }

        # Transform parameters based on configuration
        request_params = self._build_request_params("decrypt_response", base_params, session)

        # Apply authentication
        self._apply_authentication(self._http_session)

        # Make API request
        endpoint_config = self.endpoints["decrypt_response"]
        url = f"{self.host}{endpoint_config['path']}"
        timeout = endpoint_config["timeout"]

        response = self._http_session.post(url, json=request_params, timeout=timeout)

        if response.status_code != 200:
            raise requests.RequestException(f"License decrypt failed: {response.status_code} {response.text}")

        # Parse response
        response_data = response.json()
        parsed_response = self._parse_response_data("decrypt_response", response_data)

        # Check if request was successful
        if not parsed_response.get("_is_success", False):
            error_msg = parsed_response.get("_error_message", "Unknown error")
            raise requests.RequestException(f"License decrypt error: {error_msg}")

        # Extract keys from response
        license_keys = self._parse_keys_from_response("decrypt_response", parsed_response)

        # Combine all keys (vault + cached + license)
        all_keys = []

        if "vault_keys" in session:
            all_keys.extend(session["vault_keys"])

        if "cached_keys" in session:
            all_keys.extend(session["cached_keys"])

        # Add license keys, avoiding duplicates
        for license_key in license_keys:
            license_kid = license_key["kid"].replace("-", "").lower()
            already_exists = False

            for existing_key in all_keys:
                existing_kid = existing_key["kid"].replace("-", "").lower()
                if existing_kid == license_kid:
                    already_exists = True
                    break

            if not already_exists:
                all_keys.append(license_key)

        session["keys"] = all_keys
        session.pop("cached_keys", None)
        session.pop("vault_keys", None)

        # Store keys to vaults
        if self.use_vaults and session["keys"]:
            key_dict = {}
            for key in session["keys"]:
                if key["type"] == "CONTENT":
                    try:
                        clean_kid = key["kid"].replace("-", "")
                        if len(clean_kid) == 32:
                            kid_uuid = UUID(hex=clean_kid)
                        else:
                            kid_uuid = UUID(hex=clean_kid.ljust(32, "0"))
                        key_dict[kid_uuid] = key["key"]
                    except (ValueError, TypeError):
                        continue
            if key_dict:
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
            raise CustomRemoteCDMExceptions.InvalidSession(f"Invalid session ID: {session_id.hex()}")

        key_dicts = self._sessions[session_id]["keys"]
        keys = [Key(kid=k["kid"], key=k["key"], type_=k["type"]) for k in key_dicts]

        if type_:
            keys = [key for key in keys if key.type == type_]

        return keys


__all__ = ["CustomRemoteCDM"]
