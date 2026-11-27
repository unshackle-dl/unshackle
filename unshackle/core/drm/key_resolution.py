"""Content-key resolution: how a track's content keys are found, checked and stored."""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from collections.abc import Callable
from concurrent import futures
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from functools import partial
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import UUID

import click
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from unshackle.core import binaries
from unshackle.core.cdm.detect import cdm_system, is_playready_cdm
from unshackle.core.console import console
from unshackle.core.constants import DOWNLOAD_CANCELLED, AnyTrack, DownloadCancelled
from unshackle.core.drm import (
    DRM_T,
    ClearKey,
    ClearKeyCENC,
    ClearKeyCTR,
    MonaLisa,
    PlayReady,
    Widevine,
    drop_null_keys,
    is_null_key,
    real_kids,
    verify,
)
from unshackle.core.title_source import TitleSource
from unshackle.core.titles import Title_T
from unshackle.core.tracks import Video
from unshackle.core.utilities import declared_kwargs, log_event
from unshackle.core.utils.redact import redact_secrets
from unshackle.core.utils.subprocess import ffmpeg_decodes
from unshackle.core.vault import Vault
from unshackle.core.vaults import Vaults

if TYPE_CHECKING:
    from unshackle.core.title_source import ServerKeySource
    from unshackle.core.utilities import DebugLogger


def truncate_pssh_for_display(pssh_string: str, drm_type: str) -> str:
    """Truncate PSSH string for display when not in debug mode."""
    if logging.root.level == logging.DEBUG or not pssh_string:
        return pssh_string

    max_width = console.width - len(drm_type) - 12
    if len(pssh_string) <= max_width:
        return pssh_string

    return pssh_string[: max_width - 3] + "..."


class CdmChooser(Protocol):
    """The CDM a run licenses with, which resolution can switch, and a chooser for one DRM system."""

    cdm: Any

    def cdm_for(self, system: str, quality: int | None = None) -> object | None: ...


class ContentKeyResolver:
    """Content-key resolution for one dl run.

    The order of key sources is: the keys a server key source gave, the keys this run already
    holds, the key vaults, then a licence from the CDM. The interface is ``take_server_keys``,
    ``licence_for``, ``decrypt_verified`` and ``wait_vault_writes``. The locks, the key
    cache and the vault writer are class-level, so every run in one process shares them.
    """

    DRM_LOCKS: dict[str, Lock] = {}
    DRM_LOCKS_GUARD = Lock()
    LICENCE_LOCK = Lock()
    VAULT_WRITER = ThreadPoolExecutor(1, thread_name_prefix="vault-writer")
    LICENSE_KEY_CACHE: dict[UUID, str] = {}
    ALL_DRM_LOCK = Lock()

    def __init__(
        self,
        vaults: Vaults,
        cdms: CdmChooser,
        service: TitleSource,
        tag: str,
        source: ServerKeySource | None = None,
        cdm_only: bool = False,
        vaults_only: bool = False,
        best_available: bool = False,
        export: Callable[[Title_T, AnyTrack, Any], None] | None = None,
        debug_logger: DebugLogger | None = None,
    ) -> None:
        self.log = logging.getLogger("download")
        self.vaults = vaults
        self.cdms = cdms
        self.service = service
        self.tag = tag
        self.source = source
        self.cdm_only = cdm_only
        self.vaults_only = vaults_only
        self.best_available = best_available
        self.export = export
        self.debug_logger = debug_logger
        self.vault_cache_tally: tuple[set[UUID], int] | None = None
        self.all_drm_attempted: defaultdict[type, set[UUID]] = defaultdict(set)
        self.all_drm_warned: set[str] = set()

    def licence_for(self, track: AnyTrack, title: Title_T, table: Table, all_drm: bool = False) -> partial:
        """The licence callback for ``track``: call it as ``fn(drm, track_kid=None)``.

        It gives the DRM its content keys and adds the key tree to ``table``. With ``all_drm`` it
        licenses each DRM system the track offers.
        """
        prepare = partial(self._prepare_drm, track=track, title=title, table=table, cdm_only=self.cdm_only)
        if all_drm:
            return partial(self._licence_all_drm, prepare, track, self.best_available, force=self.cdm_only)
        return prepare

    def decrypt_verified(
        self,
        drm: DRM_T,
        path: Path,
        licence: Callable | None = None,
        track_kid: UUID | None = None,
        decrypt: bool = True,
        track: AnyTrack | None = None,
    ) -> None:
        """Decrypt the file, and when a vault supplied a content key, prove the output decodes.

        A poisoned vault returns the right KID with a wrong content key, and the decrypters
        accept it without complaint. A content key from a vault is therefore trusted, and only
        then copied to the other vaults, once FFmpeg can decode the result. Without FFmpeg
        neither happens. For a fragmented MP4, ``verify.kid_windows`` reads from the ciphertext
        which fragments each KID encrypts, and FFmpeg decodes windows inside each KID's
        fragments, so only a KID that fails is flagged. A complete map does not judge a KID it does
        not hold, so a clear file judges and copies no KID. A KID that is not in an incomplete map,
        and every KID of any other file, gets a start and end check of the whole file, and a
        failure there flags each of those KIDs. On failure the pair is flagged in the local
        vaults, the ciphertext is restored, and the licence runs again; ``Vaults`` tells the
        vault that served it. Every vault skips a flagged pair, so each pass burns at most one
        vault; when the vaults run out the CDM answers, and a CDM key is not checked. A CDM
        that returns the flagged key clears the flag and stores the content key again, because
        then the decode check was wrong.

        With ``decrypt=False`` the downloader already decrypted the segments in place, so a
        failure can only flag and raise; the next run skips the flagged pair.

        After a decrypt that passes, the server key source's vault gets the proven content keys of
        ``track``: each licence key, and each vault key the decode check passed.

        Before the decrypt, each KID the DRM names without a content key is licensed when the file
        uses it, or when the file cannot be mapped, because ``_prepare_drm`` licensed only for the track's KID.
        """
        keys: Any = getattr(drm, "content_keys", None)
        if keys is None:
            if decrypt:
                drm.decrypt(path)
            return

        def flagged_kids() -> list[UUID]:
            """The KIDs on this track whose content key another track already proved wrong."""
            return [kid for kid, key in keys.items() if (kid, key) in self.vaults.flagged]

        def drop(kid: UUID) -> str:
            key = keys.pop(kid)
            if self.LICENSE_KEY_CACHE.get(kid) == key:
                self.LICENSE_KEY_CACHE.pop(kid)
            return key

        def relicense(bad: dict[UUID, str], kids: dict[UUID, Vault]) -> None:
            """Licence again for each dropped KID, then settle each dropped pair against the new key.

            _prepare_drm licenses only for a KID it is told to need, so without the second call a
            track that still holds other content keys would decrypt without the dropped one. A
            licence can replace the DRM's dict, so ``keys`` is read from the DRM again afterwards.
            A licence key equal to the dropped one proves the decode check wrong and clears its flag.
            """
            nonlocal keys
            if not licence:
                return
            licence(drm, track_kid=track_kid)
            for kid in sorted(bad):
                if kid != track_kid and kid not in getattr(drm, "content_keys", {}):
                    licence(drm, track_kid=kid)
            keys = getattr(drm, "content_keys", keys)
            for kid, key in bad.items():
                new = keys.get(kid)
                if new is None:
                    continue
                source = self.vaults.sources.get(kid)
                if source is None:
                    if new == key:
                        self.vaults.unflag_bad_key(kid, key)
                        self._flush_vault_writes([partial(self.vaults.add_key, kid, key)])
                    elif not self.vaults.held_locally(kid, new):
                        self._flush_vault_writes([partial(self.vaults.add_key, kid, new)])
                elif new == key and kid in kids and source[1] is kids[kid]:
                    raise ValueError(f"{key} from {kids[kid].name} was bad and no other source has the key")

        known_bad = flagged_kids()
        if known_bad and licence:
            relicense({kid: drop(kid) for kid in known_bad}, {})

        unkeyed = [kid for kid in getattr(drm, "kids", None) or [] if kid not in keys]
        mapped = bool(unkeyed and decrypt and licence)
        kid_map = verify.kid_windows(path) if mapped else None
        if mapped and licence:
            for kid in unkeyed:
                if kid_map is None or kid in kid_map.windows:
                    licence(drm, track_kid=kid)
            keys = getattr(drm, "content_keys", keys)

        def vault_kids() -> dict[UUID, Vault]:
            """The KIDs whose key on the track came from a vault, with that vault."""
            found = {kid: self.vaults.sources.get(kid) for kid in keys}
            return {kid: src[1] for kid, src in found.items() if src and src[0] == keys[kid]}

        backup = path.with_name(path.name + ".enc")
        backup.unlink(missing_ok=True)  # a killed run leaves one behind; its bytes may not match
        checking = bool(vault_kids() and decrypt and binaries.FFMPEG)
        if checking:
            try:
                os.link(path, backup)
            except OSError as e:
                self.log.debug(f"Cannot keep the ciphertext for a decrypt retry: {e!r}")
        kid_map = (kid_map if mapped else verify.kid_windows(path)) if checking else None
        windows = kid_map.windows if kid_map else {}
        video = kid_map.video if kid_map else None

        def proven(passed: dict[UUID, Vault]) -> dict[UUID | str, str]:
            """The licence keys and the vault keys in ``passed``, without null keys; a flagged pair never gets here."""
            sourced = vault_kids()
            return {
                kid: key for kid, key in keys.items() if (kid in passed or kid not in sourced) and not is_null_key(key)
            }

        def window_decodes(start: float, end: float) -> bool:
            return ffmpeg_decodes(path, start=start, seconds=end - start if end > start else 0.5, video=video)

        def judged_kids() -> dict[UUID, Vault]:
            """The vault KIDs this file can judge. A map whose KIDs all have keys skips the KIDs it lacks."""
            kids = vault_kids()
            if kid_map is not None and all(kid in keys for kid in windows):
                return {kid: vault for kid, vault in kids.items() if kid in windows}
            return kids

        def failed_kids(kids: dict[UUID, Vault]) -> set[UUID]:
            """The vault KIDs whose content key did not decode.

            A KID in the KID map fails when one of its windows fails. The KIDs that are not in the
            map share the verdict of one start and end check of the whole file, taken only when
            every mapped KID passed: a wrong mapped key fails that check too and would flag them
            for noise that is not theirs. The retry judges them again with the replaced key.
            """
            failed = {kid for kid in kids if kid in windows and not all(window_decodes(*w) for w in windows[kid])}
            absent = {kid for kid in kids if kid not in windows}
            if absent and not failed and not ffmpeg_decodes(path, video=video):
                failed |= absent
            self.log.debug(
                f"Key check on {path.name}: {len(kids) - len(absent)} KID(s) by fragment window, "
                f"{len(absent)} by whole-file check, failed {sorted(k.hex for k in failed)}"
            )
            return failed

        def passes_left(done: int) -> bool:
            """One pass per source that can still answer: every loaded vault, the server's vaults as
            one source that walks them itself over --remote, and one for the CDM."""
            remote = 1 if self.source and self.source.key_arrival.needs_no_cdm else 0
            return done < len(self.vaults) + remote + 1

        try:
            done = 0
            while passes_left(done):
                done += 1
                if decrypt:
                    drm.decrypt(path)
                kids = judged_kids()
                stale = flagged_kids()
                failed: set[UUID] = set()
                if not stale:
                    checked = bool(kids and binaries.FFMPEG and path.exists())
                    failed = failed_kids(kids) if checked else set()
                    if not failed:
                        if checked:
                            self._flush_vault_writes(
                                [
                                    partial(self.vaults.add_key, kid, keys[kid], excluding=vault)
                                    for kid, vault in kids.items()
                                ]
                            )
                        self.vaults.report_replaced(keys)
                        server_vault = self.source.server_vault if self.source and track is not None else None
                        if server_vault is not None:
                            self._flush_vault_writes(
                                [partial(server_vault.add_keys, self.tag, proven(kids if checked else {}), track)]
                            )
                        return
                bad = {kid: drop(kid) for kid in {*failed, *stale}}
                for kid, key in bad.items():
                    if kid not in kids:
                        continue
                    self.log.warning(f"{key} from {kids[kid].name} was bad, trying other vaults")
                    self.vaults.flag_bad_key(kid, key)
                if not backup.exists() or not licence:
                    raise ValueError("The content key from the vault did not decrypt the track; run again")
                path.unlink()
                os.link(backup, path)
                relicense(bad, kids)
            raise ValueError("No vault or CDM produced a content key that decrypts the track")
        finally:
            backup.unlink(missing_ok=True)

    def _flush_vault_writes(self, pending: list[Callable[[], Any]]) -> None:
        """Hand the queued vault writes to the background writer so the track does not wait on them."""
        for write in pending:
            self.VAULT_WRITER.submit(write).add_done_callback(self._report_vault_write)
        pending.clear()

    def _report_vault_write(self, future: futures.Future[Any]) -> None:
        if exc := future.exception():
            self.log.error(f"Vault write failed: {exc!r}")

    def wait_vault_writes(self) -> None:
        """Block until every queued vault write has run, then log the total once."""
        self.VAULT_WRITER.submit(lambda: None).result()
        if not (self.vault_cache_tally and self.vault_cache_tally[0]):
            self.vault_cache_tally = None
            return
        kids, successful_caches = self.vault_cache_tally
        keys = len(kids)
        self.vault_cache_tally = None
        self.log.info(f"Cached {keys} Key{'' if keys == 1 else 's'} to {successful_caches}/{len(self.vaults)} Vaults")

    def take_server_keys(self, title: Title_T) -> None:
        """Put the content keys of the server key source on the tracks of ``title``, drop the null keys, then cache the rest."""
        source = self.source
        if source is None:
            return
        source.resolve_server_keys(title)
        for track in title.tracks:
            for drm in track.drm or []:
                drop_null_keys(drm)
        if not self.cdm_only:
            self._prefer_vault_keys(source, title)
        self._cache_resolved_keys(source, title)

    def _prefer_vault_keys(self, source: ServerKeySource, title: Title_T) -> None:
        """Let the client vaults answer before a content key the server batch licence returned.

        The client only learns the KIDs from the batch response, so the server has already
        answered. A vault key still goes first, so ``decrypt_verified`` proves it and flags a
        poisoned row; the server key waits as the next candidate. A server CDM key is trusted
        like a local CDM key: it goes to the run cache and the vaults, and a retry after a
        flagged vault key takes it before any vault. A server vault key is unproven, so it
        waits in ``Vaults.candidates`` and gets the same check when its turn comes. Only the KIDs
        the DRM names are looked up, each once, so sibling keys add no remote vault requests.
        """
        looked_up: dict[UUID, tuple[str | None, Any]] = {}
        for track in title.tracks:
            for drm in getattr(track, "drm", None) or []:
                content_keys = getattr(drm, "content_keys", {})
                named = set(real_kids(drm) or content_keys)
                for kid, server_key in list(content_keys.items()):
                    if kid not in named:
                        continue
                    if kid not in looked_up:
                        looked_up[kid] = self.vaults.get_key(kid)
                    vault_key, vault = looked_up[kid]
                    if not vault_key or vault_key == server_key:
                        continue
                    drm.content_keys[kid] = vault_key
                    server_vault = source.vault_source(kid, server_key)
                    if server_vault is not None:
                        self.vaults.candidates.setdefault(kid, []).append((server_key, server_vault))
                    elif kid not in self.LICENSE_KEY_CACHE:
                        self.LICENSE_KEY_CACHE[kid] = server_key
                        self._flush_vault_writes([partial(self._cache_keys_to_vaults, {kid: server_key})])

    def _cache_resolved_keys(self, source: ServerKeySource, title: Title_T) -> None:
        """Cache the keys a batch licence or an import put on the tracks.

        Both fill track.drm before any _prepare_drm call, so this is the only point where those
        keys can still reach the local vaults. A content key a local vault already holds is not pushed again.
        """
        resolved = {
            kid: key
            for track in title.tracks
            for drm in (getattr(track, "drm", None) or [])
            for kid, key in getattr(drm, "content_keys", {}).items()
            if kid not in self.LICENSE_KEY_CACHE
            and source.vault_source(kid, key) is None
            and self.vaults.sources.get(kid, (None,))[0] != key
        }
        keys = {kid: key for kid, key in resolved.items() if not self.vaults.held_locally(kid, key)}
        if keys:
            self.LICENSE_KEY_CACHE.update(keys)
            self._flush_vault_writes([partial(self._cache_keys_to_vaults, keys)])

    def _cache_keys_to_vaults(self, content_keys: dict[UUID, str]) -> None:
        """Store licence keys in every vault. A licence key clears a flag on its pair first: the
        flag came from a decode check that was wrong, and a local vault refuses a flagged pair."""
        if not content_keys or not self.vaults:
            return
        self.vaults.clear_licensed_flags(content_keys)
        successful_caches = self.vaults.add_keys(cast(dict[UUID | str, str], content_keys))
        kids, caches = self.vault_cache_tally or (set(), successful_caches)
        self.vault_cache_tally = (kids | content_keys.keys(), min(caches, successful_caches))

    @classmethod
    def drm_lock(cls, drm: DRM_T) -> Lock:
        """Return the lock for one DRM's set of content keys.

        Tracks that share KIDs still take turns, so the second one finds the content keys in
        LICENSE_KEY_CACHE and skips the challenge. Tracks with different KIDs query the key vaults
        at the same time; their licences take turns under ``_take_licence_turn``.

        Callers take the lock after get_drm_for_cdm(), which folds a track's sibling PlayReady
        objects into the returned one, so the KID set is complete for that track's DRM list
        before the lock is chosen. This holds because every manifest parser gives the tracks
        that share a PlayReady object the same DRM list (ISM attaches one list to all tracks;
        DASH and HLS build a list per track). Two tracks that share one object but list
        different siblings would get different locks.
        """
        kids = getattr(drm, "kids", None) or []
        key = ",".join(sorted(getattr(k, "hex", str(k)) for k in kids)) or getattr(drm, "content_id", None)
        with cls.DRM_LOCKS_GUARD:
            return cls.DRM_LOCKS.setdefault(key or type(drm).__name__, Lock())

    def _take_licence_turn(
        self,
        turn: ExitStack,
        drm: ClearKeyCENC | Widevine | PlayReady | MonaLisa,
        kids: list[UUID],
        track: AnyTrack,
        track_kid: UUID | None,
        cek_tree: Tree | None,
    ) -> None:
        """Hold LICENCE_LOCK until ``turn`` closes, then take the content keys of ``kids`` an earlier licence cached.

        One licence can carry the keys of a DRM with other KIDs, so licences take turns. The caller
        holds its ``drm_lock`` first, so the vault lookups before this still run at the same time.
        """
        turn.enter_context(self.LICENCE_LOCK)
        if DOWNLOAD_CANCELLED.is_set():
            raise DownloadCancelled()
        for kid in kids:
            if kid not in drm.content_keys:
                self._take_cached_key(drm, kid, track, track_kid, cek_tree)

    def _take_cached_key(
        self,
        drm: ClearKeyCENC | Widevine | PlayReady | MonaLisa,
        kid: UUID,
        track: AnyTrack,
        track_kid: UUID | None,
        cek_tree: Tree | None,
    ) -> bool:
        """Give the DRM the content key LICENSE_KEY_CACHE holds for ``kid``, and return whether it held one."""
        key = self.LICENSE_KEY_CACHE.get(kid)
        if not key:
            return False
        drm.content_keys[kid] = key
        if cek_tree is not None:
            self._add_key_label(cek_tree, kid, key, track_kid, "from cache")
        log_event(
            "license_cache_hit",
            level="INFO",
            service=self.tag,
            context={"kid": kid.hex, "content_key": key, "track": str(track), "drm_type": type(drm).__name__},
        )
        return True

    def _take_vault_key(
        self,
        drm: ClearKeyCENC | Widevine | PlayReady | MonaLisa,
        kid: UUID,
        track: AnyTrack,
        track_kid: UUID | None,
        cek_tree: Tree | None,
    ) -> bool:
        """Give the DRM the content key a key vault holds for ``kid``, cache it for the run, and return whether one did."""
        key, vault = self.vaults.get_key(kid)
        if not key:
            return False
        drm.content_keys[kid] = key
        self.LICENSE_KEY_CACHE[kid] = key
        if cek_tree is not None:
            self._add_key_label(cek_tree, kid, key, track_kid, f"from {vault}")
        if self.debug_logger:
            self.debug_logger.log_vault_query(
                vault_name=str(vault),
                operation="get_key_success",
                service=self.tag,
                context={
                    "kid": kid.hex,
                    "content_key": key,
                    "track": str(track),
                    "from_cache": True,
                    "drm_type": type(drm).__name__,
                },
            )
        return True

    @staticmethod
    def _with_track_kid(drm: Any, track_kid: UUID | None) -> list[UUID]:
        """Return the KIDs the DRM names, plus the track's own KID when the DRM leaves it out."""
        kids = list(getattr(drm, "kids", None) or [])
        if track_kid and track_kid not in kids:
            kids.append(track_kid)
        return kids

    def _take_known_keys(
        self,
        drm: ClearKeyCENC | Widevine | PlayReady | MonaLisa,
        kids: list[UUID],
        track: AnyTrack,
        track_kid: UUID | None,
        cek_tree: Tree | None,
        use_cache: bool = True,
        use_vaults: bool = True,
        stop_at_miss: bool = False,
    ) -> list[UUID]:
        """Give the DRM the content keys of ``kids`` the run cache or a key vault holds, and return the KIDs still missing.

        ``stop_at_miss`` returns at the first miss, so no more vaults are asked for a run that fails anyway.
        """
        missing = []
        for kid in kids:
            if kid in drm.content_keys:
                if cek_tree is not None:
                    self._add_key_label(cek_tree, kid, drm.content_keys[kid], track_kid)
                continue
            if use_cache and self._take_cached_key(drm, kid, track, track_kid, cek_tree):
                continue
            if not (use_vaults and self._take_vault_key(drm, kid, track, track_kid, cek_tree)):
                missing.append(kid)
                if stop_at_miss:
                    break
        return missing

    def _license_drm(
        self,
        drm: ClearKeyCENC | Widevine | PlayReady,
        get_keys: Callable[[], None],
        track: AnyTrack,
        title: Title_T,
        track_kid: UUID | None,
        table: Table,
        cdm_only: bool,
        force: bool,
    ) -> None:
        """Give a Widevine, PlayReady or ClearKey DRM its content keys: run cache, then key vaults, then ``get_keys``.

        A held key for ``track_kid`` skips the licence, as the file is encrypted with that KID;
        ``decrypt_verified`` licenses any other KID the file turns out to use.
        ``force`` skips the run cache, so the licence runs again for each KID no vault answers.
        A licence can answer a KID with a blank or null key; a vault key for that KID wins.
        Every content key of the licence goes to the vaults and to the server key source's vault at once.
        """
        pssh: str | None = None
        if isinstance(drm, ClearKeyCENC):
            name = "ClearKey"
        else:
            name = type(drm).__name__
            pssh = drm.pssh.dumps() if isinstance(drm, Widevine) else drm.pssh_b64 or ""
        if pssh is not None and self.debug_logger:
            self.debug_logger.log_drm_operation(
                drm_type=name,
                operation="prepare_drm",
                service=self.tag,
                context={
                    "track": str(track),
                    "title": str(title),
                    "pssh": pssh,
                    "kids": [k.hex for k in drm.kids],
                    "track_kid": track_kid.hex if track_kid else None,
                },
            )
        pending_vault_writes: list[Callable[[], Any]] = []
        with self.drm_lock(drm), ExitStack() as licence_turn:
            label = [(f"({truncate_pssh_for_display(pssh, name)})", "text")] if pssh is not None else []
            cek_tree = Tree(Text.assemble((name, "cyan"), *label, overflow="fold"))
            pre_existing_tree = next(
                (x for x in table.columns[0].cells if isinstance(x, Tree) and x.label == cek_tree.label), None
            )
            if pre_existing_tree:
                cek_tree = pre_existing_tree

            def show_error(msg: str) -> None:
                cek_tree.add(f"[logging.level.error]{msg}")
                if not pre_existing_tree:
                    table.add_row(cek_tree)
                self._flush_vault_writes(pending_vault_writes)

            all_kids = self._with_track_kid(drm, track_kid)
            stop_at_miss = self.vaults_only and not cdm_only
            missing = self._take_known_keys(
                drm, all_kids, track, track_kid, cek_tree, not force, not cdm_only, stop_at_miss
            )
            if missing and stop_at_miss:
                msg = f"No Vault has a Key for {missing[0].hex} and --vaults-only was used"
                log_event(
                    "vault_key_not_found",
                    level="ERROR",
                    service=self.tag,
                    message=msg,
                    context={"kid": missing[0].hex, "track": str(track), "drm_type": type(drm).__name__},
                )
                show_error(msg)
                raise drm.Exceptions.CEKNotFound(msg)

            need_license = bool(missing) and (force or not track_kid or track_kid not in drm.content_keys)
            if need_license:
                # force licenses again, so the turn takes no cached keys
                self._take_licence_turn(licence_turn, drm, [] if force else all_kids, track, track_kid, cek_tree)
                need_license = not all(kid in drm.content_keys for kid in all_kids)

            if need_license and not self.vaults_only:
                from_vaults = drm.content_keys.copy()
                log_event(
                    "get_license",
                    level="INFO",
                    service=self.tag,
                    message=f"Requesting {name} license from service",
                    context={
                        "track": str(track),
                        "drm_type": type(drm).__name__,
                        "kids_needed": [k.hex for k in all_kids if k not in drm.content_keys],
                    },
                )
                try:
                    get_keys()
                except Exception as e:
                    if drm.content_keys and (not track_kid or track_kid in drm.content_keys):
                        self.log.warning(
                            f"Licence failed, using the {len(drm.content_keys)} known key(s): {redact_secrets(str(e))}"
                        )
                    else:
                        if isinstance(
                            e, (drm.Exceptions.EmptyLicense, drm.Exceptions.CEKNotFound, click.ClickException)
                        ):
                            msg = str(e)
                        else:
                            msg = f"An exception occurred in the Service's license function: {e}"
                        if self.debug_logger:
                            self.debug_logger.log_error(
                                "get_license" if name == "Widevine" else f"get_license_{name.lower()}",
                                e,
                                service=self.tag,
                                context={
                                    "track": str(track),
                                    "exception_type": type(e).__name__,
                                    "drm_type": type(drm).__name__,
                                },
                            )
                        show_error(msg)
                        raise
                else:
                    log_event(
                        "license_keys_retrieved",
                        level="INFO",
                        service=self.tag,
                        context={
                            "track": str(track),
                            "drm_type": type(drm).__name__,
                            "keys_count": len(drm.content_keys),
                            "kids": [k.hex for k in drm.content_keys.keys()],
                        },
                    )

                for kid, key in drm.content_keys.items():
                    self._add_key_label(cek_tree, kid, f"[red]{key}[/]" if is_null_key(key) else key, track_kid)
                drop_null_keys(drm)
                drm.content_keys.update(from_vaults)
                self.LICENSE_KEY_CACHE.update(drm.content_keys)
                licensed = {k: v for k, v in drm.content_keys.items() if k not in from_vaults}
                pending_vault_writes.append(partial(self._cache_keys_to_vaults, licensed))
                server_vault = self.source.server_vault if self.source else None
                if server_vault is not None:
                    pending_vault_writes.append(
                        partial(server_vault.add_keys, self.tag, cast(dict[UUID | str, str], licensed), track)
                    )

            if track_kid and track_kid not in drm.content_keys:
                msg = f"No Content Key for KID {track_kid.hex} was returned in the License"
                show_error(msg)
                raise drm.Exceptions.CEKNotFound(msg)

            if cek_tree.children and not pre_existing_tree:
                table.add_row()
                table.add_row(cek_tree)

        self._flush_vault_writes(pending_vault_writes)

    @staticmethod
    def _add_key_label(tree: Tree, kid: UUID, key: str, track_kid: UUID | None = None, source: str = "") -> None:
        """Add ``kid:key`` to a content key tree once, with ``*`` on the track's own KID and ``source`` after it."""
        if any(f"{kid.hex}:{key}" in str(x.label) for x in tree.children):
            return
        label = f"[text2]{kid.hex}:{key}"
        if kid == track_kid:
            label += "*"
        if source:
            label += f" {source}"
        tree.add(label)

    def _service_licence(self, drm_system: str | None = None, **kwargs: Any) -> Any:
        """Call the service's licence function for the track's DRM system.

        Arguments go through declared_kwargs, so a service receives only the ones its own
        licence function declares.

        _prepare_drm passes drm_system so the choice follows the track, not the loaded CDM:
        a track downloading at the same time can swap self.cdms.cdm.
        """
        if drm_system is None:
            drm_system = cdm_system(self.cdms.cdm) or "widevine"
        fn = self.service.get_playready_license if drm_system == "playready" else self.service.get_widevine_license
        return fn(**declared_kwargs(fn, kwargs))

    def _licence_one_drm(self, prepare: Callable, drm: Widevine | PlayReady, track_kid: UUID | None) -> None:
        """Send one challenge for the KIDs of this DRM, also when the DRM or a vault has the content keys.

        This is the ``--cdm-only`` path. The challenge goes out the first time a run sees the KIDs
        for the DRM system.

        _prepare_drm asks the CDM only for a KID with no content key, so the content keys the DRM
        holds are removed for the call and put back after it.
        """
        kids = {*drm.kids, *([track_kid] if track_kid else [])}
        attempted = self.all_drm_attempted[type(drm)]
        if kids <= attempted:
            prepare(drm, track_kid=track_kid)
            return
        with self.drm_lock(drm):
            held = dict(drm.content_keys)
            drm.content_keys.clear()
        try:
            prepare(drm, track_kid=track_kid, cdm_only=True, force=True)
        finally:
            attempted |= kids
            with self.drm_lock(drm):
                for held_kid, key in held.items():
                    drm.content_keys.setdefault(held_kid, key)

    def _licence_all_drm(
        self,
        prepare: Callable,
        track: AnyTrack,
        tolerate: bool,
        drm: DRM_T,
        track_kid: UUID | None = None,
        force: bool = False,
    ) -> None:
        """License the track with each DRM system it offers, not only the one the downloader selected.

        A DRM system sends a challenge only for a KID that no vault and no earlier track gave a
        content key for. With ``force``, each DRM system sends one challenge for a set of KIDs in
        a run. A DRM system that fails stops the title. With ``tolerate`` it is a warning, if the
        other DRM system gave the content key of the track.
        """
        if not isinstance(drm, (Widevine, PlayReady)):
            prepare(drm, track_kid=track_kid)
            return

        other_type = PlayReady if type(drm) is Widevine else Widevine
        others = [d for d in track.drm or [] if isinstance(d, (Widevine, PlayReady)) and type(d) is other_type]
        if other_type is PlayReady and len(others) > 1:
            playready = [d for d in others if isinstance(d, PlayReady)]
            playready[0].absorb(*playready[1:])
        if not others and other_type.__name__ not in self.all_drm_warned:
            self.all_drm_warned.add(other_type.__name__)
            self.log.warning(
                f"Track {track} has no {other_type.__name__} DRM, licensing with {type(drm).__name__} only"
            )

        targets = [drm, *others[:1]]
        error: Exception | None = None
        with self.ALL_DRM_LOCK:
            loaded_cdm = self.cdms.cdm
            try:
                for target in targets:
                    kid = track_kid if target is drm or track_kid in target.kids else None
                    try:
                        if force:
                            self._licence_one_drm(prepare, target, kid)
                        else:
                            prepare(target, track_kid=kid)
                    except Exception as e:
                        if not tolerate:
                            raise
                        self.log.warning(f"{type(target).__name__} licence failed for {track}: {e}")
                        error = error or e

                keys = {kid: key for target in targets for kid, key in target.content_keys.items()}
                for target in targets:
                    with self.drm_lock(target):
                        for kid, key in keys.items():
                            target.content_keys.setdefault(kid, key)
            finally:
                self.cdms.cdm = loaded_cdm

        track_keys = getattr(drm, "content_keys", {})
        if error and (not track_keys or (track_kid and track_kid not in track_keys)):
            raise error

    def _prepare_drm(
        self,
        drm: ClearKeyCENC | Widevine | PlayReady | MonaLisa,
        track: AnyTrack,
        title: Title_T,
        table: Table,
        track_kid: UUID | None = None,
        cdm_only: bool = False,
        force: bool = False,
    ) -> None:
        """
        Prepare the DRM by getting decryption data like KIDs, Keys, and such.
        The DRM object should be ready for decryption once this function ends.
        ``force`` skips the content keys of earlier tracks in this run. The CDM then licenses
        again for each KID the DRM holds no content key for.
        """
        if isinstance(drm, ClearKeyCTR):
            if drm.key is None:
                with self.LICENCE_LOCK:
                    if DOWNLOAD_CANCELLED.is_set():
                        raise DownloadCancelled()
                    if self.source is None:
                        drm.content_key()
                    else:
                        drm.take_key(self.source.license_file(track))
            return
        if not drm:
            return
        remote = self.source
        server_cdm = bool(remote and remote.key_arrival.needs_no_cdm)
        licensed_locally = bool(remote and remote.licenses_locally(track))

        if server_cdm and not licensed_locally:
            assert remote is not None
            with self.drm_lock(drm), ExitStack() as licence_turn:
                pending_vault_writes: list[Callable[[], Any]] = []
                vault_kids = self._with_track_kid(drm, track_kid)
                self._take_known_keys(drm, vault_kids, track, track_kid, None, use_vaults=not cdm_only)

                def missing_track_key() -> bool:
                    return not drm.content_keys or bool(track_kid and track_kid not in drm.content_keys)

                if missing_track_key():
                    self._take_licence_turn(licence_turn, drm, vault_kids, track, track_kid, None)
                known_keys = set(drm.content_keys)

                if missing_track_key():
                    server_keys = remote.license_track(track, drm, track_kid)
                    if server_keys is None:
                        licensed_locally = True
                    else:
                        drm.content_keys.update(server_keys)
                drop_null_keys(drm)

                new_keys = {}
                for kid, key in drm.content_keys.items():
                    source = None if cdm_only else remote.vault_source(kid, key)
                    if source is not None:
                        self.vaults.sources[kid] = (key, source)
                    elif kid not in known_keys:
                        new_keys[kid] = key
                if new_keys:
                    self.LICENSE_KEY_CACHE.update(new_keys)
                    pending_vault_writes.append(partial(self._cache_keys_to_vaults, new_keys))
                self._flush_vault_writes(pending_vault_writes)

        if server_cdm and not licensed_locally:
            if not drm.content_keys:
                self.log.warning("Server CDM did not resolve any keys for this track")
                return
            if track_kid and track_kid not in drm.content_keys:
                msg = f"No Content Key for KID {track_kid.hex} was returned by the server CDM"
                if isinstance(drm, PlayReady):
                    raise PlayReady.Exceptions.CEKNotFound(msg)
                raise Widevine.Exceptions.CEKNotFound(msg)
            assert remote is not None
            server_drm_type = remote.server_cdm_type
            drm_name = {"widevine": "Widevine", "playready": "PlayReady"}.get(server_drm_type, drm.__class__.__name__)
            log_event(
                "drm_content_keys",
                level="INFO",
                message=f"Server CDM resolved {len(drm.content_keys)} {drm_name} content key(s)",
                service=self.tag,
                drm_type=drm_name,
                key_count=len(drm.content_keys),
                keys=[{"kid": k.hex, "key": v} for k, v in drm.content_keys.items()],
                remote=True,
            )
            with self.drm_lock(drm):
                pssh_str = ""
                expected_class = "PlayReady" if server_drm_type == "playready" else "Widevine"
                matching_drm = next(
                    (d for d in (track.drm or []) if d.__class__.__name__ == expected_class),
                    drm,
                )
                if hasattr(matching_drm, "pssh") and matching_drm.pssh:
                    if hasattr(matching_drm.pssh, "dumps"):
                        pssh_str = truncate_pssh_for_display(matching_drm.pssh.dumps(), drm_name)
                    elif hasattr(matching_drm, "data") and matching_drm.data.get("pssh_b64"):
                        pssh_str = truncate_pssh_for_display(matching_drm.data["pssh_b64"], drm_name)
                if pssh_str:
                    cek_tree = Tree(Text.assemble((drm_name, "cyan"), (f"({pssh_str})", "text"), overflow="fold"))
                else:
                    cek_tree = Tree(Text.assemble((drm_name, "cyan"), overflow="fold"))
                all_kids = self._with_track_kid(drm, track_kid)
                for kid in all_kids:
                    if kid in drm.content_keys:
                        self._add_key_label(cek_tree, kid, drm.content_keys[kid], track_kid)
                for kid, key in drm.content_keys.items():
                    if kid not in all_kids:
                        self._add_key_label(cek_tree, kid, key)
                if not any(isinstance(x, Tree) and x.label == cek_tree.label for x in table.columns[0].cells):
                    table.add_row(cek_tree)
            if self.export:
                self.export(title, track, drm)
            return

        track_quality = None
        if isinstance(track, Video) and track.height:
            track_quality = track.height
        elif title is not None:
            track_quality = max((v.height for v in title.tracks.videos if v.height), default=None)

        track_cdm = self.cdms.cdm
        if licensed_locally:
            assert remote is not None
            track_cdm = remote.local_cdm
            local_drm = track.get_drm_for_cdm(track_cdm)
            if isinstance(local_drm, (Widevine, PlayReady)) and isinstance(local_drm, PlayReady) != is_playready_cdm(
                track_cdm
            ):
                track_cdm = self.cdms.cdm_for(
                    "playready" if isinstance(local_drm, PlayReady) else "widevine", track_quality
                )
            if local_drm is not None and local_drm is not drm:
                assert not isinstance(local_drm, ClearKey)
                # a mid-download handover leaves drm on the server's DRM system, and the caller decrypts with it
                self._prepare_drm(local_drm, track, title, table, track_kid, cdm_only)
                with self.drm_lock(drm):
                    drm.content_keys.update(local_drm.content_keys)
                return

        drm_system = "playready" if isinstance(drm, PlayReady) else "widevine"
        licence = partial(self._service_licence, title=title, track=track, drm_system=drm_system)

        if not server_cdm and isinstance(drm, (Widevine, PlayReady)):
            if cdm_system(self.cdms.cdm) != drm_system:
                name, other = ("PlayReady", "Widevine") if drm_system == "playready" else ("Widevine", "PlayReady")
                system_cdm = self.cdms.cdm_for(drm_system, track_quality)
                if cdm_system(system_cdm) == drm_system:
                    self.log.info(f"Switching to the {name} CDM")
                    self.cdms.cdm = system_cdm
                    track_cdm = system_cdm
                elif self.cdms.cdm is None:
                    raise click.ClickException(f"No CDM configured for {self.tag} and no default CDM")
                else:
                    raise ValueError(f"Title needs a {name} CDM but {self.tag} is configured with {other}.")

        if isinstance(drm, (Widevine, PlayReady, ClearKeyCENC)):
            if isinstance(drm, ClearKeyCENC):
                clearkey_licence = partial(self.service.get_clearkey_license, title=title, track=track)
                get_keys = partial(drm.get_content_keys, licence=clearkey_licence, session=self.service.session)
            else:
                certificate = partial(self.service.get_widevine_service_certificate, title=title, track=track)
                get_keys = partial(drm.get_content_keys, cdm=track_cdm, licence=licence, certificate=certificate)
            self._license_drm(drm, get_keys, track, title, track_kid, table, cdm_only, force)
            if self.export:
                self.export(title, track, drm)

        elif isinstance(drm, MonaLisa):
            with self.drm_lock(drm):
                display_id = drm.content_id or drm.pssh
                pssh_display = truncate_pssh_for_display(display_id, "MonaLisa")
                cek_tree = Tree(Text.assemble(("MonaLisa", "cyan"), (f"({pssh_display})", "text"), overflow="fold"))
                pre_existing_tree = next(
                    (x for x in table.columns[0].cells if isinstance(x, Tree) and x.label == cek_tree.label), None
                )
                if pre_existing_tree:
                    cek_tree = pre_existing_tree

                for kid_, key in drm.content_keys.items():
                    self._add_key_label(cek_tree, kid_, key, track_kid=kid_)  # every MonaLisa key is the track's own

                if cek_tree.children and not pre_existing_tree:
                    table.add_row()
                    table.add_row(cek_tree)

                if self.export:
                    self.export(title, track, drm)
