"""Helpers for masking secrets in text destined for logs or API responses."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlsplit, urlunsplit

REDACTED = "***"

# user:pass@ userinfo embedded in any URL (proxy URLs, remote server URLs)
URL_USERINFO_RE = re.compile(r"(?<=://)[^/@]+@")

# secret-bearing query parameters in URLs that end up in free text
SENSITIVE_QUERY_PARAM_RE = re.compile(r"(?i)\b(password|passwd|pwd|token|api_key|apikey|secret|auth)=([^&#\s\"']+)")


def redact_text(text: Optional[str], secrets: Iterable[str] = ()) -> Optional[str]:
    """
    Mask URL userinfo, secret-bearing query parameters, and any known secret
    strings inside a free-text value before it is logged or serialized.
    """
    if not isinstance(text, str) or not text:
        return text
    text = URL_USERINFO_RE.sub(f"{REDACTED}@", text)
    text = SENSITIVE_QUERY_PARAM_RE.sub(rf"\1={REDACTED}", text)
    # longest first so substrings don't survive partial replacement
    for secret in sorted({s for s in secrets if isinstance(s, str) and s}, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return text


def safe_display_url(url: str) -> str:
    """Rebuild a URL from its scheme/host/port/path only, so userinfo can never be logged."""
    parts = urlsplit(url)
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _path_bases() -> list[tuple[str, str]]:
    """Local base directories to strip from logged paths, longest-first.

    Anchors everything left of the unshackle install/root (and the venv / home dir) so an
    absolute path like ``/home/me/Projects/unshackle-live/temp`` logs as ``<unshackle>/temp``,
    hiding the username and machine layout on both Linux and Windows.
    """
    candidates: list[tuple[str, str]] = []
    try:
        # redact.py lives at <root>/unshackle/core/utils/redact.py -> parents[3] == project/install root
        candidates.append((str(Path(__file__).resolve().parents[3]), "<unshackle>"))
    except Exception:
        pass
    for raw in (getattr(sys, "prefix", ""), getattr(sys, "base_prefix", "")):
        if raw:
            candidates.append((str(Path(raw)), "<venv>"))
    try:
        candidates.append((str(Path.home()), "~"))
    except Exception:
        pass

    bases: list[tuple[str, str]] = []
    seen: set[str] = set()
    for base, token in candidates:
        # cover both separator conventions (a path may be logged with / even on Windows)
        for variant in (base, base.replace("\\", "/"), base.replace("/", "\\")):
            if variant and variant not in seen:
                seen.add(variant)
                bases.append((variant, token))
    bases.sort(key=lambda item: len(item[0]), reverse=True)
    return bases


_PATH_BASES = _path_bases()


def redact_path(text: Optional[str]) -> Optional[str]:
    """Replace local base-directory prefixes (install root, venv, home) in ``text`` with tokens.

    Idempotent and cheap; only touches strings that actually contain a known base dir, so URLs
    and relative paths pass through unchanged.
    """
    if not isinstance(text, str) or not text:
        return text
    for base, token in _PATH_BASES:
        if base in text:
            text = text.replace(base, token)
    return text


# any http(s) URL embedded in free text (content/manifest/segment/api locations)
URL_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)
# a plausible file extension to preserve (e.g. .mpd, .m3u8, .mp4, .m4s, .vtt)
_EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,5}$")


def _collapse_url(match: "re.Match[str]") -> str:
    url = match.group(0)
    try:
        suffix = Path(urlsplit(url).path).suffix
    except Exception:
        suffix = ""
    if not _EXT_RE.match(suffix):
        suffix = ""
    return f"redacted{suffix}"


def redact_url(text: Optional[str]) -> Optional[str]:
    """Collapse every http(s) URL in ``text`` to ``redacted[.ext]``.

    Hides content/CDN/manifest/segment/api locations (host + path + query) from shareable debug
    logs while keeping the file extension so the manifest/segment type stays visible
    (e.g. ``redacted.mpd``). Non-URL strings pass through unchanged.
    """
    if not isinstance(text, str) or not text:
        return text
    return URL_RE.sub(_collapse_url, text)


def redact_all(text: Optional[str]) -> Optional[str]:
    """Full redaction for logged strings: secrets, then URLs, then local path prefixes."""
    return redact_path(redact_url(redact_text(text)))
