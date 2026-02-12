import ast
import contextlib
import importlib.util
import json
import logging
import os
import re
import socket
import sys
import time
import traceback
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Optional, Sequence, Union
from urllib.parse import ParseResult, urlparse
from uuid import uuid4

import chardet
import pycountry
from construct import ValidationError
from fontTools import ttLib
from langcodes import Language, closest_match
from pymp4.parser import Box
from unidecode import unidecode

from unshackle.core.cacher import Cacher
from unshackle.core.clients.base import BaseHttpClient
from unshackle.core.clients.factory import http_unshackle
from unshackle.core.config import config
from unshackle.core.constants import LANGUAGE_EXACT_DISTANCE, LANGUAGE_MAX_DISTANCE

"""
Utility functions for the unshackle media archival tool.

This module provides various utility functions including:
- Font discovery and fallback system for subtitle rendering
- Cross-platform system font scanning with Windows → Linux font family mapping
- Log file management and rotation
- IP geolocation with caching and provider rotation
- Language matching utilities
- MP4/ISOBMFF box parsing
- File sanitization and path handling
- Structured JSON debug logging

Font System:
    The font subsystem enables cross-platform font discovery for ASS/SSA subtitles.
    On Linux, it scans standard font directories and maps Windows font names (Arial,
    Times New Roman) to their Linux equivalents (Liberation Sans, Liberation Serif).

Main Font Functions:
    - get_system_fonts(): Discover installed fonts across platforms
    - find_font_with_fallbacks(): Match fonts with intelligent fallback strategies
    - suggest_font_packages(): Recommend packages to install for missing fonts
"""


def rotate_log_file(log_path: Path, keep: int = 20) -> Path:
    """
    Update Log Filename and delete old log files.
    It keeps only the 20 newest logs by default.
    """
    if not log_path:
        raise ValueError("A log path must be provided")

    try:
        log_path.relative_to(Path(""))  # file name only
    except ValueError:
        pass
    else:
        log_path = config.directories.logs / log_path

    log_path = log_path.parent / log_path.name.format_map(
        defaultdict(str, name="root", time=datetime.now().strftime("%Y%m%d-%H%M%S"))
    )

    if log_path.parent.exists():
        log_files = [x for x in log_path.parent.iterdir() if x.suffix == log_path.suffix]
        for log_file in log_files[::-1][keep - 1 :]:
            # keep n newest files and delete the rest
            log_file.unlink()

    log_path.parent.mkdir(parents=True, exist_ok=True)
    return log_path


def import_module_by_path(path: Path) -> ModuleType:
    """Import a Python file by Path as a Module."""
    if not path:
        raise ValueError("Path must be provided")
    if not isinstance(path, Path):
        raise TypeError(f"Expected path to be a {Path}, not {path!r}")
    if not path.exists():
        raise ValueError("Path does not exist")

    # compute package hierarchy for relative import support
    if path.is_relative_to(config.directories.core_dir):
        name = []
        _path = path.parent
        while _path.stem != config.directories.core_dir.stem:
            name.append(_path.stem)
            _path = _path.parent
        name = ".".join([config.directories.core_dir.stem] + name[::-1])
    else:
        # is outside the src package
        if str(path.parent.parent) not in sys.path:
            sys.path.insert(1, str(path.parent.parent))
        name = path.parent.stem

    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


def sanitize_filename(filename: str, spacer: str = ".") -> str:
    """
    Sanitize a string to be filename safe.

    The spacer is safer to be a '.' for older DDL and p2p sharing spaces.
    This includes web-served content via direct links and such.

    Set `unicode_filenames: true` in config to preserve native language
    characters (Korean, Japanese, Chinese, etc.) instead of transliterating
    them to ASCII equivalents.
    """
    # optionally replace non-ASCII characters with ASCII equivalents
    if not config.unicode_filenames:
        filename = unidecode(filename)

    # remove or replace further characters as needed
    filename = "".join(c for c in filename if unicodedata.category(c) != "Mn")  # hidden characters
    filename = filename.replace("/", " & ").replace(";", " & ")  # e.g. multi-episode filenames
    if spacer == ".":
        filename = re.sub(r" - ", spacer, filename)  # title separators to spacer (avoids .-. pattern)
    filename = re.sub(r"[:; ]", spacer, filename)  # structural chars to (spacer)
    filename = re.sub(r"[\\*!?¿,'\"" "()<>|$#~]", "", filename)  # not filename safe chars
    filename = re.sub(rf"[{spacer}]{{2,}}", spacer, filename)  # remove extra neighbouring (spacer)s

    return filename


def is_close_match(language: Union[str, Language], languages: Sequence[Union[str, Language, None]]) -> bool:
    """Check if a language is a close match to any of the provided languages."""
    languages = [x for x in languages if x]
    if not languages:
        return False
    return closest_match(language, list(map(str, languages)))[1] <= LANGUAGE_MAX_DISTANCE


def is_exact_match(language: Union[str, Language], languages: Sequence[Union[str, Language, None]]) -> bool:
    """Check if a language is an exact match to any of the provided languages."""
    languages = [x for x in languages if x]
    if not languages:
        return False
    return closest_match(language, list(map(str, languages)))[1] <= LANGUAGE_EXACT_DISTANCE


def get_boxes(data: bytes, box_type: bytes, as_bytes: bool = False) -> Box:  # type: ignore
    """
    Scan a byte array for a wanted MP4/ISOBMFF box, then parse and yield each find.

    This function searches through binary MP4 data to find and parse specific box types.
    The MP4/ISOBMFF box format consists of:
    - 4 bytes: size of the box (including size and type fields)
    - 4 bytes: box type identifier (e.g., 'moov', 'trak', 'pssh')
    - Remaining bytes: box data

    The function uses slicing to directly locate the requested box type in the data
    rather than recursively traversing the box hierarchy. This is efficient when
    looking for specific box types regardless of their position in the hierarchy.

    Parameters:
        data: Binary data containing MP4/ISOBMFF boxes
        box_type: 4-byte identifier of the box type to find (e.g., b'pssh')
        as_bytes: If True, returns the box as bytes, otherwise returns parsed box object

    Yields:
        Box objects of the requested type found in the data

    Notes:
        - For each box found, the function updates the search offset to skip past
          the current box to avoid finding the same box multiple times
        - The function handles validation errors for certain box types (e.g., tenc)
        - The size field is located 4 bytes before the box type identifier
    """
    # using slicing to get to the wanted box is done because parsing the entire box and recursively
    # scanning through each box and its children often wouldn't scan far enough to reach the wanted box.
    # since it doesn't care what child box the wanted box is from, this works fine.
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("data must be bytes")

    offset = 0
    while offset < len(data):
        try:
            index = data[offset:].index(box_type)
        except ValueError:
            break

        pos = offset + index

        if pos < 4:
            offset = pos + len(box_type)
            continue

        box_start = pos - 4

        try:
            box = Box.parse(data[box_start:])
            if as_bytes:
                box = Box.build(box)

            yield box

            box_size = len(Box.build(box))
            offset = box_start + box_size

        except IOError:
            break
        except ValidationError as e:
            if box_type == b"tenc":
                offset = pos + len(box_type)
                continue
            raise e


def ap_case(text: str, keep_spaces: bool = False, stop_words: tuple[str] = None) -> str:
    """
    Convert a string to title case using AP/APA style.
    Based on https://github.com/words/ap-style-title-case

    Parameters:
        text: The text string to title case with AP/APA style.
        keep_spaces: To keep the original whitespace, or to just use a normal space.
            This would only be needed if you have special whitespace between words.
        stop_words: Override the default stop words with your own ones.
    """
    if not text:
        return ""

    if not stop_words:
        stop_words = (
            "a",
            "an",
            "and",
            "at",
            "but",
            "by",
            "for",
            "in",
            "nor",
            "of",
            "on",
            "or",
            "so",
            "the",
            "to",
            "up",
            "yet",
        )

    splitter = re.compile(r"(\s+|[-‑–—])")
    words = splitter.split(text)

    return "".join(
        [
            [" ", word][keep_spaces]
            if re.match(r"\s+", word)
            else word
            if splitter.match(word)
            else word.lower()
            if i != 0 and i != len(words) - 1 and word.lower() in stop_words
            else word.capitalize()
            for i, word in enumerate(words)
        ]
    )


# Common country code aliases that differ from ISO 3166-1 alpha-2
COUNTRY_CODE_ALIASES = {
    "uk": "gb",  # United Kingdom -> Great Britain
}


def get_country_name(code: str) -> Optional[str]:
    """
    Convert a 2-letter country code to full country name.

    Args:
        code: ISO 3166-1 alpha-2 country code (e.g., 'ca', 'us', 'gb', 'uk')

    Returns:
        Full country name (e.g., 'Canada', 'United States', 'United Kingdom') or None if not found

    Examples:
        >>> get_country_name('ca')
        'Canada'
        >>> get_country_name('US')
        'United States'
        >>> get_country_name('uk')
        'United Kingdom'
    """
    # Handle common aliases
    code = COUNTRY_CODE_ALIASES.get(code.lower(), code.lower())

    try:
        country = pycountry.countries.get(alpha_2=code.upper())
        if country:
            return country.name
    except (KeyError, LookupError):
        pass
    return None


def get_country_code(name: str) -> Optional[str]:
    """
    Convert a country name to its 2-letter ISO 3166-1 alpha-2 code.

    Args:
        name: Full country name (e.g., 'Canada', 'United States', 'United Kingdom')

    Returns:
        2-letter country code in uppercase (e.g., 'CA', 'US', 'GB') or None if not found

    Examples:
        >>> get_country_code('Canada')
        'CA'
        >>> get_country_code('united states')
        'US'
        >>> get_country_code('United Kingdom')
        'GB'
    """
    try:
        # Try exact name match first
        country = pycountry.countries.get(name=name.title())
        if country:
            return country.alpha_2.upper()

        # Try common name (e.g., "Bolivia" vs "Bolivia, Plurinational State of")
        country = pycountry.countries.get(common_name=name.title())
        if country:
            return country.alpha_2.upper()

        # Try fuzzy search as fallback
        results = pycountry.countries.search_fuzzy(name)
        if results:
            return results[0].alpha_2.upper()
    except (KeyError, LookupError):
        pass
    return None


def get_ip_info(session: Optional[BaseHttpClient] = None) -> dict:
    """
    Use ipinfo.io to get IP location information.

    If you provide a Requests Session with a Proxy, that proxies IP information
    is what will be returned.
    """
    return (session or http_unshackle.get('ipinfo')).get("https://ipinfo.io/json").json()


def get_cached_ip_info(session: Optional[BaseHttpClient] = None) -> Optional[dict]:
    """
    Get IP location information with 24-hour caching and fallback providers.

    This function uses a global cache to avoid repeated API calls when the IP
    hasn't changed. Should only be used for local IP checks, not for proxy verification.
    Implements smart provider rotation to handle rate limiting (429 errors).

    Args:
        session: Optional requests session (usually without proxy for local IP)

    Returns:
        Dict with IP info including 'country' key, or None if all providers fail
    """

    log = logging.getLogger("get_cached_ip_info")
    cache = Cacher("global").get("ip_info")

    if cache and not cache.expired:
        return cache.data

    provider_state_cache = Cacher("global").get("ip_provider_state")
    provider_state = provider_state_cache.data if provider_state_cache and not provider_state_cache.expired else {}

    providers = {
        "ipinfo": "https://ipinfo.io/json",
        "ipapi": "https://ipapi.co/json",
    }

    session = session or http_unshackle.get('ipinfo')
    provider_order = ["ipinfo", "ipapi"]

    current_time = time.time()
    for provider_name in list(provider_order):
        if provider_name in provider_state:
            rate_limit_info = provider_state[provider_name]
            if (current_time - rate_limit_info.get("rate_limited_at", 0)) < 300:
                log.debug(f"Provider {provider_name} was rate limited recently, trying other provider first")
                provider_order.remove(provider_name)
                provider_order.append(provider_name)
                break

    for provider_name in provider_order:
        provider_url = providers[provider_name]
        try:
            log.debug(f"Trying IP provider: {provider_name}")
            response = session.get(provider_url, timeout=10)
            # TODO: check conflicts with retry logic
            if response.status_code == 429:
                log.warning(f"Provider {provider_name} returned 429 (rate limited), trying next provider")
                if provider_name not in provider_state:
                    provider_state[provider_name] = {}
                provider_state[provider_name]["rate_limited_at"] = current_time
                provider_state[provider_name]["rate_limit_count"] = (
                    provider_state[provider_name].get("rate_limit_count", 0) + 1
                )

                provider_state_cache.set(provider_state, expiration=300)
                continue

            elif response.status_code == 200:
                data = response.json()
                normalized_data = {}

                if "country" in data:
                    normalized_data = data
                elif "country_code" in data:
                    normalized_data = {
                        "country": data.get("country_code", "").lower(),
                        "region": data.get("region", ""),
                        "city": data.get("city", ""),
                        "ip": data.get("ip", ""),
                    }

                if normalized_data and "country" in normalized_data:
                    log.debug(f"Successfully got IP info from provider: {provider_name}")

                    if provider_name in provider_state:
                        provider_state[provider_name].pop("rate_limited_at", None)
                        provider_state_cache.set(provider_state, expiration=300)

                    normalized_data["_provider"] = provider_name
                    cache.set(normalized_data, expiration=86400)
                    return normalized_data
            else:
                log.debug(f"Provider {provider_name} returned status {response.status_code}")

        except Exception as e:
            log.debug(f"Provider {provider_name} failed with exception: {e}")
            continue

    log.warning("All IP geolocation providers failed")
    return None


def time_elapsed_since(start: float) -> str:
    """
    Get time elapsed since a timestamp as a string.
    E.g., `1h56m2s`, `15m12s`, `0m55s`, e.t.c.
    """
    elapsed = int(time.time() - start)

    minutes, seconds = divmod(elapsed, 60)
    hours, minutes = divmod(minutes, 60)

    time_string = f"{minutes:d}m{seconds:d}s"
    if hours:
        time_string = f"{hours:d}h{time_string}"

    return time_string


def try_ensure_utf8(data: bytes) -> bytes:
    """
    Try to ensure that the given data is encoded in UTF-8.

    Parameters:
        data: Input data that may or may not yet be UTF-8 or another encoding.

    Returns the input data encoded in UTF-8 if successful. If unable to detect the
    encoding of the input data, then the original data is returned as-received.
    """
    try:
        data.decode("utf8")
        return data
    except UnicodeDecodeError:
        try:
            # CP-1252 is a superset of latin1 but has gaps. Replace unknown
            # characters instead of failing on them.
            return data.decode("cp1252", errors="replace").encode("utf8")
        except UnicodeDecodeError:
            try:
                # last ditch effort to detect encoding
                detection_result = chardet.detect(data)
                if not detection_result["encoding"]:
                    return data
                return data.decode(detection_result["encoding"]).encode("utf8")
            except UnicodeDecodeError:
                return data


def get_free_port() -> int:
    """
    Get an available port to use between a-b (inclusive).

    The port is freed as soon as this has returned, therefore, it
    is possible for the port to be taken before you try to use it.
    """
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def get_extension(value: Union[str, Path, ParseResult]) -> Optional[str]:
    """
    Get a URL or Path file extension/suffix.

    Note: The returned value will begin with `.`.
    """
    if isinstance(value, ParseResult):
        value_parsed = value
    elif isinstance(value, (str, Path)):
        value_parsed = urlparse(str(value))
    else:
        raise TypeError(f"Expected {str}, {Path}, or {ParseResult}, got {type(value)}")

    if value_parsed.path:
        ext = os.path.splitext(value_parsed.path)[1]
        if ext and ext != ".":
            return ext


def extract_font_family(font_path: Path) -> Optional[str]:
    """
    Extract font family name from TTF/OTF file using fontTools.

    Args:
        font_path: Path to the font file

    Returns:
        Font family name if successfully extracted, None otherwise
    """
    # Suppress verbose fontTools logging during font table parsing
    import io

    logging.getLogger("fontTools").setLevel(logging.ERROR)
    logging.getLogger("fontTools.ttLib").setLevel(logging.ERROR)
    logging.getLogger("fontTools.ttLib.tables").setLevel(logging.ERROR)
    logging.getLogger("fontTools.ttLib.tables._n_a_m_e").setLevel(logging.ERROR)
    stderr_backup = sys.stderr
    sys.stderr = io.StringIO()

    try:
        font = ttLib.TTFont(font_path, lazy=True)
        name_table = font["name"]

        # Try to get family name (nameID 1) for Windows platform (platformID 3)
        # This matches the naming convention used in Windows registry
        for record in name_table.names:
            if record.nameID == 1 and record.platformID == 3:
                return record.toUnicode()

        # Fallback to other platforms if Windows name not found
        for record in name_table.names:
            if record.nameID == 1:
                return record.toUnicode()

    except Exception:
        pass
    finally:
        sys.stderr = stderr_backup

    return None


def get_windows_fonts() -> dict[str, Path]:
    """
    Get fonts from Windows registry.

    Returns:
        Dictionary mapping font family names to their file paths
    """
    import winreg

    with winreg.ConnectRegistry(None, winreg.HKEY_LOCAL_MACHINE) as reg:
        key = winreg.OpenKey(reg, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts", 0, winreg.KEY_READ)
        total_fonts = winreg.QueryInfoKey(key)[1]
        return {
            name.replace(" (TrueType)", ""): Path(r"C:\Windows\Fonts", filename)
            for n in range(0, total_fonts)
            for name, filename, _ in [winreg.EnumValue(key, n)]
        }


def scan_font_directory(font_dir: Path, fonts: dict[str, Path], log: logging.Logger) -> None:
    """
    Scan a single directory for fonts.

    Args:
        font_dir: Directory to scan
        fonts: Dictionary to populate with found fonts
        log: Logger instance for error reporting
    """
    font_files = list(font_dir.rglob("*.ttf")) + list(font_dir.rglob("*.otf"))

    for font_file in font_files:
        try:
            if family_name := extract_font_family(font_file):
                if family_name not in fonts:
                    fonts[family_name] = font_file
        except Exception as e:
            log.debug(f"Failed to process {font_file}: {e}")


def get_unix_fonts() -> dict[str, Path]:
    """
    Get fonts from Linux/macOS standard directories.

    Returns:
        Dictionary mapping font family names to their file paths
    """
    log = logging.getLogger("get_system_fonts")
    fonts = {}

    font_dirs = [
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path.home() / ".fonts",
        Path.home() / ".local/share/fonts",
    ]

    for font_dir in font_dirs:
        if not font_dir.exists():
            continue

        try:
            scan_font_directory(font_dir, fonts, log)
        except Exception as e:
            log.warning(f"Failed to scan {font_dir}: {e}")
    return fonts


def get_system_fonts() -> dict[str, Path]:
    """
    Get system fonts as a mapping of font family names to font file paths.

    On Windows: Uses registry to get font display names
    On Linux/macOS: Scans standard font directories and extracts family names using fontTools

    Returns:
        Dictionary mapping font family names to their file paths
    """
    if sys.platform == "win32":
        return get_windows_fonts()
    return get_unix_fonts()


# Common Windows font names mapped to their Linux equivalents
# Ordered by preference (first match is used)
FONT_ALIASES = {
    "Arial": ["Liberation Sans", "DejaVu Sans", "Nimbus Sans", "FreeSans"],
    "Arial Black": ["Liberation Sans", "DejaVu Sans", "Nimbus Sans"],
    "Arial Bold": ["Liberation Sans", "DejaVu Sans"],
    "Arial Unicode MS": ["DejaVu Sans", "Noto Sans", "FreeSans"],
    "Times New Roman": ["Liberation Serif", "DejaVu Serif", "Nimbus Roman", "FreeSerif"],
    "Courier New": ["Liberation Mono", "DejaVu Sans Mono", "Nimbus Mono PS", "FreeMono"],
    "Comic Sans MS": ["Comic Neue", "Comic Relief", "DejaVu Sans"],
    "Georgia": ["Gelasio", "DejaVu Serif", "Liberation Serif"],
    "Impact": ["Impact", "Anton", "Liberation Sans"],
    "Trebuchet MS": ["Ubuntu", "DejaVu Sans", "Liberation Sans"],
    "Verdana": ["DejaVu Sans", "Bitstream Vera Sans", "Liberation Sans"],
    "Tahoma": ["DejaVu Sans", "Liberation Sans"],
    "Adobe Arabic": ["Noto Sans Arabic", "DejaVu Sans"],
    "Noto Sans Thai": ["Noto Sans Thai", "Noto Sans"],
}


def find_case_insensitive(font_name: str, fonts: dict[str, Path]) -> Optional[Path]:
    """
    Find font by case-insensitive name match.

    Args:
        font_name: Font family name to find
        fonts: Dictionary of available fonts

    Returns:
        Path to matched font, or None if not found
    """
    font_lower = font_name.lower()
    for name, path in fonts.items():
        if name.lower() == font_lower:
            return path
    return None


def find_font_with_fallbacks(font_name: str, system_fonts: dict[str, Path]) -> Optional[Path]:
    """
    Find a font by name with intelligent fallback matching.

    Tries multiple strategies in order:
    1. Exact match (case-sensitive)
    2. Case-insensitive match
    3. Alias lookup (Windows → Linux font equivalents)
    4. Partial/prefix match

    Args:
        font_name: The requested font family name (e.g., "Arial", "Times New Roman")
        system_fonts: Dictionary of available fonts (family name → path)

    Returns:
        Path to the matched font file, or None if no match found
    """
    if not system_fonts:
        return None

    # Strategy 1: Exact match (case-sensitive)
    if font_name in system_fonts:
        return system_fonts[font_name]

    # Strategy 2: Case-insensitive match
    if result := find_case_insensitive(font_name, system_fonts):
        return result

    # Strategy 3: Alias lookup
    if font_name in FONT_ALIASES:
        for alias in FONT_ALIASES[font_name]:
            # Try exact match for alias
            if alias in system_fonts:
                return system_fonts[alias]
            # Try case-insensitive match for alias
            if result := find_case_insensitive(alias, system_fonts):
                return result

    # Strategy 4: Partial/prefix match as last resort
    font_name_lower = font_name.lower()
    for name, path in system_fonts.items():
        if name.lower().startswith(font_name_lower):
            return path

    return None


# Mapping of font families to system packages that provide them
FONT_PACKAGES = {
    "liberation": {
        "debian": "fonts-liberation fonts-liberation2",
        "fonts": ["Liberation Sans", "Liberation Serif", "Liberation Mono"],
    },
    "dejavu": {
        "debian": "fonts-dejavu fonts-dejavu-core fonts-dejavu-extra",
        "fonts": ["DejaVu Sans", "DejaVu Serif", "DejaVu Sans Mono"],
    },
    "noto": {
        "debian": "fonts-noto fonts-noto-core",
        "fonts": ["Noto Sans", "Noto Serif", "Noto Sans Mono", "Noto Sans Arabic", "Noto Sans Thai"],
    },
    "ubuntu": {
        "debian": "fonts-ubuntu",
        "fonts": ["Ubuntu", "Ubuntu Mono"],
    },
}


def suggest_font_packages(missing_fonts: list[str]) -> dict[str, list[str]]:
    """
    Suggest system packages to install for missing fonts.

    Args:
        missing_fonts: List of font family names that couldn't be found

    Returns:
        Dictionary mapping package names to lists of fonts they would provide
    """
    suggestions = {}

    # Check which fonts from aliases would help
    needed_aliases = set()
    for font in missing_fonts:
        if font in FONT_ALIASES:
            needed_aliases.update(FONT_ALIASES[font])

    # Map needed aliases to packages
    for package_name, package_info in FONT_PACKAGES.items():
        provided_fonts = package_info["fonts"]
        matching_fonts = [f for f in provided_fonts if f in needed_aliases]
        if matching_fonts:
            suggestions[package_info["debian"]] = matching_fonts

    return suggestions


class FPS(ast.NodeVisitor):
    def visit_BinOp(self, node: ast.BinOp) -> float:
        if isinstance(node.op, ast.Div):
            return self.visit(node.left) / self.visit(node.right)
        raise ValueError(f"Invalid operation: {node.op}")

    def visit_Num(self, node: ast.Num) -> complex:
        return node.n

    def visit_Expr(self, node: ast.Expr) -> float:
        return self.visit(node.value)

    @classmethod
    def parse(cls, expr: str) -> float:
        return cls().visit(ast.parse(expr).body[0])


"""
Structured JSON debug logging for unshackle.

Provides comprehensive debugging information for service developers and troubleshooting.
When enabled, logs all operations, requests, responses, DRM operations, and errors in JSON format.
"""


class DebugLogger:
    """
    Structured JSON debug logger for unshackle.

    Outputs JSON Lines format where each line is a complete JSON object.
    This makes it easy to parse, filter, and analyze logs programmatically.
    """

    def __init__(self, log_path: Optional[Path] = None, enabled: bool = False, log_keys: bool = False):
        """
        Initialize the debug logger.

        Args:
            log_path: Path to the log file. If None, logging is disabled.
            enabled: Whether debug logging is enabled.
            log_keys: Whether to log decryption keys (for debugging key issues).
        """
        self.enabled = enabled and log_path is not None
        self.log_path = log_path
        self.session_id = str(uuid4())[:8]
        self.file_handle = None
        self.log_keys = log_keys

        if self.enabled:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.file_handle = open(self.log_path, "a", encoding="utf-8")
            self.log_session_start()

    def log_session_start(self):
        """Log the start of a new session with environment information."""
        import platform

        from unshackle.core import __version__

        self.log(
            level="INFO",
            operation="session_start",
            message="Debug logging session started",
            context={
                "unshackle_version": __version__,
                "python_version": sys.version,
                "platform": platform.platform(),
                "platform_system": platform.system(),
                "platform_release": platform.release(),
            },
        )

    def log(
        self,
        level: str = "DEBUG",
        operation: str = "",
        message: str = "",
        context: Optional[dict[str, Any]] = None,
        service: Optional[str] = None,
        error: Optional[Exception] = None,
        request: Optional[dict[str, Any]] = None,
        response: Optional[dict[str, Any]] = None,
        duration_ms: Optional[float] = None,
        success: Optional[bool] = None,
        **kwargs,
    ):
        """
        Log a structured JSON entry.

        Args:
            level: Log level (DEBUG, INFO, WARNING, ERROR)
            operation: Name of the operation being performed
            message: Human-readable message
            context: Additional context information
            service: Service name (e.g., DSNP, NF)
            error: Exception object if an error occurred
            request: Request details (URL, method, headers, body)
            response: Response details (status, headers, body)
            duration_ms: Operation duration in milliseconds
            success: Whether the operation succeeded
            **kwargs: Additional fields to include in the log entry
        """
        if not self.enabled or not self.file_handle:
            return

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": self.session_id,
            "level": level,
        }

        if operation:
            entry["operation"] = operation
        if message:
            entry["message"] = message
        if service:
            entry["service"] = service
        if context:
            entry["context"] = self.sanitize_data(context)
        if request:
            entry["request"] = self.sanitize_data(request)
        if response:
            entry["response"] = self.sanitize_data(response)
        if duration_ms is not None:
            entry["duration_ms"] = duration_ms
        if success is not None:
            entry["success"] = success

        if error:
            entry["error"] = {
                "type": type(error).__name__,
                "message": str(error),
                "traceback": traceback.format_exception(type(error), error, error.__traceback__),
            }

        for key, value in kwargs.items():
            if key not in entry:
                entry[key] = self.sanitize_data(value)

        try:
            self.file_handle.write(json.dumps(entry, default=str) + "\n")
            self.file_handle.flush()
        except Exception as e:
            print(f"Failed to write debug log: {e}", file=sys.stderr)

    def sanitize_data(self, data: Any) -> Any:
        """
        Sanitize data for JSON serialization.
        Handles complex objects and removes sensitive information.
        """
        if data is None:
            return None

        if isinstance(data, (str, int, float, bool)):
            return data

        if isinstance(data, (list, tuple)):
            return [self.sanitize_data(item) for item in data]

        if isinstance(data, dict):
            sanitized = {}
            for key, value in data.items():
                key_lower = str(key).lower()
                has_prefix = key_lower.startswith("has_")

                is_always_sensitive = not has_prefix and any(
                    sensitive in key_lower for sensitive in ["password", "token", "secret", "auth", "cookie"]
                )

                is_key_field = (
                    "key" in key_lower
                    and not has_prefix
                    and not any(safe in key_lower for safe in ["_count", "_id", "_type", "kid", "keys_", "key_found"])
                )

                should_redact = is_always_sensitive or (is_key_field and not self.log_keys)

                if should_redact:
                    sanitized[key] = "[REDACTED]"
                else:
                    sanitized[key] = self.sanitize_data(value)
            return sanitized

        if isinstance(data, bytes):
            try:
                return data.hex()
            except Exception:
                return "[BINARY_DATA]"

        if isinstance(data, Path):
            return str(data)

        try:
            return str(data)
        except Exception:
            return f"[{type(data).__name__}]"

    def log_operation_start(self, operation: str, **kwargs) -> str:
        """
        Log the start of an operation and return an operation ID.

        Args:
            operation: Name of the operation
            **kwargs: Additional context

        Returns:
            Operation ID that can be used to log the end of the operation
        """
        op_id = str(uuid4())[:8]
        self.log(
            level="DEBUG",
            operation=f"{operation}_start",
            message=f"Starting operation: {operation}",
            operation_id=op_id,
            **kwargs,
        )
        return op_id

    def log_operation_end(
        self, operation: str, operation_id: str, success: bool = True, duration_ms: Optional[float] = None, **kwargs
    ):
        """
        Log the end of an operation.

        Args:
            operation: Name of the operation
            operation_id: Operation ID from log_operation_start
            success: Whether the operation succeeded
            duration_ms: Operation duration in milliseconds
            **kwargs: Additional context
        """
        self.log(
            level="INFO" if success else "ERROR",
            operation=f"{operation}_end",
            message=f"Finished operation: {operation}",
            operation_id=operation_id,
            success=success,
            duration_ms=duration_ms,
            **kwargs,
        )

    def log_service_call(self, method: str, url: str, **kwargs):
        """
        Log a service API call.

        Args:
            method: HTTP method (GET, POST, etc.)
            url: Request URL
            **kwargs: Additional request details (headers, body, etc.)
        """
        self.log(level="DEBUG", operation="service_call", request={"method": method, "url": url, **kwargs})

    def log_drm_operation(self, drm_type: str, operation: str, **kwargs):
        """
        Log a DRM operation (PSSH extraction, license request, key retrieval).

        Args:
            drm_type: DRM type (Widevine, PlayReady, etc.)
            operation: DRM operation name
            **kwargs: Additional context (PSSH, KIDs, keys, etc.)
        """
        self.log(
            level="DEBUG", operation=f"drm_{operation}", message=f"{drm_type} {operation}", drm_type=drm_type, **kwargs
        )

    def log_vault_query(self, vault_name: str, operation: str, **kwargs):
        """
        Log a vault query operation.

        Args:
            vault_name: Name of the vault
            operation: Vault operation (get_key, add_key, etc.)
            **kwargs: Additional context (KID, key, success, etc.)
        """
        self.log(
            level="DEBUG",
            operation=f"vault_{operation}",
            message=f"Vault {vault_name}: {operation}",
            vault=vault_name,
            **kwargs,
        )

    def log_error(self, operation: str, error: Exception, **kwargs):
        """
        Log an error with full context.

        Args:
            operation: Operation that failed
            error: Exception that occurred
            **kwargs: Additional context
        """
        self.log(
            level="ERROR",
            operation=operation,
            message=f"Error in {operation}: {str(error)}",
            error=error,
            success=False,
            **kwargs,
        )

    def close(self):
        """Close the log file and clean up resources."""
        if self.file_handle:
            self.log(level="INFO", operation="session_end", message="Debug logging session ended")
            self.file_handle.close()
            self.file_handle = None


# Global debug logger instance
_debug_logger: Optional[DebugLogger] = None


def get_debug_logger() -> Optional[DebugLogger]:
    """Get the global debug logger instance."""
    return _debug_logger


def init_debug_logger(log_path: Optional[Path] = None, enabled: bool = False, log_keys: bool = False):
    """
    Initialize the global debug logger.

    Args:
        log_path: Path to the log file
        enabled: Whether debug logging is enabled
        log_keys: Whether to log decryption keys (for debugging key issues)
    """
    global _debug_logger
    if _debug_logger:
        _debug_logger.close()
    _debug_logger = DebugLogger(log_path=log_path, enabled=enabled, log_keys=log_keys)


def close_debug_logger():
    """Close the global debug logger."""
    global _debug_logger
    if _debug_logger:
        _debug_logger.close()
        _debug_logger = None


__all__ = (
    "DebugLogger",
    "get_debug_logger",
    "init_debug_logger",
    "close_debug_logger",
)
