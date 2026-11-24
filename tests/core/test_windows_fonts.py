"""get_windows_fonts reads the fonts installed for all users and for the current user."""

from __future__ import annotations

import sys
import types
from pathlib import Path, PureWindowsPath

import pytest

from unshackle.core import utilities

HKLM, HKCU = "HKLM", "HKCU"


class Key(list):
    def __enter__(self) -> Key:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def registry(monkeypatch, tmp_path):
    """Install a fake winreg whose hives are the returned dict, with the machine font folder in tmp_path."""
    hives: dict[str, list[tuple[str, object]]] = {}

    def open_key(hive: str, *_: object) -> Key:
        if hive not in hives:
            raise FileNotFoundError(hive)
        return Key(hives[hive])

    winreg = types.SimpleNamespace(
        HKEY_LOCAL_MACHINE=HKLM,
        HKEY_CURRENT_USER=HKCU,
        KEY_READ=0,
        OpenKey=open_key,
        QueryInfoKey=lambda key: (0, len(key), 0),
        EnumValue=lambda key, n: (*key[n], 1),
    )
    monkeypatch.setitem(sys.modules, "winreg", winreg)
    monkeypatch.setattr(sys, "platform", "win32")
    (tmp_path / "machine").mkdir()
    monkeypatch.setattr(utilities, "WINDOWS_FONTS_DIR", tmp_path / "machine")
    monkeypatch.setattr(utilities, "extract_font_family", lambda path: None)
    return hives


def font(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def test_per_user_fonts_are_found(registry, tmp_path):
    machine = font(tmp_path / "machine/arial.ttf")
    user = font(tmp_path / "user/MyFont-Bold.ttf")
    registry[HKLM] = [("Arial (TrueType)", "arial.ttf")]
    registry[HKCU] = [("My Font Bold (TrueType)", str(user)), ("Arial (TrueType)", str(user))]

    fonts = utilities.get_windows_fonts()
    assert fonts == {"Arial": machine, "My Font Bold": user}
    assert utilities.find_font_with_fallbacks("My Font", fonts) == user


def test_a_font_is_found_by_the_family_name_in_its_file(registry, tmp_path, monkeypatch):
    user = font(tmp_path / "user/My Font - Bold.ttf")
    registry[HKCU] = [("MyFont-Bold (TrueType)", str(user))]
    monkeypatch.setattr(utilities, "extract_font_family", lambda path: "My Font")

    fonts = utilities.get_windows_fonts()
    assert fonts == {"MyFont-Bold": user, "My Font": user}
    assert utilities.find_font_with_fallbacks("My Font", fonts) == user


def test_an_absolute_registry_value_replaces_the_font_folder():
    user = r"C:\Users\x\AppData\Local\Microsoft\Windows\Fonts\MyFont-Bold.ttf"
    assert PureWindowsPath(r"C:\Windows\Fonts", user) == PureWindowsPath(user)


def test_missing_files_bitmap_fonts_and_odd_values_are_skipped(registry, tmp_path):
    font(tmp_path / "machine/modern.fon")
    collection = font(tmp_path / "machine/msgothic.ttc")
    registry[HKLM] = [
        ("Modern (All res)", "modern.fon"),
        ("MS Gothic & MS UI Gothic (TrueType)", "msgothic.ttc"),
        ("Gone (TrueType)", "gone.ttf"),
        ("Binary (TrueType)", b"x.ttf"),
    ]
    registry[HKCU] = [("Deleted (TrueType)", str(tmp_path / "user/Deleted.ttf"))]
    assert utilities.get_windows_fonts() == {"MS Gothic & MS UI Gothic": collection}
