from __future__ import annotations

from importlib.machinery import EXTENSION_SUFFIXES
from pathlib import Path

from unshackle.core.services import compiled_import_hint


def _service(tmp_path: Path, *files: str) -> Path:
    (tmp_path / "EXAMPLE").mkdir()
    init = tmp_path / "EXAMPLE" / "__init__.py"
    init.write_text("from .example import EXAMPLE\n")
    for name in files:
        (init.parent / name).touch()
    return init


def test_hint_names_the_file_this_python_needs(tmp_path: Path) -> None:
    init = _service(tmp_path, "example.cp999-win_amd64.pyd", "example.cpython-999-x86_64-linux-gnu.so")
    assert f"needs example{EXTENSION_SUFFIXES[0]}" in compiled_import_hint("EXAMPLE.example", init)


def test_no_hint_when_this_python_can_load_it(tmp_path: Path) -> None:
    init = _service(tmp_path, f"example{EXTENSION_SUFFIXES[0]}")
    assert compiled_import_hint("EXAMPLE.example", init) == ""


def test_no_hint_for_a_pure_python_service(tmp_path: Path) -> None:
    init = _service(tmp_path)
    assert compiled_import_hint("EXAMPLE.example", init) == ""
