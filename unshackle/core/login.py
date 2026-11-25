"""Login state for a service: its cookie file, cookie jar and credentials, read from the config."""

from __future__ import annotations

import html
from http.cookiejar import CookieJar, MozillaCookieJar
from io import StringIO
from pathlib import Path
from typing import Optional

from unshackle.core.config import config
from unshackle.core.credential import Credential


def get_cookie_path(service: str, profile: Optional[str]) -> Optional[Path]:
    """Get Service Cookie File Path for Profile."""
    direct_cookie_file = config.directories.cookies / f"{service}.txt"
    profile_cookie_file = config.directories.cookies / service / f"{profile}.txt"
    default_cookie_file = config.directories.cookies / service / "default.txt"

    if direct_cookie_file.exists():
        return direct_cookie_file
    elif profile_cookie_file.exists():
        return profile_cookie_file
    elif default_cookie_file.exists():
        return default_cookie_file
    return None


def get_cookie_jar(service: str, profile: Optional[str]) -> Optional[CookieJar]:
    """
    Get Service Cookies for Profile.

    If the config has firefox_cookies for the service, cookies come from Firefox first. If that extraction
    raises or returns nothing, unshackle uses the profile's cookie file instead and does not log the failure.
    """
    ff_settings = getattr(config, "firefox_cookies", {}).get(service)
    if ff_settings:
        try:
            from unshackle.core.utils.firefox_cookie_extractor import get_firefox_cookies

            extracted_jar = get_firefox_cookies(ff_settings)
            if extracted_jar:
                return extracted_jar
        except Exception:
            pass

    cookie_file = get_cookie_path(service, profile)
    if cookie_file:
        return load_cookie_file(cookie_file)

    return None


def load_cookie_file(cookie_file: Path) -> MozillaCookieJar:
    """Load a Netscape cookie file with every expiry blanked, so no layer drops an expired cookie."""
    cookie_jar = parse_cookie_text(cookie_file.read_text("utf-8-sig"), str(cookie_file))
    cookie_jar.filename = str(cookie_file)
    return cookie_jar


def parse_cookie_text(text: str, source: str) -> MozillaCookieJar:
    """Parse Netscape cookie text the way :func:`load_cookie_file` does; ``source`` names it in errors."""
    cookie_jar = MozillaCookieJar()
    cookie_data = html.unescape(text).splitlines(keepends=False)
    head = next((line.lstrip() for line in cookie_data if line.strip()), "")
    if head[:1] in ("[", "{"):
        raise ValueError(f"{source} is a JSON export; export it again in Netscape format")
    for i, line in enumerate(cookie_data):
        body = line.lstrip()
        prefix = "#HttpOnly_" if body.startswith("#HttpOnly_") else ""
        body = body[len(prefix) :]
        if not body or body.startswith("#"):
            continue
        line_data = body.split("\t") if "\t" in body else body.split(None, 6)
        if len(line_data) == 6:
            line_data.append("")
        if len(line_data) != 7:
            raise ValueError(f"{source} line {i + 1} is not a Netscape cookie row: {line!r}")
        line_data[4] = ""
        line_data[1] = str(line_data[0].startswith(".")).upper()
        cookie_data[i] = prefix + "\t".join(line_data)
    cookie_data.insert(0, "# Netscape HTTP Cookie File")
    cookie_jar._really_load(StringIO("\n".join(cookie_data)), source, True, True)  # type: ignore[attr-defined]
    if not len(cookie_jar):
        raise ValueError(f"{source} holds no cookies; export it again in Netscape format")
    return cookie_jar


def save_cookies(path: Path, cookies: CookieJar) -> None:
    if hasattr(cookies, "jar"):
        cookies = cookies.jar

    cookie_jar = load_cookie_file(path) if path.exists() else MozillaCookieJar(path)
    for cookie in cookies:
        if not cookie.name or not cookie.domain:
            continue
        cookie_jar.set_cookie(cookie)
    cookie_jar.save(ignore_discard=True, ignore_expires=True)


def get_credentials(service: str, profile: Optional[str]) -> Optional[Credential]:
    """Get Service Credentials for Profile."""
    credentials = config.credentials.get(service)
    if credentials:
        if isinstance(credentials, dict):
            if profile:
                credentials = credentials.get(profile) or credentials.get("default")
            else:
                credentials = credentials.get("default")
        if credentials:
            return Credential.from_config(credentials)
    return None
