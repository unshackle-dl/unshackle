"""Session serialization helpers for remote services."""

from http.cookiejar import CookieJar
from typing import Any, Dict, Optional

import requests

from unshackle.core.credential import Credential


def serialize_session(session: requests.Session) -> Dict[str, Any]:
    """
    Serialize a requests.Session into a JSON-serializable dictionary.

    Extracts cookies, headers, and other session data that can be
    transferred to a remote client for downloading.

    Args:
        session: The requests.Session to serialize

    Returns:
        Dictionary containing serialized session data
    """
    session_data = {
        "cookies": {},
        "headers": {},
        "proxies": session.proxies.copy() if session.proxies else {},
    }

    # Serialize cookies
    if session.cookies:
        for cookie in session.cookies:
            session_data["cookies"][cookie.name] = {
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "secure": cookie.secure,
                "expires": cookie.expires,
            }

    # Serialize headers (exclude proxy-authorization for security)
    if session.headers:
        for key, value in session.headers.items():
            # Skip proxy-related headers as they're server-specific
            if key.lower() not in ["proxy-authorization"]:
                session_data["headers"][key] = value

    return session_data


def deserialize_session(
    session_data: Dict[str, Any], target_session: Optional[requests.Session] = None
) -> requests.Session:
    """
    Deserialize session data into a requests.Session.

    Applies cookies, headers, and other session data from a remote server
    to a local session for downloading.

    Args:
        session_data: Dictionary containing serialized session data
        target_session: Optional existing session to update (creates new if None)

    Returns:
        requests.Session with applied session data
    """
    if target_session is None:
        target_session = requests.Session()

    # Apply cookies
    if "cookies" in session_data:
        for cookie_name, cookie_data in session_data["cookies"].items():
            target_session.cookies.set(
                name=cookie_name,
                value=cookie_data["value"],
                domain=cookie_data.get("domain"),
                path=cookie_data.get("path", "/"),
                secure=cookie_data.get("secure", False),
                expires=cookie_data.get("expires"),
            )

    # Apply headers
    if "headers" in session_data:
        target_session.headers.update(session_data["headers"])

    # Note: We don't apply proxies from remote as the local client
    # should use its own proxy configuration

    return target_session


def extract_session_tokens(session: requests.Session) -> Dict[str, Any]:
    """
    Extract authentication tokens and similar data from a session.

    Looks for common authentication patterns like Bearer tokens,
    API keys in headers, etc.

    Args:
        session: The requests.Session to extract tokens from

    Returns:
        Dictionary containing extracted tokens
    """
    tokens = {}

    # Check for Authorization header
    if "Authorization" in session.headers:
        tokens["authorization"] = session.headers["Authorization"]

    # Check for common API key headers
    for key in ["X-API-Key", "Api-Key", "X-Auth-Token"]:
        if key in session.headers:
            tokens[key.lower().replace("-", "_")] = session.headers[key]

    return tokens


def apply_session_tokens(tokens: Dict[str, Any], target_session: requests.Session) -> None:
    """
    Apply authentication tokens to a session.

    Args:
        tokens: Dictionary containing tokens to apply
        target_session: Session to apply tokens to
    """
    # Apply Authorization header
    if "authorization" in tokens:
        target_session.headers["Authorization"] = tokens["authorization"]

    # Apply other token headers
    token_header_map = {
        "x_api_key": "X-API-Key",
        "api_key": "Api-Key",
        "x_auth_token": "X-Auth-Token",
    }

    for token_key, header_name in token_header_map.items():
        if token_key in tokens:
            target_session.headers[header_name] = tokens[token_key]


def serialize_cookies(cookie_jar: Optional[CookieJar]) -> Dict[str, Any]:
    """
    Serialize a CookieJar into a JSON-serializable dictionary.

    Args:
        cookie_jar: The CookieJar to serialize

    Returns:
        Dictionary containing serialized cookies
    """
    if not cookie_jar:
        return {}

    cookies = {}
    for cookie in cookie_jar:
        cookies[cookie.name] = {
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path,
            "secure": cookie.secure,
            "expires": cookie.expires,
        }

    return cookies


def deserialize_cookies(cookies_data: Dict[str, Any]) -> CookieJar:
    """
    Deserialize cookies into a CookieJar.

    Args:
        cookies_data: Dictionary containing serialized cookies

    Returns:
        CookieJar with cookies
    """
    import http.cookiejar

    cookie_jar = http.cookiejar.CookieJar()

    for cookie_name, cookie_data in cookies_data.items():
        cookie = http.cookiejar.Cookie(
            version=0,
            name=cookie_name,
            value=cookie_data["value"],
            port=None,
            port_specified=False,
            domain=cookie_data.get("domain", ""),
            domain_specified=bool(cookie_data.get("domain")),
            domain_initial_dot=cookie_data.get("domain", "").startswith("."),
            path=cookie_data.get("path", "/"),
            path_specified=True,
            secure=cookie_data.get("secure", False),
            expires=cookie_data.get("expires"),
            discard=False,
            comment=None,
            comment_url=None,
            rest={},
        )
        cookie_jar.set_cookie(cookie)

    return cookie_jar


def serialize_credential(credential: Optional[Credential]) -> Optional[Dict[str, str]]:
    """
    Serialize a Credential into a JSON-serializable dictionary.

    Args:
        credential: The Credential to serialize

    Returns:
        Dictionary containing username and password, or None
    """
    if not credential:
        return None

    return {"username": credential.username, "password": credential.password}


def deserialize_credential(credential_data: Optional[Dict[str, str]]) -> Optional[Credential]:
    """
    Deserialize credential data into a Credential object.

    Args:
        credential_data: Dictionary containing username and password

    Returns:
        Credential object or None
    """
    if not credential_data:
        return None

    return Credential(username=credential_data["username"], password=credential_data["password"])
