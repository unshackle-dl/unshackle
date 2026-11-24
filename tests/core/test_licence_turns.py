"""Licences take turns across DRMs with different KIDs, so one licence that carries every key is sent once."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any, Callable, Optional
from uuid import UUID

import click
import pytest
from pywidevine.pssh import PSSH
from rich.table import Table
from rich.tree import Tree

from tests.core.key_resolver import NoVaults, key_table, resolver
from tests.core.test_all_drm import playready
from unshackle.core.cdm.detect import cdm_type_stub
from unshackle.core.constants import DOWNLOAD_CANCELLED, DownloadCancelled
from unshackle.core.drm import ClearKeyCENC, PlayReady, Widevine
from unshackle.core.drm.key_resolution import ContentKeyResolver

pytestmark = pytest.mark.unit

KID_A, KID_B, KID_C = (UUID(c * 32) for c in "abc")
KEYS = {KID_A: "aa" * 16, KID_B: "bb" * 16, KID_C: "cc" * 16}
DEADLINE = 10


class Licences(list):
    """The thread name of each licence. A licence returns every content key of the title."""

    def __init__(self) -> None:
        super().__init__()
        self.second_track_looked_up = threading.Event()


@pytest.fixture
def licences(monkeypatch: pytest.MonkeyPatch) -> Licences:
    """The first licence returns only once the other track has asked the vaults, so the tracks overlap."""
    calls = Licences()

    def get_content_keys(self: Widevine, **_: Any) -> None:
        calls.append(threading.current_thread().name)
        if threading.current_thread().name == "track-0":
            calls.second_track_looked_up.wait(DEADLINE)
        self.content_keys = dict(KEYS)

    monkeypatch.setattr(Widevine, "get_content_keys", get_content_keys)
    DOWNLOAD_CANCELLED.clear()
    yield calls
    DOWNLOAD_CANCELLED.clear()


class TrackVaults(NoVaults):
    """No key vault, but a lookup by the second track sets ``looked_up``."""

    def __init__(self, looked_up: threading.Event) -> None:
        self.looked_up = looked_up

    def get_key(self, kid: UUID) -> tuple[None, None]:
        if threading.current_thread().name == "track-1":
            self.looked_up.set()
        return None, None


def widevine(*kids: UUID) -> Widevine:
    return Widevine(pssh=PSSH.new(system_id=PSSH.SystemId.Widevine, key_ids=list(kids)))


def prepare(keys: ContentKeyResolver, drm: Any, track_kid: Optional[UUID], table: Table, all_drm: bool = False) -> None:
    keys.licence_for(SimpleNamespace(drm=[drm]), None, table, all_drm)(drm, track_kid=track_kid)


def run_together(keys: ContentKeyResolver, *jobs: tuple[Widevine, UUID]) -> tuple[Table, list[BaseException]]:
    table = key_table()
    errors: list[BaseException] = []

    def run(drm: Widevine, kid: UUID) -> None:
        try:
            prepare(keys, drm, kid, table)
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=run, args=job, name=f"track-{i}") for i, job in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return table, errors


def tree_labels(table: Table, kid: UUID) -> list[str]:
    trees = [cell for cell in table.columns[0].cells if isinstance(cell, Tree)]
    return [str(child.label) for tree in trees for child in tree.children if kid.hex in str(child.label)]


def test_one_licence_for_two_pssh_with_different_kids(licences: Licences) -> None:
    keys = resolver(TrackVaults(licences.second_track_looked_up))
    first, second = widevine(KID_A, KID_B), widevine(KID_C)

    table, errors = run_together(keys, (first, KID_A), (second, KID_C))

    assert not errors
    assert len(licences) == 1
    assert first.content_keys[KID_A] == KEYS[KID_A] and second.content_keys[KID_C] == KEYS[KID_C]
    assert any(label.endswith(" from cache") for kid in (KID_A, KID_C) for label in tree_labels(table, kid))


def test_a_track_waiting_for_its_turn_sends_no_licence_after_a_failure(licences: Licences) -> None:
    DOWNLOAD_CANCELLED.set()

    with pytest.raises(DownloadCancelled):
        prepare(resolver(), widevine(KID_C), KID_C, key_table())

    assert licences == []


def test_force_licenses_again_although_the_keys_are_cached(licences: Licences) -> None:
    keys = resolver(cdm_only=True)
    ContentKeyResolver.LICENSE_KEY_CACHE.update(KEYS)

    prepare(keys, widevine(KID_C), KID_C, key_table(), all_drm=True)

    assert len(licences) == 1


def test_a_key_label_is_added_once_with_its_marker_and_source(licences: Licences) -> None:
    keys = resolver()
    ContentKeyResolver.LICENSE_KEY_CACHE[KID_A] = KEYS[KID_A]
    table = key_table()

    prepare(keys, widevine(KID_A, KID_B), KID_B, table)

    tree = list(table.columns[0].cells)[-1]
    assert [str(child.label) for child in tree.children] == [
        f"[text2]{KID_A.hex}:{KEYS[KID_A]} from cache",
        f"[text2]{KID_B.hex}:{KEYS[KID_B]}*",
        f"[text2]{KID_C.hex}:{KEYS[KID_C]}",
    ]


@pytest.mark.parametrize(
    "drm_class, build, cdm",
    [
        (Widevine, lambda: widevine(KID_A), "widevine"),
        (PlayReady, lambda: playready(KID_A), "playready"),
        (ClearKeyCENC, lambda: ClearKeyCENC(kids=[KID_A]), None),
    ],
    ids=["widevine", "playready", "clearkey"],
)
def test_an_all_zero_licence_key_is_dropped(
    monkeypatch: pytest.MonkeyPatch, drm_class: type, build: Callable[[], Any], cdm: Optional[str]
) -> None:
    def get_content_keys(self: Any, **_: Any) -> None:
        self.content_keys.update({KID_A: KEYS[KID_A], KID_C: "0" * 32})

    monkeypatch.setattr(drm_class, "get_content_keys", get_content_keys)
    keys = resolver(cdm=cdm_type_stub(cdm or "widevine"))
    drm = build()

    prepare(keys, drm, KID_A, key_table())

    assert drm.content_keys == {KID_A: KEYS[KID_A]}
    assert ContentKeyResolver.LICENSE_KEY_CACHE == {KID_A: KEYS[KID_A]}


def test_vaults_only_stops_at_the_first_kid_no_vault_holds(licences: Licences) -> None:
    asked: list[UUID] = []

    class EmptyVaults(NoVaults):
        def get_key(self, kid: UUID) -> tuple[None, None]:
            asked.append(kid)
            return None, None

    with pytest.raises(Widevine.Exceptions.CEKNotFound, match=KID_A.hex):
        prepare(resolver(EmptyVaults(), vaults_only=True), widevine(KID_A, KID_B), KID_A, key_table())

    assert asked == [KID_A]
    assert licences == []


def refuse_licence(self: Widevine, **_: Any) -> None:
    raise click.ClickException("Licence request failed")


def test_a_refused_licence_without_the_track_key_reports_the_licence_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(Widevine, "get_content_keys", refuse_licence)
    keys = resolver()
    ContentKeyResolver.LICENSE_KEY_CACHE[KID_B] = KEYS[KID_B]
    table = key_table()

    with pytest.raises(click.ClickException, match="^Licence request failed$"):
        prepare(keys, widevine(KID_A, KID_B), KID_A, table)

    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    tree = list(table.columns[0].cells)[-1]
    assert "Licence request failed" in str(tree.children[-1].label)


def test_a_held_track_key_needs_no_licence(licences: Licences) -> None:
    keys = resolver()
    ContentKeyResolver.LICENSE_KEY_CACHE[KID_A] = KEYS[KID_A]
    drm = widevine(KID_A, KID_B)

    prepare(keys, drm, KID_A, key_table())

    assert licences == []
    assert drm.content_keys == {KID_A: KEYS[KID_A]}


def test_force_licenses_a_held_track_key_again(licences: Licences) -> None:
    keys = resolver(cdm_only=True)
    ContentKeyResolver.LICENSE_KEY_CACHE[KID_A] = KEYS[KID_A]

    prepare(keys, widevine(KID_A, KID_B), KID_A, key_table(), all_drm=True)

    assert len(licences) == 1


def test_a_refused_licence_keeps_the_known_keys_when_the_track_kid_is_unknown(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(Widevine, "get_content_keys", refuse_licence)
    keys = resolver()
    ContentKeyResolver.LICENSE_KEY_CACHE[KID_A] = KEYS[KID_A]
    drm = widevine(KID_A, KID_B)

    prepare(keys, drm, None, key_table())

    assert drm.content_keys == {KID_A: KEYS[KID_A]}
    assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == [
        "Licence failed, using the 1 known key(s): Licence request failed"
    ]
