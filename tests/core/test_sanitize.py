"""Regression tests for peer-input sanitizers (``core/api/sanitize.py``)."""

from __future__ import annotations

from unshackle.core.api.sanitize import safe_cache_key


def test_safe_cache_key_rejects_backslash_traversal():
    # On POSIX, backslash is a plain character, so pathlib.Path treats
    # "..\\..\\secret" as one filename; written as "<key>.json" it escapes the
    # cache dir on Windows. PureWindowsPath handling must reject it everywhere.
    assert safe_cache_key("..\\..\\secret") is None
    assert safe_cache_key("sub\\dir") is None


def test_safe_cache_key_rejects_forward_slash_and_dotdot():
    assert safe_cache_key("../secret") is None
    assert safe_cache_key("a/b") is None
    assert safe_cache_key("..") is None
    assert safe_cache_key(".") is None
    assert safe_cache_key("") is None


def test_safe_cache_key_accepts_plain_filename():
    assert safe_cache_key("valid-key_123") == "valid-key_123"
