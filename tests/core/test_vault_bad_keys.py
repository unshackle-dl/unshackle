import time
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from unshackle.commands import dl as dl_module
from unshackle.commands.dl import dl
from unshackle.core.vaults import Vaults
from unshackle.vaults.SQLite import SQLite

KID = UUID("11111111-2222-3333-4444-555555555555")
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
    assert vaults.sources[KID] == (BAD, poisoned)
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


def make_cmd(monkeypatch: pytest.MonkeyPatch, vaults: Vaults) -> dl:
    monkeypatch.setattr(dl_module.binaries, "FFMPEG", "ffmpeg")
    monkeypatch.setattr(dl_module, "ffmpeg_decodes", lambda path: path.read_bytes() == b"plain:" + GOOD.encode())
    cmd = dl.__new__(dl)
    cmd.log = SimpleNamespace(warning=print, info=print, debug=print)
    cmd.vaults = vaults
    cmd.vault_cache_tally = None
    dl.LICENSE_KEY_CACHE.clear()
    dl.LICENSE_KEY_CACHE[KID] = BAD
    return cmd


def test_decrypt_verified_flags_and_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local = SQLite("local", tmp_path / "local.db")
    poisoned = Remote("poisoned", tmp_path / "poisoned.db")
    poisoned.add_key("SVC", KID, BAD)
    vaults = Vaults("SVC")
    vaults.vaults = [local, poisoned]
    assert vaults.get_key(KID) == (BAD, poisoned)
    cmd = make_cmd(monkeypatch, vaults)

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

    cmd.decrypt_verified(drm, path, licence, KID)
    cmd.wait_vault_writes()

    assert path.read_bytes() == b"plain:" + GOOD.encode()
    assert drm.decrypted_with == [{KID: BAD}, {KID: GOOD}]
    assert local.is_bad_key(KID, BAD)
    assert local.get_key(KID, "SVC") == GOOD  # the key that proved itself on the retry is kept locally
    assert KID not in dl.LICENSE_KEY_CACHE
    assert not (tmp_path / "video.mp4.enc").exists()

    # a sibling track that still carries the poisoned key is checked too and takes the cached good key
    sibling = tmp_path / "audio.mp4"
    sibling.write_bytes(b"cipher")
    drm2 = FakeDRM({KID: BAD})
    cmd.decrypt_verified(drm2, sibling, lambda drm, track_kid: drm.content_keys.update({KID: GOOD}), KID)
    assert sibling.read_bytes() == b"plain:" + GOOD.encode()


def test_no_local_vault_raises_instead_of_reusing_the_poison(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    poisoned = Remote("poisoned", tmp_path / "poisoned.db")
    poisoned.add_key("SVC", KID, BAD)
    vaults = Vaults("SVC")
    vaults.vaults = [poisoned]
    assert vaults.get_key(KID) == (BAD, poisoned)
    cmd = make_cmd(monkeypatch, vaults)
    path = tmp_path / "video.mp4"
    path.write_bytes(b"cipher")
    drm = FakeDRM({KID: BAD})

    def licence(drm: FakeDRM, track_kid: UUID) -> None:
        key, _ = vaults.get_key(KID)  # nothing holds the flag, so the same pair comes back
        drm.content_keys[KID] = key

    with pytest.raises(ValueError, match="no other source"):
        cmd.decrypt_verified(drm, path, licence, KID)
    assert path.read_bytes() == b"cipher"  # ciphertext restored, nothing written with the poison
    assert not (tmp_path / "video.mp4.enc").exists()


def test_segment_decrypted_track_flags_and_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local = SQLite("local", tmp_path / "local.db")
    poisoned = Remote("poisoned", tmp_path / "poisoned.db")
    poisoned.add_key("SVC", KID, BAD)
    vaults = Vaults("SVC")
    vaults.vaults = [local, poisoned]
    vaults.get_key(KID)
    cmd = make_cmd(monkeypatch, vaults)
    path = tmp_path / "video.mp4"
    path.write_bytes(b"plain:" + BAD.encode())
    with pytest.raises(ValueError, match="run again"):
        cmd.decrypt_verified(FakeDRM({KID: BAD}), path, lambda *a, **k: None, KID, decrypt=False)
    assert local.is_bad_key(KID, BAD)


def test_downloaders_route_through_the_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every real decrypt site calls decrypt_track, so the dl hook sees it."""
    import inspect

    from unshackle.core.manifests import dash, hls, ism
    from unshackle.core.tracks import track

    for module in (dash, hls, ism, track):
        src = inspect.getsource(module)
        assert "decrypt_track(" in src, module.__name__
        assert "drm.decrypt(save_path)" not in src and "drm.decrypt(merged_path)" not in src, module.__name__


def test_every_poisoned_vault_is_burned_then_the_cdm_answers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local = SQLite("local", tmp_path / "local.db")
    poisoned = [Remote(f"poisoned{i}", tmp_path / f"p{i}.db") for i in range(3)]
    for i, vault in enumerate(poisoned):
        vault.add_key("SVC", KID, f"{i + 1:02x}" * 16)
    vaults = Vaults("SVC")
    vaults.vaults = [local, *poisoned]
    cmd = make_cmd(monkeypatch, vaults)
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

    cmd.decrypt_verified(drm, path, licence, KID)
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

    local = SQLite("local", tmp_path / "local.db")
    fast = FakeVault("fast", BAD, delay=0.01)
    slow = FakeVault("slow", GOOD, delay=0.2)
    vaults = Vaults("SVC")
    vaults.vaults = [local, fast, slow]

    assert vaults.get_key(KID) == (BAD, fast)
    vaults.flag_bad_key(KID, BAD)
    time.sleep(0.4)  # the slow answer lands in candidates after the first call returned
    assert vaults.get_key(KID) == (GOOD, slow)
    assert fast.calls == 1 and slow.calls == 1


def test_cdm_returning_the_flagged_key_clears_the_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A decode failure on a key the CDM then confirms is a false positive: unflag, keep the file."""
    local = SQLite("local", tmp_path / "local.db")
    poisoned = Remote("poisoned", tmp_path / "poisoned.db")
    poisoned.add_key("SVC", KID, BAD)
    vaults = Vaults("SVC")
    vaults.vaults = [local, poisoned]
    cmd = make_cmd(monkeypatch, vaults)
    monkeypatch.setattr(dl_module, "ffmpeg_decodes", lambda path: False)
    path = tmp_path / "video.mp4"
    path.write_bytes(b"cipher")
    drm = FakeDRM({KID: vaults.get_key(KID)[0]})

    def licence(drm: FakeDRM, track_kid: UUID) -> None:
        assert vaults.get_key(KID) == (None, None)
        drm.content_keys[KID] = BAD  # the CDM answers with the same key

    cmd.decrypt_verified(drm, path, licence, KID)
    assert path.read_bytes() == b"plain:" + BAD.encode()
    assert not local.is_bad_key(KID, BAD)


def test_stale_backup_from_a_killed_run_is_not_restored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    poisoned = Remote("poisoned", tmp_path / "poisoned.db")
    poisoned.add_key("SVC", KID, BAD)
    vaults = Vaults("SVC")
    vaults.vaults = [poisoned]
    vaults.get_key(KID)
    cmd = make_cmd(monkeypatch, vaults)
    path = tmp_path / "video.mp4"
    path.write_bytes(b"cipher")
    (tmp_path / "video.mp4.enc").write_bytes(b"stale")

    def licence(drm: FakeDRM, track_kid: UUID) -> None:
        drm.content_keys[KID] = vaults.get_key(KID)[0]

    with pytest.raises(ValueError, match="no other source"):
        cmd.decrypt_verified(FakeDRM({KID: BAD}), path, licence, KID)
    assert path.read_bytes() == b"cipher"


def test_drm_without_content_keys_still_decrypts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """HLS AES-128 ClearKey has no KID:KEY map; the hook must pass it straight through."""
    cmd = make_cmd(monkeypatch, Vaults("SVC"))
    path = tmp_path / "video.ts"
    path.write_bytes(b"cipher")
    calls: list[Path] = []
    cmd.decrypt_verified(SimpleNamespace(decrypt=calls.append), path)
    assert calls == [path]


def test_flag_on_a_read_only_vault_keeps_its_rows(tmp_path: Path) -> None:
    ro = SQLite("ro", tmp_path / "ro.db", no_push=True)
    ro.add_key("SVC", KID, BAD)
    ro.flag_bad_key("SVC", KID, BAD, "poisoned")
    assert ro.is_bad_key(KID, BAD)
    assert ro.get_key(KID, "SVC") is None  # the lookup still skips the flagged pair
    assert list(ro.get_keys("SVC")) == [(KID.hex, BAD)]  # but the row itself is untouched


def test_a_sibling_verdict_during_the_decrypt_still_triggers_the_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two tracks share a KID and decrypt at the same time; the first verdict pops the shared
    source, so the second track must read the flag instead of returning unchecked."""
    local = SQLite("local", tmp_path / "local.db")
    vaults = Vaults("SVC")
    vaults.vaults = [local]
    vaults.sources[KID] = (BAD, local)
    cmd = make_cmd(monkeypatch, vaults)
    dl.LICENSE_KEY_CACHE[KID] = GOOD  # the sibling's retry already licensed the good key

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
        licences.append(dl.LICENSE_KEY_CACHE.get(KID))
        drm.content_keys[KID] = dl.LICENSE_KEY_CACHE[KID]

    cmd.decrypt_verified(drm, path, licence, KID)

    assert path.read_bytes() == b"plain:" + GOOD.encode()
    assert drm.decrypted_with == [{KID: BAD}, {KID: GOOD}]
    assert licences == [GOOD]  # the good key in the run cache survives the flag of the bad one
