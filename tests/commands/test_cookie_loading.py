"""Cookie files that use spaces instead of tabs must still load."""

from http.cookiejar import CookieJar
from pathlib import Path

import pytest

from unshackle.commands.dl import dl

HEADER = "# Netscape HTTP Cookie File\n"


def _names(jar) -> dict:
    return {c.name: c.value for c in jar}


def test_space_separated_cookie_file(tmp_path: Path) -> None:
    file = tmp_path / "cookies.txt"
    file.write_text(HEADER + ".example.com TRUE / TRUE 1234567890 sid abc123\n", "utf8")

    assert _names(dl.load_cookie_file(file)) == {"sid": "abc123"}


def test_tab_separated_value_keeps_spaces(tmp_path: Path) -> None:
    file = tmp_path / "cookies.txt"
    file.write_text(
        HEADER + "\t".join([".example.com", "TRUE", "/", "TRUE", "1234567890", "sid", "a b c"]) + "\n", "utf8"
    )

    assert _names(dl.load_cookie_file(file)) == {"sid": "a b c"}


def test_space_separated_empty_value(tmp_path: Path) -> None:
    file = tmp_path / "cookies.txt"
    file.write_text(HEADER + ".example.com TRUE / TRUE 1234567890 sid \n", "utf8")

    assert _names(dl.load_cookie_file(file)) == {"sid": ""}


def test_space_separated_httponly(tmp_path: Path) -> None:
    file = tmp_path / "cookies.txt"
    file.write_text(HEADER + "#HttpOnly_.example.com TRUE / TRUE 1234567890 sid abc123\n", "utf8")

    assert _names(dl.load_cookie_file(file)) == {"sid": "abc123"}


def test_mixed_separators_in_one_file(tmp_path: Path) -> None:
    file = tmp_path / "cookies.txt"
    file.write_text(
        HEADER
        + "\t".join([".a.com", "TRUE", "/", "TRUE", "1234567890", "tabbed", "1"])
        + "\n.b.com TRUE / TRUE 1234567890 spaced 2\n",
        "utf8",
    )

    assert _names(dl.load_cookie_file(file)) == {"tabbed": "1", "spaced": "2"}


def test_malformed_row_is_reported(tmp_path: Path) -> None:
    file = tmp_path / "cookies.txt"
    file.write_text(HEADER + ".a.com\tTRUE\t/\tTRUE\t1\tsid\tv\nnot a cookie\n", "utf8")

    with pytest.raises(ValueError, match="line 3"):
        dl.load_cookie_file(file)


def test_missing_header_is_added(tmp_path: Path) -> None:
    file = tmp_path / "cookies.txt"
    file.write_text(".example.com\tTRUE\t/\tTRUE\t1234567890\tsid\tabc123\n", "utf8")

    assert _names(dl.load_cookie_file(file)) == {"sid": "abc123"}


def test_utf8_bom_is_ignored(tmp_path: Path) -> None:
    file = tmp_path / "cookies.txt"
    file.write_bytes(b"\xef\xbb\xbf" + (HEADER + ".example.com\tTRUE\t/\tTRUE\t1234567890\tsid\tabc123\n").encode())

    assert _names(dl.load_cookie_file(file)) == {"sid": "abc123"}


def test_json_export_gives_clear_error(tmp_path: Path) -> None:
    file = tmp_path / "cookies.txt"
    file.write_text('[{"domain": ".example.com", "name": "sid", "value": "abc123"}]\n', "utf8")

    with pytest.raises(ValueError, match="JSON export"):
        dl.load_cookie_file(file)


def test_leading_blank_line_is_not_json(tmp_path: Path) -> None:
    file = tmp_path / "cookies.txt"
    file.write_text("\n" + HEADER + ".example.com TRUE / TRUE 1234567890 sid abc123\n", "utf8")

    assert _names(dl.load_cookie_file(file)) == {"sid": "abc123"}


def test_dot_flag_mismatch_does_not_abort_file(tmp_path: Path) -> None:
    file = tmp_path / "cookies.txt"
    file.write_text(HEADER + "example.com\tTRUE\t/\tTRUE\t1\tsid\tv\n", "utf8")

    assert _names(dl.load_cookie_file(file)) == {"sid": "v"}


def test_the_users_file_is_never_rewritten(tmp_path: Path) -> None:
    file = tmp_path / "cookies.txt"
    original = ".example.com TRUE / TRUE 1234567890 sid abc123\n"
    file.write_text(original, "utf8")

    dl.load_cookie_file(file)

    assert file.read_text("utf8") == original


def test_save_cookies_onto_an_unrepaired_file(tmp_path: Path) -> None:
    file = tmp_path / "cookies.txt"
    file.write_text(".example.com TRUE / TRUE 1234567890 sid abc123\n", "utf8")

    dl.save_cookies(file, CookieJar())

    assert _names(dl.load_cookie_file(file)) == {"sid": "abc123"}


def test_tabbed_row_is_never_resplit_on_spaces(tmp_path: Path) -> None:
    file = tmp_path / "cookies.txt"
    file.write_text(HEADER + ".a.com\tTRUE\t/\tFALSE\thello world\n", "utf8")

    with pytest.raises(ValueError, match="not a Netscape cookie row"):
        dl.load_cookie_file(file)


def test_file_without_cookies_is_reported(tmp_path: Path) -> None:
    file = tmp_path / "cookies.txt"
    file.write_text(HEADER + "# nothing else here\n", "utf8")

    with pytest.raises(ValueError, match="holds no cookies"):
        dl.load_cookie_file(file)


def test_round_trip_keeps_httponly_and_blank_expiry(tmp_path: Path) -> None:
    file = tmp_path / "cookies.txt"
    file.write_text(HEADER + "#HttpOnly_.example.com TRUE / TRUE 1234567890 sid abc123\n", "utf8")

    dl.save_cookies(file, CookieJar())

    assert _names(dl.load_cookie_file(file)) == {"sid": "abc123"}
    assert "#HttpOnly_" in file.read_text("utf8")
