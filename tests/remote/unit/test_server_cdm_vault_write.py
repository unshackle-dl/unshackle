"""Keys the server CDM returns must land in the local vaults.

The server-CDM branch of prepare_drm reads the local vaults but used to return before
the vault writes, so a remote run never kept the keys it paid for. Keys that came from a
vault in the first place must not be pushed back.
"""

import threading
import time
from types import SimpleNamespace
from uuid import UUID

import pytest
from rich.table import Table

from unshackle.commands.dl import dl

pytestmark = pytest.mark.unit

NEW = UUID("161219ec3df64e0eadbb3827c0ccc46d")
CACHED = UUID("75982fac2cd8412a990a59c0ef2d4bb1")


class FakeVaults:
    def __init__(self, held=None, vault_used=None):
        self.held = held or {}
        self.vault_used = vault_used
        self.added: dict = {}
        self.pushes: list = []
        self.replicated: list = []
        self.sources: dict = {}

    def __len__(self):
        return 1

    def get_key(self, kid):
        return self.held.get(kid), self.vault_used

    def add_keys(self, kid_keys):
        self.added.update(kid_keys)
        self.pushes.append(dict(kid_keys))
        return 1

    def add_key(self, kid, key, excluding=None):
        self.replicated.append((kid, key, excluding))
        return 1


class FakeWidevine:
    def __init__(self, kids):
        self.kids = list(kids)
        self.content_keys: dict = {}


def make_cmd(vaults):
    cmd = dl.__new__(dl)
    cmd.log = SimpleNamespace(warning=print, info=print, debug=print)
    cmd.service = "TEST"
    cmd.vaults = vaults
    cmd.vault_cache_tally = None
    cmd._remote_service = SimpleNamespace(_server_cdm=True, _server_cdm_type="widevine")
    dl.LICENSE_KEY_CACHE.clear()
    dl.DRM_LOCKS.clear()
    return cmd


def prepare(cmd, drm, licence, track_kid, **kwargs):
    table = Table.grid()
    table.add_row("")
    cmd.prepare_drm(
        track=SimpleNamespace(drm=[drm]),
        title=None,
        drm=drm,
        licence=licence,
        certificate=None,
        track_kid=track_kid,
        table=table,
        **kwargs,
    )


def run_prepare(vaults, kids, licence_keys, track_kid):
    cmd = make_cmd(vaults)
    drm = FakeWidevine(kids)

    def licence(**kwargs):
        drm.content_keys.update(licence_keys)

    prepare(cmd, drm, licence, track_kid)
    cmd.wait_vault_writes()
    return drm


def test_server_cdm_keys_are_cached_to_local_vaults():
    vaults = FakeVaults()
    run_prepare(vaults, [NEW], {NEW: "k_new"}, track_kid=NEW)
    assert vaults.added == {NEW: "k_new"}


def test_keys_read_from_a_vault_are_not_pushed_back():
    vaults = FakeVaults({CACHED: "k_cached"})
    run_prepare(vaults, [CACHED, NEW], {NEW: "k_new"}, track_kid=NEW)
    assert vaults.added == {NEW: "k_new"}


def test_batch_resolved_keys_are_cached_before_download():
    """The batch licence fills track.drm before prepare_drm, so dl caches it at the call site."""
    vaults = FakeVaults()
    cmd = dl.__new__(dl)
    cmd.log = SimpleNamespace(warning=print, info=print, debug=print)
    cmd.service = "TEST"
    cmd.vaults = vaults
    cmd.vault_cache_tally = None
    dl.LICENSE_KEY_CACHE.clear()

    drm = FakeWidevine([NEW])
    drm.content_keys = {NEW: "k_new"}
    title = SimpleNamespace(tracks=[SimpleNamespace(drm=[drm]), SimpleNamespace(drm=None)])

    cmd.cache_resolved_keys(title)
    cmd.wait_vault_writes()

    assert vaults.added == {NEW: "k_new"}


def test_vault_hits_wait_for_the_decrypt_before_replicating():
    """A vault hit reaches the other vaults only after decrypt_verified proves it, never from prepare_drm."""
    vaults = FakeVaults({CACHED: "k_cached"}, vault_used="vault-a")
    run_prepare(vaults, [CACHED], {}, track_kid=CACHED)
    assert vaults.replicated == []
    assert vaults.added == {}


def test_server_vault_keys_are_held_back_until_verified():
    """A key the server took from its vault is sourced like a local vault hit and not pushed yet."""
    from unshackle.core.remote_service import ServerVault

    vaults = FakeVaults()
    cmd = make_cmd(vaults)
    remote = cmd._remote_service
    remote.server_vault_keys = {CACHED: "k_cached"}
    stub = ServerVault(remote)
    remote.server_vault = stub

    drm = FakeWidevine([CACHED, NEW])

    def licence(**kwargs):
        drm.content_keys.update({CACHED: "k_cached", NEW: "k_new"})

    prepare(cmd, drm, licence, NEW)
    cmd.wait_vault_writes()

    assert vaults.added == {NEW: "k_new"}
    assert vaults.sources == {CACHED: ("k_cached", stub)}

    # the batch path caches at its own call site and must hold the same key back
    drm2 = FakeWidevine([CACHED])
    drm2.content_keys = {CACHED: "k_cached"}
    dl.LICENSE_KEY_CACHE.clear()
    cmd.cache_resolved_keys(SimpleNamespace(tracks=[SimpleNamespace(drm=[drm2])]))
    cmd.wait_vault_writes()
    assert vaults.added == {NEW: "k_new"}


def test_tracks_sharing_one_drm_licence_and_push_once():
    """Two tracks on the same DRM run at the same time; the second must find the first one's keys."""
    vaults = FakeVaults()
    cmd = make_cmd(vaults)
    drm = FakeWidevine([NEW])
    calls = []

    def licence(**kwargs):
        calls.append(1)
        time.sleep(0.2)
        drm.content_keys.update({NEW: "k_new"})

    threads = [threading.Thread(target=prepare, args=(cmd, drm, licence, NEW)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    cmd.wait_vault_writes()

    assert len(calls) == 1
    assert vaults.pushes == [{NEW: "k_new"}]


def test_cdm_only_still_reuses_keys_from_the_run_cache():
    """--cdm-only skips the vaults, not LICENSE_KEY_CACHE, so a second track with the same KID does not licence twice."""
    vaults = FakeVaults()
    cmd = make_cmd(vaults)
    calls = []

    for _ in range(2):
        drm = FakeWidevine([NEW])

        def licence(**kwargs):
            calls.append(1)
            drm.content_keys.update({NEW: "k_new"})

        prepare(cmd, drm, licence, NEW, cdm_only=True)
    cmd.wait_vault_writes()

    assert len(calls) == 1
    assert vaults.pushes == [{NEW: "k_new"}]


def test_cdm_only_trusts_server_vault_keys():
    """--cdm-only skips the vault checks, so a server vault key is not sourced for a decode check."""
    from unshackle.core.remote_service import ServerVault

    vaults = FakeVaults()
    cmd = make_cmd(vaults)
    remote = cmd._remote_service
    remote.server_vault_keys = {CACHED: "k_cached"}
    remote.server_vault = ServerVault(remote)

    drm = FakeWidevine([CACHED])
    prepare(cmd, drm, lambda **kw: drm.content_keys.update({CACHED: "k_cached"}), CACHED, cdm_only=True)
    cmd.wait_vault_writes()

    assert vaults.sources == {}
    assert dl.LICENSE_KEY_CACHE == {CACHED: "k_cached"}
