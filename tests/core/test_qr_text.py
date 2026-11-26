import segno

from unshackle.core import console
from unshackle.core.console import qr_text, show_qr

TOP = {"\u2588": True, "\u2580": True, "\u2584": False, " ": False}
BOTTOM = {"\u2588": True, "\u2580": False, "\u2584": True, " ": False}


def test_each_line_holds_two_module_rows_of_the_code() -> None:
    data = "https://login.example/scan?code=EXAMPLE"
    rows = [[bool(v) for v in row] for row in segno.make_qr(data).matrix_iter(border=2)]
    unpacked = []
    for line in qr_text(data, indent=0).plain.splitlines():
        unpacked += [[TOP[c] for c in line], [BOTTOM[c] for c in line]]
    assert unpacked[: len(rows)] == rows
    assert not any(cell for row in unpacked[len(rows) :] for cell in row)


def test_short_text_is_a_full_qr_code_not_a_micro_one() -> None:
    assert len(qr_text("12345", indent=0).plain.splitlines()) * 2 >= 21 + 4


def test_text_longer_than_a_qr_code_holds_draws_nothing(monkeypatch) -> None:
    printed: list = []
    monkeypatch.setattr(console.console, "print", lambda *a, **k: printed.extend(a))
    show_qr("\u20ac" * 1500)
    assert printed == []
