from __future__ import annotations

from unshackle.core.utilities import declared_kwargs

SENT = {"challenge": b"c", "title": "t", "track": "tr", "pssh": "P", "session_id": "S"}


def test_keeps_only_declared_arguments() -> None:
    def licence(*, challenge: bytes, title: str, track: str) -> None: ...

    assert sorted(declared_kwargs(licence, SENT)) == ["challenge", "title", "track"]


def test_passes_session_id_to_a_function_that_asks_for_it() -> None:
    def licence(*, challenge: bytes, title: str, track: str, session_id: str | None = None) -> None: ...

    assert sorted(declared_kwargs(licence, SENT)) == ["challenge", "session_id", "title", "track"]


def test_a_var_keyword_function_gets_everything() -> None:
    def licence(*, challenge: bytes, **kwargs: object) -> None: ...

    assert declared_kwargs(licence, SENT) == SENT


def test_an_unreadable_signature_gets_everything() -> None:
    # min() has no readable signature, and a compiled service may not either
    assert declared_kwargs(min, SENT) == SENT


def test_a_signature_we_cannot_call_by_keyword_gets_everything() -> None:
    def licence(challenge, /, **kwargs: object) -> None: ...

    assert declared_kwargs(licence, SENT) == SENT
