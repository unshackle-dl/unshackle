"""Regression tests for peer-input sanitizers (``core/api/sanitize.py``)."""

from __future__ import annotations

from unshackle.core.api.sanitize import safe_cache_key


def test_safe_cache_key_rejects_backslash_traversal():
    # On POSIX, backslash is a plain character, so pathlib.Path treats
    # "..\\..\\secret" as one filename; written as "<key>.json" it escapes the
    # cache dir on Windows. PureWindowsPath handling must reject it everywhere.
    assert safe_cache_key("..\\..\\secret") is None
    assert safe_cache_key("sub\\..\\secret") is None


def test_safe_cache_key_rejects_dotdot_and_empty_segments():
    assert safe_cache_key("../secret") is None
    assert safe_cache_key("a/../b") is None
    assert safe_cache_key("a/./b") is None
    assert safe_cache_key("..") is None
    assert safe_cache_key(".") is None
    assert safe_cache_key("") is None
    assert safe_cache_key("a//b") is None


def test_safe_cache_key_rejects_absolute_paths_and_drives():
    assert safe_cache_key("/etc/passwd") is None
    assert safe_cache_key("\\x") is None
    assert safe_cache_key("C:/x") is None
    assert safe_cache_key("C:x") is None
    assert safe_cache_key("//server/share/x") is None
    assert safe_cache_key("a/b:stream") is None
    assert safe_cache_key("a\x00b") is None


def test_safe_cache_key_accepts_plain_filename():
    assert safe_cache_key("valid-key_123") == "valid-key_123"


def test_safe_cache_key_accepts_nested_key_and_normalises_separators():
    # A Cacher key with a subdirectory, as NF uses ("session_web/<sha1>"), keeps its
    # posix form; a backslash from a Windows peer names the same nested file.
    assert safe_cache_key("session_web/abc") == "session_web/abc"
    assert safe_cache_key("MSL/a/b") == "MSL/a/b"
    assert safe_cache_key("session_web\\abc") == "session_web/abc"
    assert safe_cache_key("/".join(["d"] * 9)) is None
