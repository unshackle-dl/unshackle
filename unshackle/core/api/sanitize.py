"""Helpers for safely logging user-provided values."""

from __future__ import annotations


def sanitize_log(value: object) -> str:
    """Sanitize a value for safe logging by removing newlines and control characters."""
    return str(value).replace("\n", "").replace("\r", "").replace("\x00", "")
