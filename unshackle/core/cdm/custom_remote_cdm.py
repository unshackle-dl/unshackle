from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional

import requests
from requests import Session

from unshackle.core import __version__
from unshackle.core.cdm.http_cdm import HttpCdm, HttpCdmExceptions, b64_or_utf8
from unshackle.core.vaults import Vaults

CustomRemoteCDMExceptions = HttpCdmExceptions


class CustomRemoteCDM(HttpCdm):
    """
    Highly Configurable Custom Remote CDM implementation.

    This class provides a maximally flexible CDM interface that can adapt to
    ANY CDM API format through YAML configuration alone. It can hold both current
    and future CDM providers without code changes.

    Main Features:
    - Fully configuration-driven behaviour (all logic controlled through YAML)
    - Pluggable authentication strategies (header, body, bearer, basic, custom)
    - Flexible endpoint configuration (custom paths, methods, timeouts)
    - Advanced parameter mapping (rename, add static, conditional, nested)
    - Response parsing (deep field access, type detection, transforms)
    - Transform engine (base64, hex, JSON, custom content key formats)
    - Condition evaluation (response type detection, success validation)
    - Compatible with both Widevine and PlayReady DRM schemes
    - Vault integration for intelligent content key caching

    Configuration Philosophy:
    - 90% of new CDM providers: YAML config only
    - 9% of cases: Add new transform type (minimal code)
    - 1% of cases: Add new auth strategy (minimal code)
    - 0% need to modify core request/response logic

    The class adapts to these diverse API patterns:
    - Different authentication mechanisms (headers vs body vs tokens)
    - Custom endpoint paths and HTTP methods
    - Parameter name variations (scheme vs device, init_data vs pssh)
    - Nested JSON structures in requests/responses
    - Various content key formats (JSON objects, colon-separated strings, and other forms)
    - Different response success indicators and error messages
    - Conditional parameters based on device type or other factors
    """

    chain_suffix = "Custom_Remote"

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
        **kwargs: Any,
    ) -> None:
        """
        Initialise Custom Remote CDM with highly configurable options.

        Args:
            host: Base URL for the CDM API
            service_name: Service tag for content key caching and vault operations
            vaults: Vaults instance for local content key caching
            device: Device configuration (name, type, system_id, security_level)
            auth: Authentication configuration (type, credentials, headers)
            endpoints: Endpoint configuration (paths, methods, timeouts)
            request_mapping: Request transformation rules (param names, static params, transforms)
            response_mapping: Response parsing rules (field locations, type detection, success conditions)
            caching: Caching configuration (enabled, use_vaults, and the other caching keys)
            legacy: Legacy mode configuration
            timeout: Default request timeout in seconds
            **kwargs: Additional configuration options for future extensibility
        """
        device = device or {}
        self.device_type_str = device.get("type", "CHROME")
        self.auth_config = auth or {"type": "header", "header_name": "Authorization", "key": ""}
        super().__init__(
            host,
            device.get("name", "ChromeCDM"),
            self.device_type_str,
            service_name,
            vaults,
            {"User-Agent": f"unshackle-custom-cdm/{__version__}", **self.auth_config.get("custom_headers", {})},
        )
        self.timeout = timeout
        self.system_id = device.get("system_id", 26830)
        self.security_level = device.get("security_level", 3)

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

        self.request_mapping = request_mapping or {}

        self.response_mapping = response_mapping or {}

        caching = caching or {}
        self.use_vaults = caching.get("use_vaults", True) and self.vaults is not None
        self.apply_authentication(self._http_session)

    def get_nested_field(self, data: Dict[str, Any], field_path: str, default: Any = None) -> Any:
        """
        Get a nested field from a dictionary using dot notation.

        Args:
            data: Dictionary to extract field from
            field_path: Field path using dot notation (e.g., "data.cached_keys")
            default: Default value if field not found

        Returns:
            Field value or default

        Examples:
            get_nested_field({"data": {"keys": [1,2,3]}}, "data.keys") -> [1,2,3]
            get_nested_field({"message": "success"}, "message") -> "success"
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

    def apply_transform(self, value: Any, transform_type: str) -> Any:
        """
        Apply a transformation to a value.

        Args:
            value: Value to transform
            transform_type: Type of transformation to apply

        Returns:
            Transformed value. An unknown transform_type returns the value unchanged instead of raising.

        Supported transforms:
            - base64_encode: Encode bytes/string to base64
            - base64_decode: Decode base64 string to bytes
            - hex_encode: Encode bytes to hex string
            - hex_decode: Decode hex string to bytes
            - json_stringify: Convert object to JSON string
            - json_parse: Parse JSON string to object
            - parse_key_string: Parse newline-separated "kid:key" lines, with or without a leading "--key "
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

        return value

    def evaluate_condition(self, condition: str, context: Dict[str, Any]) -> bool:
        """
        Evaluate a simple condition against a context.

        Args:
            condition: Condition string (e.g., "message == 'success'")
            context: Context dictionary with the values to examine

        Returns:
            True if condition is met, False otherwise. A condition that matches none of the supported
            forms counts as unmet, so the method returns False.

        Supported conditions:
            - "field == value": Equality check
            - "field != value": Inequality check
            - "field == null": Null check
            - "field != null": Not null check
            - "field exists": Existence check
        """
        condition = condition.strip()

        if " exists" in condition:
            field = condition.replace(" exists", "").strip()
            return self.get_nested_field(context, field) is not None

        if " == null" in condition:
            field = condition.replace(" == null", "").strip()
            return self.get_nested_field(context, field) is None

        if " != null" in condition:
            field = condition.replace(" != null", "").strip()
            return self.get_nested_field(context, field) is not None

        if " == " in condition:
            parts = condition.split(" == ", 1)
            field = parts[0].strip()
            expected_value = parts[1].strip().strip("'\"")
            actual_value = self.get_nested_field(context, field)
            return str(actual_value) == expected_value

        if " != " in condition:
            parts = condition.split(" != ", 1)
            field = parts[0].strip()
            expected_value = parts[1].strip().strip("'\"")
            actual_value = self.get_nested_field(context, field)
            return str(actual_value) != expected_value

        return False

    def build_request_params(
        self, endpoint_name: str, base_params: Dict[str, Any], session: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Assemble request parameters with mapping and transformations.

        Args:
            endpoint_name: Name of the endpoint (e.g., "get_request", "decrypt_response")
            base_params: Base parameters to transform
            session: Optional CDM session data for context

        Returns:
            Transformed parameters dictionary

        This method applies the following transformations in order:
        1. Parameter name mappings (rename parameters)
        2. Static parameters (add fixed values)
        3. Conditional parameters (add based on conditions)
        4. Parameter transforms (apply data transformations)
        5. Nested parameter structure (make nested objects)
        6. Parameter exclusions (remove unwanted params)
        """
        mapping_config = self.request_mapping.get(endpoint_name, {})

        params = base_params.copy()

        param_names = mapping_config.get("param_names", {})
        if param_names:
            renamed_params = {}
            for old_name, new_name in param_names.items():
                if old_name in params:
                    renamed_params[new_name] = params.pop(old_name)
            params.update(renamed_params)

        static_params = mapping_config.get("static_params", {})
        if static_params:
            params.update(static_params)

        conditional_params = mapping_config.get("conditional_params", [])
        for condition_block in conditional_params:
            condition = condition_block.get("condition", "")
            context = {
                "device_type": self.device_type_str,
                "device_name": self.device_name,
                "is_playready": self._is_playready,
            }
            if session:
                context.update(session)

            if self.evaluate_condition(condition, context):
                params.update(condition_block.get("params", {}))

        transforms = mapping_config.get("transforms", [])
        for transform in transforms:
            param_name = transform.get("param")
            transform_type = transform.get("type")
            if param_name in params:
                params[param_name] = self.apply_transform(params[param_name], transform_type)

        nested_params = mapping_config.get("nested_params", {})
        if nested_params:
            for parent_key, child_keys in nested_params.items():
                nested_obj = {}
                for child_key in child_keys:
                    if child_key in params:
                        nested_obj[child_key] = params.pop(child_key)
                if nested_obj:
                    params[parent_key] = nested_obj

        exclude_params = mapping_config.get("exclude_params", [])
        for param_name in exclude_params:
            params.pop(param_name, None)

        return params

    def apply_authentication(self, session: Session) -> None:
        """
        Apply authentication to the HTTP session based on auth configuration.

        Args:
            session: `requests.Session` to apply authentication to

        Supported auth types:
            - header: Add authentication header (e.g., x-api-key, Authorization)
            - body: the request body carries the authentication (not applied here)
            - bearer: Add Bearer token to Authorization header
            - basic: Add HTTP Basic authentication
            - query: the query string carries the authentication (not applied here)
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

    def parse_response_data(self, endpoint_name: str, response_data: Dict[str, Any]) -> Dict[str, Any]:
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
        mapping_config = self.response_mapping.get(endpoint_name, {})

        fields_config = mapping_config.get("fields", {})
        parsed = {}

        for standard_name, field_path in fields_config.items():
            value = self.get_nested_field(response_data, field_path)
            if value is not None:
                parsed[standard_name] = value

        transforms = mapping_config.get("transforms", [])
        for transform in transforms:
            field_name = transform.get("field")
            transform_type = transform.get("type")
            if field_name in parsed:
                parsed[field_name] = self.apply_transform(parsed[field_name], transform_type)

        response_types = mapping_config.get("response_types", [])
        for response_type_config in response_types:
            condition = response_type_config.get("condition", "")
            if self.evaluate_condition(condition, parsed):
                parsed["_response_type"] = response_type_config.get("type")
                break

        success_conditions = mapping_config.get("success_conditions", [])
        is_success = True
        if success_conditions:
            is_success = all(self.evaluate_condition(cond, parsed) for cond in success_conditions)
        parsed["_is_success"] = is_success

        if not is_success:
            error_fields = mapping_config.get("error_fields", ["error", "message", "details"])
            error_messages = []
            for error_field in error_fields:
                error_msg = self.get_nested_field(response_data, error_field)
                if error_msg and error_msg not in error_messages:
                    error_messages.append(str(error_msg))
            parsed["_error_message"] = " - ".join(error_messages) if error_messages else "Unknown error"

        return parsed

    def parse_keys_from_response(self, endpoint_name: str, response_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Parse content keys from response data with the key field mapping.

        Args:
            endpoint_name: Name of the endpoint
            response_data: Parsed response data

        Returns:
            List of content key dictionaries with standardized format
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
                        keys.append(
                            {
                                "kid": str(kid),
                                "key": str(key),
                                "type": str(key_type),
                                "permissions": key_obj.get("permissions", []),
                            }
                        )

        elif isinstance(keys_data, str):
            keys = self.apply_transform(keys_data, "parse_key_string")

        return keys

    def _call(self, endpoint_name: str, params: Dict[str, Any], failure: str) -> Dict[str, Any]:
        """POST to a configured endpoint and return its JSON body; raises on a status other than 200."""
        endpoint = self.endpoints[endpoint_name]
        return self._post(endpoint["path"], params, failure, endpoint["timeout"])

    def _request_challenge(
        self,
        session: Dict[str, Any],
        body: Dict[str, Any],
        license_type: str,
        privacy_mode: bool,
    ) -> bytes:
        base_params = {**body, "license_type": license_type, "privacy_mode": privacy_mode}
        request_params = self.build_request_params("get_request", base_params, session)
        parsed_response = self.parse_response_data(
            "get_request", self._call("get_request", request_params, "API request failed")
        )

        if not parsed_response.get("_is_success", False):
            error_msg = parsed_response.get("_error_message", "Unknown error")
            raise requests.RequestException(f"API error: {error_msg}")

        if parsed_response.get("_response_type") == "cached_keys" or "cached_keys" in parsed_response:
            cached_keys = self.parse_keys_from_response("get_request", parsed_response)
            if self._take_cached_keys(session, cached_keys):
                return b""

        challenge = parsed_response.get("challenge")
        remote_session_id = parsed_response.get("session_id")

        if challenge and remote_session_id:
            if isinstance(challenge, str):
                challenge = b64_or_utf8(challenge)
            return self._keep_challenge(session, challenge, remote_session_id)

        if session.get("keys"):
            return b""

        raise requests.RequestException("API response did not contain challenge or cached keys")

    def _request_keys(self, session: Dict[str, Any], body: Dict[str, Any]) -> List[Dict[str, Any]]:
        request_params = self.build_request_params("decrypt_response", body, session)
        response_data = self._call("decrypt_response", request_params, "License decrypt failed")
        parsed_response = self.parse_response_data("decrypt_response", response_data)

        if not parsed_response.get("_is_success", False):
            error_msg = parsed_response.get("_error_message", "Unknown error")
            raise requests.RequestException(f"License decrypt error: {error_msg}")

        return self.parse_keys_from_response("decrypt_response", parsed_response)


__all__ = ["CustomRemoteCDM", "CustomRemoteCDMExceptions"]
