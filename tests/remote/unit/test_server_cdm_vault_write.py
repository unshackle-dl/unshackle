"""Keys the server CDM returns must land in the local vaults.

The server-CDM branch of content-key resolution reads the local vaults but used to return before
the vault writes, so a remote run never kept the keys it paid for. Keys that came from a
vault in the first place must not be pushed back.
"""

import threading
from types import SimpleNamespace
from uuid import UUID

import pytest

from tests.core.key_resolver import key_table, resolver
from tests.remote.key_source import CAPPED, PR_DEVICE, one_title, remote_keys, remote_session
from unshackle.core.drm.key_resolution import ContentKeyResolver

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


def make_resolver(vaults, licence=None, source=None, **flags):
    return resolver(vaults, licence=licence, source=source or remote_keys(), **flags)


def prepare(keys, drm, track_kid, track=None):
    track = track or SimpleNamespace(id="vid", drm=[drm])
    keys.licence_for(track, None, key_table())(drm, track_kid=track_kid)


def run_prepare(vaults, kids, licence_keys, track_kid):
    drm = FakeWidevine(kids)
    keys = make_resolver(vaults, licence=lambda **kwargs: drm.content_keys.update(licence_keys))
    prepare(keys, drm, track_kid)
    keys.wait_vault_writes()
    return drm


def batch(keys, source, title):
    """Run the batch step for ``title`` with ``source``, whose keys are already on the tracks."""
    source.resolve_server_keys = lambda title: None
    keys.take_server_keys(title)
    keys.wait_vault_writes()


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
    svc = remote_session(PR_DEVICE, CAPPED)
    drm = type("Widevine", (FakeWidevine,), {})([CACHED])
    track = SimpleNamespace(id="vid", height=2160, drm=[drm], drm_preference=None)
    keys = make_resolver(vaults, licence=lambda **kwargs: svc.proxy_license(b"", track, "widevine"), source=svc)
    svc.resolve_server_keys(one_title(track))

    prepare(keys, drm, CACHED, track)
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
    source = remote_keys()
    keys = make_resolver(vaults, source=source)

    drm = FakeWidevine([NEW])
    drm.content_keys = {NEW: "k_new"}
    title = SimpleNamespace(tracks=[SimpleNamespace(drm=[drm]), SimpleNamespace(drm=None)])

    batch(keys, source, title)

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
    remote = remote_keys()
    remote.server_vault_keys = {CACHED: "k_cached"}
    stub = ServerVault(remote)
    remote.server_vault = stub

    drm = FakeWidevine([CACHED, NEW])
    keys = make_resolver(
        vaults, licence=lambda **kwargs: drm.content_keys.update({CACHED: "k_cached", NEW: "k_new"}), source=remote
    )

    prepare(keys, drm, NEW)
    keys.wait_vault_writes()

    assert vaults.added == {NEW: "k_new"}
    assert vaults.sources == {CACHED: ("k_cached", stub)}

    # the batch path caches at its own call site and must hold the same key back
    drm2 = FakeWidevine([CACHED])
    drm2.content_keys = {CACHED: "k_cached"}
    ContentKeyResolver.LICENSE_KEY_CACHE.clear()
    batch(keys, remote, SimpleNamespace(tracks=[SimpleNamespace(drm=[drm2])]))
    assert vaults.added == {NEW: "k_new"}


def test_tracks_sharing_one_drm_licence_and_push_once():
    """Two tracks on the same DRM run at the same time; the second must find the first one's keys."""
    vaults = FakeVaults()
    drm = FakeWidevine([NEW])
    calls = []

    def licence(**kwargs):
        calls.append(1)
        drm.content_keys.update({NEW: "k_new"})

    keys = make_resolver(vaults, licence=licence)
    both_started = threading.Barrier(2)

    def track():
        both_started.wait(10)
        prepare(keys, drm, NEW)

    threads = [threading.Thread(target=track) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    keys.wait_vault_writes()

    assert len(calls) == 1
    assert vaults.pushes == [{NEW: "k_new"}]


def test_cdm_only_still_reuses_keys_from_the_run_cache():
    """--cdm-only skips the vaults, not LICENSE_KEY_CACHE, so a second track with the same KID does not licence twice."""
    vaults = FakeVaults()
    calls = []
    drms = [FakeWidevine([NEW]) for _ in range(2)]

    def licence(**kwargs):
        calls.append(1)
        drms[len(calls) - 1].content_keys.update({NEW: "k_new"})

    keys = make_resolver(vaults, licence=licence, cdm_only=True)
    for drm in drms:
        prepare(keys, drm, NEW)
    keys.wait_vault_writes()

    assert len(calls) == 1
    assert vaults.pushes == [{NEW: "k_new"}]


def test_a_forced_licence_trusts_server_vault_keys():
    """A forced licence (cdm_only=True, as --all-drm passes) does not name the server vault as a source, so no decode check."""
    from unshackle.core.remote_service import ServerVault

    vaults = FakeVaults()
    remote = remote_keys()
    remote.server_vault_keys = {CACHED: "k_cached"}
    remote.server_vault = ServerVault(remote)

    drm = FakeWidevine([CACHED])
    keys = make_resolver(
        vaults, licence=lambda **kw: drm.content_keys.update({CACHED: "k_cached"}), source=remote, cdm_only=True
    )
    prepare(keys, drm, CACHED)
    keys.wait_vault_writes()

    assert vaults.sources == {}
    assert ContentKeyResolver.LICENSE_KEY_CACHE == {CACHED: "k_cached"}


@pytest.mark.parametrize("system", ["widevine", "playready"])
def test_no_cdm_and_no_server_cdm_names_the_missing_cdm(system):
    """The server does not license this service for the API key, and the client has no CDM for it."""
    import click

    from unshackle.core.drm import PlayReady, Widevine

    keys = resolver(
        FakeVaults(), cdm=None, cdm_for=lambda system, quality=None: None, source=remote_keys(server_cdm=False)
    )
    drm_class = PlayReady if system == "playready" else Widevine
    drm = drm_class.__new__(drm_class)
    drm.content_keys = {}
    with pytest.raises(click.ClickException, match="No CDM configured for TEST"):
        prepare(keys, drm, NEW)


NULL = UUID("0b3e2d1c4a5f46e7b8c9d0e1f2a3b4c5")


def test_a_null_key_from_the_server_licence_is_dropped():
    vaults = FakeVaults()
    drm = run_prepare(vaults, [NEW], {NEW: "k_new", NULL: "0" * 32}, track_kid=NEW)
    assert drm.content_keys == {NEW: "k_new"}
    assert vaults.added == {NEW: "k_new"}
    assert NULL not in ContentKeyResolver.LICENSE_KEY_CACHE


def test_a_null_key_from_the_batch_licence_is_dropped(monkeypatch):
    vaults = FakeVaults()
    source = remote_keys()
    keys = make_resolver(vaults, source=source)
    drm = FakeWidevine([NEW])
    title = SimpleNamespace(tracks=[SimpleNamespace(drm=[drm])])
    monkeypatch.setattr(
        source, "resolve_server_keys", lambda t: drm.content_keys.update({NEW: "k_new", NULL: "0" * 32})
    )

    keys.take_server_keys(title)
    keys.wait_vault_writes()

    assert drm.content_keys == {NEW: "k_new"}
    assert vaults.added == {NEW: "k_new"}
    assert NULL not in ContentKeyResolver.LICENSE_KEY_CACHE
