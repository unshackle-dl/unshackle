"""Unit tests for try_ensure_utf8."""

from __future__ import annotations

import gzip

import pytest

from unshackle.core.utilities import try_ensure_utf8

pytestmark = pytest.mark.unit


def test_utf8_input_is_returned_unchanged() -> None:
    data = "caf\u00e9 \u2014 \u65e5\u672c".encode("utf8")
    assert try_ensure_utf8(data) is data


def test_cp1252_input_is_converted_to_utf8() -> None:
    assert try_ensure_utf8(b"caf\xe9 \x93q\x94") == "caf\u00e9 \u201cq\u201d".encode("utf8")


def test_bytes_undefined_in_cp1252_become_replacement_characters() -> None:
    assert try_ensure_utf8(b"a\x81\x8d\x8f\x90\x9db") == ("a" + "\ufffd" * 5 + "b").encode("utf8")


def test_gzip_input_is_decompressed_first() -> None:
    assert try_ensure_utf8(gzip.compress(b"caf\xe9")) == "caf\u00e9".encode("utf8")
