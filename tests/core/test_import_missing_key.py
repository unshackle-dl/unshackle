"""An import stops with a clear error when neither the export nor a vault has a content key.

An import never licenses through a CDM. When dl asks it for a licence, it names the KIDs it
has no content key for, and dl shows that message without a traceback, also when the retry
after a failed vault key asks for the licence.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import click
import pytest
from pywidevine.pssh import PSSH
from rich.table import Table

from unshackle.commands import dl as dl_module
from unshackle.commands.dl import dl
from unshackle.core.drm import Widevine
from unshackle.core.import_service import ImportService
from unshackle.core.vaults import Vaults
from unshackle.vaults.SQLite import SQLite

KID = UUID("11111111-2222-3333-4444-555555555555")
OTHER = UUID("66666666-7777-8888-9999-000000000000")
BAD = "aa" * 16
OTHER_KEY = "cc" * 16


class Remote(SQLite):
    """A second SQLite file standing in for a remote vault."""

    local = False


class FakeDRM:
    """A track DRM with two KIDs; decrypting records the content keys it had."""

    def __init__(self, keys: dict[UUID, str]) -> None:
        self.kids = [KID, OTHER]
        self.content_keys = dict(keys)
        self.decrypted_with: list[dict[UUID, str]] = []

    def decrypt(self, path: Path) -> None:
        self.decrypted_with.append(dict(self.content_keys))
        out = path.with_stem(path.stem + "_decrypted")
        out.write_bytes(b"plain:" + self.content_keys.get(KID, "none").encode())
        path.unlink()
        out.rename(path)


class EmptyVaults:
    sources: dict[UUID, Any] = {}

    def get_key(self, kid: UUID) -> tuple[None, None]:
        return None, None


def make_cmd(monkeypatch: pytest.MonkeyPatch, vaults: Any) -> dl:
    """A dl in server-CDM mode, the mode an import runs in, with a decoder that fails every file."""
    monkeypatch.setattr(dl_module.binaries, "FFMPEG", "ffmpeg")
    monkeypatch.setattr(dl_module, "ffmpeg_decodes", lambda path, start=None, seconds=3, video=None: False)
    cmd = dl.__new__(dl)
    cmd.log = SimpleNamespace(warning=print, info=print, debug=print, error=print)
    cmd.service = "EXAMPLE"
    cmd.vaults = vaults
    cmd.vault_cache_tally = None
    cmd._remote_service = SimpleNamespace(_server_cdm=True, _server_cdm_type="widevine")
    dl.LICENSE_KEY_CACHE.clear()
    dl.DRM_LOCKS.clear()
    return cmd


def import_licence(track: Any, calls: list[str]) -> Any:
    """The licence dl binds for an import: service_licence calls the ImportService method."""

    def licence(**kwargs: Any) -> None:
        calls.append("licence")
        raise ImportService.missing_key(track)

    return licence


def prepare_drm_for(cmd: dl, track: Any, calls: list[str]) -> Any:
    table = Table.grid()
    table.add_row("")
    return partial(
        cmd.prepare_drm,
        track=track,
        title=None,
        licence=import_licence(track, calls),
        certificate=None,
        table=table,
    )


def test_the_error_names_the_kids_without_a_content_key() -> None:
    drm = Widevine(pssh=PSSH.new(system_id=PSSH.SystemId.Widevine, key_ids=[KID, OTHER]))
    drm.content_keys[OTHER] = OTHER_KEY

    error = ImportService.missing_key(SimpleNamespace(drm=[drm]))

    assert isinstance(error, click.ClickException)
    assert KID.hex in error.message and OTHER.hex not in error.message
    assert "no vault has it" in error.message


def test_the_server_cdm_branch_lets_the_import_error_through(monkeypatch: pytest.MonkeyPatch) -> None:
    drm = FakeDRM({})
    track = SimpleNamespace(drm=[drm])
    cmd = make_cmd(monkeypatch, EmptyVaults())

    with pytest.raises(click.ClickException, match=KID.hex):
        prepare_drm_for(cmd, track, [])(drm, track_kid=None)


def test_the_retry_after_a_bad_vault_key_ends_in_the_import_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The track still holds another content key and has no track KID, so only the request for
    the dropped KID reaches the licence. It must stop there, once, not decrypt without the key."""
    poisoned = Remote("poisoned", tmp_path / "poisoned.db")
    poisoned.add_key("SVC", KID, BAD)
    vaults = Vaults("SVC")
    vaults.vaults = [SQLite("local", tmp_path / "local.db"), poisoned]
    assert vaults.get_key(KID) == (BAD, poisoned)
    cmd = make_cmd(monkeypatch, vaults)
    drm = FakeDRM({KID: BAD, OTHER: OTHER_KEY})
    track = SimpleNamespace(drm=[drm])
    calls: list[str] = []
    path = tmp_path / "video.mp4"
    path.write_bytes(b"cipher")

    with pytest.raises(click.ClickException, match=KID.hex):
        cmd.decrypt_verified(drm, path, prepare_drm_for(cmd, track, calls), None)
    cmd.wait_vault_writes()

    assert calls == ["licence"]
    assert drm.decrypted_with == [{KID: BAD, OTHER: OTHER_KEY}]
    assert not (tmp_path / "video.mp4.enc").exists()


def test_a_download_stopped_by_a_click_exception_reports_only_its_message() -> None:
    error = ImportService.missing_key(SimpleNamespace(drm=[FakeDRM({})]))

    lines = dl.failure_lines(error)

    assert lines == [":x: Download Failed...", f"   {error.format_message()}"]
    assert "unexpected error" in dl.failure_lines(ValueError("boom"))[-1]
