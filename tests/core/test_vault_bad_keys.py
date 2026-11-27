import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from tests.core.key_resolver import MemoryVault, key_table, resolver, vaults_of
from unshackle.core.drm import key_resolution
from unshackle.core.drm.key_resolution import ContentKeyResolver
from unshackle.core.vault import RateLimited, vault_session
from unshackle.core.vaults import Vaults
from unshackle.vaults.API import API, Exceptions
from unshackle.vaults.HTTP import HTTP
from unshackle.vaults.SQLite import SQLite

KID = UUID("11111111-2222-3333-4444-555555555555")
SIBLING = UUID("99999999-2222-3333-4444-555555555555")
BAD = "aa" * 16
GOOD = "bb" * 16


class Remote(SQLite):
    """A second SQLite file standing in for a remote vault."""

    local = False


def test_flagged_pair_is_skipped_and_named(tmp_path: Path) -> None:
    local = SQLite("local", tmp_path / "local.db")
    poisoned = Remote("poisoned", tmp_path / "poisoned.db")
    poisoned.add_key("SVC", KID, BAD)

    vaults = Vaults("SVC")
    vaults.vaults = [local, poisoned]

    assert vaults.get_key(KID) == (BAD, poisoned)
    assert vaults.source_of(KID, BAD) is poisoned
    local.add_key("SVC", KID, BAD)  # the cross-vault copy that happened before verification

    vaults.flag_bad_key(KID, BAD)

    # both the local copy and every later remote hit of that pair are gone
    assert local.get_key(KID, "SVC") is None
    assert vaults.get_key(KID) == (None, None)
    assert local.is_bad_key(KID, BAD)
    assert "bad_keys" not in list(local.get_services())
    row = local.conn_factory.get().execute("SELECT service, source FROM bad_keys").fetchone()
    assert row == ("SVC", "poisoned")

    # another vault with a different key for the same KID still answers
    other = Remote("other", tmp_path / "other.db")
    other.add_key("SVC", KID, GOOD)
    vaults.vaults.append(other)
    assert vaults.get_key(KID) == (GOOD, other)

    # a flagged row that comes back (kv copy from an old export) is invisible to the lookup
    local.conn_factory.get().execute("INSERT OR IGNORE INTO `SVC` (kid, key_) VALUES (?, ?)", (KID.hex, BAD))
    local.conn_factory.get().commit()
    assert local.get_key(KID, "SVC") is None

    vaults.unflag_bad_key(KID, BAD)
    assert not local.is_bad_key(KID, BAD)
    assert local.get_key(KID, "SVC") == BAD


class FakeDRM:
    def __init__(self, keys: dict[UUID, str]):
        self.content_keys = dict(keys)
        self.decrypted_with: list[dict[UUID, str]] = []

    def decrypt(self, path: Path) -> None:
        self.decrypted_with.append(dict(self.content_keys))
        # like the real decrypters: write a new file, then replace the input
        out = path.with_stem(path.stem + "_decrypted")
        out.write_bytes(b"plain:" + self.content_keys[KID].encode())
        path.unlink()
        out.rename(path)


def make_resolver(monkeypatch: pytest.MonkeyPatch, vaults: Vaults) -> ContentKeyResolver:
    monkeypatch.setattr(key_resolution.binaries, "FFMPEG", "ffmpeg")
    monkeypatch.setattr(
        key_resolution,
        "ffmpeg_decodes",
        lambda path, start=None, seconds=3, video=None: path.read_bytes() == b"plain:" + GOOD.encode(),
    )
    keys = resolver(vaults, tag="SVC")
    ContentKeyResolver.LICENSE_KEY_CACHE[KID] = BAD
    return keys


class BatchDRM(SimpleNamespace):
    """A DRM that a batch licence fills with ``batch``."""


SERVER = SimpleNamespace(
    resolve_server_keys=lambda title: [d.content_keys.update(d.batch) for t in title.tracks for d in t.drm],
    vault_source=lambda kid, key: None,
)


def batch_title(*batches: dict[UUID, str], kids: list[UUID] | None = None) -> SimpleNamespace:
    """A title with one track per batch, whose DRM names ``kids`` (every KID of its batch by default)."""
    return SimpleNamespace(
        tracks=[
            SimpleNamespace(drm=[BatchDRM(kids=kids or list(keys), content_keys={}, batch=keys)]) for keys in batches
        ]
    )


def test_decrypt_verified_flags_and_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local = SQLite("local", tmp_path / "local.db")
    poisoned = Remote("poisoned", tmp_path / "poisoned.db")
    poisoned.add_key("SVC", KID, BAD)
    vaults = Vaults("SVC")
    vaults.vaults = [local, poisoned]
    assert vaults.get_key(KID) == (BAD, poisoned)
    keys = make_resolver(monkeypatch, vaults)

    path = tmp_path / "video.mp4"
    path.write_bytes(b"cipher")
    drm = FakeDRM({KID: BAD})

    other = Remote("other", tmp_path / "other.db")
    other.add_key("SVC", KID, GOOD)

    def licence(drm: FakeDRM, track_kid: UUID) -> None:
        assert KID not in drm.content_keys
        assert vaults.get_key(KID) == (None, None)  # the flagged pair is skipped on the retry
        vaults.vaults.append(other)
        drm.content_keys[KID] = vaults.get_key(KID)[0]

    keys.decrypt_verified(drm, path, licence, KID)
    keys.wait_vault_writes()

    assert path.read_bytes() == b"plain:" + GOOD.encode()
    assert drm.decrypted_with == [{KID: BAD}, {KID: GOOD}]
    assert local.is_bad_key(KID, BAD)
    assert local.get_key(KID, "SVC") == GOOD  # the key that proved itself on the retry is kept locally
    assert KID not in ContentKeyResolver.LICENSE_KEY_CACHE
    assert not (tmp_path / "video.mp4.enc").exists()

    # a sibling track that still carries the poisoned key is checked too and takes the cached good key
    sibling = tmp_path / "audio.mp4"
    sibling.write_bytes(b"cipher")
    drm2 = FakeDRM({KID: BAD})
    keys.decrypt_verified(drm2, sibling, lambda drm, track_kid: drm.content_keys.update({KID: GOOD}), KID)
    assert sibling.read_bytes() == b"plain:" + GOOD.encode()


def test_no_local_vault_raises_instead_of_reusing_the_poison(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    poisoned = Remote("poisoned", tmp_path / "poisoned.db")
    poisoned.add_key("SVC", KID, BAD)
    vaults = Vaults("SVC")
    vaults.vaults = [poisoned]
    assert vaults.get_key(KID) == (BAD, poisoned)
    keys = make_resolver(monkeypatch, vaults)
    path = tmp_path / "video.mp4"
    path.write_bytes(b"cipher")
    drm = FakeDRM({KID: BAD})

    def licence(drm: FakeDRM, track_kid: UUID) -> None:
        key, _ = vaults.get_key(KID)  # nothing holds the flag, so the same pair comes back
        drm.content_keys[KID] = key

    with pytest.raises(ValueError, match="no other source"):
        keys.decrypt_verified(drm, path, licence, KID)
    assert path.read_bytes() == b"cipher"  # ciphertext restored, nothing written with the poison
    assert not (tmp_path / "video.mp4.enc").exists()


def test_segment_decrypted_track_flags_and_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local = SQLite("local", tmp_path / "local.db")
    poisoned = Remote("poisoned", tmp_path / "poisoned.db")
    poisoned.add_key("SVC", KID, BAD)
    vaults = Vaults("SVC")
    vaults.vaults = [local, poisoned]
    vaults.get_key(KID)
    keys = make_resolver(monkeypatch, vaults)
    path = tmp_path / "video.mp4"
    path.write_bytes(b"plain:" + BAD.encode())
    with pytest.raises(ValueError, match="run again"):
        keys.decrypt_verified(FakeDRM({KID: BAD}), path, lambda *a, **k: None, KID, decrypt=False)
    assert local.is_bad_key(KID, BAD)


def test_downloaders_route_through_the_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every real decrypt site calls decrypt_track, so the dl hook sees it."""
    import inspect

    from unshackle.core.manifests import dash, hls, ism, segment_pipeline
    from unshackle.core.tracks import track

    for module in (segment_pipeline, hls, track):
        assert "decrypt_track(" in inspect.getsource(module), module.__name__
    # DASH and ISM decrypt through the pipeline's complete_track, which calls decrypt_track
    for module in (dash, ism):
        assert "complete_track(" in inspect.getsource(module), module.__name__
    for module in (dash, hls, ism, segment_pipeline, track):
        src = inspect.getsource(module)
        assert "drm.decrypt(save_path)" not in src and "drm.decrypt(merged_path)" not in src, module.__name__


def test_every_poisoned_vault_is_burned_then_the_cdm_answers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local = SQLite("local", tmp_path / "local.db")
    poisoned = [Remote(f"poisoned{i}", tmp_path / f"p{i}.db") for i in range(3)]
    for i, vault in enumerate(poisoned):
        vault.add_key("SVC", KID, f"{i + 1:02x}" * 16)
    vaults = Vaults("SVC")
    vaults.vaults = [local, *poisoned]
    keys = make_resolver(monkeypatch, vaults)
    path = tmp_path / "video.mp4"
    path.write_bytes(b"cipher")
    drm = FakeDRM({KID: vaults.get_key(KID)[0]})
    cdm_calls = 0

    def licence(drm: FakeDRM, track_kid: UUID) -> None:
        nonlocal cdm_calls
        key, _ = vaults.get_key(KID)
        if key is None:
            cdm_calls += 1
            key = GOOD
        drm.content_keys[KID] = key

    keys.decrypt_verified(drm, path, licence, KID)
    assert path.read_bytes() == b"plain:" + GOOD.encode()
    assert cdm_calls == 1
    assert len(drm.decrypted_with) == 4  # three vault keys burned, then the CDM key
    assert all(local.is_bad_key(KID, f"{i + 1:02x}" * 16) for i in range(3))


def test_kv_copy_never_moves_the_flag_table(tmp_path: Path) -> None:
    import logging

    from unshackle.commands.kv import copy_service_data

    src = SQLite("src", tmp_path / "src.db")
    dst = SQLite("dst", tmp_path / "dst.db")
    src.add_key("SVC", KID, GOOD)
    src.flag_bad_key("SVC", KID, BAD, "poisoned")
    assert list(src.get_services()) == ["SVC"]
    assert copy_service_data(dst, src, "bad_keys", logging.getLogger("t")) == 0
    assert list(dst.get_services()) == []
    # a flagged pair in the source never reaches a destination that knows it is bad
    kid2 = UUID("22222222-2222-3333-4444-555555555555")
    src.add_key("SVC", kid2, BAD)
    dst.flag_bad_key("SVC", kid2, BAD, "poisoned")
    assert copy_service_data(dst, src, "SVC", logging.getLogger("t")) == 1
    assert list(dst.get_keys("SVC")) == [(KID.hex, GOOD)]


def test_retry_takes_the_next_parallel_answer_without_a_new_request(tmp_path: Path) -> None:
    from tests.core.test_vaults_lookup import FakeVault

    started, release = threading.Event(), threading.Event()

    class Fast(FakeVault):
        def get_key(self, kid: UUID | str, service: str) -> str | None:
            # answer only once the slow query runs, or the pool shutdown cancels it before it starts
            started.wait(5)
            return super().get_key(kid, service)

    class Slow(FakeVault):
        def get_key(self, kid: UUID | str, service: str) -> str | None:
            started.set()
            release.wait(5)
            return super().get_key(kid, service)

    local = SQLite("local", tmp_path / "local.db")
    fast, slow = Fast("fast", BAD), Slow("slow", GOOD)
    vaults = Vaults("SVC")
    vaults.vaults = [local, fast, slow]
    landed = threading.Event()
    offer = vaults.offer

    def offered(kid: UUID | str, key: str, source: object) -> None:
        offer(kid, key, source)  # type: ignore[arg-type]
        if source is slow:
            landed.set()

    vaults.offer = offered  # type: ignore[method-assign]

    assert vaults.get_key(KID) == (BAD, fast)
    vaults.flag_bad_key(KID, BAD)
    release.set()
    assert landed.wait(5)
    assert vaults.get_key(KID) == (GOOD, slow)
    assert fast.calls == 1 and slow.calls == 1


@pytest.mark.parametrize("replace", [False, True])
def test_cdm_returning_the_flagged_key_clears_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replace: bool
) -> None:
    """A decode failure on a key the CDM then confirms is a false positive: unflag, keep the file."""
    local = SQLite("local", tmp_path / "local.db")
    poisoned = Remote("poisoned", tmp_path / "poisoned.db")
    poisoned.add_key("SVC", KID, BAD)
    vaults = Vaults("SVC")
    vaults.vaults = [local, poisoned]
    keys = make_resolver(monkeypatch, vaults)
    monkeypatch.setattr(key_resolution, "ffmpeg_decodes", lambda path, start=None, seconds=3, video=None: False)
    path = tmp_path / "video.mp4"
    path.write_bytes(b"cipher")
    drm = FakeDRM({KID: vaults.get_key(KID)[0]})

    def licence(drm: FakeDRM, track_kid: UUID) -> None:
        assert vaults.get_key(KID) == (None, None)
        if replace:
            drm.content_keys = {KID: BAD}  # a CDM licence builds a new dict
        else:
            drm.content_keys[KID] = BAD  # the CDM answers with the same key

    keys.decrypt_verified(drm, path, licence, KID)
    keys.wait_vault_writes()
    assert path.read_bytes() == b"plain:" + BAD.encode()
    assert not local.is_bad_key(KID, BAD)
    assert local.get_key(KID, "SVC") == BAD


def test_a_replacement_key_in_a_new_dict_is_checked_and_stored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local = SQLite("local", tmp_path / "local.db")
    poisoned = Remote("poisoned", tmp_path / "poisoned.db")
    poisoned.add_key("SVC", KID, BAD)
    vaults = Vaults("SVC")
    vaults.vaults = [local, poisoned]
    assert vaults.get_key(KID) == (BAD, poisoned)
    keys = make_resolver(monkeypatch, vaults)
    path = tmp_path / "video.mp4"
    path.write_bytes(b"cipher")
    drm = FakeDRM({KID: BAD})
    other = Remote("other", tmp_path / "other.db")
    other.add_key("SVC", KID, GOOD)

    def licence(drm: FakeDRM, track_kid: UUID) -> None:
        vaults.vaults.append(other)
        drm.content_keys = {KID: vaults.get_key(KID)[0]}

    keys.decrypt_verified(drm, path, licence, KID)
    keys.wait_vault_writes()
    assert path.read_bytes() == b"plain:" + GOOD.encode()
    assert local.get_key(KID, "SVC") == GOOD


@pytest.mark.parametrize("replace", [True, False])
def test_a_cdm_that_confirms_a_key_flagged_on_entry_clears_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replace: bool
) -> None:
    """Another track flagged the pair, then this track's CDM licence answers with the same key."""
    local = SQLite("local", tmp_path / "local.db")
    vaults = Vaults("SVC")
    vaults.vaults = [local]
    keys = make_resolver(monkeypatch, vaults)
    vaults.flag_bad_key(KID, BAD)
    path = tmp_path / "video.mp4"
    path.write_bytes(b"cipher")
    drm = FakeDRM({KID: BAD})

    def licence(drm: FakeDRM, track_kid: UUID) -> None:
        if replace:
            drm.content_keys = {KID: BAD}
        else:
            drm.content_keys[KID] = BAD

    keys.decrypt_verified(drm, path, licence, KID)
    keys.wait_vault_writes()
    assert path.read_bytes() == b"plain:" + BAD.encode()
    assert not local.is_bad_key(KID, BAD)
    assert local.get_key(KID, "SVC") == BAD


def test_a_licence_key_that_replaces_a_bad_local_pair_is_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local vault held the bad pair when the licence key arrived, so it skipped the KID; store it now."""
    local = SQLite("local", tmp_path / "local.db")
    local.add_key("SVC", KID, BAD)
    vaults = Vaults("SVC")
    vaults.vaults = [local]
    keys = make_resolver(monkeypatch, vaults)
    drm = FakeDRM({KID: vaults.get_key(KID)[0]})
    path = tmp_path / "video.mp4"
    path.write_bytes(b"cipher")
    assert local.add_keys("SVC", {KID: GOOD}) == 0

    keys.decrypt_verified(drm, path, lambda drm, track_kid: drm.content_keys.update({KID: GOOD}), KID)
    keys.wait_vault_writes()
    assert local.is_bad_key(KID, BAD)
    assert local.get_key(KID, "SVC") == GOOD


def test_stale_backup_from_a_killed_run_is_not_restored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    poisoned = Remote("poisoned", tmp_path / "poisoned.db")
    poisoned.add_key("SVC", KID, BAD)
    vaults = Vaults("SVC")
    vaults.vaults = [poisoned]
    vaults.get_key(KID)
    keys = make_resolver(monkeypatch, vaults)
    path = tmp_path / "video.mp4"
    path.write_bytes(b"cipher")
    (tmp_path / "video.mp4.enc").write_bytes(b"stale")

    def licence(drm: FakeDRM, track_kid: UUID) -> None:
        drm.content_keys[KID] = vaults.get_key(KID)[0]

    with pytest.raises(ValueError, match="no other source"):
        keys.decrypt_verified(FakeDRM({KID: BAD}), path, licence, KID)
    assert path.read_bytes() == b"cipher"


@pytest.mark.parametrize(
    ("windows", "licensed"),
    [({KID: [(0.0, 2.0)], SIBLING: [(2.0, 4.0)]}, [SIBLING]), ({KID: [(0.0, 2.0)]}, []), (None, [SIBLING])],
    ids=["file-uses-it", "file-does-not", "unmapped"],
)
def test_an_unkeyed_kid_the_file_uses_is_licensed_before_the_decrypt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows: dict | None, licensed: list[UUID]
) -> None:
    keys = make_resolver(monkeypatch, Vaults("SVC"))
    drm = FakeDRM({KID: GOOD})
    drm.kids = [KID, SIBLING]
    monkeypatch.setattr(
        key_resolution.verify,
        "kid_windows",
        lambda path: None if windows is None else key_resolution.verify.KidMap(windows, True),
    )
    path = tmp_path / "video.mp4"
    path.write_bytes(b"cipher")
    licences: list[UUID] = []

    keys.decrypt_verified(drm, path, lambda drm, track_kid: licences.append(track_kid), KID)

    assert licences == licensed
    assert drm.decrypted_with == [{KID: GOOD}]


def test_drm_without_content_keys_still_decrypts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """HLS AES-128 ClearKey has no KID:KEY map; the hook must pass it straight through."""
    keys = make_resolver(monkeypatch, Vaults("SVC"))
    path = tmp_path / "video.ts"
    path.write_bytes(b"cipher")
    calls: list[Path] = []
    keys.decrypt_verified(SimpleNamespace(decrypt=calls.append), path)
    assert calls == [path]


def test_flag_on_a_read_only_vault_keeps_its_rows(tmp_path: Path) -> None:
    ro = SQLite("ro", tmp_path / "ro.db", no_push=True)
    ro.add_key("SVC", KID, BAD)
    ro.flag_bad_key("SVC", KID, BAD, "poisoned")
    assert ro.is_bad_key(KID, BAD)
    assert ro.get_key(KID, "SVC") is None  # the lookup still skips the flagged pair
    assert list(ro.get_keys("SVC")) == [(KID.hex, BAD)]  # but the row itself is untouched


def test_flag_moves_the_row_and_blocks_it_coming_back(tmp_path: Path) -> None:
    local = SQLite("local", tmp_path / "local.db")
    local.add_key("SVC", KID, BAD)
    local.flag_bad_key("SVC", KID, BAD, "poisoned")
    assert local.is_bad_key(KID, BAD)
    assert list(local.get_keys("SVC")) == []
    assert local.add_key("SVC", KID, BAD) is False
    assert local.add_keys("SVC", {KID: BAD}) == 0
    assert list(local.get_keys("SVC")) == []
    assert local.add_key("SVC", KID, GOOD)  # the KID itself is not blocked, only the pair


def test_a_licence_key_clears_a_flag_from_an_earlier_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A false flag must not outlive the run: the next licence returns the pair and it is stored again."""
    local = SQLite("local", tmp_path / "local.db")
    local.flag_bad_key("SVC", KID, GOOD, "poisoned")
    vaults = Vaults("SVC")
    vaults.vaults = [local]
    keys = resolver(vaults, source=SERVER)
    keys.take_server_keys(batch_title({KID: GOOD}))
    keys.wait_vault_writes()
    assert not local.is_bad_key(KID, GOOD)
    assert local.get_key(KID, "SVC") == GOOD


def test_a_licence_with_no_new_keys_sends_no_vault_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PSSH KID the licence never answers forces a licence on every run; the vaults must not get an empty push."""
    from unshackle.core.drm import ClearKeyCENC

    vault = MemoryVault()
    monkeypatch.setattr(ClearKeyCENC, "get_content_keys", lambda self, **_: None)
    keys = resolver(vaults_of(vault))
    drm = ClearKeyCENC(kids=[KID])

    keys.licence_for(SimpleNamespace(drm=[drm]), None, key_table())(drm, track_kid=None)
    keys.wait_vault_writes()

    assert vault.pushes == []


def test_the_cached_key_count_is_unique_and_skipped_without_vaults(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Two tracks licensed in parallel can return the same key; --cdm-only loads no vaults at all."""
    other = UUID(int=9)
    vaults = Vaults("SVC")
    vaults.vaults = [SQLite("local", tmp_path / "local.db")]
    keys = resolver(vaults, source=SERVER)
    keys.take_server_keys(batch_title({KID: GOOD}))
    ContentKeyResolver.LICENSE_KEY_CACHE.clear()
    keys.take_server_keys(batch_title({KID: GOOD, other: GOOD}))
    with caplog.at_level(logging.INFO, logger="download"):
        keys.wait_vault_writes()
    assert [r.getMessage() for r in caplog.records if r.name == "download"] == ["Cached 2 Keys to 1/1 Vaults"]

    caplog.clear()
    keys = resolver(Vaults("SVC"), source=SERVER)
    keys.take_server_keys(batch_title({KID: GOOD}))
    with caplog.at_level(logging.INFO, logger="download"):
        keys.wait_vault_writes()
    assert not [r for r in caplog.records if r.name == "download"]


def test_bulk_add_refuses_a_flagged_pair_in_any_case_or_kid_form(tmp_path: Path) -> None:
    local = SQLite("local", tmp_path / "local.db")
    local.flag_bad_key("SVC", KID, BAD, "poisoned")
    for kid, key in ((KID.hex.upper(), BAD), (str(KID), BAD.upper()), (KID, BAD)):
        assert local.add_keys("SVC", {kid: key}) == 0
    assert list(local.get_keys("SVC")) == []


def test_only_the_kid_whose_window_fails_is_flagged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A lead under one KID and the rest under another: the lead's key is right and stays."""
    lead = UUID(int=7)
    local = SQLite("local", tmp_path / "local.db")
    poisoned = Remote("poisoned", tmp_path / "poisoned.db")
    poisoned.add_key("SVC", lead, BAD)  # the same wrong key under both KIDs
    poisoned.add_key("SVC", KID, BAD)
    other = Remote("other", tmp_path / "other.db")
    other.add_key("SVC", KID, GOOD)
    vaults = Vaults("SVC")
    vaults.vaults = [local, poisoned]
    keys = make_resolver(monkeypatch, vaults)
    drm = FakeDRM({lead: vaults.get_key(lead)[0], KID: vaults.get_key(KID)[0]})
    right = {lead: BAD, KID: GOOD}
    monkeypatch.setattr(
        key_resolution.verify,
        "kid_windows",
        lambda path: key_resolution.verify.KidMap({lead: [(0.0, 2.0)], KID: [(120.0, 122.0)]}, True),
    )
    monkeypatch.setattr(
        key_resolution,
        "ffmpeg_decodes",
        lambda path, start=None, seconds=3, video=None: (
            drm.decrypted_with[-1][lead if start == 0.0 else KID] == right[lead if start == 0.0 else KID]
        ),
    )
    path = tmp_path / "video.mp4"
    path.write_bytes(b"cipher")

    def licence(drm: FakeDRM, track_kid: UUID) -> None:
        vaults.vaults.append(other)
        drm.content_keys[KID] = vaults.get_key(KID)[0]

    keys.decrypt_verified(drm, path, licence, KID)
    keys.wait_vault_writes()
    assert drm.content_keys == right
    assert local.is_bad_key(KID, BAD)
    assert not local.is_bad_key(lead, BAD)


def test_a_kid_absent_from_the_map_is_judged_by_the_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A map that holds an all-zero tenc KID is not complete, so a KID it does not know
    shares the whole-file verdict, and a wrong key there is flagged."""
    lead = UUID(int=7)
    local = SQLite("local", tmp_path / "local.db")
    poisoned = Remote("poisoned", tmp_path / "poisoned.db")
    poisoned.add_key("SVC", lead, GOOD)
    poisoned.add_key("SVC", KID, BAD)
    other = Remote("other", tmp_path / "other.db")
    other.add_key("SVC", KID, GOOD)
    vaults = Vaults("SVC")
    vaults.vaults = [local, poisoned]
    keys = make_resolver(monkeypatch, vaults)
    drm = FakeDRM({lead: vaults.get_key(lead)[0], KID: vaults.get_key(KID)[0]})
    calls: list[tuple] = []

    def decodes(path: Path, start: object = None, seconds: float = 3, video: object = None) -> bool:
        calls.append((start, seconds, video))
        return drm.decrypted_with[-1][lead if start is not None else KID] == GOOD

    monkeypatch.setattr(
        key_resolution.verify,
        "kid_windows",
        lambda path: key_resolution.verify.KidMap({lead: [(4.0, 6.0)], UUID(int=0): [(8.0, 10.0)]}, True),
    )
    monkeypatch.setattr(key_resolution, "ffmpeg_decodes", decodes)
    path = tmp_path / "video.mp4"
    path.write_bytes(b"cipher")

    def licence(drm: FakeDRM, track_kid: UUID) -> None:
        vaults.vaults.append(other)
        drm.content_keys[KID] = vaults.get_key(KID)[0]

    keys.decrypt_verified(drm, path, licence, KID)
    keys.wait_vault_writes()
    assert drm.content_keys == {lead: GOOD, KID: GOOD}
    assert local.is_bad_key(KID, BAD)
    assert not local.is_bad_key(lead, GOOD)
    assert (4.0, 2.0, True) in calls  # the window is capped at the fragment's end
    assert (None, 3, True) in calls  # the fallback knows the track is video


def test_a_wrong_mapped_key_does_not_flag_a_kid_the_file_does_not_carry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audio KID is in the licence and the vault but not in this video file: the whole-file
    check fails on the wrong video key's noise, and that verdict must not reach the audio KID."""
    audio = UUID(int=7)
    local = SQLite("local", tmp_path / "local.db")
    poisoned = Remote("poisoned", tmp_path / "poisoned.db")
    poisoned.add_key("SVC", audio, GOOD)
    poisoned.add_key("SVC", KID, BAD)
    other = Remote("other", tmp_path / "other.db")
    other.add_key("SVC", KID, GOOD)
    vaults = Vaults("SVC")
    vaults.vaults = [local, poisoned]
    keys = make_resolver(monkeypatch, vaults)
    drm = FakeDRM({audio: vaults.get_key(audio)[0], KID: vaults.get_key(KID)[0]})
    monkeypatch.setattr(
        key_resolution.verify, "kid_windows", lambda path: key_resolution.verify.KidMap({KID: [(4.0, 6.0)]}, True)
    )
    monkeypatch.setattr(
        key_resolution,
        "ffmpeg_decodes",
        lambda path, start=None, seconds=3, video=None: drm.decrypted_with[-1][KID] == GOOD,
    )
    path = tmp_path / "video.mp4"
    path.write_bytes(b"cipher")

    def licence(drm: FakeDRM, track_kid: UUID) -> None:
        vaults.vaults.append(other)
        drm.content_keys[KID] = vaults.get_key(KID)[0]

    keys.decrypt_verified(drm, path, licence, KID)
    keys.wait_vault_writes()
    assert drm.content_keys == {audio: GOOD, KID: GOOD}
    assert local.is_bad_key(KID, BAD)
    assert not local.is_bad_key(audio, GOOD)
    assert local.get_key(audio, "SVC") is None


def test_a_kid_the_file_does_not_carry_is_not_judged_by_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CDM licensed the video KID and a vault served the audio KID. An FFmpeg that fails
    every decode of the video file must not flag the audio KID, nor copy it to another vault."""
    audio = UUID(int=7)
    local = SQLite("local", tmp_path / "local.db")
    remote = Remote("remote", tmp_path / "remote.db")
    remote.add_key("SVC", audio, GOOD)
    vaults = Vaults("SVC")
    vaults.vaults = [local, remote]
    keys = make_resolver(monkeypatch, vaults)
    drm = FakeDRM({audio: vaults.get_key(audio)[0], KID: GOOD})
    monkeypatch.setattr(
        key_resolution.verify, "kid_windows", lambda path: key_resolution.verify.KidMap({KID: [(4.0, 6.0)]}, True)
    )
    monkeypatch.setattr(key_resolution, "ffmpeg_decodes", lambda path, start=None, seconds=3, video=None: False)
    path = tmp_path / "video.mp4"
    path.write_bytes(b"cipher")
    licences: list[UUID] = []

    keys.decrypt_verified(drm, path, lambda drm, track_kid: licences.append(track_kid), KID)
    keys.wait_vault_writes()
    assert drm.content_keys == {audio: GOOD, KID: GOOD}
    assert not licences
    assert not local.is_bad_key(audio, GOOD)
    assert local.get_key(audio, "SVC") is None


@pytest.mark.parametrize(
    ("served", "decodes", "told"),
    [
        (GOOD, False, False),  # broken FFmpeg, the CDM returns the same key
        (BAD, True, True),  # the CDM returns another key
    ],
)
def test_a_remote_vault_is_told_only_when_another_key_replaces_its_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served: str, decodes: bool, told: bool
) -> None:
    remote = Remote("remote", tmp_path / "remote.db")
    remote.add_key("SVC", KID, served)
    vaults = Vaults("SVC")
    vaults.vaults = [SQLite("local", tmp_path / "local.db"), remote]
    keys = make_resolver(monkeypatch, vaults)
    if not decodes:
        monkeypatch.setattr(key_resolution, "ffmpeg_decodes", lambda path, start=None, seconds=3, video=None: False)
    drm = FakeDRM({KID: vaults.get_key(KID)[0]})
    path = tmp_path / "video.mp4"
    path.write_bytes(b"cipher")

    def licence(drm: FakeDRM, track_kid: UUID) -> None:
        assert not remote.is_bad_key(KID, served)
        drm.content_keys[KID] = GOOD

    keys.decrypt_verified(drm, path, licence, KID)
    keys.wait_vault_writes()
    assert drm.content_keys == {KID: GOOD}
    assert remote.is_bad_key(KID, served) is told


def test_the_server_vault_is_told_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Over --remote the report is what makes the server skip the pair on the next licence."""
    reports: list[tuple[UUID, str]] = []
    server = SimpleNamespace(
        name="server_cdm",
        local=False,
        report_now=True,
        flag_bad_key=lambda service, kid, key, source: reports.append((kid, key)),
    )
    vaults = Vaults("SVC")
    vaults.vaults = [SQLite("local", tmp_path / "local.db")]
    vaults.supplied(KID, BAD, server)  # type: ignore[arg-type]
    keys = make_resolver(monkeypatch, vaults)
    drm = FakeDRM({KID: BAD})
    path = tmp_path / "video.mp4"
    path.write_bytes(b"cipher")

    def licence(drm: FakeDRM, track_kid: UUID) -> None:
        assert reports == [(KID, BAD)]
        drm.content_keys[KID] = GOOD

    keys.decrypt_verified(drm, path, licence, KID)
    assert drm.content_keys == {KID: GOOD}


def test_only_checked_kids_are_copied_to_other_vaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the mapped KID is proven and copied; the KID this file does not carry waits for its own file."""
    lead = UUID(int=7)
    local = SQLite("local", tmp_path / "local.db")
    remote = Remote("remote", tmp_path / "remote.db")
    remote.add_key("SVC", lead, GOOD)
    remote.add_key("SVC", KID, GOOD)
    vaults = Vaults("SVC")
    vaults.vaults = [local, remote]
    keys = make_resolver(monkeypatch, vaults)
    drm = FakeDRM({lead: vaults.get_key(lead)[0], KID: vaults.get_key(KID)[0]})
    starts: list = []

    def decodes(path: Path, start: object = None, seconds: float = 3, video: object = None) -> bool:
        starts.append(start)
        return True

    monkeypatch.setattr(
        key_resolution.verify, "kid_windows", lambda path: key_resolution.verify.KidMap({lead: [(4.0, 6.0)]}, True)
    )
    monkeypatch.setattr(key_resolution, "ffmpeg_decodes", decodes)
    path = tmp_path / "video.mp4"
    path.write_bytes(b"cipher")

    keys.decrypt_verified(drm, path, None, KID)
    keys.wait_vault_writes()
    assert None not in starts
    assert local.get_key(lead, "SVC") == GOOD
    assert local.get_key(KID, "SVC") is None


def test_a_clear_file_judges_and_copies_no_vault_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local = SQLite("local", tmp_path / "local.db")
    remote = Remote("remote", tmp_path / "remote.db")
    remote.add_key("SVC", KID, BAD)
    vaults = Vaults("SVC")
    vaults.vaults = [local, remote]
    keys = make_resolver(monkeypatch, vaults)
    drm = FakeDRM({KID: vaults.get_key(KID)[0]})
    checks: list = []

    def decodes(path: Path, start: object = None, seconds: float = 3, video: object = None) -> bool:
        checks.append(start)
        return True

    monkeypatch.setattr(key_resolution.verify, "kid_windows", lambda path: key_resolution.verify.KidMap({}, False))
    monkeypatch.setattr(key_resolution, "ffmpeg_decodes", decodes)
    path = tmp_path / "audio.mp4"
    path.write_bytes(b"clear")

    keys.decrypt_verified(drm, path, None, KID)
    keys.wait_vault_writes()
    assert checks == []
    assert local.get_key(KID, "SVC") is None
    assert not local.is_bad_key(KID, BAD)


def test_flagged_pair_warns_once_per_vault(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    local = SQLite("local", tmp_path / "local.db")
    poisoned = Remote("poisoned", tmp_path / "poisoned.db")
    poisoned.add_key("SVC", KID, BAD)
    local.flag_bad_key("SVC", KID, BAD, "poisoned")
    vaults = Vaults("SVC")
    vaults.vaults = [local, poisoned]
    with caplog.at_level(logging.WARNING, logger="unshackle.core.vaults"):
        for _ in range(3):
            assert vaults.get_key(KID) == (None, None)
    assert sum("flagged bad, skipping" in r.getMessage() for r in caplog.records) == 1


def test_add_key_refuses_a_flagged_pair_with_a_dashed_kid(tmp_path: Path) -> None:
    local = SQLite("local", tmp_path / "local.db")
    local.flag_bad_key("SVC", KID, BAD, "poisoned")
    assert local.add_key("SVC", str(KID), BAD) is False
    assert local.add_key("SVC", str(KID).upper(), BAD.upper()) is False
    assert list(local.get_keys("SVC")) == []


def test_a_sibling_verdict_during_the_decrypt_still_triggers_the_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two tracks share a KID and decrypt at the same time; the first verdict pops the shared
    source, so the second track must read the flag instead of returning unchecked."""
    local = SQLite("local", tmp_path / "local.db")
    vaults = Vaults("SVC")
    vaults.vaults = [local]
    vaults.supplied(KID, BAD, local)
    keys = make_resolver(monkeypatch, vaults)
    ContentKeyResolver.LICENSE_KEY_CACHE[KID] = GOOD  # the sibling's retry already licensed the good key

    path = tmp_path / "audio.mp4"
    path.write_bytes(b"cipher")

    class SiblingFlagsMeanwhile(FakeDRM):
        def decrypt(self, path: Path) -> None:
            super().decrypt(path)
            if self.content_keys[KID] == BAD:
                vaults.flag_bad_key(KID, BAD)

    drm = SiblingFlagsMeanwhile({KID: BAD})
    licences: list = []

    def licence(drm: FakeDRM, track_kid: UUID) -> None:
        licences.append(ContentKeyResolver.LICENSE_KEY_CACHE.get(KID))
        drm.content_keys[KID] = ContentKeyResolver.LICENSE_KEY_CACHE[KID]

    keys.decrypt_verified(drm, path, licence, KID)

    assert path.read_bytes() == b"plain:" + GOOD.encode()
    assert drm.decrypted_with == [{KID: BAD}, {KID: GOOD}]
    assert licences == [GOOD]  # the good key in the run cache survives the flag of the bad one


def test_api_vault_reports_the_pair_upstream(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    api = API("vault", "https://vault.example/", "tok")
    calls: list[tuple[str, str, object]] = []
    ok = SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {"code": 0})
    monkeypatch.setattr(api.session, "post", lambda url, **kw: calls.append(("POST", url, kw.get("json"))) or ok)
    monkeypatch.setattr(api.session, "delete", lambda url, **kw: calls.append(("DELETE", url, None)) or ok)

    vaults = Vaults("SVC")
    vaults.vaults = [api]
    vaults.supplied(KID, BAD, api)
    vaults.flag_bad_key(KID, BAD)
    assert not calls
    vaults.proven({KID: GOOD}, {})
    vaults.unflag_bad_key(KID, BAD)

    assert calls == [
        ("POST", f"https://vault.example/bad-keys/{KID.hex}", {"content_key": BAD, "service": "SVC"}),
        ("DELETE", f"https://vault.example/bad-keys/{KID.hex}/{BAD}", None),
    ]

    def flag_and_replace(source: object) -> None:
        vaults.supplied(KID, BAD, source)  # type: ignore[arg-type]
        vaults.flag_bad_key(KID, BAD)
        vaults.proven({KID: GOOD}, {})

    calls.clear()
    flag_and_replace(SimpleNamespace(name="other", local=False, report_now=False))
    assert calls == []

    monkeypatch.setattr(api.session, "post", lambda url, **kw: (_ for _ in ()).throw(ConnectionError("down")))
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="unshackle.core.vaults"):
        flag_and_replace(api)
    assert [r.levelname for r in caplog.records] == ["DEBUG"]

    gone = SimpleNamespace(status_code=404, raise_for_status=lambda: (_ for _ in ()).throw(RuntimeError("404")))
    monkeypatch.setattr(api.session, "post", lambda url, **kw: gone)
    caplog.clear()
    flag_and_replace(api)
    assert not caplog.records

    denied = SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {"code": 1, "message": "no"})
    monkeypatch.setattr(api.session, "post", lambda url, **kw: denied)
    monkeypatch.setattr(api.session, "delete", lambda url, **kw: denied)
    flag_and_replace(api)
    vaults.unflag_bad_key(KID, BAD)
    assert not caplog.records


def test_prefer_vault_keys_queries_each_named_kid_once() -> None:
    remote = MemoryVault(local=False)
    keys = resolver(vaults_of(remote), source=SERVER)
    keys.take_server_keys(batch_title({KID: GOOD, SIBLING: BAD}, {KID: GOOD, SIBLING: BAD}, kids=[KID]))

    assert remote.asked == [KID]


def test_cache_resolved_keys_skips_a_key_a_local_vault_holds(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    local = SQLite("local", tmp_path / "local.db")
    local.add_key("SVC", SIBLING, BAD)
    vaults = Vaults("SVC")
    vaults.vaults = [local]
    keys = resolver(vaults, source=SERVER)

    keys.take_server_keys(batch_title({KID: GOOD, SIBLING: BAD}, kids=[KID]))
    with caplog.at_level(logging.INFO, logger="download"):
        keys.wait_vault_writes()

    assert local.get_key(KID, "SVC") == GOOD
    assert [r.getMessage() for r in caplog.records if r.name == "download"] == ["Cached 1 Key to 1/1 Vaults"]


def test_api_vault_does_not_shrink_the_batch_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    api = API("vault", "https://vault.example/", "tok")
    posts: list[str] = []
    limited = SimpleNamespace(status_code=429)
    monkeypatch.setattr(api.session, "post", lambda url, **kw: posts.append(url) or limited)

    with pytest.raises(Exceptions.TooManyRequests):
        api.add_keys("SVC", {KID: GOOD})

    assert len(posts) == 1


def test_http_vault_does_not_fall_back_to_single_inserts_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    vault = HTTP("vault", "https://vault.example/", password="tok", api_mode="json")
    posts: list[str] = []
    limited = SimpleNamespace(status_code=429, ok=False)
    monkeypatch.setattr(vault.session, "post", lambda url, **kw: posts.append(url) or limited)

    with pytest.raises(RateLimited):
        vault.add_keys("SVC", {KID: GOOD, SIBLING: BAD})

    assert len(posts) == 1


def test_vault_session_retries_429_but_not_a_dead_vault() -> None:
    retry = vault_session(10).get_adapter("https://vault.example").max_retries
    assert retry.status_forcelist == [429]
    assert (retry.connect, retry.read, retry.other) == (0, 0, 0)


def test_an_unreachable_vault_costs_one_attempt_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests

    def down(url: str, **kw: object) -> None:
        calls.append(url)
        raise requests.ConnectTimeout("no answer")

    calls: list[str] = []
    api = API("api", "https://api.example/", "tok")
    http = HTTP("http", "https://http.example/", password="tok", api_mode="json")
    for vault in (api, http):
        monkeypatch.setattr(vault.session, "get", down)
        monkeypatch.setattr(vault.session, "post", down)
        with pytest.raises(requests.ConnectTimeout):
            vault.add_keys("SVC", {KID: GOOD, SIBLING: BAD})
    assert len(calls) == 2  # no batch step-down, no single-key fallback

    calls.clear()
    vaults = Vaults("SVC")
    vaults.vaults = [api]
    assert vaults.get_key(KID) == (None, None)
    assert vaults.get_key(SIBLING) == (None, None)
    assert vaults.add_keys({KID: GOOD}) == 0
    assert calls == ["https://api.example/svc/" + KID.hex]


def test_a_vault_still_rate_limited_is_not_used_again(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    api = API("api", "https://api.example/", "tok")
    monkeypatch.setattr(api.session, "get", lambda url, **kw: calls.append(url) or SimpleNamespace(status_code=429))
    vaults = Vaults("SVC")
    vaults.vaults = [api]

    assert vaults.get_key(KID) == (None, None)
    assert vaults.get_key(SIBLING) == (None, None)
    assert calls == ["https://api.example/svc/" + KID.hex]
