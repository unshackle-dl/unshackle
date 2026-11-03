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

from tests.remote.key_source import CAPPED, PR_DEVICE, one_title, remote_keys, remote_session
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

    def held_locally(self, kid, key):
        return False

    def get_key(self, kid):
        return self.held.get(kid), self.vault_used

    def is_flagged(self, kid, key):
        return False

    def clear_licensed_flags(self, content_keys):
        return []

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
    cmd.log = SimpleNamespace(warning=print, info=print, debug=print, error=print)
    cmd.service = "TEST"
    cmd.vaults = vaults
    cmd.vault_cache_tally = None
    cmd.debug_logger = None
    cmd._remote_service = remote_keys()
    dl.LICENSE_KEY_CACHE.clear()
    dl.DRM_LOCKS.clear()
    return cmd


def prepare(cmd, drm, licence, track_kid, **kwargs):
    table = Table.grid()
    table.add_row("")
    cmd.prepare_drm(
        track=SimpleNamespace(id="vid", drm=[drm]),
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


def capped_run(vaults):
    """A batch licence refuses the track for a local PlayReady device, then dl prepares its Widevine DRM."""
    cmd = make_cmd(vaults)
    svc = remote_session(PR_DEVICE, CAPPED)
    cmd._remote_service = svc

    drm = type("Widevine", (FakeWidevine,), {})([CACHED])
    track = SimpleNamespace(id="vid", height=2160, drm=[drm], drm_preference=None)
    svc.resolve_server_keys(one_title(track))

    table = Table.grid()
    table.add_row("")
    cmd.prepare_drm(
        track=track,
        title=None,
        drm=drm,
        licence=lambda **kwargs: svc.proxy_license(b"", track, "widevine"),
        certificate=None,
        track_kid=CACHED,
        table=table,
    )
    return drm


def test_a_capped_track_takes_a_local_vault_key_before_the_refusal():
    assert capped_run(FakeVaults({CACHED: "k_cached"})).content_keys == {CACHED: "k_cached"}


def test_a_capped_track_no_vault_holds_raises_the_refusal():
    import click

    with pytest.raises(click.ClickException, match="only Widevine DRM for your PlayReady device"):
        capped_run(FakeVaults())


def test_batch_resolved_keys_are_cached_before_download():
    """The batch licence fills track.drm before prepare_drm, so dl caches it at the call site."""
    vaults = FakeVaults()
    cmd = dl.__new__(dl)
    cmd.log = SimpleNamespace(warning=print, info=print, debug=print, error=print)
    cmd.service = "TEST"
    cmd.vaults = vaults
    cmd.vault_cache_tally = None
    cmd.debug_logger = None
    dl.LICENSE_KEY_CACHE.clear()

    drm = FakeWidevine([NEW])
    drm.content_keys = {NEW: "k_new"}
    title = SimpleNamespace(tracks=[SimpleNamespace(drm=[drm]), SimpleNamespace(drm=None)])

    cmd.cache_resolved_keys(remote_keys(), title)
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
    cmd.cache_resolved_keys(remote, SimpleNamespace(tracks=[SimpleNamespace(drm=[drm2])]))
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


@pytest.mark.parametrize("system", ["widevine", "playready"])
def test_no_cdm_and_no_server_cdm_names_the_missing_cdm(system):
    """The server does not license this service for the API key, and the client has no CDM for it."""
    import click

    from unshackle.core.drm import PlayReady, Widevine

    cmd = make_cmd(FakeVaults())
    cmd._remote_service = remote_keys(server_cdm=False)
    cmd.cdm = None
    cmd.cdm_for = lambda system, quality=None: None
    drm_class = PlayReady if system == "playready" else Widevine
    drm = drm_class.__new__(drm_class)
    drm.content_keys = {}
    with pytest.raises(click.ClickException, match="No CDM configured for TEST"):
        prepare(cmd, drm, lambda **kwargs: None, NEW)


NULL = UUID("0b3e2d1c4a5f46e7b8c9d0e1f2a3b4c5")


def test_a_null_key_from_the_server_licence_is_dropped():
    vaults = FakeVaults()
    drm = run_prepare(vaults, [NEW], {NEW: "k_new", NULL: "0" * 32}, track_kid=NEW)
    assert drm.content_keys == {NEW: "k_new"}
    assert vaults.added == {NEW: "k_new"}
    assert NULL not in dl.LICENSE_KEY_CACHE


def test_a_null_key_from_the_batch_licence_is_dropped(monkeypatch):
    vaults = FakeVaults()
    cmd = make_cmd(vaults)
    drm = FakeWidevine([NEW])
    title = SimpleNamespace(tracks=[SimpleNamespace(drm=[drm])])
    source = cmd._remote_service
    monkeypatch.setattr(
        source, "resolve_server_keys", lambda t: drm.content_keys.update({NEW: "k_new", NULL: "0" * 32})
    )

    cmd.take_server_keys(source, title, cdm_only=False)
    cmd.wait_vault_writes()

    assert drm.content_keys == {NEW: "k_new"}
    assert vaults.added == {NEW: "k_new"}
    assert NULL not in dl.LICENSE_KEY_CACHE
