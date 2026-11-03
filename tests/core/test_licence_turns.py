"""Licences take turns across DRMs with different KIDs, so one licence that carries every key is sent once."""

from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable, Optional
from uuid import UUID

import pytest
from pywidevine.pssh import PSSH
from rich.table import Table
from rich.tree import Tree

from tests.core.test_all_drm import playready
from unshackle.commands.dl import dl
from unshackle.core.cdm.detect import cdm_type_stub
from unshackle.core.constants import DOWNLOAD_CANCELLED, DownloadCancelled
from unshackle.core.drm import ClearKeyCENC, PlayReady, Widevine

pytestmark = pytest.mark.unit

KID_A, KID_B, KID_C = (UUID(c * 32) for c in "abc")
KEYS = {KID_A: "aa" * 16, KID_B: "bb" * 16, KID_C: "cc" * 16}


class NoVaults:
    sources: dict = {}

    def __bool__(self) -> bool:
        return False

    def get_key(self, kid: UUID) -> tuple[None, None]:
        return None, None


@pytest.fixture
def licences(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Each licence returns every content key of the title, after a delay that lets the tracks overlap."""
    calls: list[str] = []

    def get_content_keys(self: Widevine, **_: Any) -> None:
        calls.append(threading.current_thread().name)
        time.sleep(0.2)
        self.content_keys = dict(KEYS)

    monkeypatch.setattr(Widevine, "get_content_keys", get_content_keys)
    monkeypatch.setattr(dl, "LICENSE_KEY_CACHE", {})
    monkeypatch.setattr(dl, "DRM_LOCKS", {})
    DOWNLOAD_CANCELLED.clear()
    yield calls
    DOWNLOAD_CANCELLED.clear()


@pytest.fixture
def cmd() -> dl:
    cmd = dl.__new__(dl)
    cmd.log = logging.getLogger(__name__)
    cmd.service = "TEST"
    cmd.vaults = NoVaults()
    cmd.vault_cache_tally = None
    cmd._remote_service = None
    cmd.debug_logger = None
    cmd.cdm = cdm_type_stub("widevine")
    return cmd


def widevine(*kids: UUID) -> Widevine:
    return Widevine(pssh=PSSH.new(system_id=PSSH.SystemId.Widevine, key_ids=list(kids)))


def prepare(cmd: dl, drm: Widevine, track_kid: UUID, table: Table, **kwargs: Any) -> None:
    cmd.prepare_drm(
        drm,
        track=SimpleNamespace(drm=[drm]),
        title=None,
        certificate=lambda **_: None,
        licence=lambda **_: None,
        table=table,
        track_kid=track_kid,
        **kwargs,
    )


def new_table() -> Table:
    table = Table.grid()
    table.add_row("")
    return table


def run_together(cmd: dl, *jobs: tuple[Widevine, UUID]) -> tuple[Table, list[BaseException]]:
    table = new_table()
    errors: list[BaseException] = []

    def run(drm: Widevine, kid: UUID) -> None:
        try:
            prepare(cmd, drm, kid, table)
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


def test_one_licence_for_two_pssh_with_different_kids(cmd: dl, licences: list[str]) -> None:
    first, second = widevine(KID_A, KID_B), widevine(KID_C)

    table, errors = run_together(cmd, (first, KID_A), (second, KID_C))

    assert not errors
    assert len(licences) == 1
    assert first.content_keys[KID_A] == KEYS[KID_A] and second.content_keys[KID_C] == KEYS[KID_C]
    assert any(label.endswith(" from cache") for kid in (KID_A, KID_C) for label in tree_labels(table, kid))


def test_a_track_waiting_for_its_turn_sends_no_licence_after_a_failure(cmd: dl, licences: list[str]) -> None:
    DOWNLOAD_CANCELLED.set()

    with pytest.raises(DownloadCancelled):
        prepare(cmd, widevine(KID_C), KID_C, new_table())

    assert licences == []


def test_force_licenses_again_although_the_keys_are_cached(cmd: dl, licences: list[str]) -> None:
    dl.LICENSE_KEY_CACHE.update(KEYS)

    prepare(cmd, widevine(KID_C), KID_C, new_table(), force=True)

    assert len(licences) == 1


def test_a_key_label_is_added_once_with_its_marker_and_source() -> None:
    tree = Tree("Widevine")

    dl.add_key_label(tree, KID_A, KEYS[KID_A], KID_A, "from cache")
    dl.add_key_label(tree, KID_A, KEYS[KID_A])
    dl.add_key_label(tree, KID_B, KEYS[KID_B], KID_A)

    assert [str(child.label) for child in tree.children] == [
        f"[text2]{KID_A.hex}:{KEYS[KID_A]}* from cache",
        f"[text2]{KID_B.hex}:{KEYS[KID_B]}",
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
    cmd: dl, monkeypatch: pytest.MonkeyPatch, drm_class: type, build: Callable[[], Any], cdm: Optional[str]
) -> None:
    def get_content_keys(self: Any, **_: Any) -> None:
        self.content_keys.update({KID_A: KEYS[KID_A], KID_C: "0" * 32})

    monkeypatch.setattr(drm_class, "get_content_keys", get_content_keys)
    monkeypatch.setattr(dl, "LICENSE_KEY_CACHE", {})
    monkeypatch.setattr(dl, "DRM_LOCKS", {})
    drm = build()
    if cdm:
        cmd.cdm = cdm_type_stub(cdm)

    prepare(cmd, drm, KID_A, new_table())

    assert drm.content_keys == {KID_A: KEYS[KID_A]}
    assert dl.LICENSE_KEY_CACHE == {KID_A: KEYS[KID_A]}


def test_vaults_only_stops_at_the_first_kid_no_vault_holds(cmd: dl, licences: list[str]) -> None:
    asked: list[UUID] = []

    class EmptyVaults(NoVaults):
        def get_key(self, kid: UUID) -> tuple[None, None]:
            asked.append(kid)
            return None, None

    cmd.vaults = EmptyVaults()

    with pytest.raises(Widevine.Exceptions.CEKNotFound, match=KID_A.hex):
        prepare(cmd, widevine(KID_A, KID_B), KID_A, new_table(), vaults_only=True)

    assert asked == [KID_A]
    assert licences == []
