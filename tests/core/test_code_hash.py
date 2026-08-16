"""Unit tests for the code fingerprint behind the banner and the /api/health `code_hash` field.
It runs at import time, so it must always return a string and must never raise. It must change when
the framework source changes and stay unchanged when anything else does."""

from __future__ import annotations

from pathlib import Path

import pytest

import unshackle.core
from unshackle.core import __code_hash__, code_files, code_hash

pytestmark = pytest.mark.unit


def test_is_hex_and_stable() -> None:
    first = code_hash()
    assert len(first) == 40
    assert int(first, 16) >= 0
    assert first == code_hash()


def test_exported_value_is_the_short_form() -> None:
    assert __code_hash__ == code_hash()[:7]


def test_covers_the_framework_dirs_only() -> None:
    rels = code_files()
    assert "__main__.py" in rels
    assert any(rel.startswith("core/") for rel in rels)
    assert not any(rel.startswith("services/") for rel in rels)
    assert not any("__pycache__" in rel for rel in rels)
    assert all(rel.endswith(".py") for rel in rels)
    assert rels == sorted(rels)  # order must not depend on the filesystem


def test_content_changes_the_hash() -> None:
    rels = ["core/__init__.py", "commands/dl.py"]
    blobs = {"core/__init__.py": b"a", "commands/dl.py": b"b"}
    before = code_hash(files=rels, read=blobs.__getitem__)

    blobs["commands/dl.py"] = b"b "
    assert code_hash(files=rels, read=blobs.__getitem__) != before


def test_path_is_part_of_the_hash() -> None:
    """A rename with identical bytes must not look like the same code."""
    same_bytes = {"core/a.py": b"x", "core/b.py": b"x"}
    assert code_hash(files=["core/a.py"], read=same_bytes.__getitem__) != code_hash(
        files=["core/b.py"], read=same_bytes.__getitem__
    )


def test_file_order_does_not_matter_to_the_caller() -> None:
    """code_files() sorts, so two orderings of the same set must agree once sorted."""
    blobs = {"core/a.py": b"x", "core/b.py": b"y"}
    assert code_hash(files=sorted(blobs), read=blobs.__getitem__) == code_hash(
        files=sorted(reversed(list(blobs))), read=blobs.__getitem__
    )


def test_unreadable_source_yields_empty_string() -> None:
    def unreadable(rel: str) -> bytes:
        raise PermissionError(rel)

    assert code_hash(files=["core/__init__.py"], read=unreadable) == ""


def test_unwalkable_source_yields_empty_string(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dir os.walk cannot enter must yield "", not a valid-looking hash of fewer files."""
    (tmp_path / "__main__.py").write_bytes(b"")
    monkeypatch.setattr(unshackle.core, "PKG", tmp_path)
    assert code_hash() == ""
