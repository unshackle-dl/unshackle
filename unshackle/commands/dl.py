from __future__ import annotations

import logging
import os
import random
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Sequence
from concurrent import futures
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, timedelta
from functools import partial
from itertools import groupby
from operator import attrgetter
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any, TypedDict

import click
import mediaexport
from click.core import ParameterSource
from langcodes import Language, tag_is_valid
from pymediainfo import MediaInfo
from rich.console import Group
from rich.padding import Padding
from rich.rule import Rule
from rich.spinner import Spinner
from rich.table import Table

from unshackle.core import __version__, binaries, login, providers
from unshackle.core.cdm.choice import choose_cdm
from unshackle.core.cdm.detect import cdm_system, cdm_type_stub, is_playready_cdm, is_remote_cdm
from unshackle.core.config import cdm_entry_names_device, config, resolve_cdm_name, resolve_decryption
from unshackle.core.console import SyncLive, console, listing_panel, print_listing
from unshackle.core.constants import (
    DEFAULT_SAMPLE,
    DOWNLOAD_ALL_DRM,
    DOWNLOAD_CANCELLED,
    DOWNLOAD_LICENCE_ONLY,
    AnyTrack,
    context_settings,
)
from unshackle.core.dl_options import DL_OPTION_ALIASES
from unshackle.core.downloaders import (
    default_max_workers,
    format_speed,
    parse_speed_limit,
    retries_exhausted,
    set_speed_limit,
)
from unshackle.core.drm import (
    MonaLisa,
    cbcs,
    own_kids,
    verify,
)
from unshackle.core.drm.key_resolution import ContentKeyResolver
from unshackle.core.events import events
from unshackle.core.export_name import ExportNamer, move_export, plural, season_episodes, title_label
from unshackle.core.launch import LaunchKeys, launch, load_service_config
from unshackle.core.manifests.snapshot import capture
from unshackle.core.mux_step import mux_title
from unshackle.core.placement import SAMPLE_DIR, Placement, sample_name
from unshackle.core.providers.tvdb import SEASON_TYPES, parse_int
from unshackle.core.proxies.resolve import (
    REGION,
    describe_explicit_proxy,
    describe_proxy,
    is_loopback,
    load_proxy_providers,
    pick_proxy,
    require_alone_local_proxy,
    resolve_proxy,
    split_proxy_query,
)
from unshackle.core.run_plan import PlanError, RunOutcome, RunStopped, bitrate_range, check_options
from unshackle.core.service import grow_session_pool
from unshackle.core.services import ROUTING_PARAMS, Services
from unshackle.core.temp import with_task_temp
from unshackle.core.title_source import KeyArrival, TitleSource
from unshackle.core.titles import Movie, Movies, Series, Song, Title_T
from unshackle.core.titles.episode import Episode
from unshackle.core.tracks import Audio, Subtitle, Tracks, Video
from unshackle.core.tracks.attachment import Attachment
from unshackle.core.tracks.selection import Skip, TrackSelection, generated_dv_ranges
from unshackle.core.tracks.subtitle_convert import PYCAPTION_WRITERS, SUBSTATION, build_ass_style
from unshackle.core.tracks.track import (
    Track,
    assert_fragments_decrypted,
    fix_data_reference_indexes,
    has_encrypted_sample_entry,
)
from unshackle.core.utilities import (
    apply_original_language,
    find_font_with_fallbacks,
    get_debug_logger,
    get_system_fonts,
    inherited_exclusions,
    init_debug_logger,
    log_event,
    matching_languages,
    partition_exclusions,
    resolve_sort_langs,
    suggest_font_packages,
    time_elapsed_since,
)
from unshackle.core.utils import tags
from unshackle.core.utils.bitrate import apply_real_bitrates
from unshackle.core.utils.click_types import (
    AUDIO_CODEC_LIST,
    LANGUAGE_RANGE,
    QUALITY_LIST,
    SAMPLE_RANGE,
    SEASON_RANGE,
    SLOW_DELAY_RANGE,
    SUBTITLE_CODEC,
    VIDEO_CODEC_LIST,
    ContextData,
    MultipleChoice,
)
from unshackle.core.utils.collections import ci_get, merge_dict
from unshackle.core.utils.hdr10plus import probed_tracks
from unshackle.core.utils.post_scripts import NO_POST_SCRIPTS, build_context, dispatch, season_context
from unshackle.core.utils.redact import mask_proxy
from unshackle.core.utils.selector import select_multiple
from unshackle.core.utils.subprocess import ffprobe
from unshackle.core.vaults import Vaults, load_config_vaults

if TYPE_CHECKING:
    import pysubs2

    from unshackle.core.title_source import ServerKeySource


class SkippedTitle(TypedDict):
    """A title skipped because a requested track or language is missing. Accumulated as ``dl.skipped_titles``."""

    title: str
    reason: str


class SkippedSubtitle(TypedDict):
    """A subtitle skipped under ``--skip-subtitle-errors``. Accumulated as ``dl.skipped_subtitles``,
    one entry per track. ``id`` and ``title`` identify which subtitle of which title was unavailable."""

    id: str
    language: str
    title: str


def parse_language(tag: str | None) -> Language | None:
    """A provider's original-language tag as a Language, or None if it is absent or not a real tag."""
    tag = (tag or "").strip()
    if not tag or not tag_is_valid(tag) or tag.startswith("und"):
        return None
    return Language.get(tag)


def normalize_dl_config(dl_config: dict[str, Any]) -> dict[str, Any]:
    """Map aliased config keys (e.g. ``range``) to their Click parameter names (``range_``)."""
    return {DL_OPTION_ALIASES.get(key, key): value for key, value in dl_config.items()}


def convert_scc(subtitle: Subtitle, sub_format: Subtitle.Codec | str | None) -> Subtitle.Codec:
    """
    Convert an SCC subtitle to sub_format when pycaption can write it, else to SRT, and return the new codec.
    Only pycaption reads SCC, and nothing later in the pipeline (styling, SDH strip, mux) can.
    """
    target = Subtitle.Codec.SubRip
    if isinstance(sub_format, Subtitle.Codec) and sub_format in PYCAPTION_WRITERS:
        target = sub_format
    subtitle.convert(target)
    if subtitle.path and not subtitle.path.read_text("utf8").strip():
        logging.getLogger("Subtitle").warning(f"SCC subtitle {subtitle.language} has no CC1 captions")
    return target


def subtitle_look(
    sub_format: Subtitle.Codec | str | None,
    sub_font: str | None,
    style_fields: Any,
    *,
    typed_font: bool = True,
    typed_format: bool = True,
) -> tuple[Subtitle.Codec | str | None, pysubs2.SSAStyle | None]:
    """The subtitle format and ASS style for --sub-font and the subtitle.ass_style config.

    A style makes ASS the format when none is set. Another format leaves the style unused. A typed --sub-font
    overrides a format from config, refuses a typed one, and a font from config is ignored like the config style.
    """
    if typed_font and sub_font and sub_format is not None and sub_format not in SUBSTATION:
        if typed_format:
            raise ValueError(f"--sub-font needs ASS subtitles, not {getattr(sub_format, 'name', sub_format)}")
        sub_format = None
    if not (style_fields or sub_font) or sub_format not in (None, *SUBSTATION):
        return sub_format, None
    if not isinstance(style_fields, (dict, type(None))):
        raise ValueError("subtitle.ass_style must be a mapping of style fields")
    style = build_ass_style({**(style_fields or {}), **({"fontname": sub_font} if sub_font else {})})
    return sub_format or Subtitle.Codec.SubStationAlphav4, style


def set_subtitle_look(subtitles: Iterable[Subtitle], style: pysubs2.SSAStyle | None, videos: Iterable[Video]) -> None:
    """Give each subtitle that is not already SSA/ASS the style, and every subtitle the size of the first video."""
    size = next(((v.width, v.height) for v in videos if v.width and v.height), None)
    for subtitle in subtitles:
        subtitle.ass_style = None if subtitle.codec in SUBSTATION else style
        subtitle.ass_video_size = size


def title_wanted(candidate: Any, wanted: Collection[str]) -> bool:
    """Whether ``-w`` selects this title. A title type without selection keys is always kept.

    The download loop and the post-script counter both read this, so the counter cannot
    fall out of step with the titles the loop skips.
    """
    if not wanted or not isinstance(candidate, (Episode, Song)):
        return True
    return bool(candidate.matches_wanted(wanted))


def latest_episode_keys(titles: Any, latest_episode: bool, latest_episodes: int | None) -> list[tuple[int, int]]:
    """The (season, number) of each episode ``--latest-episode(s)`` keeps, oldest first.

    Counts distinct episodes, not Series entries, so a split episode takes one slot and all its parts download.
    """
    count = latest_episodes or (1 if latest_episode else 0)
    if not count or not isinstance(titles, Series):
        return []
    # a Series keeps itself sorted, so the newest episodes are last
    return list(dict.fromkeys((ep.season, ep.number) for ep in titles))[-count:]


def apply_enrich(title: Title_T, name: str | None, year: int | None, language: Language | None) -> None:
    """Set the --enrich name, year and original language on a title. A song takes only the language.

    A service can set the title again in get_tracks, so dl applies them after that call too.
    """
    if isinstance(title, Episode):
        title.title = name or title.title
    elif isinstance(title, Movie):
        title.name = name or title.name
    if isinstance(title, (Episode, Movie)):
        title.year = year or title.year
    if language:
        apply_original_language(title, language)


def load_tracks(
    service: TitleSource,
    title: Title_T,
    *,
    name: str | None = None,
    year: int | None = None,
    language: Language | None = None,
    workers: int | None = None,
    on_probe: Callable[[], None] | None = None,
) -> None:
    """Load the tracks and chapters of a title, then apply the --enrich name, year and language again."""
    title.tracks.add(probed_tracks(service, title, workers=workers, on_probe=on_probe), warn_only=True)
    title.tracks.chapters = service.get_chapters(title)
    apply_enrich(title, name, year, language)


def server_url(server_name: str | None) -> str:
    """The configured URL of a remote server, or an empty string when the config cannot be read."""
    from unshackle.core.remote_service import resolve_server

    try:
        return resolve_server(server_name)[0]
    except Exception:
        return ""


def post_script_group(candidate: Any) -> Any:
    """Group a title by the folder its outputs share, for post-scripts in season mode."""
    if isinstance(candidate, Episode):
        return ("episode", candidate.title, candidate.season)
    if isinstance(candidate, Song):
        artist = getattr(candidate, "album_artist", None) or getattr(candidate, "artist", "")
        return ("album", artist, getattr(candidate, "album", ""))
    return ("title", id(candidate))


def remux_music_tracks(title: Any, log: logging.Logger, sample: tuple[int, int] | None = None) -> None:
    """Remux each audio track of a Song into the container of its codec, cut to ``sample`` when given.

    A Song never reaches the muxer, so each downloaded audio track is itself a delivered
    file. A failure warns, keeps that downloaded track, and moves on to the next one: the
    user still gets the audio, in a container a player cuts short.
    """
    if not isinstance(title, Song):
        return
    for track in title.tracks.audio:
        window = (max(sample[0] - track.sample_start, 0), sample[1] - sample[0]) if sample else (0.0, None)
        try:
            track.to_music_container(*window)
        except Exception as e:  # noqa: BLE001
            log.warning(f"Could not remux audio track {track.id} for music: {e}")


def mux_skipped(no_mux: bool, title: Any) -> bool:
    """A Song has no mux step, so --no-mux never applies to it."""
    return no_mux and not isinstance(title, Song)


def take_unmuxable_audio(tracks: Tracks) -> list[Audio]:
    """Remove and return the Audio Vivid tracks, which no muxer carries, to be saved beside the output."""
    kept = [a for a in tracks.audio if a.codec == Audio.Codec.AV3A]
    tracks.audio = [a for a in tracks.audio if a.codec != Audio.Codec.AV3A]
    return kept


def cdm_summary(cdm: Any) -> dict[str, Any]:
    """The loaded CDM's DRM system, whether it is remote, its device and its security level, for the load log."""
    drm_type = "PlayReady" if is_playready_cdm(cdm) else "Widevine"
    remote = is_remote_cdm(cdm)
    if remote:
        device = getattr(cdm, "label", None) or cdm.device_name
    elif drm_type == "Widevine":
        device = str(cdm.system_id)
    else:
        device = cdm.certificate_chain.get_name() if cdm.certificate_chain else "Unknown"
    return {"drm_type": drm_type, "remote": remote, "device": device, "security_level": cdm.security_level}


def apply_service_dl_overrides(ctx: click.Context, service_dl_config: dict[str, Any], log: logging.Logger) -> None:
    """Apply ``services.<TAG>.dl`` config onto ``ctx.params``. Explicit CLI/env values win.
    The config replaces defaults and global ``dl:`` default_map values.
    ``remote`` and ``server`` are skipped: they pick the code that runs, so ``Services.resolve_command`` sets
    them before the service loads, and an API job takes them from its request."""
    params_by_name = {param.name: param for param in ctx.command.params if param.name}
    for name, value in normalize_dl_config(service_dl_config).items():
        if name in ROUTING_PARAMS:
            continue
        param = params_by_name.get(name)
        if param is None:
            log.warning(f"Ignoring unknown dl option '{name}' in service config")
            continue
        if name not in ctx.params:
            log.debug(f"Skipping service dl override '{name}': not present in this context")
            continue
        if value is None:
            continue
        source = ctx.get_parameter_source(name)
        if source not in (ParameterSource.DEFAULT, ParameterSource.DEFAULT_MAP):
            continue
        try:
            ctx.params[name] = param.type_cast_value(ctx, value)
            log.debug(f"Applied service dl override '{name}': {ctx.params[name]}")
        except Exception as e:
            log.warning(f"Failed to apply service dl override '{name}'={value!r}: {e}")


def download_tracks_in_passes(
    tracks: Iterable[AnyTrack],
    max_concurrent: int,
    run_one: Callable[[AnyTrack, int], None],
    *,
    skip_subtitle_errors: bool,
    on_subtitle_skipped: Callable[[Subtitle], None],
) -> None:
    """Download a title's tracks so a skippable subtitle failure cannot corrupt the rest.

    A failed track sets the process-global ``DOWNLOAD_CANCELLED`` event, making other in-flight
    tracks early-return without raising. With ``skip_subtitle_errors`` set, video/audio download
    concurrently first. The subtitles then download one at a time (a concurrent pass would let one
    failure's cancel silently drop the others, unrecorded), with the event cleared before each.
    Video/audio failures stay fatal. This function clears the event on entry (stale cancel from a
    prior title) and in ``finally`` (never leave it set for later code).

    ``run_one(track, index)`` downloads a single track. ``on_subtitle_skipped(track)`` records a
    subtitle whose download raised (handled here).
    """
    DOWNLOAD_CANCELLED.clear()
    indexed = list(enumerate(tracks))
    if skip_subtitle_errors:
        primary: list[tuple[int, AnyTrack]] = [(i, t) for i, t in indexed if not isinstance(t, Subtitle)]
        skippable = [(i, t) for i, t in indexed if isinstance(t, Subtitle)]
    else:
        primary, skippable = indexed, []

    try:
        with ThreadPoolExecutor(max_concurrent) as pool:
            future_to_track = {pool.submit(run_one, track, i): track for i, track in primary}
            for download in futures.as_completed(future_to_track):
                download.result()  # a video/audio failure is fatal

        for i, track in skippable:
            DOWNLOAD_CANCELLED.clear()
            try:
                run_one(track, i)
            except Exception:
                on_subtitle_skipped(track)
    finally:
        DOWNLOAD_CANCELLED.clear()  # never leave a failed track's cancel set for later code


class dl:
    def find_custom_font(self, font_name: str) -> Path | None:
        """
        Find font in custom fonts directory.

        Args:
            font_name: Font family name to find

        Returns:
            Path to font file, or None if not found. A name that is a path, not one folder name, finds nothing.
        """
        if Path(font_name).name != font_name or font_name in ("", ".."):
            return None
        family_dir = Path(config.directories.fonts, font_name)
        if family_dir.exists():
            fonts = list(family_dir.glob("*.*tf"))
            return fonts[0] if fonts else None
        return None

    def prepare_temp_font(self, font_name: str, matched_font: Path, system_fonts: dict[str, Path]) -> Path:
        """
        Copy system font to temp and log if using fallback.

        Args:
            font_name: Requested font name
            matched_font: Path to matched system font
            system_fonts: Dictionary of available system fonts

        Returns:
            Path to temp font file
        """
        matched_name = next((name for name, path in system_fonts.items() if path == matched_font), None)

        if matched_name and matched_name.lower() != font_name.lower():
            self.log.info(f"Using '{matched_name}' as fallback for '{font_name}'")

        safe_name = font_name.replace(" ", "_").replace("/", "_")
        temp_path = config.directories.temp / f"font_{safe_name}{matched_font.suffix}"

        if not temp_path.exists():
            shutil.copy2(matched_font, temp_path)

        return temp_path

    def attach_subtitle_fonts(self, font_names: list[str], title: Title_T) -> tuple[int, list[str]]:
        """
        Attach fonts for subtitle rendering.

        Args:
            font_names: List of font names requested by subtitles
            title: Title object to attach fonts to

        Returns:
            Tuple of (fonts_attached_count, missing_fonts_list)
        """
        if not font_names:
            return 0, []
        system_fonts = get_system_fonts()

        font_count = 0
        missing_fonts = []

        for font_name in set(font_names):
            if custom_font := self.find_custom_font(font_name):
                title.tracks.add(Attachment(path=custom_font, name=f"{font_name} ({custom_font.stem})"))
                font_count += 1
                continue

            if system_font := find_font_with_fallbacks(font_name, system_fonts):
                temp_path = self.prepare_temp_font(font_name, system_font, system_fonts)
                title.tracks.add(Attachment(path=temp_path, name=f"{font_name} ({system_font.stem})"))
                font_count += 1
            else:
                self.log.warning(f"Subtitle uses font '{font_name}' but it could not be found")
                missing_fonts.append(font_name)

        return font_count, missing_fonts

    def suggest_missing_fonts(self, missing_fonts: list[str]) -> None:
        """
        Show package installation suggestions for missing fonts.

        Args:
            missing_fonts: List of font names that could not be found
        """
        if suggestions := suggest_font_packages(missing_fonts):
            self.log.info("Install font packages to improve subtitle rendering:")
            for package_cmd, fonts in suggestions.items():
                self.log.info(f"  $ sudo apt install {package_cmd}")
                self.log.info(f"    → Provides: {', '.join(fonts)}")

    def generate_sidecar_subtitle_path(
        self,
        subtitle: Subtitle,
        base_filename: str,
        output_dir: Path,
        target_codec: Subtitle.Codec | None = None,
        source_path: Path | None = None,
    ) -> Path:
        """Make the sidecar path: {base}.{lang}[.forced][.sdh].{ext}"""
        lang_suffix = str(subtitle.language) if subtitle.language else "und"
        forced_suffix = ".forced" if subtitle.forced else ""
        sdh_suffix = ".sdh" if (subtitle.sdh or subtitle.cc) else ""

        extension = (target_codec or subtitle.codec or Subtitle.Codec.SubRip).extension
        if not target_codec and not subtitle.codec and source_path and source_path.suffix:
            extension = source_path.suffix.lstrip(".")

        filename = f"{base_filename}.{lang_suffix}{forced_suffix}{sdh_suffix}.{extension}"
        return output_dir / filename

    def output_subtitle_sidecars(
        self,
        subtitles: list[Subtitle],
        base_filename: str,
        output_dir: Path,
        sidecar_format: str,
        original_paths: dict[str, Path] | None = None,
    ) -> list[Path]:
        """
        Output subtitles as sidecar files, converting if needed.

        Conversion runs on a temp copy, so each subtitle's own file stays untouched and mkvmerge can still mux it.
        """
        created_paths: list[Path] = []
        config.directories.temp.mkdir(parents=True, exist_ok=True)

        for subtitle in subtitles:
            source_path = subtitle.path
            if sidecar_format == "original" and original_paths and subtitle.id in original_paths:
                source_path = original_paths[subtitle.id]

            if not source_path or not source_path.exists():
                continue

            if sidecar_format == "original":
                target_codec = None
                if source_path.suffix:
                    try:
                        target_codec = Subtitle.Codec.from_mime(source_path.suffix.lstrip("."))
                    except ValueError:
                        target_codec = None
            else:
                target_codec = Subtitle.Codec.from_mime(sidecar_format)

            sidecar_path = self.generate_sidecar_subtitle_path(
                subtitle, base_filename, output_dir, target_codec, source_path=source_path
            )

            if not target_codec or subtitle.codec == target_codec:
                shutil.copy2(source_path, sidecar_path)
            else:
                temp_path = config.directories.temp / f"sidecar_{subtitle.id}{source_path.suffix}"
                shutil.copy2(source_path, temp_path)

                temp_sub = Subtitle(
                    subtitle.url,
                    subtitle.language,
                    is_original_lang=subtitle.is_original_lang,
                    descriptor=subtitle.descriptor,
                    codec=subtitle.codec,
                    forced=subtitle.forced,
                    sdh=subtitle.sdh,
                    cc=subtitle.cc,
                    id_=f"{subtitle.id}_sc",
                )
                temp_sub.path = temp_path
                try:
                    temp_sub.convert(target_codec, forced=True)
                    if temp_sub.path and temp_sub.path.exists():
                        shutil.copy2(temp_sub.path, sidecar_path)
                finally:
                    if temp_sub.path and temp_sub.path.exists():
                        temp_sub.path.unlink(missing_ok=True)
                    temp_path.unlink(missing_ok=True)

            created_paths.append(sidecar_path)

        return created_paths

    def post_script_ids(self) -> dict[str, Any]:
        """Tagging IDs as they stand right now.

        Read at each post-script call, never snapshotted: the search inside the title loop
        finds an Episode's TMDB ID, so a snapshot taken before it is always empty.
        """
        return {"tmdb": self.tmdb_id, "imdb": self.imdb_id, "tvdb": self.tvdb_id}

    @click.command(
        short_help="Download, Decrypt, and Mux tracks for titles from a Service.",
        cls=Services,
        context_settings=dict(**context_settings, default_map=normalize_dl_config(config.dl)),
    )
    @click.option(
        "-p", "--profile", type=str, default=None, help="Profile to use for Credentials and Cookies (if available)."
    )
    @click.option(
        "-q",
        "--quality",
        type=QUALITY_LIST,
        default=[],
        help="Download Resolution(s), defaults to the best available resolution.",
    )
    @click.option(
        "-v",
        "--vcodec",
        type=VIDEO_CODEC_LIST,
        default=[],
        help="Video Codec(s) to download, defaults to any codec.",
    )
    @click.option(
        "-a",
        "--acodec",
        type=AUDIO_CODEC_LIST,
        default=[],
        help="Audio Codec(s) to download (comma-separated), e.g., 'AAC,EC3'. Defaults to any.",
    )
    @click.option(
        "-vb",
        "--vbitrate",
        type=int,
        default=None,
        help="Video Bitrate to download (in kbps), defaults to highest available.",
    )
    @click.option(
        "-ab",
        "--abitrate",
        type=int,
        default=None,
        help="Audio Bitrate to download (in kbps), defaults to highest available.",
    )
    @click.option(
        "-vb-range",
        "--vbitrate-range",
        type=str,
        default=None,
        help="Video Bitrate range in kbps (e.g., '6000-7000'). Selects the highest bitrate within the range.",
    )
    @click.option(
        "-ab-range",
        "--abitrate-range",
        type=str,
        default=None,
        help="Audio Bitrate range in kbps (e.g., '128-256'). Selects the highest bitrate within the range.",
    )
    @click.option(
        "-r",
        "--range",
        "range_",
        type=MultipleChoice(Video.Range, case_sensitive=False),
        default=[Video.Range.SDR],
        help="Video Color Range(s) to download, defaults to SDR.",
    )
    @click.option(
        "-c",
        "--channels",
        type=float,
        default=None,
        help="Audio Channel(s) to download. Matches sub-channel layouts like 5.1 with 6.0 implicitly.",
    )
    @click.option(
        "-naa",
        "--noatmos",
        "no_atmos",
        is_flag=True,
        default=False,
        help="Exclude Dolby Atmos audio tracks when selecting audio.",
    )
    @click.option(
        "--split-audio",
        "split_audio",
        is_flag=True,
        default=None,
        help="Create separate output files per audio codec instead of merging all audio.",
    )
    @click.option(
        "--merge-video",
        "merge_video",
        is_flag=True,
        default=None,
        help="Mux all selected video tracks into a single file instead of one file per track.",
    )
    @click.option(
        "--select-titles",
        is_flag=True,
        default=False,
        help="Interactively select downloads from a list: episodes of a series, or films when a title has more than one.",
    )
    @click.option(
        "-w",
        "--wanted",
        type=SEASON_RANGE,
        default=None,
        help="Wanted episodes, e.g. `S01-S05,S07`, `S01E01-S02E03`, `S02-S02E03`, etc. Music uses track numbers, e.g. `1-5`, `1,3,7`, or `2x3` for disc 2 track 3. Defaults to all.",
    )
    @click.option(
        "-l",
        "--lang",
        type=LANGUAGE_RANGE,
        default="orig",
        help="Language(s) wanted for Video and Audio (comma-separated). Use 'orig' to select the original language, e.g. 'orig,en' for both original and English. Prefix a value with '-' to exclude it, e.g. 'all,-es'.",
    )
    @click.option(
        "--latest-episode",
        is_flag=True,
        default=False,
        help="Download only the single most recent episode available.",
    )
    @click.option(
        "--latest-episodes",
        type=click.IntRange(min=1),
        default=None,
        help="Download only the N most recent episodes available.",
    )
    @click.option(
        "-vl",
        "--v-lang",
        type=LANGUAGE_RANGE,
        default=[],
        help="Language wanted for Video. You would use this if the video language doesn't match the audio. Prefix a value with '-' to exclude it, e.g. 'all,-es'.",
    )
    @click.option(
        "-al",
        "--a-lang",
        type=LANGUAGE_RANGE,
        default=[],
        help="Language wanted for Audio, overrides -l/--lang for audio tracks. Prefix a value with '-' to exclude it, e.g. 'all,-es'.",
    )
    @click.option(
        "--require-audio",
        type=LANGUAGE_RANGE,
        default=[],
        help="Audio languages that must exist. The title fails even with --best-available.",
    )
    @click.option(
        "--require-video",
        type=LANGUAGE_RANGE,
        default=[],
        help="Video languages that must exist. The title fails even with --best-available.",
    )
    @click.option(
        "-sl",
        "--s-lang",
        type=LANGUAGE_RANGE,
        default=["all"],
        help="Language wanted for Subtitles. Prefix a value with '-' to exclude it, e.g. 'all,-es'.",
    )
    @click.option(
        "--require-subs",
        type=LANGUAGE_RANGE,
        default=[],
        help="Subtitle languages that must exist. The title fails if one is missing. -sl still decides what to keep.",
    )
    @click.option(
        "--skip-missing",
        is_flag=True,
        default=False,
        help="Skip a title that misses a required language or track and continue with the next title.",
    )
    @click.option("-fs", "--forced-subs", is_flag=True, default=False, help="Include forced subtitle tracks.")
    @click.option(
        "-fso",
        "--forced-subs-only",
        is_flag=True,
        default=False,
        help="Download forced subtitle tracks and no other subtitle tracks.",
    )
    @click.option(
        "-fsl",
        "--forced-s-lang",
        type=LANGUAGE_RANGE,
        default=[],
        help="Languages wanted for forced subtitles, implies -fs. Keeps forced subs only in these languages. Prefix a value with '-' to exclude it, e.g. 'all,-es'.",
    )
    @click.option(
        "--exact-lang",
        is_flag=True,
        default=False,
        help="A language tag without a region selects only the variant it means and never falls back to another: -l es does not select es-419. A tag with a region, such as es-419, is always exact.",
    )
    @click.option(
        "--proxy",
        type=str,
        default=None,
        help="Proxy URI to use. If a 2-letter country is provided, it will try to get a proxy from the config.",
    )
    @click.option(
        "--local-proxy",
        is_flag=True,
        default=False,
        help="With --remote, the server sends its service requests out through this machine's own connection (and any VPN on it), or through a --proxy dialled from this machine.",
    )
    @click.option(
        "--tag",
        type=str,
        default=None,
        help="Set the Group Tag to be used, overriding the config tag and any tag rules.",
    )
    @click.option("--repack", is_flag=True, default=False, help="Add REPACK tag to the output filename.")
    @click.option(
        "-rvb",
        "--real-video-bitrate",
        is_flag=True,
        default=False,
        help="Probe actual media size to compute true video bitrates (top renditions per codec/range), "
        "overriding the manifest's declared bitrate.",
    )
    @click.option(
        "-rab",
        "--real-audio-bitrate",
        is_flag=True,
        default=False,
        help="Probe actual media size to compute true audio bitrates (top renditions per codec/channels/language), "
        "overriding the manifest's declared bitrate. Slower than --real-video-bitrate (more renditions).",
    )
    @click.option(
        "--tmdb",
        "tmdb_id",
        type=int,
        default=None,
        help="Use this TMDB ID for tagging instead of an automatic search. Add --enrich to also take "
        "its title, year and original language.",
    )
    @click.option(
        "--tvdb",
        "tvdb_id",
        type=int,
        default=None,
        help="Use this TVDB ID for --tvdb-order and tagging instead of looking the series up "
        "automatically. Add --enrich to also take its title, year and original language.",
    )
    @click.option(
        "--tvdb-order",
        type=click.Choice(SEASON_TYPES, case_sensitive=False),
        default=None,
        help="Renumber episodes to a TVDB season order. Services normally use 'official' (aired) order; "
        "a series' Streaming Order is 'alternate'. Defaults to the tvdb_order config option.",
    )
    @click.option(
        "-ce",
        "--continuous-episodes",
        is_flag=True,
        default=False,
        help="Number each season on from the season before it, e.g. S02E05 after a four-episode season one.",
    )
    @click.option(
        "--anilist",
        "anilist_id",
        type=str,
        default=None,
        help="Use this AniList ID for tagging instead of an automatic search, or a MyAnimeList ID as mal:12345. "
        "Add --enrich to also take its title, year and original language. Combines with one of "
        "--tmdb, --imdb or --tvdb, which AniList does not know.",
    )
    @click.option(
        "--enrich",
        is_flag=True,
        default=False,
        help=(
            "Overwrite the show title, year and original language with the external source's. "
            "Requires --tmdb, --imdb, --tvdb, or --anilist."
        ),
    )
    @click.option(
        "--daily",
        is_flag=True,
        default=False,
        help="Treat the title as daily/date-based content and fill missing air dates from TVDB during --enrich.",
    )
    @click.option(
        "--imdb",
        "imdb_id",
        type=str,
        default=None,
        help="Use this IMDB ID (e.g. tt1375666) for tagging instead of an automatic search. Add --enrich "
        "to also take its title, year and original language.",
    )
    @click.option(
        "--sub-format",
        type=SUBTITLE_CODEC,
        default=None,
        help="Set Output Subtitle Format, only converting if necessary. Use 'original' to keep source format.",
    )
    @click.option(
        "--sub-font",
        type=str,
        default=None,
        help="Font for subtitles converted to ASS, with the subtitle.ass_style look. Implies --sub-format ASS.",
    )
    @click.option("-V", "--video-only", is_flag=True, default=False, help="Only download video tracks.")
    @click.option("-A", "--audio-only", is_flag=True, default=False, help="Only download audio tracks.")
    @click.option("-S", "--subs-only", is_flag=True, default=False, help="Only download subtitle tracks.")
    @click.option("-C", "--chapters-only", is_flag=True, default=False, help="Only download chapter markers.")
    @click.option("-ns", "--no-subs", is_flag=True, default=False, help="Do not download subtitle tracks.")
    @click.option(
        "--skip-subtitle-errors",
        is_flag=True,
        default=False,
        help="If a subtitle track fails to download, skip it and continue instead of "
        "aborting the whole title (video/audio failures stay fatal).",
    )
    @click.option("-na", "--no-audio", is_flag=True, default=False, help="Do not download audio tracks.")
    @click.option("-nc", "--no-chapters", is_flag=True, default=False, help="Do not download chapter markers.")
    @click.option("-nv", "--no-video", is_flag=True, default=False, help="Do not download video tracks.")
    @click.option(
        "-nt",
        "--no-attachments",
        is_flag=True,
        default=False,
        help="Do not download or mux attachments (cover art, subtitle fonts, and files the service attaches).",
    )
    @click.option("-ad", "--audio-description", is_flag=True, default=False, help="Download audio description tracks.")
    @click.option(
        "-ado",
        "--audio-description-only",
        is_flag=True,
        default=False,
        help="Download audio description tracks and no standard audio tracks.",
    )
    @click.option(
        "--slow",
        type=SLOW_DELAY_RANGE,
        default=None,
        help="Add a delay between each Title download to act more like a real device. "
        "Use --slow for a 60-120s delay, or --slow MIN-MAX (e.g., --slow 20-40) for a custom range. "
        "Minimum delay is 20 seconds.",
    )
    @click.option(
        "--list",
        "list_",
        is_flag=True,
        default=False,
        help="Skip downloading and list available tracks and what tracks would have been downloaded.",
    )
    @click.option(
        "--list-titles",
        is_flag=True,
        default=False,
        help="Skip downloading, only list available titles that would have been downloaded.",
    )
    @click.option(
        "--skip-dl", is_flag=True, default=False, help="Skip downloading while still retrieving the decryption keys."
    )
    @click.option(
        "--all-drm",
        is_flag=True,
        default=False,
        help="License each track with both Widevine and PlayReady. Needs a CDM for each DRM system.",
    )
    @click.option(
        "--export",
        is_flag=True,
        default=False,
        help="Export track info and decryption keys to a JSON file in the exports directory.",
    )
    @click.option(
        "--import",
        "import_file",
        type=str,
        default=None,
        hidden=True,
        help="Internal: path to an export JSON to reconstruct a download from (used by 'unshackle import').",
    )
    @click.option(
        "--cdm-only/--vaults-only",
        is_flag=True,
        default=None,
        help="Only use CDM, or only use Key Vaults for retrieval of Decryption Keys.",
    )
    @click.option(
        "--cdm",
        "cdm_name",
        type=str,
        default=None,
        help="Use this CDM device for the run, overriding the cdm config mapping and, over --remote, the server CDM.",
    )
    @click.option("--no-proxy", is_flag=True, default=False, help="Force disable all proxy use.")
    @click.option(
        "--no-proxy-download",
        is_flag=True,
        default=False,
        help="Bypass proxy for all downloads. Manifest, license, and auth still use proxy.",
    )
    @click.option(
        "--proxy-download",
        type=str,
        default=None,
        help="Proxy for the downloads only, in the same form as --proxy. Manifest, license, and auth use --proxy.",
    )
    @click.option("--no-folder", is_flag=True, default=False, help="Disable folder creation for TV Shows.")
    @click.option(
        "--no-source", is_flag=True, default=False, help="Disable the source tag from the output file name and path."
    )
    @click.option("--no-mux", is_flag=True, default=False, help="Do not mux tracks into a container file.")
    @click.option(
        "--sample",
        type=SAMPLE_RANGE,
        default=None,
        help="Download a short sample and write it to a Sample folder. "
        "Use --sample for 60 seconds from 2 minutes in, or --sample START-END (e.g., --sample 0-60) for a custom window.",
    )
    @click.option(
        "--include-sample",
        is_flag=True,
        default=False,
        help="Also cut a 60 second sample from each muxed file into a Sample folder next to it.",
    )
    @click.option(
        "--postscript",
        "postscript",
        type=str,
        multiple=True,
        metavar="COMMAND",
        help=(
            "Run COMMAND after each downloaded file, with {filepath}, {title}, {season} and other "
            "variables substituted. Repeatable. Replaces the post_scripts config for this run."
        ),
    )
    @click.option(
        "--no-postscript",
        is_flag=True,
        default=False,
        help="Do not run any post-script for this run, configured or --postscript.",
    )
    @click.option(
        "--workers",
        type=int,
        default=None,
        help="Max workers/threads to download with per-track. Default depends on the downloader.",
    )
    @click.option(
        "--adaptive-workers",
        is_flag=True,
        default=False,
        help=(
            "Dynamically scale per-track segment workers up to the --workers cap (or the built-in cap "
            "when --workers is unset) based on measured CDN throughput and errors. Off by default."
        ),
    )
    @click.option(
        "--download-processes",
        type=int,
        default=1,
        help=(
            "Split a track's segment downloads across this many processes to beat the single-interpreter "
            "throughput cap. Only engages for large segment batches. Default 1 (single process)."
        ),
    )
    @click.option(
        "--continue-downloads",
        is_flag=True,
        default=False,
        help=(
            "Keep completed segment files across runs and resume a previously failed download. "
            "One-off enable of the continue_downloads config option."
        ),
    )
    @click.option("--downloads", type=int, default=1, help="Amount of tracks to download concurrently.")
    @click.option(
        "--speed-limit",
        "speed_limit",
        type=str,
        default=None,
        metavar="SPEED",
        help="Cap total download speed across all downloads combined, e.g. 500k, 5M, 1.5G or plain "
        "bytes/sec (5M = 5.0 MB/s; bytes, not bits). "
        "'off' disables a limit configured in the dl: config block.",
    )
    @click.option(
        "-o",
        "--output",
        "output_dir",
        type=Path,
        default=None,
        help="Override the output directory for this download, instead of the one in config.",
    )
    @click.option("--no-cache", "no_cache", is_flag=True, default=False, help="Bypass title cache for this download.")
    @click.option(
        "--reset-cache", "reset_cache", is_flag=True, default=False, help="Clear title cache before fetching."
    )
    @click.option(
        "--worst",
        is_flag=True,
        default=False,
        help="Select the lowest bitrate track within the specified quality, or of the lowest resolution if no "
        "quality is set.",
    )
    @click.option(
        "--best-available",
        "--warn-only",
        "best_available",
        is_flag=True,
        default=False,
        help="Warn instead of failing when a requested resolution, range, video language, or audio language is missing.",
    )
    @click.option(
        "--generate-dv",
        is_flag=True,
        default=False,
        help="Make Dolby Vision from HDR10+ or the decoded video: with -r HYBRID when a title has no DV track, "
        "with -r HDR10 or HDR10P for that base alone.",
    )
    @click.option(
        "--remote/--local",
        default=False,
        is_eager=True,
        help="Use a remote unshackle server instead of local service code. --local overrides a remote: true config.",
    )
    @click.option(
        "--server",
        type=str,
        default=None,
        is_eager=True,
        help="Name of the remote server from remote_services config (if multiple configured).",
    )
    @click.pass_context
    def cli(ctx: click.Context, **kwargs: Any) -> dl:
        return dl(ctx, **kwargs)

    EXPORT_LOCK = Lock()
    export_snapshots: dict[str, dict[str, Any] | None]
    keys: ContentKeyResolver
    _remote_service: ServerKeySource | None = None
    export_service: TitleSource | None = None
    typed_params: frozenset[str] = frozenset()

    def __init__(
        self,
        ctx: click.Context,
        no_proxy: bool,
        profile: str | None = None,
        proxy: str | None = None,
        repack: bool = False,
        tag: str | None = None,
        tmdb_id: int | None = None,
        imdb_id: str | None = None,
        tvdb_id: int | None = None,
        tvdb_order: str | None = None,
        continuous_episodes: bool = False,
        anilist_id: int | str | None = None,
        enrich: bool = False,
        daily: bool = False,
        output_dir: Path | None = None,
        proxy_providers: list[Any] | None = None,
        *_: Any,
        **__: Any,
    ) -> None:
        if not ctx.invoked_subcommand:
            raise ValueError("A subcommand to invoke was not specified, the main code cannot continue.")

        self.log = logging.getLogger("download")
        self.typed_params = frozenset(
            name
            for name in ("sub_font", "sub_format")
            if ctx.get_parameter_source(name) not in (None, ParameterSource.DEFAULT, ParameterSource.DEFAULT_MAP)
        )
        self.completed_files: list[Path] = []
        # Subtitles skipped under --skip-subtitle-errors, recorded so an embedding caller can
        # report which weren't available without parsing the console output. See SkippedSubtitle.
        self.skipped_subtitles: list[SkippedSubtitle] = []
        self.skipped_titles: list[SkippedTitle] = []
        # result() catches a download-worker failure, reports it, and returns rather than
        # re-raising (so the CLI still exits cleanly). Keep it so an embedding caller (the API
        # worker) can tell the title did not complete instead of reading it as a success.
        self.download_error: BaseException | None = None

        if not config.output_template:
            raise click.ClickException("No 'output_template' in your unshackle.yaml")

        self.service = Services.get_tag(ctx.invoked_subcommand)
        self.vault_service = Services.get_vault_tag(self.service)
        apply_service_dl_overrides(ctx, (ci_get(config.services, self.service) or {}).get("dl") or {}, self.log)

        # Refresh locals Click bound before the overrides ran.
        no_proxy = ctx.params.get("no_proxy", no_proxy)
        profile = ctx.params.get("profile", profile)
        proxy = ctx.params.get("proxy", proxy)
        repack = ctx.params.get("repack", repack)
        tag = ctx.params.get("tag", tag)
        tmdb_id = ctx.params.get("tmdb_id", tmdb_id)
        imdb_id = ctx.params.get("imdb_id", imdb_id)
        tvdb_id = ctx.params.get("tvdb_id", tvdb_id)
        tvdb_order = ctx.params.get("tvdb_order", tvdb_order)
        continuous_episodes = ctx.params.get("continuous_episodes", continuous_episodes)
        anilist_id = ctx.params.get("anilist_id", anilist_id)
        enrich = ctx.params.get("enrich", enrich)
        daily = ctx.params.get("daily", daily)
        output_dir = ctx.params.get("output_dir", output_dir)

        self.profile = profile
        self.proxy_requested = bool(proxy)
        check_options(ctx.params)
        self.tmdb_id = tmdb_id
        self.imdb_id = imdb_id
        self.tvdb_id = tvdb_id
        self.anilist_id = anilist_id
        self.tvdb_order = (tvdb_order or config.tvdb_order or "").lower()
        self.continuous_episodes = continuous_episodes
        self.enrich = enrich
        self.daily = daily
        self.output_dir = output_dir
        self.service_anime = False
        self.service_daily = False

        if self.daily and not self.enrich:
            self.log.warning("--daily takes TVDB air dates only with --enrich")

        # Initialize debug logger with service name if debug logging is enabled
        if config.debug or logging.root.level == logging.DEBUG:
            from collections import defaultdict
            from datetime import datetime

            debug_log_path = config.directories.logs / config.filenames.debug_log.format_map(
                defaultdict(str, service=self.service, time=datetime.now().strftime("%Y%m%d-%H%M%S"))
            )
            init_debug_logger(log_path=debug_log_path, enabled=True, log_keys=config.debug_keys)
            self.debug_logger = get_debug_logger()

            if self.debug_logger:
                self.debug_logger.log(
                    level="INFO",
                    operation="download_init",
                    message=f"Download command initialized for service {self.service}",
                    service=self.service,
                    context={
                        "profile": profile,
                        "proxy": proxy,
                        "tag": tag,
                        "tmdb_id": self.tmdb_id,
                        "imdb_id": self.imdb_id,
                        "anilist_id": anilist_id,
                        "enrich": enrich,
                        "cli_params": {
                            k: v
                            for k, v in ctx.params.items()
                            if k
                            not in [
                                "profile",
                                "proxy",
                                "tag",
                                "tmdb_id",
                                "imdb_id",
                                "anilist_id",
                                "enrich",
                            ]
                        },
                    },
                )

                binary_versions: dict[str, dict[str, str | None] | None] = {}
                for name, binary in [
                    ("shaka_packager", binaries.ShakaPackager),
                    ("mp4decrypt", binaries.Mp4decrypt),
                    ("mkvmerge", binaries.MKVToolNix),
                    ("ffmpeg", binaries.FFMPEG),
                    ("ffprobe", binaries.FFProbe),
                ]:
                    binary_versions[name] = (
                        {"path": str(binary), "version": binaries.binary_version(binary)} if binary else None
                    )

                self.debug_logger.log(
                    level="DEBUG",
                    operation="binary_versions",
                    message="Binary tool versions",
                    context=binary_versions,
                )
        else:
            self.debug_logger = None

        if self.profile:
            self.log.info(f"Using profile: '{self.profile}'")

        self.is_remote = bool(ctx.params.get("remote"))

        with console.status("Loading Service Config...", spinner="dots"):
            self.service_config = load_service_config(self.service, local=not self.is_remote)
            if not self.is_remote:
                self.log.info("Service Config loaded")
                # log key names only: the full config carries service certificates,
                # device fingerprints and endpoints that bloat the log and may be sensitive
                log_event(
                    "load_service_config",
                    level="DEBUG",
                    service=self.service,
                    context={"config_keys": sorted(map(str, self.service_config))},
                )

        service_config = ci_get(config.services, self.service, {})
        if service_config:
            reserved_keys = {
                "profiles",
                "api_key",
                "certificate",
                "api_endpoint",
                "region",
                "device",
                "endpoints",
                "client",
                "dl",
            }

            for config_key, override_value in service_config.items():
                if config_key in reserved_keys or not isinstance(override_value, dict):
                    continue

                if hasattr(config, config_key):
                    current_config = getattr(config, config_key, {})

                    if isinstance(current_config, dict):
                        merged_config = deepcopy(current_config)
                        merge_dict(override_value, merged_config)
                        setattr(config, config_key, merged_config)

                        self.log.debug(
                            f"Applied service-specific '{config_key}' overrides for {self.service}: {override_value}"
                        )

        self.cdm_override = ctx.params.get("cdm_name")
        cdm_only = ctx.params.get("cdm_only")

        if cdm_only:
            self.vaults = Vaults(self.vault_service)
            self.log.info("CDM-only mode: Skipping vault loading")
            log_event(
                "vault_loading_skipped",
                level="INFO",
                service=self.service,
                context={"reason": "cdm_only flag set"},
            )
        else:
            with console.status("Loading Key Vaults...", spinner="dots"):
                self.vaults, failed_vaults = load_config_vaults(self.vault_service, critical={"sqlite"})
                total_vaults = len(config.key_vaults)

                loaded_count = len(self.vaults)
                if failed_vaults:
                    self.log.warning(f"Failed to load {len(failed_vaults)} vault(s): {', '.join(failed_vaults)}")
                self.log.info(f"Loaded {loaded_count}/{total_vaults} Vaults")

                # Debug: Show detailed vault status
                if loaded_count > 0:
                    vault_names = [vault.name for vault in self.vaults]
                    self.log.debug(f"Active vaults: {', '.join(vault_names)}")
                else:
                    self.log.debug("No vaults are currently active")

        with console.status("Loading DRM CDM...", spinner="dots"):
            try:
                self.cdm = self.get_cdm(self.service, self.profile)
            except ValueError as e:
                if self.debug_logger:
                    self.debug_logger.log_error("load_cdm", e, service=self.service)
                raise click.ClickException(f"Failed to load CDM, {e}") from None

            if self.cdm:
                cdm_info = cdm_summary(self.cdm)
                where = " Remote" if cdm_info["remote"] else ""
                self.log.info(
                    f"Loaded {cdm_info['drm_type']}{where} CDM: {cdm_info['device']} (L{cdm_info['security_level']})"
                )
                log_event("load_cdm", level="INFO", service=self.service, context={"cdm": cdm_info})

        if ctx.params.get("local_proxy"):
            require_alone_local_proxy(ctx)
            proxy, no_proxy = ctx.params.get("proxy"), bool(ctx.params.get("no_proxy"))

        # A server on this machine can reach a local proxy, so only a server elsewhere is guarded
        self.remote_needs_public_proxy = (
            self.is_remote
            and not ctx.params.get("local_proxy")
            and not is_loopback(server_url(ctx.params.get("server")))
        )

        self.proxy_providers = []
        keys: LaunchKeys = {}
        if no_proxy:
            ctx.params["proxy"] = None
        else:
            if self.remote_needs_public_proxy and proxy and proxy.lower().startswith("gluetun:"):
                raise click.UsageError("Gluetun runs on your machine, so --remote needs --local-proxy to use it.")
            if proxy_providers is not None:
                self.proxy_providers = list(proxy_providers)
            else:
                with console.status("Loading Proxy Providers...", spinner="dots"):
                    self.proxy_providers = load_proxy_providers()

            keys["proxy_arg"] = proxy
            if proxy and proxy.startswith("controld://"):
                proxy = ctx.params["proxy"] = resolve_proxy(proxy, [])
            if proxy:
                try:
                    requested_provider, query = split_proxy_query(proxy, self.proxy_providers)
                except ValueError as e:
                    raise PlanError(str(e)) from None
                # Match region codes (us, ca, uk1, us:ny, yul) or provider:region (nordvpn:ca, protonvpn:us:ny).
                if re.fullmatch(rf"(?:[a-z]+:)?{REGION}", query, re.IGNORECASE):
                    # Kept apart from ctx.params["proxy"], which becomes the URI: a service proxy_map keys on it.
                    proxy_query = query.lower()
                    status_msg = (
                        f"Connecting to VPN ({proxy_query})..."
                        if requested_provider == "gluetun"
                        else f"Getting a Proxy to {proxy_query}..."
                    )
                    with console.status(status_msg, spinner="dots"):
                        try:
                            proxy_provider, proxy_uri = pick_proxy(
                                self.proxy_providers, proxy_query, requested_provider
                            )
                        except ValueError as e:
                            raise click.ClickException(str(e)) from None
                        ctx.params["proxy"] = proxy_uri
                        self.log.info(describe_proxy(proxy_provider, proxy_uri, proxy_query))
                    keys["proxy_query"] = proxy_query
                    keys["proxy_provider"] = requested_provider
                else:
                    self.log.info(describe_explicit_proxy(ctx.params["proxy"]))
                    keys["proxy_query"] = None
                    keys["proxy_provider"] = None

        launch(
            ctx,
            keys,
            ContextData(
                config=self.service_config,
                cdm=self.cdm,
                proxy_providers=self.proxy_providers,
                profile=self.profile,
                cdm_entry=self.cdm_from_service_entry(),
                cdm_for=self.cdm_for,
            ),
        )

        if repack:
            config.repack = True

        if tag:
            config.tag = tag
            config.tag_rules = []

        # needs to be added this way instead of @cli.result_callback to be
        # able to keep `self` as the first positional
        self.cli._result_callback = self.result

    def outcome(self) -> RunOutcome:
        """What the run gave back so far, for an embedding caller such as the API job."""
        return RunOutcome(list(self.completed_files), list(self.skipped_subtitles), self.download_error)

    @with_task_temp
    def result(
        self,
        service: TitleSource,
        quality: list[int],
        vcodec: list[Video.Codec],
        acodec: list[Audio.Codec],
        vbitrate: int | None,
        abitrate: int | None,
        vbitrate_range: str | None,
        abitrate_range: str | None,
        range_: list[Video.Range],
        channels: float | None,
        no_atmos: bool,
        select_titles: bool,
        wanted: list[str],
        latest_episode: bool,
        latest_episodes: int | None,
        lang: list[str],
        v_lang: list[str] | None,
        a_lang: list[str] | None,
        s_lang: list[str] | None,
        require_audio: list[str],
        require_video: list[str],
        require_subs: list[str],
        skip_missing: bool,
        forced_subs: bool,
        forced_subs_only: bool,
        forced_s_lang: list[str],
        exact_lang: bool,
        sub_format: Subtitle.Codec | str | None,
        sub_font: str | None,
        video_only: bool,
        audio_only: bool,
        subs_only: bool,
        chapters_only: bool,
        no_subs: bool,
        skip_subtitle_errors: bool,
        no_audio: bool,
        no_chapters: bool,
        no_video: bool,
        no_attachments: bool,
        audio_description: bool,
        audio_description_only: bool,
        slow: tuple[int, int] | None,
        list_: bool,
        list_titles: bool,
        skip_dl: bool,
        export: bool | None,
        cdm_only: bool | None,
        no_proxy: bool,
        no_proxy_download: bool,
        proxy_download: str | None,
        no_folder: bool,
        no_source: bool,
        no_mux: bool,
        sample: tuple[int, int] | None,
        include_sample: bool,
        workers: int | None,
        adaptive_workers: bool,
        download_processes: int,
        continue_downloads: bool,
        downloads: int,
        worst: bool,
        best_available: bool,
        speed_limit: str | None = None,
        split_audio: bool | None = None,
        merge_video: bool | None = None,
        real_video_bitrate: bool = False,
        real_audio_bitrate: bool = False,
        progress_sink: Callable[[dict[str, Any]], None] | None = None,
        postscript: Sequence[str] = (),
        no_postscript: bool = False,
        all_drm: bool = False,
        generate_dv: bool = False,
        *_: Any,
        **__: Any,
    ) -> RunOutcome:
        if all_drm:
            DOWNLOAD_ALL_DRM.set()
        else:
            DOWNLOAD_ALL_DRM.clear()
        if no_postscript:
            postscript = NO_POST_SCRIPTS
        if continue_downloads:
            config.continue_downloads = True
        config.sample = sample
        self.tmdb_searched = False
        self.search_source = None
        self.service_anime = service.ANIME
        self.service_daily = service.DAILY
        # a remote session sends --cdm to the server, which can hand it the tracks above its cap
        if self.cdm_override and service.key_arrival is KeyArrival.EXPORT:
            self.log.warning("--cdm is ignored: an import uses the keys in the export")
        self._remote_service = service.key_source
        start_time = time.time()

        lang, lang_excl = partition_exclusions(lang)
        v_lang, v_lang_excl = partition_exclusions(v_lang)
        a_lang, a_lang_excl = partition_exclusions(a_lang)
        s_lang, s_lang_excl = partition_exclusions(s_lang)
        forced_s_lang, fsl_excl = partition_exclusions(forced_s_lang)
        # exclusion-only use falls back to the flag's own default; -vl/-al have none, they cascade to -l
        if lang_excl and not lang:
            lang = ["orig"]
        if s_lang_excl and not s_lang:
            s_lang = ["all"]
        video_excl = inherited_exclusions(v_lang, v_lang_excl, lang_excl)
        audio_excl = inherited_exclusions(a_lang, a_lang_excl, lang_excl)

        ctx = service.ctx
        parent_params = ctx.parent.params if ctx and ctx.parent else None
        if parent_params is not None:
            # services read the raw ctx params, they must never see a '-xx' token
            for name, includes, excludes in (
                ("lang", lang, lang_excl),
                ("v_lang", v_lang, v_lang_excl),
                ("a_lang", a_lang, a_lang_excl),
                ("s_lang", s_lang, s_lang_excl),
                ("forced_s_lang", forced_s_lang, fsl_excl),
            ):
                if excludes and name in parent_params:
                    parent_params[name] = includes

        if audio_description_only:
            audio_description = True

        if forced_s_lang or fsl_excl or forced_subs_only:
            # services read forced_subs from raw ctx params, so the implication must be pushed there too
            forced_subs = True
            if "all" in forced_s_lang:
                forced_s_lang = []
            if parent_params is not None:
                parent_params["forced_subs"] = True

        if skip_dl:
            DOWNLOAD_LICENCE_ONLY.set()

        speed_limit_bps = parse_speed_limit(speed_limit)
        set_speed_limit(speed_limit_bps)
        if speed_limit_bps:
            self.log.info(f"Speed limit: {format_speed(speed_limit_bps)}")

        if no_proxy or no_proxy_download:
            proxy_download = None
        elif proxy_download and (
            proxy_download.startswith("controld://")
            or re.fullmatch(rf"(?:[a-z]+:){{0,2}}{REGION}", proxy_download, re.I)
        ):
            # same shapes --proxy resolves against providers (two prefixes for gluetun:nordvpn:ca); else an explicit URI
            try:
                proxy_download = resolve_proxy(proxy_download.lower(), self.proxy_providers)
            except ValueError as e:
                raise click.ClickException(f"--proxy-download: {e}") from None

        if export:
            config.directories.exports.mkdir(parents=True, exist_ok=True)
            # a working name until the first write, which renames it after what it holds
            export_path = config.directories.exports / f"export_{self.service}_{int(time.time())}.json"
            self.export_service = service
            self.export_namer = ExportNamer(self.service)
        else:
            export_path = None

        vbitrate_min, vbitrate_max = bitrate_range(vbitrate_range, "--vbitrate-range", "6000-7000") or (None, None)
        abitrate_min, abitrate_max = bitrate_range(abitrate_range, "--abitrate-range", "128-256") or (None, None)

        if not acodec:
            acodec = []
        elif isinstance(acodec, Audio.Codec):
            acodec = [acodec]
        elif isinstance(acodec, str) or (
            isinstance(acodec, list) and not all(isinstance(v, Audio.Codec) for v in acodec)
        ):
            acodec = AUDIO_CODEC_LIST.convert(acodec)

        try:
            sub_format, ass_style = subtitle_look(
                sub_format,
                sub_font,
                config.subtitle.get("ass_style"),
                typed_font="sub_font" in self.typed_params,
                typed_format="sub_format" in self.typed_params,
            )
        except ValueError as e:
            raise PlanError(str(e)) from None

        if generate_dv and Video.Range.DV in range_ and Video.Range.HYBRID not in range_:
            self.log.warning("--generate-dv ignored: DV is in the selected ranges")
        range_, generated_dv_bases = generated_dv_ranges(range_, generate_dv)

        if cdm_only is None:
            vaults_only = None
        else:
            vaults_only = not cdm_only
        self.start_key_resolution(
            service,
            cdm_only=bool(cdm_only),
            vaults_only=bool(vaults_only),
            best_available=bool(best_available),
            export=export_path,
        )

        config.decryption = resolve_decryption(
            config.decryption_map, config.decryption, service.__class__.__name__.upper()
        )

        log_event(
            "drm_mode_config",
            level="DEBUG",
            service=self.service,
            context={
                "cdm_only": cdm_only,
                "vaults_only": vaults_only,
                "mode": "CDM only" if cdm_only else ("Vaults only" if vaults_only else "Both CDM and Vaults"),
                "decryption": config.decryption,
            },
        )

        with console.status(
            "Preparing Remote Service Session..." if self.is_remote else "Preparing Service Session...",
            spinner="dots",
        ):
            try:
                cookies = login.get_cookie_jar(self.service, self.profile)
                credential = login.get_credentials(self.service, self.profile)
                service.authenticate(cookies, credential)
                if cookies or credential:
                    self.log.info("Authenticated with Service")
                    log_event(
                        "authenticate",
                        level="INFO",
                        service=self.service,
                        context={
                            "has_cookies": bool(cookies),
                            "has_credentials": bool(credential),
                            "profile": self.profile,
                        },
                    )
            except Exception as e:
                if self.debug_logger:
                    self.debug_logger.log_error(
                        "authenticate", e, service=self.service, context={"profile": self.profile}
                    )
                raise

        with console.status(
            "Fetching Remote Title Metadata..." if self.is_remote else "Fetching Title Metadata...", spinner="dots"
        ):
            try:
                titles = service.get_titles_cached()
                if not titles:
                    self.log.error("No titles returned, nothing to download...")
                    log_event(
                        "get_titles",
                        level="ERROR",
                        service=self.service,
                        message="No titles returned from service",
                        success=False,
                    )
                    raise RunStopped("No titles returned")
            except Exception as e:
                if self.debug_logger:
                    self.debug_logger.log_error("get_titles", e, service=self.service)
                raise

            if self.debug_logger:
                titles_info = {
                    "type": titles.__class__.__name__,
                    "count": len(titles) if hasattr(titles, "__len__") else 1,
                    "title": str(titles),
                }
                if hasattr(titles, "seasons"):
                    titles_info["seasons"] = len(titles.seasons) if hasattr(titles, "seasons") else 0
                self.debug_logger.log(
                    level="INFO", operation="get_titles", service=self.service, context={"titles": titles_info}
                )

        title_cacher, cache_title_id, cache_region, cache_account_hash = service.cache_identity()

        enrich_lang: Language | None = None
        enrich_title: str | None = None
        enrich_year: int | None = None
        if self.enrich:
            sample_title = titles[0] if hasattr(titles, "__getitem__") else titles
            kind = "tv" if isinstance(sample_title, Episode) else "movie"

            enrich_result = providers.resolve_by_ids(
                self.tmdb_id,
                self.imdb_id,
                self.tvdb_id,
                self.anilist_id,
                kind=kind,
                title_cacher=title_cacher,
                cache_title_id=cache_title_id,
                cache_region=cache_region,
                cache_account_hash=cache_account_hash,
                anime=self.anime_hint(sample_title),
            )
            if enrich_result:
                enrich_title = enrich_result.title
                enrich_year = enrich_result.year
                enrich_lang = parse_language(enrich_result.original_language)

            if enrich_lang:
                self.log.info(f"Original language from metadata: {enrich_lang}")
            if not (enrich_title or enrich_year or enrich_lang):
                self.log.warning("--enrich found no metadata")
            elif missing := [
                name
                for name, value in (("title", enrich_title), ("year", enrich_year), ("language", enrich_lang))
                if not value
            ]:
                self.log.warning(f"--enrich source gave no {', '.join(missing)}, kept the service's values")

            if isinstance(titles, (Series, Movies)):
                for t in titles:
                    apply_enrich(t, enrich_title, enrich_year, enrich_lang)

            if isinstance(titles, Series):
                enrich_tvdb_id = self.tvdb_id or (enrich_result.external_ids.tvdb_id if enrich_result else None)
                self.fill_absolute_numbers(titles, enrich_tvdb_id)
                if any(self.daily_hint(t) for t in titles):
                    self.fill_air_dates(titles, enrich_tvdb_id)

        if self.tvdb_order and isinstance(titles, Series):
            titles = self.apply_tvdb_order(titles, title_cacher, cache_title_id, cache_region, cache_account_hash)

        if self.continuous_episodes and isinstance(titles, Series):
            titles = self.apply_continuous_episodes(titles)

        console.print(Padding(Rule(f"[rule.text]{titles.__class__.__name__}: {titles}"), (1, 2)))
        print_listing(titles.tree(verbose=list_titles))

        if list_titles:
            return self.outcome()

        if export_path and isinstance(titles, Series):
            # before --select-titles and -w drop titles, so a whole season can be told from part of one
            self.export_namer.seasons = season_episodes(titles)

        if select_titles and isinstance(titles, Movies) and len(titles) > 1:
            console.print(Padding(Rule("[rule.text]Select Titles"), (1, 2)))

            selection_start = time.time()
            selected_idx = select_multiple(
                [str(movie) for movie in titles],
                minimal_count=1,
                page_size=8,
                return_indices=True,
            )
            if not selected_idx:
                console.print(Padding(":x: Selection Cancelled...", (0, 5, 1, 5)))
                return self.outcome()
            start_time += time.time() - selection_start

            keep = set(selected_idx)
            for i in range(len(titles) - 1, -1, -1):
                if i not in keep:
                    del titles[i]

            console.print(Padding(f"[text]Total selected: {len(titles)}[/]", (0, 5)))

        # Enables manual selection for Series when --select-titles is set
        if select_titles and isinstance(titles, Series):
            console.print(Padding(Rule("[rule.text]Select Titles"), (1, 2)))

            selection_titles = []
            dependencies: dict[int, list[int]] = {}
            original_indices = {}

            current_season = None
            current_season_header_idx = -1

            unique_seasons = {t.season for t in titles}
            multiple_seasons = len(unique_seasons) > 1

            # Build selection options
            for i, t in enumerate(titles):
                # Insert season header only if multiple seasons exist
                if multiple_seasons and t.season != current_season:
                    current_season = t.season
                    header_text = f"Season {t.season}"
                    selection_titles.append(header_text)
                    current_season_header_idx = len(selection_titles) - 1
                    dependencies[current_season_header_idx] = []

                display_name = ((t.name[:30].rstrip() + "...") if len(t.name) > 30 else t.name) if t.name else None

                prefix = " " if multiple_seasons else ""
                part_label = f".{t.part}" if t.part is not None else ""
                option_text = f"{prefix}{t.number}{part_label}" + (f". {display_name}" if t.name else "")

                selection_titles.append(option_text)
                current_ui_idx = len(selection_titles) - 1

                original_indices[current_ui_idx] = i

                if current_season_header_idx != -1:
                    dependencies[current_season_header_idx].append(current_ui_idx)

            selection_start = time.time()

            selected_ui_idx = select_multiple(
                selection_titles,
                minimal_count=1,
                page_size=8,
                return_indices=True,
                dependencies=dependencies,
                collapse_on_start=multiple_seasons,
            )

            if not selected_ui_idx:
                console.print(Padding(":x: Selection Cancelled...", (0, 5, 1, 5)))
                return self.outcome()

            selection_end = time.time()
            start_time += selection_end - selection_start

            selected_idx = []
            for idx in selected_ui_idx:
                if idx in original_indices:
                    selected_idx.append(original_indices[idx])

            # Ensure indices are unique and ordered
            selected_idx = sorted(set(selected_idx))
            keep = set(selected_idx)

            for i in range(len(titles) - 1, -1, -1):
                if i not in keep:
                    del titles[i]

            if titles:
                count = len(titles)
                console.print(Padding(f"[text]Total selected: {count}[/]", (0, 5)))

        latest_keys = latest_episode_keys(titles, latest_episode, latest_episodes)
        if latest_keys:
            picked = ", ".join(f"S{season:02}E{number:02}" for season, number in latest_keys)
            self.log.info(f"Latest episode mode: Selecting {picked}")
            # replaces -w; a base SxE key selects every part of a split episode
            wanted = [f"{season}x{number}" for season, number in latest_keys]

        selection = TrackSelection(
            quality=quality,
            vcodec=vcodec,
            acodec=acodec,
            vbitrate=vbitrate,
            abitrate=abitrate,
            vbitrate_min=vbitrate_min,
            vbitrate_max=vbitrate_max,
            abitrate_min=abitrate_min,
            abitrate_max=abitrate_max,
            range_=range_,
            generated_dv_bases=generated_dv_bases,
            channels=channels,
            no_atmos=no_atmos,
            lang=lang,
            v_lang=v_lang,
            a_lang=a_lang,
            s_lang=s_lang,
            video_excl=video_excl,
            audio_excl=audio_excl,
            s_lang_excl=s_lang_excl,
            fsl_excl=fsl_excl,
            forced_s_lang=forced_s_lang,
            require_audio=require_audio,
            require_video=require_video,
            require_subs=require_subs,
            forced_subs=forced_subs,
            forced_subs_only=forced_subs_only,
            exact_lang=exact_lang,
            video_only=video_only,
            audio_only=audio_only,
            subs_only=subs_only,
            chapters_only=chapters_only,
            no_subs=no_subs,
            no_audio=no_audio,
            no_chapters=no_chapters,
            no_video=no_video,
            no_attachments=no_attachments,
            audio_description=audio_description,
            audio_description_only=audio_description_only,
            worst=worst,
            best_available=best_available,
            generate_dv=generate_dv,
        )

        post_script_pending: dict[Any, int] = {}
        for candidate in titles:
            if title_wanted(candidate, wanted):
                key = post_script_group(candidate)
                post_script_pending[key] = post_script_pending.get(key, 0) + 1
        post_script_last: dict[Any, dict[Path, dict[str, str]]] = {}
        post_script_folders: list[Path] = []
        post_script_sample: dict[str, str] = {}
        wanted_total = sum(post_script_pending.values())

        def settle_post_script_group(title: Any) -> None:
            """Count one title of its group as done; send the season event after the last one."""
            group_key = post_script_group(title)
            post_script_pending[group_key] = post_script_pending.get(group_key, 1) - 1
            if post_script_pending[group_key] <= 0:
                for folder, context in post_script_last.get(group_key, {}).items():
                    dispatch("success", "season", season_context(context, folder), postscript)

        def skip_title(title: Any, reason: str, kind_only: bool = False) -> None:
            """Skip a title that has no track the selection asks for, and report it to the API job.

            Only a kind-only miss (-ado, -fso, no track left) skips without --skip-missing; any other miss
            stops the run. A run with one title always stops, because it has nothing else to download.
            """
            if wanted_total <= 1 or not (kind_only or skip_missing):
                stop = f"{title}: {reason}"
                self.log.error(stop)
                raise RunStopped(stop)
            self.skipped_titles.append(SkippedTitle(title=str(title), reason=reason))
            if progress_sink:
                progress_sink({"skipped_titles": list(self.skipped_titles)})
            self.log.warning(f"Skipping {title}: {reason}")
            if not mux_skipped(no_mux, title):
                settle_post_script_group(title)

        for i, title in enumerate(titles):
            if not title_wanted(title, wanted):
                continue

            if progress_sink:
                if isinstance(title, Episode):
                    progress_sink(
                        {
                            "title": title.title,
                            "current_title": f"S{title.season or 0:02}E{title.number or 0:02}"
                            + (f".{title.part}" if title.part is not None else ""),
                        }
                    )
                else:
                    progress_sink({"title": getattr(title, "name", None) or str(title)})

            title_rule = f"Track {title.track:02}: {title.name}" if isinstance(title, Song) else str(title)
            console.print(Padding(Rule(f"[rule.text]{title_rule}"), (1, 2)))

            if isinstance(title, Episode) and not self.tmdb_searched:
                kind = "tv"
                tmdb_title: str | None = None
                has_ids = bool(self.tmdb_id or self.imdb_id or self.tvdb_id or self.anilist_id)
                result = providers.resolve_by_ids(
                    self.tmdb_id,
                    self.imdb_id,
                    self.tvdb_id,
                    self.anilist_id,
                    title=title.title,
                    year=title.year,
                    kind=kind,
                    title_cacher=title_cacher,
                    cache_title_id=cache_title_id,
                    cache_region=cache_region,
                    cache_account_hash=cache_account_hash,
                    anime=self.anime_hint(title),
                )
                if result:
                    if has_ids:
                        tmdb_title = result.title
                        if not self.tmdb_id:
                            self.tmdb_id = result.external_ids.tmdb_id
                    elif result.title and providers.fuzzy_match(result.title, title.title):
                        self.tmdb_id = result.external_ids.tmdb_id
                        tmdb_title = result.title
                        self.search_source = result.source
                if list_ or list_titles:
                    if self.tmdb_id:
                        console.print(
                            Padding(
                                f"Search -> {tmdb_title or '?'} [bright_black](ID {self.tmdb_id})",
                                (0, 5),
                            )
                        )
                    else:
                        console.print(Padding("Search -> [bright_black]No match found[/]", (0, 5)))
                self.tmdb_searched = True

            if isinstance(title, Movie) and (list_ or list_titles) and not self.tmdb_id:
                movie_result = providers.resolve_by_ids(
                    None,
                    self.imdb_id,
                    self.tvdb_id,
                    self.anilist_id,
                    title=title.name,
                    year=title.year,
                    kind="movie",
                    title_cacher=title_cacher,
                    cache_title_id=cache_title_id,
                    cache_region=cache_region,
                    cache_account_hash=cache_account_hash,
                    anime=self.anime_hint(title),
                )
                if movie_result and movie_result.external_ids.tmdb_id:
                    console.print(
                        Padding(
                            f"Search -> {movie_result.title or '?'} "
                            f"[bright_black](ID {movie_result.external_ids.tmdb_id})",
                            (0, 5),
                        )
                    )
                else:
                    console.print(Padding("Search -> [bright_black]No match found[/]", (0, 5)))

            if self.tmdb_id and getattr(self, "search_source", None) not in ("simkl", "imdb"):
                kind = "tv" if isinstance(title, Episode) else "movie"
                providers.fetch_external_ids(
                    self.tmdb_id, kind, title_cacher, cache_title_id, cache_region, cache_account_hash
                )

            if slow and i != 0:
                delay = random.randint(slow[0], slow[1])
                spinner = Spinner("dots", text=f"Delaying by {delay} seconds...")
                with SyncLive(Padding(spinner, (0, 5)), console=console, refresh_per_second=12.5, transient=True):
                    for seconds_left in range(delay, 0, -1):
                        spinner.update(text=f"Delaying by {seconds_left} seconds...")
                        time.sleep(1)

            with console.status("Subscribing to events...", spinner="dots"):
                events.reset()
                events.subscribe(events.Types.SEGMENT_DOWNLOADED, service.on_segment_downloaded)
                events.subscribe(events.Types.TRACK_DOWNLOADED, service.on_track_downloaded)
                events.subscribe(events.Types.TRACK_DECRYPTED, service.on_track_decrypted)
                events.subscribe(events.Types.TRACK_REPACKED, service.on_track_repacked)
                events.subscribe(events.Types.TRACK_MULTIPLEX, service.on_track_multiplex)

            tracks_label = "Getting Remote Tracks..." if self.is_remote else "Getting Tracks..."
            tracks_spinner = Spinner("dots", text=tracks_label)
            with SyncLive(Padding(tracks_spinner, (0, 5)), console=console, refresh_per_second=12.5, transient=True):
                try:
                    load_tracks(
                        service,
                        title,
                        name=enrich_title,
                        year=enrich_year,
                        language=enrich_lang,
                        workers=workers,
                        on_probe=partial(tracks_spinner.update, text="Probing HDR10 tracks for HDR10+..."),
                    )
                except Exception as e:
                    if self.debug_logger:
                        self.debug_logger.log_error(
                            "get_tracks", e, service=self.service, context={"title": str(title)}
                        )
                    raise

                if self.debug_logger:
                    tracks_info = {
                        "title": str(title),
                        "video_tracks": len(title.tracks.videos),
                        "audio_tracks": len(title.tracks.audio),
                        "subtitle_tracks": len(title.tracks.subtitles),
                        "has_chapters": bool(title.tracks.chapters),
                        "videos": [
                            {
                                "codec": str(v.codec),
                                "resolution": f"{v.width}x{v.height}" if v.width and v.height else "unknown",
                                "bitrate": v.bitrate,
                                "range": str(v.range),
                                "language": str(v.language) if v.language else None,
                                "drm": [str(type(d).__name__) for d in v.drm] if v.drm else [],
                            }
                            for v in title.tracks.videos
                        ],
                        "audio": [
                            {
                                "codec": str(a.codec),
                                "bitrate": a.bitrate,
                                "channels": a.channels,
                                "language": str(a.language) if a.language else None,
                                "descriptive": a.descriptive,
                                "drm": [str(type(d).__name__) for d in a.drm] if a.drm else [],
                            }
                            for a in title.tracks.audio
                        ],
                        "subtitles": [
                            {
                                "codec": str(s.codec),
                                "language": str(s.language) if s.language else None,
                                "forced": s.forced,
                                "sdh": s.sdh,
                            }
                            for s in title.tracks.subtitles
                        ],
                    }
                    self.debug_logger.log(
                        level="INFO", operation="get_tracks", service=self.service, context=tracks_info
                    )

            if dedupe_codecs := config.subtitle.get("dedupe_codecs"):
                codecs = map(SUBTITLE_CODEC.convert, dedupe_codecs)
                title.tracks.dedupe_subtitle_codecs([c for c in codecs if isinstance(c, Subtitle.Codec)])

            # a plain track covers an SDH track only when it is the same variant, as -sl selects them,
            # so en-US SDH is stripped next to a plain en-GB: -sl en-US would otherwise keep only the SDH
            if config.subtitle.get("strip_sdh", True):
                plain_langs = [x.language for x in title.tracks.subtitles if not x.sdh and not x.forced]
                for subtitle in title.tracks.subtitles:
                    if subtitle.sdh and not matching_languages(subtitle.language, plain_langs, exact=True):
                        plain_langs.append(subtitle.language)
                        non_sdh_sub = deepcopy(subtitle)
                        non_sdh_sub.id += "_stripped"
                        non_sdh_sub.sdh = False
                        title.tracks.add(non_sdh_sub)
                        events.subscribe(
                            events.Types.TRACK_MULTIPLEX,
                            lambda track, sub_id=non_sdh_sub.id: (
                                (track.strip_hearing_impaired()) if track.id == sub_id else None
                            ),
                        )

            if real_video_bitrate:
                with console.status("Probing real video bitrates...", spinner="dots"):
                    apply_real_bitrates(
                        title.tracks.videos,
                        service.session,
                        log=self.log,
                        group_key=lambda t: (t.codec, t.range),
                    )

            if real_audio_bitrate:
                with console.status("Probing real audio bitrates...", spinner="dots"):
                    apply_real_bitrates(
                        title.tracks.audio,
                        service.session,
                        log=self.log,
                        group_key=lambda t: (t.codec, t.channels, str(t.language), t.descriptive),
                    )

            with console.status("Sorting tracks by language and bitrate...", spinner="dots"):
                audio_priority = config.audio.get("language_priority") or []
                subtitle_priority = config.subtitle.get("language_priority") or []

                title.tracks.sort_videos(
                    by_language=resolve_sort_langs(v_lang or lang, title.language, exact=exact_lang),
                    exact_match=exact_lang,
                )
                title.tracks.sort_audio(
                    by_language=resolve_sort_langs(
                        [*audio_priority, *(a_lang or lang)], title.language, exact=exact_lang
                    ),
                    codec_priority=config.audio.get("codec_priority"),
                    exact_match=exact_lang,
                )
                title.tracks.sort_subtitles(
                    by_language=resolve_sort_langs(
                        [*subtitle_priority, *(s_lang or [])], title.language, exact=exact_lang
                    ),
                    type_priority=config.subtitle.get("type_priority"),
                    group_by=config.subtitle.get("group_by"),
                    exact_match=exact_lang,
                )

            if list_:
                available_tracks, _ = title.tracks.tree()
                print_listing(listing_panel(available_tracks, "Available Tracks"))
                continue

            if no_proxy_download and any(service.session.proxies.values()):
                console.log("Bypassing proxy for downloads as --no-proxy-download was used...")
            elif proxy_download:
                console.log(f"Using a separate proxy for downloads: {mask_proxy(proxy_download)}")

            outcome = selection.select(title, service.NO_SUBTITLES)
            if isinstance(outcome, Skip):
                skip_title(title, outcome.reason, outcome.kind_only)
                continue
            range_ = outcome.ranges

            selected_tracks, tracks_progress_callables = title.tracks.tree(add_progress=True)

            log_event(
                "track_select",
                level="INFO",
                message=(
                    f"Selected {len(title.tracks.videos)} video, {len(title.tracks.audio)} audio, "
                    f"{len(title.tracks.subtitles)} subtitle track(s)"
                ),
                context={
                    "videos": [
                        f"{v.codec} {v.range} {v.height}p {v.bitrate}bps {v.language}" for v in title.tracks.videos
                    ],
                    "audio": [
                        f"{a.codec} {a.language} {getattr(a, 'channels', None)}ch {a.bitrate}bps"
                        for a in title.tracks.audio
                    ],
                    "subtitles": [f"{s.codec} {s.language}" for s in title.tracks.subtitles],
                },
            )

            if progress_sink is not None:
                from unshackle.core.api.progress import build_job_progress_callables

                tracks_progress_callables = build_job_progress_callables(
                    list(title.tracks), tracks_progress_callables, progress_sink
                )

            drm_pending = [track for track in title.tracks if getattr(track, "needs_drm_loading", False)]
            if drm_pending:
                with ThreadPoolExecutor(min(16, len(drm_pending))) as drm_pool:
                    list(drm_pool.map(lambda track: track.load_drm_if_needed(service), drm_pending))

            download_table = Table.grid()
            download_table.add_row(selected_tracks)

            server_cdm_type = None
            if self._remote_service and self._remote_service.key_arrival.needs_no_cdm:
                server_cdm_type = self._remote_service.server_cdm_type
                self.cdm = cdm_type_stub(server_cdm_type)
                if self._remote_service.key_arrival is KeyArrival.EXPORT:
                    self.log.info("Using keys from the export; no CDM required")
                else:
                    self.log.info(f"Using server CDM ({server_cdm_type.title()}); no local CDM required")

            video_tracks = title.tracks.videos
            if video_tracks and not server_cdm_type:
                highest_quality = max((track.height for track in video_tracks if track.height), default=0)
                system = cdm_system(self.cdm)
                if highest_quality > 0 and system:
                    quality_based_cdm = self.get_cdm(self.service, self.profile, drm=system, quality=highest_quality)
                    if quality_based_cdm and quality_based_cdm != self.cdm:
                        self.log.debug(
                            f"Pre-selecting the {system} CDM for the highest quality {highest_quality}p of all video tracks"
                        )
                        self.cdm = quality_based_cdm

            title_all_drm = all_drm and not server_cdm_type
            if all_drm and server_cdm_type and DOWNLOAD_ALL_DRM.is_set():
                DOWNLOAD_ALL_DRM.clear()
                self.log.warning("--all-drm is ignored: the server CDM licenses this session with one DRM system")
            if title_all_drm:
                heights = {track.height for track in title.tracks.videos if track.height} or {None}
                for system, name in (("widevine", "Widevine"), ("playready", "PlayReady")):
                    system_cdms = [self.cdm_for(system, height) for height in heights]
                    if not all(cdm_system(system_cdm) == system for system_cdm in system_cdms):
                        hint = (
                            "drop --cdm, which selects one device"
                            if self.cdm_override
                            else f"set cdm.{self.service}.{system}"
                        )
                        raise click.ClickException(f"--all-drm needs a {name} CDM for {self.service}: {hint}")
                    if is_remote_cdm(system_cdms[0]) and name not in self.keys.all_drm_warned:
                        self.keys.all_drm_warned.add(name)
                        self.log.warning(f"The {name} CDM is remote and can answer with cached content keys")

            prepare_drm_for = partial(self.keys.licence_for, title=title, table=download_table, all_drm=title_all_drm)
            self.keys.take_server_keys(title)

            if sample and not skip_dl:
                whole_files = [
                    track
                    for track in (*title.tracks.videos, *title.tracks.audio)
                    if not track.can_sample(track.session or service.session)
                ]
                if whole_files:
                    raise click.ClickException(
                        f"--sample cannot cut {len(whole_files)} track(s) of {title}: "
                        f"{self.service} gives each one as one file with no segment index."
                    )

            dl_start_time = time.time()

            try:
                with SyncLive(Padding(download_table, (1, 5)), console=console, refresh_per_second=20):

                    def download_track(
                        track: AnyTrack,
                        i: int,
                        *,
                        title: Title_T,
                        prepare_drm_for: Callable[[AnyTrack], partial],
                        progress_callables: list[partial],
                    ) -> None:
                        track.download(
                            session=track.session or service.session,
                            no_proxy_download=no_proxy_download,
                            proxy_download=proxy_download,
                            prepare_drm=prepare_drm_for(track),
                            cdm=self.cdm,
                            max_workers=workers,
                            adaptive_workers=adaptive_workers,
                            download_processes=download_processes,
                            progress=progress_callables[i],
                        )
                        if (
                            track.path
                            and service.decrypts_samples(track)
                            and not DOWNLOAD_CANCELLED.is_set()
                            and not DOWNLOAD_LICENCE_ONLY.is_set()
                        ):
                            progress = progress_callables[i]
                            progress(downloaded="Decrypting", completed=0, total=None)
                            cbcs.decrypt_file(
                                track.path, lambda _l, entry: service.get_sample_decrypter(track, entry.index)
                            )
                            assert_fragments_decrypted(track.path)
                            progress(downloaded="Decrypted", completed=100, total=100)
                        # DRM-free and HLS-ClearKey tracks never reach prepare_drm, so export here.
                        # drm=None on purpose: licensed tracks already recorded their DRM/keys
                        # in prepare_drm, and write_export merges via setdefault.
                        if export_path:
                            self.write_export(export_path, title, track)

                    def on_subtitle_skipped(track: Subtitle, *, title: Title_T) -> None:
                        lang = str(track.language)
                        self.log.warning(f"Subtitle {lang} failed to download, skipping it.")
                        self.skipped_subtitles.append(SkippedSubtitle(id=track.id, language=lang, title=str(title)))
                        try:
                            title.tracks.subtitles.remove(track)
                        except ValueError:
                            self.log.debug(f"Skipped subtitle {track.id} was already absent from the track list.")

                    skipped_before = len(self.skipped_subtitles)
                    grow_session_pool(service.session, downloads * (workers or default_max_workers()))
                    download_tracks_in_passes(
                        title.tracks,
                        downloads,
                        partial(
                            download_track,
                            title=title,
                            prepare_drm_for=prepare_drm_for,
                            progress_callables=tracks_progress_callables,
                        ),
                        skip_subtitle_errors=skip_subtitle_errors,
                        on_subtitle_skipped=partial(on_subtitle_skipped, title=title),
                    )

                    for attachment in title.tracks.attachments:
                        attachment.download(
                            attachment.session or service.session,
                            no_proxy_download=no_proxy_download,
                            proxy_download=proxy_download,
                        )

                    if (
                        len(self.skipped_subtitles) > skipped_before
                        and not title.tracks.videos
                        and not title.tracks.audio
                        and not title.tracks.subtitles
                    ):
                        self.log.warning(
                            f"{title}: all subtitles were skipped and no video or audio was "
                            "downloaded - nothing was produced for this title."
                        )

            except KeyboardInterrupt:
                console.print(Padding(":x: Download Cancelled...", (0, 5, 1, 5)))
                log_event(
                    "download_tracks",
                    level="WARNING",
                    service=self.service,
                    message="Download cancelled by user",
                    context={"title": str(title)},
                )
                self.keys.wait_vault_writes()
                if export_path:
                    self.log_export_summary()
                return self.outcome()
            except Exception as e:  # noqa
                # Reported and swallowed (no re-raise) so the CLI exits cleanly; keep it so the
                # API worker sees the title failed rather than completing with no output.
                self.download_error = e
                console.print(Padding(Group(*self.failure_lines(e)), (1, 5)))
                if not isinstance(e, click.ClickException) and not retries_exhausted(e):
                    console.print_exception()

                if self.debug_logger:
                    self.debug_logger.log_error(
                        "download_tracks",
                        e,
                        service=self.service,
                        context={
                            "title": str(title),
                            "error_type": type(e).__name__,
                            "tracks_count": len(title.tracks),
                            "returncode": getattr(e, "returncode", None),
                        },
                    )

                dispatch(
                    "failure",
                    "file",
                    build_context(
                        title,
                        service=self.service,
                        ids=self.post_script_ids(),
                        error=f"{type(e).__name__}: {e}",
                    ),
                    postscript,
                )
                self.keys.wait_vault_writes()
                if export_path:
                    self.log_export_summary()
                return self.outcome()

            self.keys.wait_vault_writes()
            if export_path:
                self.log_export_title(title)

            if skip_dl:
                console.log("Skipped downloads as --skip-dl was used...")
            else:
                dl_time = time_elapsed_since(dl_start_time)
                console.print(Padding(f"Track downloads finished in [progress.elapsed]{dl_time}[/]", (0, 5)))

                subtitle_output_mode = config.subtitle.get("output_mode", "mux")
                sidecar_format = config.subtitle.get("sidecar_format", "srt")
                skip_subtitle_mux = subtitle_output_mode == "sidecar" and (title.tracks.videos or title.tracks.audio)
                sidecar_subtitles: list[Subtitle] = []
                sidecar_original_paths: dict[str, Path] = {}
                sidecar_files: dict[Path | None, list[Path]] = {}
                if subtitle_output_mode in ("sidecar", "both") and not no_mux:
                    sidecar_subtitles = [s for s in title.tracks.subtitles if s.path and s.path.exists()]
                    if sidecar_format == "original":
                        config.directories.temp.mkdir(parents=True, exist_ok=True)
                        for subtitle in sidecar_subtitles:
                            assert subtitle.path is not None
                            original_path = (
                                config.directories.temp / f"sidecar_original_{subtitle.id}{subtitle.path.suffix}"
                            )
                            shutil.copy2(subtitle.path, original_path)
                            sidecar_original_paths[subtitle.id] = original_path

                set_subtitle_look(title.tracks.subtitles, ass_style, title.tracks.videos)
                with console.status("Converting Subtitles..."):
                    sub_conversions: dict[tuple[str, str], int] = {}
                    for subtitle in title.tracks.subtitles:
                        if subtitle.codec == Subtitle.Codec.SCC:
                            scc_dst = convert_scc(subtitle, sub_format).name
                            sub_conversions[("SCC", scc_dst)] = sub_conversions.get(("SCC", scc_dst), 0) + 1
                        if sub_format == "original":
                            continue
                        if sub_format:
                            if subtitle.codec != sub_format:
                                src = getattr(subtitle.codec, "name", str(subtitle.codec))
                                dst = getattr(sub_format, "name", str(sub_format))
                                subtitle.convert(sub_format, forced=True)
                                sub_conversions[(src, dst)] = sub_conversions.get((src, dst), 0) + 1
                        elif subtitle.codec == Subtitle.Codec.TimedTextMarkupLang:
                            # MKV does not support TTML, VTT is the next best option
                            src = getattr(subtitle.codec, "name", str(subtitle.codec))
                            subtitle.convert(Subtitle.Codec.WebVTT)
                            sub_conversions[(src, Subtitle.Codec.WebVTT.name)] = (
                                sub_conversions.get((src, Subtitle.Codec.WebVTT.name), 0) + 1
                            )
                    if sample:
                        for subtitle in title.tracks.subtitles:
                            if subtitle.path and subtitle.path.exists():
                                subtitle.trim(*sample)
                    for (src, dst), count in sub_conversions.items():
                        log_event(
                            "subtitle_convert",
                            level="INFO",
                            message=f"Converted {src}->{dst} x{count}",
                            context={"from": src, "to": dst, "count": count},
                        )

                with console.status("Checking Subtitles for Fonts..."):
                    font_names: list[str] = []
                    for subtitle in [] if no_attachments else title.tracks.subtitles:
                        if subtitle.codec in (Subtitle.Codec.SubStationAlpha, Subtitle.Codec.SubStationAlphav4):
                            font_names.extend(Subtitle.extract_fonts(subtitle.path.read_text("utf8")))

                    font_count, missing_fonts = self.attach_subtitle_fonts(font_names, title)

                    if font_count:
                        self.log.info(f"Attached {font_count} fonts for the Subtitles")

                    if missing_fonts and sys.platform != "win32":
                        self.suggest_missing_fonts(missing_fonts)

                # Handle DRM decryption BEFORE repacking (must decrypt first!)
                drm_tracks = [track for track in title.tracks if track.drm]
                if drm_tracks:
                    with console.status("Decrypting tracks..."):
                        has_decrypted = False
                        for track in drm_tracks:
                            drm = track.get_drm_for_cdm(self.cdm)
                            if drm and hasattr(drm, "decrypt"):
                                self.keys.decrypt_verified(drm, track.path, prepare_drm_for(track), track=track)
                                if not isinstance(drm, MonaLisa):
                                    # MonaLisa decrypts per segment; its decrypt() here is a no-op
                                    assert_fragments_decrypted(track.path)
                                    has_decrypted = True
                                events.emit(events.Types.TRACK_REPACKED, track=track)
                            else:
                                self.log.warning(
                                    f"No matching DRM found for track {track} with CDM type {type(self.cdm).__name__}"
                                )
                        if has_decrypted:
                            self.log.info("Decrypted tracks")

                if not no_subs and not service.NO_SUBTITLES and not video_only and not no_video:
                    for video_track_n, video_track in enumerate(title.tracks.videos):
                        has_manifest_cc = bool(getattr(video_track, "closed_captions", None))
                        has_eia_cc = (
                            not has_manifest_cc
                            and not title.tracks.subtitles
                            and any(
                                x.get("codec_name", "").startswith("eia_")
                                for x in ffprobe(video_track.path).get("streams", [])
                            )
                        )
                        if not has_manifest_cc and not has_eia_cc:
                            continue

                        with console.status(f"Checking Video track {video_track_n + 1} for Closed Captions..."):
                            try:
                                cc_lang = (
                                    Language.get(video_track.closed_captions[0]["language"])
                                    if has_manifest_cc and video_track.closed_captions[0].get("language")
                                    else title.language or video_track.language
                                )
                                track_id = f"ccextractor-{video_track.id}"
                                cc = video_track.ccextractor(
                                    track_id=track_id,
                                    out_path=config.directories.temp
                                    / config.filenames.subtitle.format(id=track_id, language=cc_lang),
                                    language=cc_lang,
                                    original=False,
                                )
                                if cc:
                                    cc.cc = True
                                    title.tracks.add(cc)
                                    self.log.info(f"Extracted a Closed Caption from Video track {video_track_n + 1}")
                                else:
                                    self.log.info(f"No Closed Captions were found in Video track {video_track_n + 1}")
                            except OSError:
                                self.log.error(
                                    "Cannot extract Closed Captions as the ccextractor executable was not found..."
                                )
                                break

                # Now repack the decrypted tracks
                if progress_sink and any(getattr(t, "needs_repack", False) for t in title.tracks):
                    progress_sink(
                        {"phase": "repackaging", "progress": 92.0, "status": "downloading", "active_tracks": []}
                    )
                with console.status("Repackaging tracks with FFMPEG..."):
                    has_repacked = False
                    # Video tracks whose VUI was folded into the repack pass; skipped in the VUI loop below.
                    vui_folded = set()
                    for track in title.tracks:
                        if track.needs_repack:
                            bsf = track.vui_bsf() if isinstance(track, Video) else None
                            if track.repackage(bsf_v=bsf):
                                vui_folded.add(id(track))
                            has_repacked = True
                            events.emit(events.Types.TRACK_REPACKED, track=track)
                            if has_encrypted_sample_entry(track.path):
                                self.log.warning(f"Track {track.id} still has an encrypted sample entry after repack")
                    if has_repacked:
                        self.log.info("Repacked one or more tracks with FFMPEG")

                with console.status("Normalizing video VUI..."):
                    for track in title.tracks.videos:
                        if id(track) in vui_folded:
                            continue
                        try:
                            track.normalize_vui()
                        except Exception as e:  # noqa: BLE001
                            self.log.warning(f"VUI normalization skipped for {track.id}: {e}")

                remux_music_tracks(title, self.log, sample)

                placement = Placement(
                    title,
                    root=self.output_dir or config.directories.downloads,
                    no_folder=no_folder,
                    no_source=no_source,
                    sample=bool(sample),
                )
                skip_mux = mux_skipped(no_mux, title)
                kept_audio = [] if skip_mux else take_unmuxable_audio(title.tracks)
                if kept_audio and not title.tracks:
                    title.tracks.audio = kept_audio
                    kept_audio = []
                    skip_mux = True

                if skip_mux:
                    sample_track = next(iter(title.tracks.videos or title.tracks.audio or title.tracks.subtitles), None)
                    final_dir = placement.folder(
                        MediaInfo.parse(sample_track.path)
                        if placement.has_title_folder
                        and sample_track
                        and sample_track.path
                        and sample_track.path.exists()
                        else None
                    )
                    for track in title.tracks:
                        if not (track.path and track.path.exists()):
                            continue
                        fix_data_reference_indexes(track.path)
                        final_path = placement.track_file(final_dir, track, MediaInfo.parse(track.path))
                        shutil.move(track.path, final_path)
                        self.completed_files.append(final_path)
                        self.log.debug(f"Saved: {final_path.name}")
                elif isinstance(title, (Movie, Episode)):
                    outputs, mux_sidecars = mux_title(
                        title,
                        placement,
                        log=self.log,
                        service_tag=self.service,
                        split_audio=split_audio,
                        merge_video=merge_video,
                        audio_expected=not video_only and not no_audio,
                        skip_subtitles=bool(skip_subtitle_mux),
                        quality=quality,
                        range_=range_,
                        vcodec=vcodec,
                        generate_dv=generate_dv,
                        progress_sink=progress_sink,
                        write_sidecars=partial(
                            self.output_subtitle_sidecars,
                            sidecar_subtitles,
                            sidecar_format=sidecar_format,
                            original_paths=sidecar_original_paths or None,
                        )
                        if sidecar_subtitles
                        else None,
                    )
                    sidecar_files.update(mux_sidecars)
                    for temp_path in sidecar_original_paths.values():
                        temp_path.unlink(missing_ok=True)
                else:
                    outputs = [(track.path, None) for track in title.tracks.audio if track.path and track.path.exists()]

                if not skip_mux:
                    for muxed_path, audio_codec in outputs:
                        media_info = MediaInfo.parse(muxed_path)
                        final_path = placement.claim(
                            placement.folder(media_info), placement.name(media_info), muxed_path.suffix, audio_codec
                        )
                        try:
                            os.replace(muxed_path, final_path)
                        except OSError:
                            if final_path.exists():
                                final_path.unlink()
                            shutil.move(muxed_path, final_path)
                        self.completed_files.append(final_path)
                        for kept in kept_audio:
                            assert kept.path is not None
                            kept_path = placement.track_file(
                                final_path.parent, kept, MediaInfo.parse(kept.path), stem=final_path.stem
                            )
                            shutil.move(kept.path, kept_path)
                            self.completed_files.append(kept_path)
                            sidecar_files.setdefault(muxed_path, []).append(kept_path)
                            self.log.info(f"Saved {kept_path.name}: no muxer carries Audio Vivid")
                        kept_audio = []
                        tags.tag_file(
                            final_path,
                            title,
                            self.tmdb_id,
                            self.imdb_id,
                            self.tvdb_id,
                            self.anilist_id,
                            self.anime_hint(title),
                            session=service.session,
                        )
                        if include_sample:
                            self.write_sample(final_path, title, media_info)
                        if isinstance(title, Song) and title.synced_lyrics and title.lyrics_suffix:
                            lyrics_path = final_path.with_suffix(title.lyrics_suffix)
                            lyrics_path.write_text(f"{title.synced_lyrics}\n", encoding="utf-8")
                            sidecar_files.setdefault(muxed_path, []).append(lyrics_path)
                            self.log.info(f"Saved lyrics to {lyrics_path.name}")

                        post_script_context = build_context(
                            title,
                            media_info,
                            filepath=final_path,
                            sidecars=sidecar_files.get(muxed_path, []),
                            service=self.service,
                            ids=self.post_script_ids(),
                        )
                        dispatch("success", "file", post_script_context, postscript)
                        post_script_last.setdefault(post_script_group(title), {})[final_path.parent] = (
                            post_script_context
                        )
                        post_script_sample = post_script_context
                        if final_path.parent not in post_script_folders:
                            post_script_folders.append(final_path.parent)
                    settle_post_script_group(title)

                title_dl_time = time_elapsed_since(dl_start_time)
                downloaded_label = "Track" if isinstance(title, Song) else "Title"
                console.print(
                    Padding(
                        f":tada: {downloaded_label} downloaded in [progress.elapsed]{title_dl_time}[/]!",
                        (0, 5, 1, 5),
                    )
                )

                if progress_sink:
                    progress_sink({"output_files": [str(p) for p in self.completed_files]})

            if service.login_cookies is not None:
                cookie_file = login.get_cookie_path(self.service, self.profile)
                if cookie_file:
                    login.save_cookies(cookie_file, service.login_cookies)

        self.keys.wait_vault_writes()
        service.close()

        if post_script_folders and post_script_sample:
            run_context = dict.fromkeys(post_script_sample, "")
            for folder in post_script_folders:
                run_context["folder"] = str(folder)
                dispatch("success", "run", run_context, postscript)

        if export_path:
            self.log_export_summary()
        if self.skipped_titles:
            if len(self.skipped_titles) == wanted_total:
                stop = f"Skipped all {wanted_total} titles"
                self.log.error(stop)
                raise RunStopped(stop)
            skipped = ", ".join(t["title"] for t in self.skipped_titles)
            self.log.warning(f"Skipped {len(self.skipped_titles)} of {wanted_total} titles: {skipped}")
        dl_time = time_elapsed_since(start_time)

        console.print(Padding(f"Processed all titles in [progress.elapsed]{dl_time}", (0, 5, 1, 5)))
        return self.outcome()

    def write_sample(self, source: Path, title: Title_T, media_info: MediaInfo) -> None:
        """Cut a 60 second sample from a finished file into a ``Sample`` folder next to it.

        The sample starts 2 minutes in. A file shorter than 3 minutes gives its middle 60 seconds.
        mkvmerge cuts only Matroska: an MP4 (3D video, or audio Matroska cannot carry) gets no sample.
        """
        if source.suffix.lower() != ".mkv":
            self.log.warning(f"No sample for {source.name}: mkvmerge cuts only Matroska files")
            return
        duration = float(media_info.general_tracks[0].duration or 0) / 1000
        start: float
        start, end = DEFAULT_SAMPLE
        length = end - start
        if duration < end:
            start = max(0.0, (duration - length) / 2)
        output = source.parent / SAMPLE_DIR / f"{sample_name(source.stem, title)}{source.suffix}"
        output.parent.mkdir(exist_ok=True)
        returncode = subprocess.run(
            [
                str(binaries.MKVToolNix),
                "-q",
                "-o",
                str(output),
                "--split",
                f"parts:{start:.3f}s-{start + length:.3f}s",
                source,
            ]
        ).returncode
        if returncode > 1 or not output.exists():
            self.log.warning(f"mkvmerge wrote no sample for {source.name}")

    def anime_hint(self, title: Title_T | None = None) -> bool:
        """Whether metadata lookups for this title should prefer AniList."""
        per_title = getattr(title, "anime", None)
        return self.service_anime if per_title is None else bool(per_title)

    def daily_hint(self, title: Title_T | None = None) -> bool:
        """Whether this title is daily content named by air date."""
        per_title = getattr(title, "daily", None)
        return self.daily or (self.service_daily if per_title is None else bool(per_title))

    def fill_air_dates(self, titles: Series, tvdb_id: int | None = None) -> None:
        """Fill in missing episode air dates from TVDB.

        Additive only: an air date the service already set is kept.
        """
        tvdb = providers.get_provider("tvdb")
        if not tvdb:
            self.log.debug("Air dates need a tvdb_api_key in your config, skipping.")
            return

        if not tvdb_id:
            self.log.debug("Could not resolve a TVDB ID, skipping air dates.")
            return

        wanted = [t for t in titles if self.daily_hint(t)]
        if not wanted:
            return

        keys = list(dict.fromkeys((t.season, t.number) for t in titles))
        source_order = tvdb.detect_order(tvdb_id, keys)  # type: ignore[attr-defined]
        episodes = tvdb.get_episodes(tvdb_id, source_order)  # type: ignore[attr-defined]
        if not episodes:
            self.log.debug("TVDB listed no episodes for series %s, skipping air dates.", tvdb_id)
            return

        aired_by_key: dict[tuple[int, int], str] = {}
        for episode in episodes:
            season, number = parse_int(episode.get("seasonNumber")), parse_int(episode.get("number"))
            aired = episode.get("aired")
            if season is None or number is None or not aired:
                continue
            aired_by_key[(season, number)] = str(aired)

        # TVDB carries placeholder schedule dates for unaired episodes. Stamping one would
        # name the file with a date the episode never aired on.
        limit = date.today() + timedelta(days=1)

        filled = 0
        for title in wanted:
            if title.air_date is not None:
                continue
            aired = aired_by_key.get((title.season, title.number))
            if not aired:
                continue
            try:
                air_date = date.fromisoformat(aired[:10])
            except ValueError:
                continue
            if air_date.year < 1970 or air_date > limit:
                continue
            title.air_date = air_date
            filled += 1

        if filled:
            self.log.info("Filled air dates for %d episode(s) from TVDB (ID %s).", filled, tvdb_id)

    def fill_absolute_numbers(self, titles: Series, tvdb_id: int | None = None) -> None:
        """Fill in missing absolute episode numbers from TVDB's absolute order.

        Additive only: season and number are never written, and an absolute number the
        service already set is kept.
        """
        tvdb = providers.get_provider("tvdb")
        if not tvdb:
            self.log.debug("Absolute episode numbers need a tvdb_api_key in your config, skipping.")
            return

        if not tvdb_id:
            self.log.debug("Could not resolve a TVDB ID, skipping absolute episode numbers.")
            return

        keys = list(dict.fromkeys((t.season, t.number) for t in titles))
        source_order = tvdb.detect_order(tvdb_id, keys)  # type: ignore[attr-defined]
        if source_order == "absolute":
            # the service already numbers in absolute order; get_order_map would return the
            # empty same-order map, but here the episode numbers are their own fill
            mapping = {key: (*key, None) for key in keys}
        else:
            mapping = tvdb.get_order_map(tvdb_id, "absolute", source_order)  # type: ignore[attr-defined]
        if not mapping:
            self.log.debug("TVDB has no absolute order for series %s, skipping absolute episode numbers.", tvdb_id)
            return

        filled = 0
        for title in titles:
            if title.absolute is not None:
                continue
            entry = mapping.get((title.season, title.number))
            if not entry:
                continue
            title.absolute = entry[1]
            filled += 1

        if filled:
            self.log.info("Filled absolute numbers for %d episode(s) from TVDB (ID %s).", filled, tvdb_id)

    def apply_continuous_episodes(self, titles: Series) -> Series:
        """Number each season on from the last episode of the season before it.

        Season 0 and episode 0 keep their numbers, and a season that already starts past the season before it is kept.
        The Series is rebuilt, as it does not re-sort an episode whose number changed.
        """
        last = 0
        renumbered = 0
        for season, group in groupby(titles, key=attrgetter("season")):
            episodes = [t for t in group if t.number]
            if not season or not episodes:
                continue
            if min(t.number for t in episodes) <= last:
                for t in episodes:
                    t.number += last
                renumbered += len(episodes)
            last = max(t.number for t in episodes)
        self.log.info("Numbered %d episode(s) on from the season before.", renumbered)
        return type(titles)(list(titles))

    def apply_tvdb_order(
        self,
        titles: Series,
        title_cacher: Any = None,
        cache_title_id: str | None = None,
        cache_region: str | None = None,
        cache_account_hash: str | None = None,
    ) -> Series:
        """Renumber episodes from the service's own numbering into a TVDB season order."""
        tvdb = providers.get_provider("tvdb")
        if not tvdb:
            self.log.warning("--tvdb-order needs a tvdb_api_key in your config, skipping renumber.")
            return titles

        tvdb_id = self.tvdb_id
        if not tvdb_id:
            show = titles[0]
            result = providers.resolve_by_ids(
                self.tmdb_id,
                self.imdb_id,
                None,
                title=show.title,
                year=show.year,
                kind="tv",
                title_cacher=title_cacher,
                cache_title_id=cache_title_id,
                cache_region=cache_region,
                cache_account_hash=cache_account_hash,
            )
            tvdb_id = result.external_ids.tvdb_id if result else None
        if not tvdb_id:
            self.log.warning("Could not resolve a TVDB ID for %r, skipping %s renumber.", str(titles), self.tvdb_order)
            return titles
        self.tvdb_id = tvdb_id  # keep the mux tag on the series the renumbering used

        # parts of one episode share a (season, number) slot; without the dedupe the
        # collision check below reads them as several episodes clashing on one slot
        keys = list(dict.fromkeys((t.season, t.number) for t in titles))
        source_order = tvdb.detect_order(tvdb_id, keys)  # type: ignore[attr-defined]
        if source_order == self.tvdb_order:
            self.log.info("%s already lists this series in TVDB %s order.", self.service, self.tvdb_order)
            return titles

        mapping = tvdb.get_order_map(tvdb_id, self.tvdb_order, source_order)  # type: ignore[attr-defined]
        if not mapping:
            return titles

        # an unmapped episode keeps its own numbering and can land on a remapped episode's slot
        renumbered = [mapping.get(key, (*key, None))[:2] for key in keys]
        unmapped = sum(1 for key in keys if key not in mapping)
        collisions = [slot for slot, count in Counter(renumbered).items() if count > 1]
        if collisions:
            self.log.error(
                "TVDB's %s order misses %d episode(s) and puts two in %d slot(s), keeping the %s numbering of %s",
                self.tvdb_order,
                unmapped,
                len(collisions),
                source_order,
                self.service,
            )
            return titles

        for title in titles:
            entry = mapping.get((title.season, title.number))
            if not entry:
                continue
            title.season, title.number, tvdb_name = entry
            if tvdb_name and not title.name:
                title.name = tvdb_name

        if unmapped:
            self.log.warning(
                "%d episode(s) are not in TVDB's %s order and keep their %s numbering.",
                unmapped,
                self.tvdb_order,
                source_order,
            )
        self.log.info("Renumbered episodes from TVDB %s to %s order (ID %s).", source_order, self.tvdb_order, tvdb_id)

        # Series is a SortedKeyList; renumbering in place does not re-sort it
        return type(titles)(list(titles))

    @staticmethod
    def title_to_meta(title: Title_T) -> dict[str, Any]:
        """Capture the title fields needed to rebuild a Title for output naming on import."""
        from unshackle.core import wire

        return wire.title_to_meta(title)

    def write_export(self, export: Path, title: Title_T, track: AnyTrack, drm: Any = None) -> None:
        """Write a shareable mediaexport file usable by ``unshackle import`` and unidl.

        Carries no HTTP session or cookies. The export records the region (country code)
        only when the export used ``--proxy``, as an import geofence. DRM init data and
        content keys live once per title. ``drm`` may be None (DRM-free track) or a DRM
        system without ``to_dict``/``content_keys`` (e.g. ClearKey) - the export still
        records the track, manifest, chapter and attachment info. unshackle's full track
        dicts, title meta and manifest snapshot go under ``x-unshackle`` for its own importer.
        """
        namer: ExportNamer | None = getattr(self, "export_namer", None)
        snapshots = getattr(self, "export_snapshots", None)
        if snapshots is None:
            snapshots = self.export_snapshots = {}
        if str(title.id) not in snapshots:
            try:
                fresh = capture(title.tracks)
                snapshots[str(title.id)] = fresh if any(fresh.values()) else None
            except Exception as e:
                self.log.warning(f"Not saving the manifest of {title.id} in {export}: {e!r}")
                snapshots[str(title.id)] = None
        with self.EXPORT_LOCK:
            if namer and namer.path:
                export = namer.path
            try:
                doc = mediaexport.read(export) if export.is_file() else mediaexport.Document(service_tag=self.service)
            except mediaexport.ExportError as e:
                # a download mid-licensing must not die over the file it exports to
                self.log.warning(f"Not exporting {title.id}: {export} is not a usable export ({e})")
                return
            doc.generator.setdefault("app", "unshackle")
            doc.generator.setdefault("version", __version__)
            if not doc.region and getattr(self, "proxy_requested", False):
                doc.region = (self.export_service and self.export_service.current_region) or ""

            entry = doc.get(str(title.id))
            if entry is None:
                entry = self.export_entry(title)
                doc.add(entry)

            tracks_map = entry.ext("unshackle").setdefault("tracks", {})
            track_dict = tracks_map.setdefault(str(track.id), track.to_dict())
            unwanted = (track.data.get("hls") or {}).get("unwanted_segments")
            if unwanted:
                track_dict["unwanted_segments"] = unwanted
            elif unwanted is not None:
                track_dict.pop("unwanted_segments", None)
            if saved := snapshots[str(title.id)]:
                entry.ext("unshackle")["snapshot"] = saved
            # the row names the track's KIDs, so an import gives the track only their keys
            track_kids = [kid.hex for kid in own_kids(drm)] if drm is not None else []
            for row in entry.tracks:
                if row.get("id") == str(track.id):
                    row["selected"] = True
                    if track_kids:
                        row["kids"] = list(dict.fromkeys([*(row.get("kids") or []), *track_kids]))

            if drm is not None:
                if hasattr(drm, "to_dict"):
                    d = drm.to_dict()
                    own = entry.ext("unshackle").setdefault("drm", [])
                    if d not in own:
                        own.append(d)
                    system = str(d.get("system", "")).lower().replace("cenc", "")
                    pssh = str(d.get("pssh_b64") or "")
                    if system and all(x.pssh != pssh or x.system != system for x in entry.drm):
                        entry.drm.append(mediaexport.Drm(system, pssh))
                content_keys = getattr(drm, "content_keys", None) or {}
                pool = doc.key_pool()
                for kid, key in content_keys.items():
                    # a KID that already holds a different key must not be overwritten:
                    # the reader rejects a file that disagrees with itself
                    if pool.setdefault(kid.hex, key.lower()) != key.lower():
                        self.log.warning(
                            f"KID {kid.hex} already has a different key in {export}, keeping the existing one"
                        )
                        continue
                    try:
                        entry.add_key(kid.hex, key)
                    except mediaexport.ExportError as e:
                        # a key the format cannot hold (not 16 bytes of hex) costs the export that key,
                        # never the download
                        self.log.warning(f"Not exporting the key for KID {kid.hex}: {e}")

            try:
                mediaexport.write(export, doc)
            except (mediaexport.ExportError, OSError) as e:
                self.log.warning(f"Could not write export {export}: {e}")
                return
            if namer:
                self.rename_export(namer, export, title, track)

    def rename_export(self, namer: ExportNamer, export: Path, title: Title_T, track: AnyTrack) -> None:
        """Give the run's export a name that tells what it holds, after each write to it.

        The file on disk is complete after each write, so a run that stops early still leaves
        a usable file with a name that matches what it holds.
        """
        first = namer.path is None
        namer.add(title, track)
        try:
            namer.path = move_export(export, namer.name())
        except OSError as e:
            self.log.warning(f"Could not rename export {export}: {e}")
            namer.path = export
        if first:
            self.log.info(f"Exporting to {namer.path.name}")
        elif namer.path != export:
            self.log.debug(f"Renamed the export to {namer.path.name}")

    def log_export_title(self, title: Title_T) -> None:
        """Log what the export holds for one title: its track counts and its keys."""
        namer: ExportNamer | None = getattr(self, "export_namer", None)
        entry = None
        if namer and namer.path:
            try:
                entry = mediaexport.read(namer.path).get(str(title.id))
            except (mediaexport.ExportError, OSError) as e:
                self.log.warning(f"Could not read export {namer.path}: {e}")
                return
        if entry is None:
            self.log.warning(f"Exported nothing for {title_label(title)}")
            return
        selected = [r.get("type") for r in entry.tracks if r.get("selected")]
        self.log.info(
            f"Exported {title_label(title)}: {selected.count('video')} video, {selected.count('audio')} audio, "
            f"{plural(selected.count('subtitle'), 'subtitle')}, {plural(len(entry.keys), 'key')}"
        )

    def log_export_summary(self) -> None:
        """Log where the run's export is and what it holds, and that it must stay private."""
        namer: ExportNamer | None = getattr(self, "export_namer", None)
        if not namer or not namer.path:
            self.log.warning("Nothing was exported")
            return
        try:
            doc = mediaexport.read(namer.path)
        except (mediaexport.ExportError, OSError) as e:
            self.log.warning(f"Could not read export {namer.path}: {e}")
            return
        keys = len(doc.key_pool())
        self.log.info(f"Saved the export to {namer.path}: {plural(len(doc.titles), 'title')}, {plural(keys, 'key')}")
        if keys:
            self.log.warning("The export holds content keys: keep it private")

    def export_entry(self, title: Title_T) -> mediaexport.Entry:
        """A mediaexport entry for ``title``: manifests, chapters, a small track list, no keys yet."""
        meta = self.title_to_meta(title)
        all_tracks = [*title.tracks.videos, *title.tracks.audio, *title.tracks.subtitles]
        primary_url = title.tracks.manifest_url or next(
            (str(t.url) for t in all_tracks if t.descriptor != Video.Descriptor.URL), ""
        )
        manifest_type = next(
            (t.descriptor.name.lower() for t in all_tracks if t.descriptor != Video.Descriptor.URL), ""
        )
        session = self.export_service.session if self.export_service else None
        # mediaexport drops Cookie and Authorization itself, on write and on read
        headers = dict(getattr(session, "headers", None) or {})
        manifests = (
            [
                mediaexport.Manifest(
                    primary_url,
                    manifest_type,
                    headers,
                    role="primary",
                    content_periods=self.content_periods(all_tracks, primary_url),
                )
            ]
            if primary_url
            else []
        )
        # the whole URL identifies a manifest: two profiles on one endpoint are two manifests
        seen = {primary_url}
        for t in all_tracks:
            url = str(t.url)
            if t.descriptor in (Video.Descriptor.DASH, Video.Descriptor.ISM) and url not in seen:
                seen.add(url)
                manifests.append(
                    mediaexport.Manifest(
                        url,
                        t.descriptor.name.lower(),
                        role="extra",
                        content_periods=self.content_periods(all_tracks, url),
                    )
                )

        rows = []
        for t in all_tracks:
            codec = getattr(t, "codec", None)
            row: dict[str, Any] = {
                "id": str(t.id),
                "type": t.__class__.__name__.lower(),
                # a subtitle side-load names its file format so another tool can fetch it
                "codec": (codec.value if isinstance(t, Subtitle) else codec.name).lower() if codec else "",
                "language": str(t.language) if t.language else "",
                "bitrate": int(bitrate) if (bitrate := getattr(t, "bitrate", None)) else None,
            }
            if isinstance(t, Video):
                row.update(width=t.width, height=t.height, range=t.range.name.lower() if t.range else "")
                if t.stereo:
                    row["stereo"] = True
            elif isinstance(t, Audio):
                row.update(channels=str(t.channels) if t.channels else "", atmos=bool(t.joc), descriptive=t.descriptive)
            elif isinstance(t, Subtitle):
                row.update(sdh=t.sdh, forced=t.forced, cc=t.cc)
            if t.descriptor == Video.Descriptor.URL:
                row["url"] = str(t.url)
            rows.append({k: v for k, v in row.items() if v not in (None, "")})

        return mediaexport.Entry(
            id=str(title.id),
            kind=meta.get("type", "movie"),
            title=meta.get("name") or "",
            series=meta.get("series_title") or "",
            season=meta.get("season"),
            episode=meta.get("number"),
            year=meta.get("year"),
            language=meta.get("language") or "",
            artist=meta.get("artist") or "",
            album=meta.get("album") or "",
            track_number=meta.get("track"),
            manifests=manifests,
            chapters=[
                {"start_ms": mediaexport.ts_ms(c.timestamp), "title": c.name or ""}
                for c in (title.tracks.chapters or [])
            ],
            tracks=rows,
            extensions={
                "x-unshackle": {
                    "meta": meta,
                    "tracks": {},
                    "attachments": [a.to_dict() for a in (title.tracks.attachments or []) if a.url],
                }
            },
        )

    @staticmethod
    def content_periods(tracks: Sequence[Track], url: str) -> list[str]:
        """The ids of the DASH periods a service kept in the manifest at ``url``, empty when it filtered none.

        An import re-fetches the manifest without the service, so its period filter must travel in the export.
        Empty also when a kept period has no id, since an id allow-list cannot name it.
        """
        dash = next(
            (d for t in tracks if (d := t.data.get("dash") or {}).get("filtered_period_ids") and str(t.url) == url),
            None,
        )
        if dash is None:
            return []
        kept = [
            p.get("id") for p in dash["manifest"].findall("Period") if p.get("id") not in dash["filtered_period_ids"]
        ]
        return kept if all(kept) else []

    @staticmethod
    def failure_lines(e: BaseException) -> list[str]:
        """Return the header and the one line that report a failed download.

        A ClickException is a deliberate stop with a message for the user, so it is reported
        without its type, and the caller prints no traceback for it. Neither does a request
        that failed on every retry: its line ends with its retry notes.
        """
        if isinstance(e, click.ClickException):
            line = e.format_message()
        else:
            line = f"{type(e).__name__}: {e}"
            if retries_exhausted(e):
                line += f", {'; '.join(e.__notes__)}"
        return [":x: Download Failed...", f"   {line}"]

    def start_key_resolution(
        self, service: TitleSource, *, cdm_only: bool, vaults_only: bool, best_available: bool, export: Path | None
    ) -> None:
        """Build the run's content-key resolution and make its verified decrypt the decrypt hook."""
        self.keys = ContentKeyResolver(
            self.vaults,
            self,
            service,
            self.service,
            source=self._remote_service,
            cdm_only=cdm_only,
            vaults_only=vaults_only,
            best_available=best_available,
            export=partial(self.write_export, export) if export else None,
            debug_logger=self.debug_logger,
        )
        verify.DECRYPT_HOOK = self.keys.decrypt_verified

    def cdm_for(self, system: str, quality: int | None = None) -> object | None:
        """Get the CDM for one DRM system, ``widevine`` or ``playready``.

        A quality key that names one device applies to both DRM systems. When that device is for
        the other system, the ``widevine`` or ``playready`` config key selects the device.
        """
        cdm = self.get_cdm(self.service, self.profile, drm=system, quality=quality)
        if quality and cdm_system(cdm) != system:
            cdm = self.get_cdm(self.service, self.profile, drm=system)
        return cdm

    def cdm_from_service_entry(self) -> bool:
        """Whether the service's own ``cdm`` entry named the loaded device, not ``--cdm`` or the ``default`` entry."""
        return (
            self.cdm is not None
            and not self.cdm_override
            and cdm_entry_names_device(ci_get(config.cdm, self.service), self.profile)
        )

    def get_cdm(
        self,
        service: str,
        profile: str | None = None,
        drm: str | None = None,
        quality: int | None = None,
    ) -> Any:
        """
        Get CDM for a specified service (either Local or Remote CDM).
        Now supports quality-based selection when the caller gives a quality.
        With no ``drm``, an entry gives the DRM system that it lists first.
        Raises a ValueError if there is a problem getting a CDM.
        """
        # A per-request override (set by the REST API per job) takes precedence over the
        # global config, so a job can select a specific CDM device without mutating shared state.
        cdm_name = resolve_cdm_name(config.cdm, service, getattr(self, "cdm_override", None))
        if not cdm_name:
            return None

        if isinstance(cdm_name, dict):
            if quality:
                quality_match = None
                quality_keys = []

                for key in cdm_name.keys():
                    if (
                        isinstance(key, str)
                        and any(op in key for op in [">=", ">", "<=", "<"])
                        or (isinstance(key, str) and key.isdigit())
                    ):
                        quality_keys.append(key)

                def sort_quality_key(key: str) -> tuple[int, int]:
                    if key.isdigit():
                        return (0, int(key))  # Exact matches first
                    elif key.startswith(">="):
                        return (1, -int(key[2:]))  # >= descending
                    elif key.startswith(">"):
                        return (1, -int(key[1:]))  # > descending
                    elif key.startswith("<="):
                        return (2, int(key[2:]))  # <= ascending
                    elif key.startswith("<"):
                        return (2, int(key[1:]))  # < ascending
                    return (3, 0)  # Other keys last

                quality_keys.sort(key=sort_quality_key)

                for key in quality_keys:
                    if key.isdigit() and quality == int(key):
                        quality_match = cdm_name[key]
                        self.log.debug(f"Selected CDM based on exact quality match {quality}p: {quality_match}")
                        break
                    elif key.startswith(">="):
                        threshold = int(key[2:])
                        if quality >= threshold:
                            quality_match = cdm_name[key]
                            self.log.debug(f"Selected CDM based on quality {quality}p >= {threshold}p: {quality_match}")
                            break
                    elif key.startswith(">"):
                        threshold = int(key[1:])
                        if quality > threshold:
                            quality_match = cdm_name[key]
                            self.log.debug(f"Selected CDM based on quality {quality}p > {threshold}p: {quality_match}")
                            break
                    elif key.startswith("<="):
                        threshold = int(key[2:])
                        if quality <= threshold:
                            quality_match = cdm_name[key]
                            self.log.debug(f"Selected CDM based on quality {quality}p <= {threshold}p: {quality_match}")
                            break
                    elif key.startswith("<"):
                        threshold = int(key[1:])
                        if quality < threshold:
                            quality_match = cdm_name[key]
                            self.log.debug(f"Selected CDM based on quality {quality}p < {threshold}p: {quality_match}")
                            break

                if quality_match:
                    cdm_name = quality_match

            if isinstance(cdm_name, dict):
                cdm_name = choose_cdm(config.cdm, service, profile, drm, entry=cdm_name).device
                if not cdm_name:
                    return None

        from unshackle.core.cdm import load_cdm

        if not isinstance(cdm_name, str):
            return load_cdm(cdm_name, service_name=service, vaults=self.vaults)

        cdm_cache: dict[tuple[str, str | None, str], Any] = getattr(self, "_cdm_cache", {})
        self._cdm_cache = cdm_cache

        cache_key = (service, profile, cdm_name)
        if cache_key not in cdm_cache:
            cdm_cache[cache_key] = load_cdm(cdm_name, service_name=service, vaults=self.vaults)
        return cdm_cache[cache_key]
