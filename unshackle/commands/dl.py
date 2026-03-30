from __future__ import annotations

import html
import logging
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
from concurrent import futures
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from functools import partial
from http.cookiejar import CookieJar, MozillaCookieJar
from itertools import product
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Optional
from uuid import UUID

import click
import jsonpickle
import yaml
from langcodes import Language
from pymediainfo import MediaInfo
from rich.console import Group
from rich.live import Live
from rich.padding import Padding
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskID, TextColumn, TimeRemainingColumn
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from unshackle.core import binaries, providers
from unshackle.core.cdm import DecryptLabsRemoteCDM
from unshackle.core.cdm.detect import is_playready_cdm, is_widevine_cdm
from unshackle.core.config import config
from unshackle.core.console import console
from unshackle.core.constants import DOWNLOAD_LICENCE_ONLY, AnyTrack, context_settings
from unshackle.core.credential import Credential
from unshackle.core.drm import DRM_T, MonaLisa, PlayReady, Widevine
from unshackle.core.events import events
from unshackle.core.proxies import Basic, Gluetun, Hola, NordVPN, SurfsharkVPN, WindscribeVPN
from unshackle.core.service import Service
from unshackle.core.services import Services
from unshackle.core.title_cacher import get_account_hash
from unshackle.core.titles import Movie, Movies, Series, Song, Title_T
from unshackle.core.titles.episode import Episode
from unshackle.core.tracks import Audio, Subtitle, Tracks, Video
from unshackle.core.tracks.attachment import Attachment
from unshackle.core.tracks.hybrid import Hybrid
from unshackle.core.utilities import (find_font_with_fallbacks, get_debug_logger, get_system_fonts, init_debug_logger,
                                      is_close_match, suggest_font_packages, time_elapsed_since)
from unshackle.core.utils import tags
from unshackle.core.utils.click_types import (AUDIO_CODEC_LIST, LANGUAGE_RANGE, QUALITY_LIST, SEASON_RANGE,
                                              ContextData, MultipleChoice, MultipleVideoCodecChoice,
                                              SubtitleCodecChoice)
from unshackle.core.utils.collections import merge_dict
from unshackle.core.utils.selector import select_multiple
from unshackle.core.utils.subprocess import ffprobe
from unshackle.core.vaults import Vaults


class dl:
    @staticmethod
    def truncate_pssh_for_display(pssh_string: str, drm_type: str) -> str:
        """Truncate PSSH string for display when not in debug mode."""
        if logging.root.level == logging.DEBUG or not pssh_string:
            return pssh_string

        max_width = console.width - len(drm_type) - 12
        if len(pssh_string) <= max_width:
            return pssh_string

        return pssh_string[: max_width - 3] + "..."

    def find_custom_font(self, font_name: str) -> Optional[Path]:
        """
        Find font in custom fonts directory.

        Args:
            font_name: Font family name to find

        Returns:
            Path to font file, or None if not found
        """
        family_dir = Path(config.directories.fonts, font_name)
        if family_dir.exists():
            fonts = list(family_dir.glob("*.*tf"))
            return fonts[0] if fonts else None
        return None

    def prepare_temp_font(
        self, font_name: str, matched_font: Path, system_fonts: dict[str, Path], temp_font_files: list[Path]
    ) -> Path:
        """
        Copy system font to temp and log if using fallback.

        Args:
            font_name: Requested font name
            matched_font: Path to matched system font
            system_fonts: Dictionary of available system fonts
            temp_font_files: List to track temp files for cleanup

        Returns:
            Path to temp font file
        """
        # Find the matched name for logging
        matched_name = next((name for name, path in system_fonts.items() if path == matched_font), None)

        if matched_name and matched_name.lower() != font_name.lower():
            self.log.info(f"Using '{matched_name}' as fallback for '{font_name}'")

        # Create unique temp file path
        safe_name = font_name.replace(" ", "_").replace("/", "_")
        temp_path = config.directories.temp / f"font_{safe_name}{matched_font.suffix}"

        # Copy if not already exists
        if not temp_path.exists():
            shutil.copy2(matched_font, temp_path)
            temp_font_files.append(temp_path)

        return temp_path

    def attach_subtitle_fonts(
        self, font_names: list[str], title: Title_T, temp_font_files: list[Path]
    ) -> tuple[int, list[str]]:
        """
        Attach fonts for subtitle rendering.

        Args:
            font_names: List of font names requested by subtitles
            title: Title object to attach fonts to
            temp_font_files: List to track temp files for cleanup

        Returns:
            Tuple of (fonts_attached_count, missing_fonts_list)
        """
        system_fonts = get_system_fonts()

        font_count = 0
        missing_fonts = []

        for font_name in set(font_names):
            # Try custom fonts first
            if custom_font := self.find_custom_font(font_name):
                title.tracks.add(Attachment(path=custom_font, name=f"{font_name} ({custom_font.stem})"))
                font_count += 1
                continue

            # Try system fonts with fallback
            if system_font := find_font_with_fallbacks(font_name, system_fonts):
                temp_path = self.prepare_temp_font(font_name, system_font, system_fonts, temp_font_files)
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
            missing_fonts: List of font names that couldn't be found
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
        target_codec: Optional[Subtitle.Codec] = None,
        source_path: Optional[Path] = None,
    ) -> Path:
        """Generate sidecar path: {base}.{lang}[.forced][.sdh].{ext}"""
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
        original_paths: Optional[dict[str, Path]] = None,
    ) -> list[Path]:
        """Output subtitles as sidecar files, converting if needed."""
        created_paths: list[Path] = []
        config.directories.temp.mkdir(parents=True, exist_ok=True)

        for subtitle in subtitles:
            source_path = subtitle.path
            if sidecar_format == "original" and original_paths and subtitle.id in original_paths:
                source_path = original_paths[subtitle.id]

            if not source_path or not source_path.exists():
                continue

            # Determine target codec
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

            # Copy or convert
            if not target_codec or subtitle.codec == target_codec:
                shutil.copy2(source_path, sidecar_path)
            else:
                # Create temp copy for conversion to preserve original
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
                    temp_sub.convert(target_codec)
                    if temp_sub.path and temp_sub.path.exists():
                        shutil.copy2(temp_sub.path, sidecar_path)
                finally:
                    if temp_sub.path and temp_sub.path.exists():
                        temp_sub.path.unlink(missing_ok=True)
                    temp_path.unlink(missing_ok=True)

            created_paths.append(sidecar_path)

        return created_paths

    @click.command(
        short_help="Download, Decrypt, and Mux tracks for titles from a Service.",
        cls=Services,
        context_settings=dict(**context_settings, default_map=config.dl, token_normalize_func=Services.get_tag),
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
        type=MultipleVideoCodecChoice(Video.Codec),
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
        "--select-titles",
        is_flag=True,
        default=False,
        help="Interactively select downloads from a list. Only use with Series to select Episodes",
    )
    @click.option(
        "-w",
        "--wanted",
        type=SEASON_RANGE,
        default=None,
        help="Wanted episodes, e.g. `S01-S05,S07`, `S01E01-S02E03`, `S02-S02E03`, e.t.c, defaults to all.",
    )
    @click.option(
        "-l",
        "--lang",
        type=LANGUAGE_RANGE,
        default="orig",
        help="Language wanted for Video and Audio. Use 'orig' to select the original language, e.g. 'orig,en' for both original and English.",
    )
    @click.option(
        "--latest-episode",
        is_flag=True,
        default=False,
        help="Download only the single most recent episode available.",
    )
    @click.option(
        "-vl",
        "--v-lang",
        type=LANGUAGE_RANGE,
        default=[],
        help="Language wanted for Video, you would use this if the video language doesn't match the audio.",
    )
    @click.option(
        "-al",
        "--a-lang",
        type=LANGUAGE_RANGE,
        default=[],
        help="Language wanted for Audio, overrides -l/--lang for audio tracks.",
    )
    @click.option("-sl", "--s-lang", type=LANGUAGE_RANGE, default=["all"], help="Language wanted for Subtitles.")
    @click.option(
        "--require-subs",
        type=LANGUAGE_RANGE,
        default=[],
        help="Required subtitle languages. Downloads all subtitles only if these languages exist. Cannot be used with --s-lang.",
    )
    @click.option("-fs", "--forced-subs", is_flag=True, default=False, help="Include forced subtitle tracks.")
    @click.option(
        "--exact-lang",
        is_flag=True,
        default=False,
        help="Use exact language matching (no variants). With this flag, -l es-419 matches ONLY es-419, not es-ES or other variants.",
    )
    @click.option(
        "--proxy",
        type=str,
        default=None,
        help="Proxy URI to use. If a 2-letter country is provided, it will try get a proxy from the config.",
    )
    @click.option(
        "--tag", type=str, default=None, help="Set the Group Tag to be used, overriding the one in config if any."
    )
    @click.option("--repack", is_flag=True, default=False, help="Add REPACK tag to the output filename.")
    @click.option(
        "--tmdb",
        "tmdb_id",
        type=int,
        default=None,
        help="Use this TMDB ID for tagging instead of automatic lookup.",
    )
    @click.option(
        "--animeapi",
        "animeapi_id",
        type=str,
        default=None,
        help="Anime database ID via AnimeAPI (e.g. mal:12345, anilist:98765). Defaults to MAL if no prefix.",
    )
    @click.option(
        "--enrich",
        is_flag=True,
        default=False,
        help="Override show title and year from external source. Requires --tmdb, --imdb, or --animeapi.",
    )
    @click.option(
        "--imdb",
        "imdb_id",
        type=str,
        default=None,
        help="Use this IMDB ID (e.g. tt1375666) for tagging instead of automatic lookup.",
    )
    @click.option(
        "--sub-format",
        type=SubtitleCodecChoice(Subtitle.Codec),
        default=None,
        help="Set Output Subtitle Format, only converting if necessary.",
    )
    @click.option("-V", "--video-only", is_flag=True, default=False, help="Only download video tracks.")
    @click.option("-A", "--audio-only", is_flag=True, default=False, help="Only download audio tracks.")
    @click.option("-S", "--subs-only", is_flag=True, default=False, help="Only download subtitle tracks.")
    @click.option("-C", "--chapters-only", is_flag=True, default=False, help="Only download chapters.")
    @click.option("-ns", "--no-subs", is_flag=True, default=False, help="Do not download subtitle tracks.")
    @click.option("-na", "--no-audio", is_flag=True, default=False, help="Do not download audio tracks.")
    @click.option("-nc", "--no-chapters", is_flag=True, default=False, help="Do not download chapters tracks.")
    @click.option("-nv", "--no-video", is_flag=True, default=False, help="Do not download video tracks.")
    @click.option("-ad", "--audio-description", is_flag=True, default=False, help="Download audio description tracks.")
    @click.option(
        "--slow",
        is_flag=True,
        default=False,
        help="Add a 60-120 second delay between each Title download to act more like a real device. "
        "This is recommended if you are downloading high-risk titles or streams.",
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
    @click.option("--export", type=Path, help="Export Decryption Keys as you obtain them to a JSON file.")
    @click.option(
        "--cdm-only/--vaults-only",
        is_flag=True,
        default=None,
        help="Only use CDM, or only use Key Vaults for retrieval of Decryption Keys.",
    )
    @click.option("--no-proxy", is_flag=True, default=False, help="Force disable all proxy use.")
    @click.option("--no-folder", is_flag=True, default=False, help="Disable folder creation for TV Shows.")
    @click.option(
        "--no-source", is_flag=True, default=False, help="Disable the source tag from the output file name and path."
    )
    @click.option("--no-mux", is_flag=True, default=False, help="Do not mux tracks into a container file.")
    @click.option(
        "--workers",
        type=int,
        default=None,
        help="Max workers/threads to download with per-track. Default depends on the downloader.",
    )
    @click.option("--downloads", type=int, default=1, help="Amount of tracks to download concurrently.")
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
        help="Select the lowest bitrate track within the specified quality. Requires -q/--quality.",
    )
    @click.option(
        "--best-available",
        "best_available",
        is_flag=True,
        default=False,
        help="Continue with best available quality if requested resolutions are not available.",
    )
    @click.option(
        "--remote",
        is_flag=True,
        default=False,
        is_eager=True,
        help="Use a remote unshackle server instead of local service code.",
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

    DRM_TABLE_LOCK = Lock()

    def __init__(
        self,
        ctx: click.Context,
        no_proxy: bool,
        profile: Optional[str] = None,
        proxy: Optional[str] = None,
        repack: bool = False,
        tag: Optional[str] = None,
        tmdb_id: Optional[int] = None,
        imdb_id: Optional[str] = None,
        animeapi_id: Optional[str] = None,
        enrich: bool = False,
        output_dir: Optional[Path] = None,
        *_: Any,
        **__: Any,
    ):
        if not ctx.invoked_subcommand:
            raise ValueError("A subcommand to invoke was not specified, the main code cannot continue.")

        self.log = logging.getLogger("download")

        if not config.output_template:
            raise click.ClickException(
                "No 'output_template' configured in your unshackle.yaml.\n"
                "Please add an 'output_template' section with movies, series, and songs templates.\n"
                "See unshackle-example.yaml for examples."
            )

        self.service = Services.get_tag(ctx.invoked_subcommand)
        service_dl_config = config.services.get(self.service, {}).get("dl", {})
        if service_dl_config:
            param_types = {param.name: param.type for param in ctx.command.params if param.name}

            for param_name, service_value in service_dl_config.items():
                if param_name not in ctx.params:
                    continue

                current_value = ctx.params[param_name]
                global_default = config.dl.get(param_name)
                param_type = param_types.get(param_name)

                try:
                    if param_type and global_default is not None:
                        global_default = param_type.convert(global_default, None, ctx)
                except Exception as e:
                    self.log.debug(f"Failed to convert global default for '{param_name}': {e}")

                if current_value == global_default or (current_value is None and global_default is None):
                    try:
                        converted_value = service_value
                        if param_type and service_value is not None:
                            converted_value = param_type.convert(service_value, None, ctx)

                        ctx.params[param_name] = converted_value
                        self.log.debug(f"Applied service-specific '{param_name}' override: {converted_value}")
                    except Exception as e:
                        self.log.warning(
                            f"Failed to apply service-specific '{param_name}' override: {e}. "
                            f"Check that the value '{service_value}' is valid for this parameter."
                        )

        self.profile = profile
        self.tmdb_id = tmdb_id
        self.imdb_id = imdb_id
        self.enrich = enrich
        self.animeapi_title: Optional[str] = None
        self.output_dir = output_dir

        if animeapi_id:
            from unshackle.core.utils.animeapi import resolve_animeapi

            anime_title, anime_ids = resolve_animeapi(animeapi_id)
            self.animeapi_title = anime_title
            if not self.tmdb_id and anime_ids.tmdb_id:
                self.tmdb_id = anime_ids.tmdb_id
            if not self.imdb_id and anime_ids.imdb_id:
                self.imdb_id = anime_ids.imdb_id

        if self.enrich and not (self.tmdb_id or self.imdb_id or self.animeapi_title):
            raise click.UsageError("--enrich requires --tmdb, --imdb, or --animeapi to provide a metadata source.")

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
                        "animeapi_id": animeapi_id,
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
                                "animeapi_id",
                                "enrich",
                            ]
                        },
                    },
                )

                # Log binary versions for diagnostics
                binary_versions = {}
                for name, binary in [
                    ("shaka_packager", binaries.ShakaPackager),
                    ("mp4decrypt", binaries.Mp4decrypt),
                    ("n_m3u8dl_re", binaries.N_m3u8DL_RE),
                    ("mkvmerge", binaries.MKVToolNix),
                    ("ffmpeg", binaries.FFMPEG),
                    ("ffprobe", binaries.FFProbe),
                ]:
                    if binary:
                        version = None
                        try:
                            if name == "shaka_packager":
                                r = subprocess.run(
                                    [str(binary), "--version"], capture_output=True, text=True, timeout=5
                                )
                                version = (r.stdout or r.stderr or "").strip()
                            elif name in ("ffmpeg", "ffprobe"):
                                r = subprocess.run([str(binary), "-version"], capture_output=True, text=True, timeout=5)
                                version = (r.stdout or "").split("\n")[0].strip()
                            elif name == "mkvmerge":
                                r = subprocess.run(
                                    [str(binary), "--version"], capture_output=True, text=True, timeout=5
                                )
                                version = (r.stdout or "").strip()
                            elif name == "mp4decrypt":
                                r = subprocess.run([str(binary)], capture_output=True, text=True, timeout=5)
                                output = (r.stdout or "") + (r.stderr or "")
                                lines = [line.strip() for line in output.split("\n") if line.strip()]
                                version = " | ".join(lines[:2]) if lines else None
                            elif name == "n_m3u8dl_re":
                                r = subprocess.run(
                                    [str(binary), "--version"], capture_output=True, text=True, timeout=5
                                )
                                version = (r.stdout or r.stderr or "").strip().split("\n")[0]
                        except Exception:
                            version = "<error getting version>"
                        binary_versions[name] = {"path": str(binary), "version": version}
                    else:
                        binary_versions[name] = None

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
            self.service_config = {}
            if not self.is_remote:
                try:
                    service_config_path = Services.get_path(self.service) / config.filenames.config
                    if service_config_path.exists():
                        self.service_config = yaml.safe_load(service_config_path.read_text(encoding="utf8"))
                        self.log.info("Service Config loaded")
                        if self.debug_logger:
                            self.debug_logger.log(
                                level="DEBUG",
                                operation="load_service_config",
                                service=self.service,
                                context={"config_path": str(service_config_path), "config": self.service_config},
                            )
                except KeyError:
                    pass
            merge_dict(config.services.get(self.service), self.service_config)

        if getattr(config, "downloader_map", None):
            config.downloader = config.downloader_map.get(self.service, config.downloader)

        if getattr(config, "decryption_map", None):
            config.decryption = config.decryption_map.get(self.service, config.decryption)

        service_config = config.services.get(self.service, {})
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

        cdm_only = ctx.params.get("cdm_only")

        if cdm_only:
            self.vaults = Vaults(self.service)
            self.log.info("CDM-only mode: Skipping vault loading")
            if self.debug_logger:
                self.debug_logger.log(
                    level="INFO",
                    operation="vault_loading_skipped",
                    service=self.service,
                    context={"reason": "cdm_only flag set"},
                )
        else:
            with console.status("Loading Key Vaults...", spinner="dots"):
                self.vaults = Vaults(self.service)
                total_vaults = len(config.key_vaults)
                failed_vaults = []

                for vault in config.key_vaults:
                    vault_type = vault["type"]
                    vault_name = vault.get("name", vault_type)
                    vault_copy = vault.copy()
                    del vault_copy["type"]

                    if vault_type.lower() == "api" and "decrypt_labs" in vault_name.lower():
                        if "token" not in vault_copy or not vault_copy["token"]:
                            if config.decrypt_labs_api_key:
                                vault_copy["token"] = config.decrypt_labs_api_key
                            else:
                                self.log.warning(
                                    f"No token provided for DecryptLabs vault '{vault_name}' and no global "
                                    "decrypt_labs_api_key configured"
                                )

                    if vault_type.lower() == "sqlite":
                        try:
                            self.vaults.load_critical(vault_type, **vault_copy)
                            self.log.debug(f"Successfully loaded vault: {vault_name} ({vault_type})")
                        except Exception as e:
                            self.log.error(f"vault failure: {vault_name} ({vault_type}) - {e}")
                            raise
                    else:
                        # Other vaults (MySQL, HTTP, API) - soft fail
                        if not self.vaults.load(vault_type, **vault_copy):
                            failed_vaults.append(vault_name)
                            self.log.debug(f"Failed to load vault: {vault_name} ({vault_type})")
                        else:
                            self.log.debug(f"Successfully loaded vault: {vault_name} ({vault_type})")

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
                self.log.error(f"Failed to load CDM, {e}")
                if self.debug_logger:
                    self.debug_logger.log_error("load_cdm", e, service=self.service)
                sys.exit(1)

            if self.cdm:
                cdm_info = {}
                if isinstance(self.cdm, DecryptLabsRemoteCDM):
                    drm_type = "PlayReady" if self.cdm.is_playready else "Widevine"
                    self.log.info(f"Loaded {drm_type} Remote CDM: DecryptLabs (L{self.cdm.security_level})")
                    cdm_info = {"type": "DecryptLabs", "drm_type": drm_type, "security_level": self.cdm.security_level}
                elif hasattr(self.cdm, "device_type") and self.cdm.device_type.name in ["ANDROID", "CHROME"]:
                    self.log.info(f"Loaded Widevine CDM: {self.cdm.system_id} (L{self.cdm.security_level})")
                    cdm_info = {
                        "type": "Widevine",
                        "system_id": self.cdm.system_id,
                        "security_level": self.cdm.security_level,
                        "device_type": self.cdm.device_type.name,
                    }
                else:
                    # Handle both local PlayReady CDM and RemoteCdm (which has certificate_chain=None)
                    is_remote = self.cdm.certificate_chain is None and hasattr(self.cdm, "device_name")
                    if is_remote:
                        cdm_name = self.cdm.device_name
                        self.log.info(f"Loaded PlayReady Remote CDM: {cdm_name} (L{self.cdm.security_level})")
                    else:
                        cdm_name = self.cdm.certificate_chain.get_name() if self.cdm.certificate_chain else "Unknown"
                        self.log.info(f"Loaded PlayReady CDM: {cdm_name} (L{self.cdm.security_level})")
                    cdm_info = {
                        "type": "PlayReady",
                        "certificate": cdm_name,
                        "security_level": self.cdm.security_level,
                    }

                if self.debug_logger and cdm_info:
                    self.debug_logger.log(
                        level="INFO", operation="load_cdm", service=self.service, context={"cdm": cdm_info}
                    )

        self.proxy_providers = []
        if no_proxy:
            ctx.params["proxy"] = None
        else:
            with console.status("Loading Proxy Providers...", spinner="dots"):
                if config.proxy_providers.get("basic"):
                    self.proxy_providers.append(Basic(**config.proxy_providers["basic"]))
                if config.proxy_providers.get("nordvpn"):
                    self.proxy_providers.append(NordVPN(**config.proxy_providers["nordvpn"]))
                if config.proxy_providers.get("surfsharkvpn"):
                    self.proxy_providers.append(SurfsharkVPN(**config.proxy_providers["surfsharkvpn"]))
                if config.proxy_providers.get("windscribevpn"):
                    self.proxy_providers.append(WindscribeVPN(**config.proxy_providers["windscribevpn"]))
                if config.proxy_providers.get("gluetun"):
                    self.proxy_providers.append(Gluetun(**config.proxy_providers["gluetun"]))
                if binaries.HolaProxy:
                    self.proxy_providers.append(Hola())
                for proxy_provider in self.proxy_providers:
                    self.log.info(f"Loaded {proxy_provider.__class__.__name__}: {proxy_provider}")

            if proxy:
                requested_provider = None
                if re.match(r"^[a-z]+:.+$", proxy, re.IGNORECASE):
                    # requesting proxy from a specific proxy provider
                    requested_provider, proxy = proxy.split(":", maxsplit=1)
                # Match simple region codes (us, ca, uk1) or provider:region format (nordvpn:ca, windscribe:us)
                if re.match(r"^[a-z]{2}(?:\d+)?$", proxy, re.IGNORECASE) or re.match(
                    r"^[a-z]+:[a-z]{2}(?:\d+)?$", proxy, re.IGNORECASE
                ):
                    proxy = proxy.lower()
                    # Preserve the original user query (region code) for service-specific proxy_map overrides.
                    # NOTE: `proxy` may be overwritten with the resolved proxy URI later.
                    proxy_query = proxy
                    status_msg = (
                        f"Connecting to VPN ({proxy})..."
                        if requested_provider == "gluetun"
                        else f"Getting a Proxy to {proxy}..."
                    )
                    with console.status(status_msg, spinner="dots"):
                        if requested_provider:
                            proxy_provider = next(
                                (x for x in self.proxy_providers if x.__class__.__name__.lower() == requested_provider),
                                None,
                            )
                            if not proxy_provider:
                                self.log.error(f"The proxy provider '{requested_provider}' was not recognised.")
                                sys.exit(1)
                            proxy_uri = proxy_provider.get_proxy(proxy)
                            if not proxy_uri:
                                self.log.error(f"The proxy provider {requested_provider} had no proxy for {proxy}")
                                sys.exit(1)
                            proxy = ctx.params["proxy"] = proxy_uri
                            # Show connection info for Gluetun (IP, location) instead of proxy URL
                            if hasattr(proxy_provider, "get_connection_info"):
                                conn_info = proxy_provider.get_connection_info(proxy_query)
                                if conn_info and conn_info.get("public_ip"):
                                    location_parts = [conn_info.get("city"), conn_info.get("country")]
                                    location = ", ".join(p for p in location_parts if p)
                                    self.log.info(f"VPN Connected: {conn_info['public_ip']} ({location})")
                                else:
                                    self.log.info(f"Using {proxy_provider.__class__.__name__} Proxy: {proxy}")
                            else:
                                self.log.info(f"Using {proxy_provider.__class__.__name__} Proxy: {proxy}")
                        else:
                            for proxy_provider in self.proxy_providers:
                                proxy_uri = proxy_provider.get_proxy(proxy)
                                if proxy_uri:
                                    proxy = ctx.params["proxy"] = proxy_uri
                                    # Show connection info for Gluetun (IP, location) instead of proxy URL
                                    if hasattr(proxy_provider, "get_connection_info"):
                                        conn_info = proxy_provider.get_connection_info(proxy_query)
                                        if conn_info and conn_info.get("public_ip"):
                                            location_parts = [conn_info.get("city"), conn_info.get("country")]
                                            location = ", ".join(p for p in location_parts if p)
                                            self.log.info(f"VPN Connected: {conn_info['public_ip']} ({location})")
                                        else:
                                            self.log.info(f"Using {proxy_provider.__class__.__name__} Proxy: {proxy}")
                                    else:
                                        self.log.info(f"Using {proxy_provider.__class__.__name__} Proxy: {proxy}")
                                    break
                    # Store proxy query info for service-specific overrides
                    ctx.params["proxy_query"] = proxy_query
                    ctx.params["proxy_provider"] = requested_provider
                else:
                    self.log.info(f"Using explicit Proxy: {proxy}")
                    # For explicit proxies, store None for query/provider
                    ctx.params["proxy_query"] = None
                    ctx.params["proxy_provider"] = None

        ctx.obj = ContextData(
            config=self.service_config, cdm=self.cdm, proxy_providers=self.proxy_providers, profile=self.profile
        )

        if repack:
            config.repack = True

        if tag:
            config.tag = tag

        # needs to be added this way instead of @cli.result_callback to be
        # able to keep `self` as the first positional
        self.cli._result_callback = self.result

    def result(
        self,
        service: Service,
        quality: list[int],
        vcodec: list[Video.Codec],
        acodec: list[Audio.Codec],
        vbitrate: int,
        abitrate: int,
        vbitrate_range: Optional[str],
        abitrate_range: Optional[str],
        range_: list[Video.Range],
        channels: float,
        no_atmos: bool,
        select_titles: bool,
        wanted: list[str],
        latest_episode: bool,
        lang: list[str],
        v_lang: list[str],
        a_lang: list[str],
        s_lang: list[str],
        require_subs: list[str],
        forced_subs: bool,
        exact_lang: bool,
        sub_format: Optional[Subtitle.Codec],
        video_only: bool,
        audio_only: bool,
        subs_only: bool,
        chapters_only: bool,
        no_subs: bool,
        no_audio: bool,
        no_chapters: bool,
        no_video: bool,
        audio_description: bool,
        slow: bool,
        list_: bool,
        list_titles: bool,
        skip_dl: bool,
        export: Optional[Path],
        cdm_only: Optional[bool],
        no_proxy: bool,
        no_folder: bool,
        no_source: bool,
        no_mux: bool,
        workers: Optional[int],
        downloads: int,
        worst: bool,
        best_available: bool,
        split_audio: Optional[bool] = None,
        *_: Any,
        **__: Any,
    ) -> None:
        self.tmdb_searched = False
        self.search_source = None
        self.server_cdm = getattr(service, "_server_cdm", False)
        self._remote_service = service if self.server_cdm else None
        start_time = time.time()

        if skip_dl:
            DOWNLOAD_LICENCE_ONLY.set()

        # Parse bitrate range options
        vbitrate_min, vbitrate_max = None, None
        if vbitrate_range:
            if vbitrate and vbitrate_range:
                self.log.error("Cannot use both --vbitrate and --vbitrate-range at the same time.")
                sys.exit(1)
            try:
                parts = vbitrate_range.split("-")
                if len(parts) != 2:
                    raise ValueError
                vbitrate_min, vbitrate_max = int(parts[0]), int(parts[1])
                if vbitrate_min > vbitrate_max:
                    vbitrate_min, vbitrate_max = vbitrate_max, vbitrate_min
            except (ValueError, IndexError):
                self.log.error("Invalid --vbitrate-range format. Use 'MIN-MAX' (e.g., '6000-7000').")
                sys.exit(1)

        abitrate_min, abitrate_max = None, None
        if abitrate_range:
            if abitrate and abitrate_range:
                self.log.error("Cannot use both --abitrate and --abitrate-range at the same time.")
                sys.exit(1)
            try:
                parts = abitrate_range.split("-")
                if len(parts) != 2:
                    raise ValueError
                abitrate_min, abitrate_max = int(parts[0]), int(parts[1])
                if abitrate_min > abitrate_max:
                    abitrate_min, abitrate_max = abitrate_max, abitrate_min
            except (ValueError, IndexError):
                self.log.error("Invalid --abitrate-range format. Use 'MIN-MAX' (e.g., '128-256').")
                sys.exit(1)

        if not acodec:
            acodec = []
        elif isinstance(acodec, Audio.Codec):
            acodec = [acodec]
        elif isinstance(acodec, str) or (
            isinstance(acodec, list) and not all(isinstance(v, Audio.Codec) for v in acodec)
        ):
            acodec = AUDIO_CODEC_LIST.convert(acodec)

        if require_subs and s_lang != ["all"]:
            self.log.error("--require-subs and --s-lang cannot be used together")
            sys.exit(1)

        if worst and not quality:
            self.log.error("--worst requires -q/--quality to be specified")
            sys.exit(1)

        if select_titles and wanted:
            self.log.error("--select-titles and -w/--wanted cannot be used together")
            sys.exit(1)

        # Check if dovi_tool is available when hybrid mode is requested
        if any(r == Video.Range.HYBRID for r in range_):
            from unshackle.core.binaries import DoviTool

            if not DoviTool:
                self.log.error("Unable to run hybrid mode: dovi_tool not detected")
                self.log.error("Please install dovi_tool from https://github.com/quietvoid/dovi_tool")
                sys.exit(1)

        if cdm_only is None:
            vaults_only = None
        else:
            vaults_only = not cdm_only

        if self.debug_logger:
            self.debug_logger.log(
                level="DEBUG",
                operation="drm_mode_config",
                service=self.service,
                context={
                    "cdm_only": cdm_only,
                    "vaults_only": vaults_only,
                    "mode": "CDM only" if cdm_only else ("Vaults only" if vaults_only else "Both CDM and Vaults"),
                },
            )

        with console.status("Authenticating with Remote Service..." if self.is_remote else "Authenticating with Service...", spinner="dots"):
            try:
                cookies = self.get_cookie_jar(self.service, self.profile)
                credential = self.get_credentials(self.service, self.profile)
                service.authenticate(cookies, credential)
                if cookies or credential:
                    self.log.info("Authenticated with Service")
                    if self.debug_logger:
                        self.debug_logger.log(
                            level="INFO",
                            operation="authenticate",
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

        with console.status("Fetching Remote Title Metadata..." if self.is_remote else "Fetching Title Metadata...", spinner="dots"):
            try:
                titles = service.get_titles_cached()
                if not titles:
                    self.log.error("No titles returned, nothing to download...")
                    if self.debug_logger:
                        self.debug_logger.log(
                            level="ERROR",
                            operation="get_titles",
                            service=self.service,
                            message="No titles returned from service",
                            success=False,
                        )
                    sys.exit(1)
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

        title_cacher = service.title_cache if hasattr(service, "title_cache") else None
        cache_title_id = None
        if hasattr(service, "title"):
            cache_title_id = service.title
        elif hasattr(service, "title_id"):
            cache_title_id = service.title_id
        cache_region = service.current_region if hasattr(service, "current_region") else None
        cache_account_hash = get_account_hash(service.credential) if hasattr(service, "credential") else None

        if self.enrich:
            sample_title = titles[0] if hasattr(titles, "__getitem__") else titles
            kind = "tv" if isinstance(sample_title, Episode) else "movie"

            enrich_title: Optional[str] = None
            enrich_year: Optional[int] = None

            if self.animeapi_title:
                enrich_title = self.animeapi_title

            if self.tmdb_id:
                if not enrich_title:
                    enrich_title = providers.get_title_by_id(
                        self.tmdb_id, kind, title_cacher, cache_title_id, cache_region, cache_account_hash
                    )
                enrich_year = providers.get_year_by_id(
                    self.tmdb_id, kind, title_cacher, cache_title_id, cache_region, cache_account_hash
                )
            elif self.imdb_id:
                imdbapi = providers.get_provider("imdbapi")
                if imdbapi:
                    imdb_result = imdbapi.get_by_id(self.imdb_id, kind)
                    if imdb_result:
                        if not enrich_title:
                            enrich_title = imdb_result.title
                        enrich_year = imdb_result.year

            if enrich_title or enrich_year:
                if isinstance(titles, (Series, Movies)):
                    for t in titles:
                        if enrich_title:
                            if isinstance(t, Episode):
                                t.title = enrich_title
                            else:
                                t.name = enrich_title
                        if enrich_year and not t.year:
                            t.year = enrich_year
                else:
                    if enrich_title:
                        if isinstance(titles, Episode):
                            titles.title = enrich_title
                        else:
                            titles.name = enrich_title
                    if enrich_year and not titles.year:
                        titles.year = enrich_year

        console.print(Padding(Rule(f"[rule.text]{titles.__class__.__name__}: {titles}"), (1, 2)))

        console.print(Padding(titles.tree(verbose=list_titles), (0, 5)))
        if list_titles:
            return

        # Enables manual selection for Series when --select-titles is set
        if select_titles and isinstance(titles, Series):
            console.print(Padding(Rule("[rule.text]Select Titles"), (1, 2)))

            selection_titles = []
            dependencies = {}
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
                    # Note: Headers are not mapped to actual title indices

                # Format display name
                display_name = ((t.name[:30].rstrip() + "…") if len(t.name) > 30 else t.name) if t.name else None

                # Apply indentation only for multiple seasons
                prefix = " " if multiple_seasons else ""
                option_text = f"{prefix}{t.number}" + (f". {display_name}" if t.name else "")

                selection_titles.append(option_text)
                current_ui_idx = len(selection_titles) - 1

                # Map UI index to actual title index
                original_indices[current_ui_idx] = i

                # Link episode to season header for group selection
                if current_season_header_idx != -1:
                    dependencies[current_season_header_idx].append(current_ui_idx)

            selection_start = time.time()

            # Execute selector with dependencies (headers select all children)
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
                return

            selection_end = time.time()
            start_time += selection_end - selection_start

            # Map UI indices back to title indices (excluding headers)
            selected_idx = []
            for idx in selected_ui_idx:
                if idx in original_indices:
                    selected_idx.append(original_indices[idx])

            # Ensure indices are unique and ordered
            selected_idx = sorted(set(selected_idx))
            keep = set(selected_idx)

            # In-place filter: remove unselected items (iterate backwards)
            for i in range(len(titles) - 1, -1, -1):
                if i not in keep:
                    del titles[i]

            # Show selected count
            if titles:
                count = len(titles)
                console.print(Padding(f"[text]Total selected: {count}[/]", (0, 5)))

        # Determine the latest episode if --latest-episode is set
        latest_episode_id = None
        if latest_episode and isinstance(titles, Series) and len(titles) > 0:
            # Series is already sorted by (season, number, year)
            # The last episode in the sorted list is the latest
            latest_ep = titles[-1]
            latest_episode_id = f"{latest_ep.season}x{latest_ep.number}"
            self.log.info(f"Latest episode mode: Selecting S{latest_ep.season:02}E{latest_ep.number:02}")

        for i, title in enumerate(titles):
            if isinstance(title, Episode) and latest_episode and latest_episode_id:
                # If --latest-episode is set, only process the latest episode
                if f"{title.season}x{title.number}" != latest_episode_id:
                    continue
            elif isinstance(title, Episode) and wanted and f"{title.season}x{title.number}" not in wanted:
                continue

            console.print(Padding(Rule(f"[rule.text]{title}"), (1, 2)))
            temp_font_files = []

            if isinstance(title, Episode) and not self.tmdb_searched:
                kind = "tv"
                tmdb_title: Optional[str] = None
                if self.tmdb_id:
                    tmdb_title = providers.get_title_by_id(
                        self.tmdb_id, kind, title_cacher, cache_title_id, cache_region, cache_account_hash
                    )
                else:
                    result = providers.search_metadata(
                        title.title, title.year, kind, title_cacher, cache_title_id, cache_region, cache_account_hash
                    )
                    if result and result.title and providers.fuzzy_match(result.title, title.title):
                        self.tmdb_id = result.external_ids.tmdb_id
                        tmdb_title = result.title
                        self.search_source = result.source
                    else:
                        self.tmdb_id = None
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
                movie_result = providers.search_metadata(
                    title.name, title.year, "movie", title_cacher, cache_title_id, cache_region, cache_account_hash
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

            if self.tmdb_id and getattr(self, "search_source", None) not in ("simkl", "imdbapi"):
                kind = "tv" if isinstance(title, Episode) else "movie"
                providers.fetch_external_ids(
                    self.tmdb_id, kind, title_cacher, cache_title_id, cache_region, cache_account_hash
                )

            if slow and i != 0:
                delay = random.randint(60, 120)
                with console.status(f"Delaying by {delay} seconds..."):
                    time.sleep(delay)

            with console.status("Subscribing to events...", spinner="dots"):
                events.reset()
                events.subscribe(events.Types.SEGMENT_DOWNLOADED, service.on_segment_downloaded)
                events.subscribe(events.Types.TRACK_DOWNLOADED, service.on_track_downloaded)
                events.subscribe(events.Types.TRACK_DECRYPTED, service.on_track_decrypted)
                events.subscribe(events.Types.TRACK_REPACKED, service.on_track_repacked)
                events.subscribe(events.Types.TRACK_MULTIPLEX, service.on_track_multiplex)

            if hasattr(service, "NO_SUBTITLES") and service.NO_SUBTITLES:
                console.log("Skipping subtitles - service does not support subtitle downloads")
                no_subs = True
                s_lang = None
                title.tracks.subtitles = []
            elif no_subs:
                console.log("Skipped subtitles as --no-subs was used...")
                s_lang = None
                title.tracks.subtitles = []

            if no_video:
                console.log("Skipped video as --no-video was used...")
                v_lang = None
                title.tracks.videos = []

            if no_audio:
                console.log("Skipped audio as --no-audio was used...")
                a_lang = None
                title.tracks.audio = []

            if no_chapters:
                console.log("Skipped chapters as --no-chapters was used...")
                title.tracks.chapters = []

            tracks_label = "Getting Remote Tracks..." if self.is_remote else "Getting Tracks..."
            with console.status(tracks_label, spinner="dots"):
                try:
                    title.tracks.add(service.get_tracks(title), warn_only=True)
                    title.tracks.chapters = service.get_chapters(title)
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

            # strip SDH subs to non-SDH if no equivalent same-lang non-SDH is available
            # uses a loose check, e.g, wont strip en-US SDH sub if a non-SDH en-GB is available
            # Check if automatic SDH stripping is enabled in config
            if config.subtitle.get("strip_sdh", True):
                for subtitle in title.tracks.subtitles:
                    if subtitle.sdh and not any(
                        is_close_match(subtitle.language, [x.language])
                        for x in title.tracks.subtitles
                        if not x.sdh and not x.forced
                    ):
                        non_sdh_sub = deepcopy(subtitle)
                        non_sdh_sub.id += "_stripped"
                        non_sdh_sub.sdh = False
                        title.tracks.add(non_sdh_sub)
                        events.subscribe(
                            events.Types.TRACK_MULTIPLEX,
                            lambda track, sub_id=non_sdh_sub.id: (track.strip_hearing_impaired())
                            if track.id == sub_id
                            else None,
                        )

            with console.status("Sorting tracks by language and bitrate...", spinner="dots"):
                video_sort_lang = v_lang or lang
                processed_video_sort_lang = []
                for language in video_sort_lang:
                    if language == "orig":
                        if title.language:
                            orig_lang = str(title.language) if hasattr(title.language, "__str__") else title.language
                            if orig_lang not in processed_video_sort_lang:
                                processed_video_sort_lang.append(orig_lang)
                    else:
                        if language not in processed_video_sort_lang:
                            processed_video_sort_lang.append(language)

                audio_sort_lang = a_lang or lang
                processed_audio_sort_lang = []
                for language in audio_sort_lang:
                    if language == "orig":
                        if title.language:
                            orig_lang = str(title.language) if hasattr(title.language, "__str__") else title.language
                            if orig_lang not in processed_audio_sort_lang:
                                processed_audio_sort_lang.append(orig_lang)
                    else:
                        if language not in processed_audio_sort_lang:
                            processed_audio_sort_lang.append(language)

                title.tracks.sort_videos(by_language=processed_video_sort_lang)
                title.tracks.sort_audio(by_language=processed_audio_sort_lang)
                title.tracks.sort_subtitles(by_language=s_lang)

            if list_:
                available_tracks, _ = title.tracks.tree()
                console.print(Padding(Panel(available_tracks, title="Available Tracks"), (0, 5)))
                continue

            with console.status("Selecting tracks...", spinner="dots"):
                if isinstance(title, (Movie, Episode)):
                    # filter video tracks
                    if vcodec:
                        title.tracks.select_video(lambda x: x.codec in vcodec)
                        missing_codecs = [c for c in vcodec if not any(x.codec == c for x in title.tracks.videos)]
                        for codec in missing_codecs:
                            self.log.warning(f"Skipping {codec.name} video tracks as none are available.")
                        if not title.tracks.videos:
                            self.log.error(f"There's no {', '.join(c.name for c in vcodec)} Video Track...")
                            sys.exit(1)

                    if range_:
                        # Special handling for HYBRID - don't filter, keep all HDR10 and DV tracks
                        if Video.Range.HYBRID not in range_:
                            title.tracks.select_video(lambda x: x.range in range_)
                            missing_ranges = [r for r in range_ if not any(x.range == r for x in title.tracks.videos)]
                            for color_range in missing_ranges:
                                self.log.warning(f"Skipping {color_range.name} video tracks as none are available.")
                            if not title.tracks.videos:
                                self.log.error(f"There's no {', '.join(r.name for r in range_)} Video Track...")
                                sys.exit(1)

                    if vbitrate:
                        if any(r == Video.Range.HYBRID for r in range_):
                            # In HYBRID mode, only apply bitrate filter to non-DV tracks
                            # DV tracks are kept regardless since they're only used for RPU metadata
                            title.tracks.select_video(
                                lambda x: x.range == Video.Range.DV or (x.bitrate and x.bitrate // 1000 == vbitrate)
                            )
                            if not any(x.range != Video.Range.DV for x in title.tracks.videos):
                                self.log.error(f"There's no {vbitrate}kbps Video Track...")
                                sys.exit(1)
                        else:
                            title.tracks.select_video(lambda x: x.bitrate and x.bitrate // 1000 == vbitrate)
                            if not title.tracks.videos:
                                self.log.error(f"There's no {vbitrate}kbps Video Track...")
                                sys.exit(1)

                    if vbitrate_min is not None and vbitrate_max is not None:
                        title.tracks.select_video(
                            lambda x: x.bitrate and vbitrate_min <= x.bitrate // 1000 <= vbitrate_max
                        )
                        if not title.tracks.videos:
                            self.log.error(
                                f"No Video Track in {vbitrate_min}-{vbitrate_max}kbps range..."
                            )
                            sys.exit(1)

                    video_languages = [lang for lang in (v_lang or lang) if lang != "best"]
                    if video_languages and "all" not in video_languages:
                        processed_video_lang = []
                        for language in video_languages:
                            if language == "orig":
                                if title.language:
                                    orig_lang = (
                                        str(title.language) if hasattr(title.language, "__str__") else title.language
                                    )
                                    if orig_lang not in processed_video_lang:
                                        processed_video_lang.append(orig_lang)
                                else:
                                    self.log.warning(
                                        "Original language not available for title, skipping 'orig' selection for video"
                                    )
                            else:
                                if language not in processed_video_lang:
                                    processed_video_lang.append(language)
                        title.tracks.videos = title.tracks.by_language(
                            title.tracks.videos, processed_video_lang, exact_match=exact_lang
                        )
                        if not title.tracks.videos:
                            self.log.error(f"There's no {processed_video_lang} Video Track...")
                            sys.exit(1)

                    has_hybrid = any(r == Video.Range.HYBRID for r in range_)
                    non_hybrid_ranges = [r for r in range_ if r != Video.Range.HYBRID]

                    if quality:
                        missing_resolutions = []
                        if has_hybrid:
                            # Split tracks: hybrid candidates vs non-hybrid
                            hybrid_candidate_tracks = [
                                v
                                for v in title.tracks.videos
                                if v.range in (Video.Range.HDR10, Video.Range.HDR10P, Video.Range.DV)
                            ]
                            non_hybrid_tracks = [
                                v
                                for v in title.tracks.videos
                                if v.range not in (Video.Range.HDR10, Video.Range.HDR10P, Video.Range.DV)
                            ]

                            # Apply hybrid selection to HDR10+DV tracks
                            hybrid_filter = title.tracks.select_hybrid(hybrid_candidate_tracks, quality)
                            hybrid_selected = list(filter(hybrid_filter, hybrid_candidate_tracks))

                            if non_hybrid_ranges and non_hybrid_tracks:
                                # Also filter non-hybrid tracks by resolution
                                non_hybrid_selected = [
                                    v
                                    for v in non_hybrid_tracks
                                    if any(v.height == res or int(v.width * (9 / 16)) == res for res in quality)
                                ]
                                title.tracks.videos = hybrid_selected + non_hybrid_selected
                            else:
                                title.tracks.videos = hybrid_selected
                        else:
                            title.tracks.by_resolutions(quality)

                            for resolution in quality:
                                if any(v.height == resolution for v in title.tracks.videos):
                                    continue
                                if any(int(v.width * 9 / 16) == resolution for v in title.tracks.videos):
                                    continue
                                missing_resolutions.append(resolution)

                        if missing_resolutions:
                            res_list = ""
                            if len(missing_resolutions) > 1:
                                res_list = ", ".join([f"{x}p" for x in missing_resolutions[:-1]]) + " or "
                            res_list = f"{res_list}{missing_resolutions[-1]}p"
                            plural = "s" if len(missing_resolutions) > 1 else ""

                            if best_available:
                                self.log.warning(
                                    f"There's no {res_list} Video Track{plural}, continuing with available qualities..."
                                )
                            else:
                                self.log.error(f"There's no {res_list} Video Track{plural}...")
                                sys.exit(1)

                    # choose best track by range and quality
                    if has_hybrid:
                        # Apply hybrid selection for HYBRID tracks
                        hybrid_candidate_tracks = [
                            v
                            for v in title.tracks.videos
                            if v.range in (Video.Range.HDR10, Video.Range.HDR10P, Video.Range.DV)
                        ]
                        non_hybrid_tracks = [
                            v
                            for v in title.tracks.videos
                            if v.range not in (Video.Range.HDR10, Video.Range.HDR10P, Video.Range.DV)
                        ]

                        if not quality:
                            best_resolution = max((v.height for v in hybrid_candidate_tracks), default=None)
                            if best_resolution:
                                hybrid_filter = title.tracks.select_hybrid(hybrid_candidate_tracks, [best_resolution])
                                hybrid_selected = list(filter(hybrid_filter, hybrid_candidate_tracks))
                            else:
                                hybrid_selected = []
                        else:
                            hybrid_filter = title.tracks.select_hybrid(hybrid_candidate_tracks, quality)
                            hybrid_selected = list(filter(hybrid_filter, hybrid_candidate_tracks))

                        # For non-hybrid ranges, apply Cartesian product selection
                        non_hybrid_selected: list[Video] = []
                        if non_hybrid_ranges and non_hybrid_tracks:
                            for resolution, color_range, codec in product(
                                quality or [None], non_hybrid_ranges, vcodec or [None]
                            ):
                                candidates = [
                                    t
                                    for t in non_hybrid_tracks
                                    if (
                                        not resolution
                                        or t.height == resolution
                                        or int(t.width * (9 / 16)) == resolution
                                    )
                                    and (not color_range or t.range == color_range)
                                    and (not codec or t.codec == codec)
                                ]
                                match = candidates[-1] if worst and candidates else next(iter(candidates), None)
                                if match and match not in non_hybrid_selected:
                                    non_hybrid_selected.append(match)

                        title.tracks.videos = hybrid_selected + non_hybrid_selected
                    else:
                        selected_videos: list[Video] = []
                        for resolution, color_range, codec in product(
                            quality or [None], range_ or [None], vcodec or [None]
                        ):
                            candidates = [
                                t
                                for t in title.tracks.videos
                                if (
                                    not resolution
                                    or t.height == resolution
                                    or int(t.width * (9 / 16)) == resolution
                                )
                                and (not color_range or t.range == color_range)
                                and (not codec or t.codec == codec)
                            ]
                            match = candidates[-1] if worst and candidates else next(iter(candidates), None)
                            if match and match not in selected_videos:
                                selected_videos.append(match)
                        title.tracks.videos = selected_videos

                    # validate hybrid mode requirements
                    if any(r == Video.Range.HYBRID for r in range_):
                        base_tracks = [
                            v for v in title.tracks.videos if v.range in (Video.Range.HDR10, Video.Range.HDR10P)
                        ]
                        dv_tracks = [v for v in title.tracks.videos if v.range == Video.Range.DV]

                        hybrid_failed = False
                        if not base_tracks and not dv_tracks:
                            available_ranges = sorted(set(v.range.name for v in title.tracks.videos))
                            msg = "HYBRID mode requires both HDR10/HDR10+ and DV tracks, but neither is available"
                            msg_detail = (
                                f"Available ranges: {', '.join(available_ranges) if available_ranges else 'none'}"
                            )
                            hybrid_failed = True
                        elif not base_tracks:
                            available_ranges = sorted(set(v.range.name for v in title.tracks.videos))
                            msg = "HYBRID mode requires both HDR10/HDR10+ and DV tracks, but only DV is available"
                            msg_detail = f"Available ranges: {', '.join(available_ranges)}"
                            hybrid_failed = True
                        elif not dv_tracks:
                            available_ranges = sorted(set(v.range.name for v in title.tracks.videos))
                            msg = "HYBRID mode requires both HDR10/HDR10+ and DV tracks, but only HDR10 is available"
                            msg_detail = f"Available ranges: {', '.join(available_ranges)}"
                            hybrid_failed = True

                        if hybrid_failed:
                            other_ranges = [r for r in range_ if r != Video.Range.HYBRID]
                            if best_available and other_ranges:
                                self.log.warning(msg)
                                self.log.warning(
                                    f"Continuing with remaining range(s): {', '.join(r.name for r in other_ranges)}"
                                )
                                range_ = other_ranges
                            else:
                                self.log.error(msg)
                                self.log.error(msg_detail)
                                sys.exit(1)

                    # filter subtitle tracks
                    if require_subs:
                        missing_langs = [
                            lang
                            for lang in require_subs
                            if not any(is_close_match(lang, [sub.language]) for sub in title.tracks.subtitles)
                        ]

                        if missing_langs:
                            self.log.error(f"Required subtitle language(s) not found: {', '.join(missing_langs)}")
                            sys.exit(1)

                        self.log.info(
                            f"Required languages found ({', '.join(require_subs)}), downloading all available subtitles"
                        )
                    elif s_lang and "all" not in s_lang:
                        from unshackle.core.utilities import is_exact_match

                        match_func = is_exact_match if exact_lang else is_close_match

                        missing_langs = [
                            lang_
                            for lang_ in s_lang
                            if not any(match_func(lang_, [sub.language]) for sub in title.tracks.subtitles)
                        ]
                        if missing_langs:
                            self.log.error(", ".join(missing_langs) + " not found in tracks")
                            sys.exit(1)

                        title.tracks.select_subtitles(lambda x: match_func(x.language, s_lang))
                        if not title.tracks.subtitles:
                            self.log.error(f"There's no {s_lang} Subtitle Track...")
                            sys.exit(1)

                    if not forced_subs:
                        title.tracks.select_subtitles(lambda x: not x.forced)

                # filter audio tracks
                # might have no audio tracks if part of the video, e.g. transport stream hls
                if len(title.tracks.audio) > 0:
                    if not audio_description:
                        title.tracks.select_audio(lambda x: not x.descriptive)  # exclude descriptive audio
                    if acodec:
                        title.tracks.select_audio(lambda x: x.codec in acodec)
                        if not title.tracks.audio:
                            codec_names = ", ".join(c.name for c in acodec)
                            self.log.error(f"No audio tracks matching codecs: {codec_names}")
                            sys.exit(1)
                    if channels:
                        title.tracks.select_audio(lambda x: math.ceil(x.channels) == math.ceil(channels))
                        if not title.tracks.audio:
                            self.log.error(f"There's no {channels} Audio Track...")
                            sys.exit(1)
                    if no_atmos:
                        title.tracks.audio = [x for x in title.tracks.audio if not x.atmos]
                        if not title.tracks.audio:
                            self.log.error("No non-Atmos audio tracks available...")
                            sys.exit(1)
                    if abitrate:
                        title.tracks.select_audio(lambda x: x.bitrate and x.bitrate // 1000 == abitrate)
                        if not title.tracks.audio:
                            self.log.error(f"There's no {abitrate}kbps Audio Track...")
                            sys.exit(1)
                    if abitrate_min is not None and abitrate_max is not None:
                        title.tracks.select_audio(
                            lambda x: x.bitrate and abitrate_min <= x.bitrate // 1000 <= abitrate_max
                        )
                        if not title.tracks.audio:
                            self.log.error(
                                f"No Audio Track in {abitrate_min}-{abitrate_max}kbps range..."
                            )
                            sys.exit(1)
                    audio_languages = a_lang or lang
                    if audio_languages:
                        processed_lang = []
                        for language in audio_languages:
                            if language == "orig":
                                if title.language:
                                    orig_lang = (
                                        str(title.language) if hasattr(title.language, "__str__") else title.language
                                    )
                                    if orig_lang not in processed_lang:
                                        processed_lang.append(orig_lang)
                                else:
                                    self.log.warning(
                                        "Original language not available for title, skipping 'orig' selection"
                                    )
                            else:
                                if language not in processed_lang:
                                    processed_lang.append(language)

                        if "best" in processed_lang or "all" in processed_lang:
                            unique_languages = {track.language for track in title.tracks.audio}
                            selected_audio = []
                            for language in unique_languages:
                                codecs_to_check = acodec if (acodec and len(acodec) > 1) else [None]
                                for codec in codecs_to_check:
                                    base_candidates = [
                                        t
                                        for t in title.tracks.audio
                                        if t.language == language and (codec is None or t.codec == codec)
                                    ]
                                    if not base_candidates:
                                        continue
                                    if audio_description:
                                        standards = [t for t in base_candidates if not t.descriptive]
                                        if standards:
                                            selected_audio.append(max(standards, key=lambda x: x.bitrate or 0))
                                        descs = [t for t in base_candidates if t.descriptive]
                                        if descs:
                                            selected_audio.append(max(descs, key=lambda x: x.bitrate or 0))
                                    else:
                                        selected_audio.append(max(base_candidates, key=lambda x: x.bitrate or 0))
                            title.tracks.audio = selected_audio
                        else:
                            # If multiple codecs were explicitly requested, pick the best track per codec per
                            # requested language instead of selecting *all* bitrate variants of a codec.
                            if acodec and len(acodec) > 1:
                                selected_audio: list[Audio] = []

                                for language in processed_lang:
                                    for codec in acodec:
                                        codec_tracks = [a for a in title.tracks.audio if a.codec == codec]
                                        if not codec_tracks:
                                            continue

                                        candidates = title.tracks.by_language(
                                            codec_tracks, [language], per_language=0, exact_match=exact_lang
                                        )
                                        if not candidates:
                                            continue

                                        if audio_description:
                                            standards = [t for t in candidates if not t.descriptive]
                                            if standards:
                                                selected_audio.append(max(standards, key=lambda x: x.bitrate or 0))
                                            descs = [t for t in candidates if t.descriptive]
                                            if descs:
                                                selected_audio.append(max(descs, key=lambda x: x.bitrate or 0))
                                        else:
                                            selected_audio.append(max(candidates, key=lambda x: x.bitrate or 0))

                                title.tracks.audio = selected_audio
                            else:
                                per_language = 1
                                if audio_description:
                                    standard_audio = [a for a in title.tracks.audio if not a.descriptive]
                                    selected_standards = title.tracks.by_language(
                                        standard_audio,
                                        processed_lang,
                                        per_language=per_language,
                                        exact_match=exact_lang,
                                    )
                                    desc_audio = [a for a in title.tracks.audio if a.descriptive]
                                    # Include all descriptive tracks for the requested languages.
                                    selected_descs = title.tracks.by_language(
                                        desc_audio, processed_lang, per_language=0, exact_match=exact_lang
                                    )
                                    title.tracks.audio = selected_standards + selected_descs
                                else:
                                    title.tracks.audio = title.tracks.by_language(
                                        title.tracks.audio,
                                        processed_lang,
                                        per_language=per_language,
                                        exact_match=exact_lang,
                                    )
                            if not title.tracks.audio:
                                self.log.error(f"There's no {processed_lang} Audio Track, cannot continue...")
                                sys.exit(1)

                if (
                    video_only
                    or audio_only
                    or subs_only
                    or chapters_only
                    or no_subs
                    or no_audio
                    or no_chapters
                    or no_video
                ):
                    keep_videos = False
                    keep_audio = False
                    keep_subtitles = False
                    keep_chapters = False

                    if video_only or audio_only or subs_only or chapters_only:
                        if video_only:
                            keep_videos = True
                        if audio_only:
                            keep_audio = True
                        if subs_only:
                            keep_subtitles = True
                        if chapters_only:
                            keep_chapters = True
                    else:
                        keep_videos = True
                        keep_audio = True
                        keep_subtitles = True
                        keep_chapters = True

                    if no_subs:
                        keep_subtitles = False
                    if no_audio:
                        keep_audio = False
                    if no_chapters:
                        keep_chapters = False
                    if no_video:
                        keep_videos = False

                    kept_tracks = []
                    if keep_videos:
                        kept_tracks.extend(title.tracks.videos)
                    if keep_audio:
                        kept_tracks.extend(title.tracks.audio)
                    if keep_subtitles:
                        kept_tracks.extend(title.tracks.subtitles)
                    if keep_chapters:
                        kept_tracks.extend(title.tracks.chapters)
                    kept_tracks.extend(title.tracks.attachments)

                    title.tracks = Tracks(kept_tracks)

            selected_tracks, tracks_progress_callables = title.tracks.tree(add_progress=True)

            for track in title.tracks:
                if hasattr(track, "needs_drm_loading") and track.needs_drm_loading:
                    track.load_drm_if_needed(service)

            download_table = Table.grid()
            download_table.add_row(selected_tracks)

            video_tracks = title.tracks.videos
            if video_tracks:
                highest_quality = max((track.height for track in video_tracks if track.height), default=0)
                if highest_quality > 0:
                    if is_widevine_cdm(self.cdm):
                        quality_based_cdm = self.get_cdm(
                            self.service, self.profile, drm="widevine", quality=highest_quality
                        )
                        if quality_based_cdm and quality_based_cdm != self.cdm:
                            self.log.debug(
                                f"Pre-selecting Widevine CDM based on highest quality {highest_quality}p across all video tracks"
                            )
                            self.cdm = quality_based_cdm
                    elif is_playready_cdm(self.cdm):
                        quality_based_cdm = self.get_cdm(
                            self.service, self.profile, drm="playready", quality=highest_quality
                        )
                        if quality_based_cdm and quality_based_cdm != self.cdm:
                            self.log.debug(
                                f"Pre-selecting PlayReady CDM based on highest quality {highest_quality}p across all video tracks"
                            )
                            self.cdm = quality_based_cdm

            if hasattr(service, "resolve_server_keys"):
                service.resolve_server_keys(title)

            dl_start_time = time.time()

            try:
                with Live(Padding(download_table, (1, 5)), console=console, refresh_per_second=5):
                    with ThreadPoolExecutor(downloads) as pool:
                        for download in futures.as_completed(
                            (
                                pool.submit(
                                    track.download,
                                    session=service.session,
                                    prepare_drm=partial(
                                        partial(self.prepare_drm, table=download_table),
                                        track=track,
                                        title=title,
                                        certificate=partial(
                                            service.get_widevine_service_certificate,
                                            title=title,
                                            track=track,
                                        ),
                                        licence=partial(
                                            service.get_playready_license
                                            if is_playready_cdm(self.cdm)
                                            else service.get_widevine_license,
                                            title=title,
                                            track=track,
                                        ),
                                        cdm_only=cdm_only,
                                        vaults_only=vaults_only,
                                        export=export,
                                    ),
                                    cdm=self.cdm,
                                    max_workers=workers,
                                    progress=tracks_progress_callables[i],
                                )
                                for i, track in enumerate(title.tracks)
                            )
                        ):
                            download.result()

            except KeyboardInterrupt:
                console.print(Padding(":x: Download Cancelled...", (0, 5, 1, 5)))
                if self.debug_logger:
                    self.debug_logger.log(
                        level="WARNING",
                        operation="download_tracks",
                        service=self.service,
                        message="Download cancelled by user",
                        context={"title": str(title)},
                    )
                return
            except Exception as e:  # noqa
                error_messages = [
                    ":x: Download Failed...",
                ]
                if isinstance(e, EnvironmentError):
                    error_messages.append(f"   {e}")
                if isinstance(e, ValueError):
                    error_messages.append(f"   {e}")
                if isinstance(e, (AttributeError, TypeError)):
                    console.print_exception()
                else:
                    error_messages.append(
                        "   An unexpected error occurred in one of the download workers.",
                    )
                    if hasattr(e, "returncode"):
                        error_messages.append(f"   Binary call failed, Process exit code: {e.returncode}")
                    error_messages.append("   See the error trace above for more information.")
                    if isinstance(e, subprocess.CalledProcessError):
                        # CalledProcessError already lists the exception trace
                        console.print_exception()
                console.print(Padding(Group(*error_messages), (1, 5)))

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
                return

            if skip_dl:
                console.log("Skipped downloads as --skip-dl was used...")
            else:
                dl_time = time_elapsed_since(dl_start_time)
                console.print(Padding(f"Track downloads finished in [progress.elapsed]{dl_time}[/]", (0, 5)))

                # Subtitle output mode configuration (for sidecar originals)
                subtitle_output_mode = config.subtitle.get("output_mode", "mux")
                sidecar_format = config.subtitle.get("sidecar_format", "srt")
                skip_subtitle_mux = subtitle_output_mode == "sidecar" and (title.tracks.videos or title.tracks.audio)
                sidecar_subtitles: list[Subtitle] = []
                sidecar_original_paths: dict[str, Path] = {}
                if subtitle_output_mode in ("sidecar", "both") and not no_mux:
                    sidecar_subtitles = [s for s in title.tracks.subtitles if s.path and s.path.exists()]
                    if sidecar_format == "original":
                        config.directories.temp.mkdir(parents=True, exist_ok=True)
                        for subtitle in sidecar_subtitles:
                            original_path = (
                                config.directories.temp / f"sidecar_original_{subtitle.id}{subtitle.path.suffix}"
                            )
                            shutil.copy2(subtitle.path, original_path)
                            sidecar_original_paths[subtitle.id] = original_path

                with console.status("Converting Subtitles..."):
                    for subtitle in title.tracks.subtitles:
                        if sub_format:
                            if subtitle.codec != sub_format:
                                subtitle.convert(sub_format)
                        elif subtitle.codec == Subtitle.Codec.TimedTextMarkupLang:
                            # MKV does not support TTML, VTT is the next best option
                            subtitle.convert(Subtitle.Codec.WebVTT)

                with console.status("Checking Subtitles for Fonts..."):
                    font_names = []
                    for subtitle in title.tracks.subtitles:
                        if subtitle.codec == Subtitle.Codec.SubStationAlphav4:
                            for line in subtitle.path.read_text("utf8").splitlines():
                                if line.startswith("Style: "):
                                    font_names.append(line.removeprefix("Style: ").split(",")[1].strip())

                    font_count, missing_fonts = self.attach_subtitle_fonts(font_names, title, temp_font_files)

                    if font_count:
                        self.log.info(f"Attached {font_count} fonts for the Subtitles")

                    if missing_fonts and sys.platform != "win32":
                        self.suggest_missing_fonts(missing_fonts)

                # Handle DRM decryption BEFORE repacking (must decrypt first!)
                service_name = service.__class__.__name__.upper()
                decryption_method = config.decryption_map.get(service_name, config.decryption)
                decrypt_tool = "mp4decrypt" if decryption_method.lower() == "mp4decrypt" else "Shaka Packager"

                drm_tracks = [track for track in title.tracks if track.drm]
                if drm_tracks:
                    with console.status(f"Decrypting tracks with {decrypt_tool}..."):
                        has_decrypted = False
                        for track in drm_tracks:
                            drm = track.get_drm_for_cdm(self.cdm)
                            if drm and hasattr(drm, "decrypt"):
                                drm.decrypt(track.path)
                                if not isinstance(drm, MonaLisa):
                                    has_decrypted = True
                                events.emit(events.Types.TRACK_REPACKED, track=track)
                            else:
                                self.log.warning(
                                    f"No matching DRM found for track {track} with CDM type {type(self.cdm).__name__}"
                                )
                        if has_decrypted:
                            self.log.info(f"Decrypted tracks with {decrypt_tool}")

                # Extract Closed Captions from decrypted video tracks
                if (
                    not no_subs
                    and not (hasattr(service, "NO_SUBTITLES") and service.NO_SUBTITLES)
                    and not video_only
                    and not no_video
                ):
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
                                    self.log.info(
                                        f"Extracted a Closed Caption from Video track {video_track_n + 1}"
                                    )
                                else:
                                    self.log.info(
                                        f"No Closed Captions were found in Video track {video_track_n + 1}"
                                    )
                            except EnvironmentError:
                                self.log.error(
                                    "Cannot extract Closed Captions as the ccextractor executable was not found..."
                                )
                                break

                # Now repack the decrypted tracks
                with console.status("Repackaging tracks with FFMPEG..."):
                    has_repacked = False
                    for track in title.tracks:
                        if track.needs_repack:
                            track.repackage()
                            has_repacked = True
                            events.emit(events.Types.TRACK_REPACKED, track=track)
                    if has_repacked:
                        # we don't want to fill up the log with "Repacked x track"
                        self.log.info("Repacked one or more tracks with FFMPEG")

                muxed_paths = []
                muxed_audio_codecs: dict[Path, Optional[Audio.Codec]] = {}
                append_audio_codec_suffix = True

                if no_mux:
                    # Skip muxing, handle individual track files
                    for track in title.tracks:
                        if track.path and track.path.exists():
                            muxed_paths.append(track.path)
                elif isinstance(title, (Movie, Episode)):
                    progress = Progress(
                        TextColumn("[progress.description]{task.description}"),
                        SpinnerColumn(finished_text=""),
                        BarColumn(),
                        "•",
                        TimeRemainingColumn(compact=True, elapsed_when_finished=True),
                        console=console,
                    )

                    merge_audio = (
                        (not split_audio) if split_audio is not None else config.muxing.get("merge_audio", True)
                    )
                    # When we split audio (merge_audio=False), multiple outputs may exist per title, so suffix codec.
                    append_audio_codec_suffix = not merge_audio

                    multiplex_tasks: list[tuple[TaskID, Tracks, Optional[Audio.Codec]]] = []
                    # Track hybrid-processing outputs explicitly so we can always clean them up,
                    # even if muxing fails early (e.g. SystemExit) before the normal delete loop.
                    hybrid_temp_paths: list[Path] = []

                    def clone_tracks_for_audio(base_tracks: Tracks, audio_tracks: list[Audio]) -> Tracks:
                        task_tracks = Tracks()
                        task_tracks.videos = list(base_tracks.videos)
                        task_tracks.audio = audio_tracks
                        task_tracks.subtitles = list(base_tracks.subtitles)
                        task_tracks.chapters = base_tracks.chapters
                        task_tracks.attachments = list(base_tracks.attachments)
                        return task_tracks

                    def enqueue_mux_tasks(task_description: str, base_tracks: Tracks) -> None:
                        if merge_audio or not base_tracks.audio:
                            task_id = progress.add_task(f"{task_description}...", total=None, start=False)
                            multiplex_tasks.append((task_id, base_tracks, None))
                            return

                        audio_by_codec: dict[Optional[Audio.Codec], list[Audio]] = {}
                        for audio_track in base_tracks.audio:
                            audio_by_codec.setdefault(audio_track.codec, []).append(audio_track)

                        for audio_codec, codec_audio_tracks in audio_by_codec.items():
                            description = task_description
                            if audio_codec:
                                description = f"{task_description} {audio_codec.name}"

                            task_id = progress.add_task(f"{description}...", total=None, start=False)
                            task_tracks = clone_tracks_for_audio(base_tracks, codec_audio_tracks)
                            multiplex_tasks.append((task_id, task_tracks, audio_codec))

                    # Check if we're in hybrid mode
                    if any(r == Video.Range.HYBRID for r in range_) and title.tracks.videos:
                        # Hybrid mode: process DV and HDR10 tracks separately for each resolution
                        self.log.info("Processing Hybrid HDR10+DV tracks...")

                        # Group video tracks by resolution (prefer HDR10+ over HDR10 as base)
                        resolutions_processed = set()
                        base_tracks_list = [
                            v for v in title.tracks.videos if v.range in (Video.Range.HDR10P, Video.Range.HDR10)
                        ]
                        dv_tracks = [v for v in title.tracks.videos if v.range == Video.Range.DV]

                        for hdr10_track in base_tracks_list:
                            resolution = hdr10_track.height
                            if resolution in resolutions_processed:
                                continue
                            resolutions_processed.add(resolution)

                            # Find matching DV track for this resolution (use the lowest DV resolution)
                            matching_dv = min(dv_tracks, key=lambda v: v.height) if dv_tracks else None

                            if matching_dv:
                                # Create track pair for this resolution
                                resolution_tracks = [hdr10_track, matching_dv]

                                for track in resolution_tracks:
                                    track.needs_duration_fix = True

                                # Run the hybrid processing for this resolution
                                Hybrid(resolution_tracks, self.service)

                                # Create unique output filename for this resolution
                                hybrid_filename = f"HDR10-DV-{resolution}p.hevc"
                                hybrid_output_path = config.directories.temp / hybrid_filename
                                hybrid_temp_paths.append(hybrid_output_path)

                                # The Hybrid class creates HDR10-DV.hevc, rename it for this resolution
                                default_output = config.directories.temp / "HDR10-DV.hevc"
                                if default_output.exists():
                                    # If a previous run left this behind, replace it to avoid move() failures.
                                    hybrid_output_path.unlink(missing_ok=True)
                                    shutil.move(str(default_output), str(hybrid_output_path))

                                # Create tracks with the hybrid video output for this resolution
                                task_description = f"Multiplexing Hybrid HDR10+DV {resolution}p"
                                task_tracks = Tracks(title.tracks) + title.tracks.chapters + title.tracks.attachments

                                # Create a new video track for the hybrid output
                                hybrid_track = deepcopy(hdr10_track)
                                hybrid_track.id = f"hybrid_{hdr10_track.id}_{resolution}"
                                hybrid_track.path = hybrid_output_path
                                hybrid_track.range = Video.Range.DV  # It's now a DV track
                                hybrid_track.needs_duration_fix = True
                                title.tracks.add(hybrid_track)
                                task_tracks.videos = [hybrid_track]

                                enqueue_mux_tasks(task_description, task_tracks)

                        console.print()
                    else:
                        # Normal mode: process each video track separately
                        for video_track in title.tracks.videos or [None]:
                            task_description = "Multiplexing"
                            if video_track:
                                if len(quality) > 1:
                                    task_description += f" {video_track.height}p"
                                if len(range_) > 1:
                                    task_description += f" {video_track.range.name}"
                                if len(vcodec) > 1:
                                    task_description += f" {video_track.codec.name}"

                            task_tracks = Tracks(title.tracks) + title.tracks.chapters + title.tracks.attachments
                            if video_track:
                                task_tracks.videos = [video_track]

                            enqueue_mux_tasks(task_description, task_tracks)

                    try:
                        with Live(Padding(progress, (0, 5, 1, 5)), console=console):
                            mux_index = 0
                            for task_id, task_tracks, audio_codec in multiplex_tasks:
                                progress.start_task(task_id)  # TODO: Needed?
                                audio_expected = not video_only and not no_audio
                                muxed_path, return_code, errors = task_tracks.mux(
                                    str(title),
                                    progress=partial(progress.update, task_id=task_id),
                                    delete=False,
                                    audio_expected=audio_expected,
                                    title_language=title.language,
                                    skip_subtitles=skip_subtitle_mux,
                                )
                                if muxed_path.exists():
                                    mux_index += 1
                                    unique_path = muxed_path.with_name(
                                        f"{muxed_path.stem}.{mux_index}{muxed_path.suffix}"
                                    )
                                    if unique_path != muxed_path:
                                        shutil.move(muxed_path, unique_path)
                                        muxed_path = unique_path
                                muxed_paths.append(muxed_path)
                                muxed_audio_codecs[muxed_path] = audio_codec
                                if return_code >= 2:
                                    self.log.error(f"Failed to Mux video to Matroska file ({return_code}):")
                                elif return_code == 1 or errors:
                                    self.log.warning("mkvmerge had at least one warning or error, continuing anyway...")
                                for line in errors:
                                    if line.startswith("#GUI#error"):
                                        self.log.error(line)
                                    else:
                                        self.log.warning(line)
                                if return_code >= 2:
                                    sys.exit(1)

                            # Output sidecar subtitles before deleting track files
                            if sidecar_subtitles and not no_mux:
                                media_info = MediaInfo.parse(muxed_paths[0]) if muxed_paths else None
                                if media_info:
                                    base_filename = title.get_filename(media_info, show_service=not no_source)
                                else:
                                    base_filename = str(title)

                                sidecar_dir = self.output_dir or config.directories.downloads
                                if not no_folder and isinstance(title, (Episode, Song)) and media_info:
                                    sidecar_dir /= title.get_filename(
                                        media_info, show_service=not no_source, folder=True
                                    )
                                sidecar_dir.mkdir(parents=True, exist_ok=True)

                                with console.status("Saving subtitle sidecar files..."):
                                    created = self.output_subtitle_sidecars(
                                        sidecar_subtitles,
                                        base_filename,
                                        sidecar_dir,
                                        sidecar_format,
                                        original_paths=sidecar_original_paths or None,
                                    )
                                    if created:
                                        self.log.info(f"Saved {len(created)} sidecar subtitle files")

                            for track in title.tracks:
                                track.delete()

                            # Clear temp font attachment paths and delete other attachments
                            for attachment in title.tracks.attachments:
                                if attachment.path and attachment.path in temp_font_files:
                                    attachment.path = None
                                else:
                                    attachment.delete()

                            # Clean up temp fonts
                            for temp_path in temp_font_files:
                                temp_path.unlink(missing_ok=True)
                            for temp_path in sidecar_original_paths.values():
                                temp_path.unlink(missing_ok=True)
                    finally:
                        # Hybrid() produces a temp HEVC output we rename; make sure it's never left behind.
                        # Also attempt to remove the default hybrid output name if it still exists.
                        for temp_path in hybrid_temp_paths:
                            try:
                                temp_path.unlink(missing_ok=True)
                            except PermissionError:
                                self.log.warning(f"Failed to delete temp file (in use?): {temp_path}")
                        try:
                            (config.directories.temp / "HDR10-DV.hevc").unlink(missing_ok=True)
                        except PermissionError:
                            self.log.warning(
                                f"Failed to delete temp file (in use?): {config.directories.temp / 'HDR10-DV.hevc'}"
                            )

                else:
                    # dont mux
                    muxed_paths.append(title.tracks.audio[0].path)

                if no_mux:
                    # Handle individual track files without muxing
                    final_dir = self.output_dir or config.directories.downloads
                    if not no_folder and isinstance(title, (Episode, Song)):
                        # Create folder based on title
                        # Use first available track for filename generation
                        sample_track = (
                            title.tracks.videos[0]
                            if title.tracks.videos
                            else (
                                title.tracks.audio[0]
                                if title.tracks.audio
                                else (title.tracks.subtitles[0] if title.tracks.subtitles else None)
                            )
                        )
                        if sample_track and sample_track.path:
                            media_info = MediaInfo.parse(sample_track.path)
                            final_dir /= title.get_filename(media_info, show_service=not no_source, folder=True)

                    final_dir.mkdir(parents=True, exist_ok=True)

                    for track_path in muxed_paths:
                        # Generate appropriate filename for each track
                        media_info = MediaInfo.parse(track_path)
                        base_filename = title.get_filename(media_info, show_service=not no_source)

                        # Add track type suffix to filename
                        track = next((t for t in title.tracks if t.path == track_path), None)
                        if track:
                            if isinstance(track, Video):
                                track_suffix = f".{track.codec.name if hasattr(track.codec, 'name') else 'video'}"
                            elif isinstance(track, Audio):
                                lang_suffix = f".{track.language}" if track.language else ""
                                track_suffix = (
                                    f"{lang_suffix}.{track.codec.name if hasattr(track.codec, 'name') else 'audio'}"
                                )
                            elif isinstance(track, Subtitle):
                                lang_suffix = f".{track.language}" if track.language else ""
                                forced_suffix = ".forced" if track.forced else ""
                                sdh_suffix = ".sdh" if track.sdh else ""
                                track_suffix = f"{lang_suffix}{forced_suffix}{sdh_suffix}"
                            else:
                                track_suffix = ""

                            final_path = final_dir / f"{base_filename}{track_suffix}{track_path.suffix}"
                        else:
                            final_path = final_dir / f"{base_filename}{track_path.suffix}"

                        shutil.move(track_path, final_path)
                        self.log.debug(f"Saved: {final_path.name}")
                else:
                    # Handle muxed files
                    used_final_paths: set[Path] = set()
                    for muxed_path in muxed_paths:
                        media_info = MediaInfo.parse(muxed_path)
                        final_dir = self.output_dir or config.directories.downloads
                        final_filename = title.get_filename(media_info, show_service=not no_source)
                        audio_codec_suffix = muxed_audio_codecs.get(muxed_path)

                        if not no_folder and isinstance(title, (Episode, Song)):
                            final_dir /= title.get_filename(media_info, show_service=not no_source, folder=True)

                        final_dir.mkdir(parents=True, exist_ok=True)
                        final_path = final_dir / f"{final_filename}{muxed_path.suffix}"
                        template_type = (
                            "series" if isinstance(title, Episode) else "songs" if isinstance(title, Song) else "movies"
                        )
                        sep = config.get_template_separator(template_type)

                        if final_path.exists() and audio_codec_suffix and append_audio_codec_suffix:
                            final_filename = f"{final_filename.rstrip()}{sep}{audio_codec_suffix.name}"
                            final_path = final_dir / f"{final_filename}{muxed_path.suffix}"

                        if final_path in used_final_paths:
                            i = 2
                            while final_path in used_final_paths:
                                final_path = final_dir / f"{final_filename.rstrip()}{sep}{i}{muxed_path.suffix}"
                                i += 1

                        try:
                            os.replace(muxed_path, final_path)
                        except OSError:
                            if final_path.exists():
                                final_path.unlink()
                            shutil.move(muxed_path, final_path)
                        used_final_paths.add(final_path)
                        tags.tag_file(final_path, title, self.tmdb_id, self.imdb_id)

                title_dl_time = time_elapsed_since(dl_start_time)
                console.print(
                    Padding(f":tada: Title downloaded in [progress.elapsed]{title_dl_time}[/]!", (0, 5, 1, 5))
                )

            if not hasattr(service, "close"):
                cookie_file = self.get_cookie_path(self.service, self.profile)
                if cookie_file:
                    self.save_cookies(cookie_file, service.session.cookies)

        if hasattr(service, "close"):
            service.close()

        dl_time = time_elapsed_since(start_time)

        console.print(Padding(f"Processed all titles in [progress.elapsed]{dl_time}", (0, 5, 1, 5)))

    def prepare_drm(
        self,
        drm: DRM_T,
        track: AnyTrack,
        title: Title_T,
        certificate: Callable,
        licence: Callable,
        track_kid: Optional[UUID] = None,
        table: Table = None,
        cdm_only: bool = False,
        vaults_only: bool = False,
        export: Optional[Path] = None,
    ) -> None:
        """
        Prepare the DRM by getting decryption data like KIDs, Keys, and such.
        The DRM object should be ready for decryption once this function ends.
        """
        if not drm:
            return

        server_cdm = getattr(self, "server_cdm", False)

        if server_cdm:
            if not drm.content_keys:
                self.log.warning("Server CDM did not resolve any keys for this track")
                return
            svc = getattr(self, "_remote_service", None)
            server_drm_type = getattr(svc, "_server_cdm_type", None) if svc else None
            drm_name = {"widevine": "Widevine", "playready": "PlayReady"}.get(server_drm_type or "", drm.__class__.__name__)
            with self.DRM_TABLE_LOCK:
                pssh_str = ""
                expected_class = "PlayReady" if server_drm_type == "playready" else "Widevine"
                matching_drm = next(
                    (d for d in (track.drm or []) if d.__class__.__name__ == expected_class),
                    drm,
                )
                if hasattr(matching_drm, "pssh") and matching_drm.pssh:
                    if hasattr(matching_drm.pssh, "dumps"):
                        pssh_str = self.truncate_pssh_for_display(matching_drm.pssh.dumps(), drm_name)
                    elif hasattr(matching_drm, "data") and matching_drm.data.get("pssh_b64"):
                        pssh_str = self.truncate_pssh_for_display(matching_drm.data["pssh_b64"], drm_name)
                if pssh_str:
                    cek_tree = Tree(Text.assemble((drm_name, "cyan"), (f"({pssh_str})", "text"), overflow="fold"))
                else:
                    cek_tree = Tree(Text.assemble((drm_name, "cyan"), overflow="fold"))
                all_kids = list(getattr(drm, "kids", []))
                if track_kid and track_kid not in all_kids:
                    all_kids.append(track_kid)
                for kid in all_kids:
                    if kid in drm.content_keys:
                        is_track_kid = ["", "*"][kid == track_kid]
                        key = drm.content_keys[kid]
                        cek_tree.add(f"[text2]{kid.hex}:{key}{is_track_kid}")
                for kid, key in drm.content_keys.items():
                    if kid not in all_kids:
                        cek_tree.add(f"[text2]{kid.hex}:{key}")
                if not any(
                    isinstance(x, Tree) and x.label == cek_tree.label
                    for x in table.columns[0].cells
                ):
                    table.add_row(cek_tree)
            return

        track_quality = None
        if isinstance(track, Video) and track.height:
            track_quality = track.height

        if not server_cdm:
            if isinstance(drm, Widevine):
                if not is_widevine_cdm(self.cdm):
                    widevine_cdm = self.get_cdm(self.service, self.profile, drm="widevine", quality=track_quality)
                    if widevine_cdm:
                        if track_quality:
                            self.log.info(f"Switching to Widevine CDM for Widevine {track_quality}p content")
                        else:
                            self.log.info("Switching to Widevine CDM for Widevine content")
                        self.cdm = widevine_cdm

            elif isinstance(drm, PlayReady):
                if not is_playready_cdm(self.cdm):
                    playready_cdm = self.get_cdm(self.service, self.profile, drm="playready", quality=track_quality)
                    if playready_cdm:
                        if track_quality:
                            self.log.info(f"Switching to PlayReady CDM for PlayReady {track_quality}p content")
                        else:
                            self.log.info("Switching to PlayReady CDM for PlayReady content")
                        self.cdm = playready_cdm

        if isinstance(drm, Widevine):
            if self.debug_logger:
                self.debug_logger.log_drm_operation(
                    drm_type="Widevine",
                    operation="prepare_drm",
                    service=self.service,
                    context={
                        "track": str(track),
                        "title": str(title),
                        "pssh": drm.pssh.dumps() if drm.pssh else None,
                        "kids": [k.hex for k in drm.kids],
                        "track_kid": track_kid.hex if track_kid else None,
                    },
                )

            with self.DRM_TABLE_LOCK:
                pssh_display = self.truncate_pssh_for_display(drm.pssh.dumps(), "Widevine")
                cek_tree = Tree(Text.assemble(("Widevine", "cyan"), (f"({pssh_display})", "text"), overflow="fold"))
                pre_existing_tree = next(
                    (x for x in table.columns[0].cells if isinstance(x, Tree) and x.label == cek_tree.label), None
                )
                if pre_existing_tree:
                    cek_tree = pre_existing_tree

                need_license = False
                all_kids = list(drm.kids)
                if track_kid and track_kid not in all_kids:
                    all_kids.append(track_kid)

                for kid in all_kids:
                    if kid in drm.content_keys:
                        is_track_kid = ["", "*"][kid == track_kid]
                        key = drm.content_keys[kid]
                        label = f"[text2]{kid.hex}:{key}{is_track_kid}"
                        if not any(f"{kid.hex}:{key}" in x.label for x in cek_tree.children):
                            cek_tree.add(label)
                        continue

                    is_track_kid = ["", "*"][kid == track_kid]

                    if not cdm_only:
                        content_key, vault_used = self.vaults.get_key(kid)
                        if content_key:
                            drm.content_keys[kid] = content_key
                            label = f"[text2]{kid.hex}:{content_key}{is_track_kid} from {vault_used}"
                            if not any(f"{kid.hex}:{content_key}" in x.label for x in cek_tree.children):
                                cek_tree.add(label)
                            self.vaults.add_key(kid, content_key, excluding=vault_used)

                            if self.debug_logger:
                                self.debug_logger.log_vault_query(
                                    vault_name=vault_used,
                                    operation="get_key_success",
                                    service=self.service,
                                    context={
                                        "kid": kid.hex,
                                        "content_key": content_key,
                                        "track": str(track),
                                        "from_cache": True,
                                    },
                                )
                        elif vaults_only:
                            msg = f"No Vault has a Key for {kid.hex} and --vaults-only was used"
                            cek_tree.add(f"[logging.level.error]{msg}")
                            if not pre_existing_tree:
                                table.add_row(cek_tree)
                            if self.debug_logger:
                                self.debug_logger.log(
                                    level="ERROR",
                                    operation="vault_key_not_found",
                                    service=self.service,
                                    message=msg,
                                    context={"kid": kid.hex, "track": str(track)},
                                )
                            raise Widevine.Exceptions.CEKNotFound(msg)
                        else:
                            need_license = True

                    if kid not in drm.content_keys and cdm_only:
                        need_license = True

                if need_license and all(kid in drm.content_keys for kid in all_kids):
                    need_license = False

                if need_license and not vaults_only:
                    from_vaults = drm.content_keys.copy()

                    if self.debug_logger:
                        self.debug_logger.log(
                            level="INFO",
                            operation="get_license",
                            service=self.service,
                            message="Requesting Widevine license from service",
                            context={
                                "track": str(track),
                                "kids_needed": [k.hex for k in all_kids if k not in drm.content_keys],
                            },
                        )

                    try:
                        if self.service == "NF":
                            drm.get_NF_content_keys(cdm=self.cdm, licence=licence, certificate=certificate)
                        else:
                            drm.get_content_keys(cdm=self.cdm, licence=licence, certificate=certificate)
                    except Exception as e:
                        if drm.content_keys:
                            self.log.debug(f"License call failed but keys already in content_keys: {e}")
                        else:
                            if isinstance(e, (Widevine.Exceptions.EmptyLicense, Widevine.Exceptions.CEKNotFound)):
                                msg = str(e)
                            else:
                                msg = f"An exception occurred in the Service's license function: {e}"
                            cek_tree.add(f"[logging.level.error]{msg}")
                            if not pre_existing_tree:
                                table.add_row(cek_tree)
                            if self.debug_logger:
                                self.debug_logger.log_error(
                                    "get_license",
                                    e,
                                    service=self.service,
                                    context={"track": str(track), "exception_type": type(e).__name__},
                                )
                            raise e

                    if self.debug_logger:
                        self.debug_logger.log(
                            level="INFO",
                            operation="license_keys_retrieved",
                            service=self.service,
                            context={
                                "track": str(track),
                                "keys_count": len(drm.content_keys),
                                "kids": [k.hex for k in drm.content_keys.keys()],
                            },
                        )

                    for kid_, key in drm.content_keys.items():
                        if key == "0" * 32:
                            key = f"[red]{key}[/]"
                        is_track_kid_marker = ["", "*"][kid_ == track_kid]
                        label = f"[text2]{kid_.hex}:{key}{is_track_kid_marker}"
                        if not any(f"{kid_.hex}:{key}" in x.label for x in cek_tree.children):
                            cek_tree.add(label)

                    drm.content_keys = {
                        kid_: key for kid_, key in drm.content_keys.items() if key and key.count("0") != len(key)
                    }

                    # The CDM keys may have returned blank content keys for KIDs we got from vaults.
                    # So we re-add the keys from vaults earlier overwriting blanks or removed KIDs data.
                    drm.content_keys.update(from_vaults)

                    successful_caches = self.vaults.add_keys(drm.content_keys)
                    self.log.info(
                        f"Cached {len(drm.content_keys)} Key{'' if len(drm.content_keys) == 1 else 's'} to "
                        f"{successful_caches}/{len(self.vaults)} Vaults"
                    )

                if track_kid and track_kid not in drm.content_keys:
                    msg = f"No Content Key for KID {track_kid.hex} was returned in the License"
                    cek_tree.add(f"[logging.level.error]{msg}")
                    if not pre_existing_tree:
                        table.add_row(cek_tree)
                    raise Widevine.Exceptions.CEKNotFound(msg)

                if cek_tree.children and not pre_existing_tree:
                    table.add_row()
                    table.add_row(cek_tree)

                if export:
                    keys = {}
                    if export.is_file():
                        keys = jsonpickle.loads(export.read_text(encoding="utf8")) or {}
                    if str(title) not in keys:
                        keys[str(title)] = {}
                    if str(track) not in keys[str(title)]:
                        keys[str(title)][str(track)] = {}

                    track_data = keys[str(title)][str(track)]
                    track_data["url"] = track.url
                    track_data["descriptor"] = track.descriptor.name

                    if "keys" not in track_data:
                        track_data["keys"] = {}
                    for kid, key in drm.content_keys.items():
                        track_data["keys"][kid.hex] = key

                    export.write_text(jsonpickle.dumps(keys, indent=4), encoding="utf8")

        elif isinstance(drm, PlayReady):
            if self.debug_logger:
                self.debug_logger.log_drm_operation(
                    drm_type="PlayReady",
                    operation="prepare_drm",
                    service=self.service,
                    context={
                        "track": str(track),
                        "title": str(title),
                        "pssh": drm.pssh_b64 or "",
                        "kids": [k.hex for k in drm.kids],
                        "track_kid": track_kid.hex if track_kid else None,
                    },
                )

            with self.DRM_TABLE_LOCK:
                pssh_display = self.truncate_pssh_for_display(drm.pssh_b64 or "", "PlayReady")
                cek_tree = Tree(
                    Text.assemble(
                        ("PlayReady", "cyan"),
                        (f"({pssh_display})", "text"),
                        overflow="fold",
                    )
                )
                pre_existing_tree = next(
                    (x for x in table.columns[0].cells if isinstance(x, Tree) and x.label == cek_tree.label), None
                )
                if pre_existing_tree:
                    cek_tree = pre_existing_tree

                need_license = False
                all_kids = list(drm.kids)
                if track_kid and track_kid not in all_kids:
                    all_kids.append(track_kid)

                for kid in all_kids:
                    if kid in drm.content_keys:
                        is_track_kid = ["", "*"][kid == track_kid]
                        key = drm.content_keys[kid]
                        label = f"[text2]{kid.hex}:{key}{is_track_kid}"
                        if not any(f"{kid.hex}:{key}" in x.label for x in cek_tree.children):
                            cek_tree.add(label)
                        continue

                    is_track_kid = ["", "*"][kid == track_kid]

                    if not cdm_only:
                        content_key, vault_used = self.vaults.get_key(kid)
                        if content_key:
                            drm.content_keys[kid] = content_key
                            label = f"[text2]{kid.hex}:{content_key}{is_track_kid} from {vault_used}"
                            if not any(f"{kid.hex}:{content_key}" in x.label for x in cek_tree.children):
                                cek_tree.add(label)
                            self.vaults.add_key(kid, content_key, excluding=vault_used)

                            if self.debug_logger:
                                self.debug_logger.log_vault_query(
                                    vault_name=vault_used,
                                    operation="get_key_success",
                                    service=self.service,
                                    context={
                                        "kid": kid.hex,
                                        "content_key": content_key,
                                        "track": str(track),
                                        "from_cache": True,
                                        "drm_type": "PlayReady",
                                    },
                                )
                        elif vaults_only:
                            msg = f"No Vault has a Key for {kid.hex} and --vaults-only was used"
                            cek_tree.add(f"[logging.level.error]{msg}")
                            if not pre_existing_tree:
                                table.add_row(cek_tree)
                            if self.debug_logger:
                                self.debug_logger.log(
                                    level="ERROR",
                                    operation="vault_key_not_found",
                                    service=self.service,
                                    message=msg,
                                    context={"kid": kid.hex, "track": str(track), "drm_type": "PlayReady"},
                                )
                            raise PlayReady.Exceptions.CEKNotFound(msg)
                        else:
                            need_license = True

                    if kid not in drm.content_keys and cdm_only:
                        need_license = True

                if need_license and all(kid in drm.content_keys for kid in all_kids):
                    need_license = False

                if need_license and not vaults_only:
                    from_vaults = drm.content_keys.copy()

                    try:
                        drm.get_content_keys(cdm=self.cdm, licence=licence, certificate=certificate)
                    except Exception as e:
                        if drm.content_keys:
                            self.log.debug(f"License call failed but keys already in content_keys: {e}")
                        else:
                            if isinstance(e, (PlayReady.Exceptions.EmptyLicense, PlayReady.Exceptions.CEKNotFound)):
                                msg = str(e)
                            else:
                                msg = f"An exception occurred in the Service's license function: {e}"
                            cek_tree.add(f"[logging.level.error]{msg}")
                            if not pre_existing_tree:
                                table.add_row(cek_tree)
                            if self.debug_logger:
                                self.debug_logger.log_error(
                                    "get_license_playready",
                                    e,
                                    service=self.service,
                                    context={
                                        "track": str(track),
                                        "exception_type": type(e).__name__,
                                        "drm_type": "PlayReady",
                                    },
                                )
                            raise e

                    for kid_, key in drm.content_keys.items():
                        is_track_kid_marker = ["", "*"][kid_ == track_kid]
                        label = f"[text2]{kid_.hex}:{key}{is_track_kid_marker}"
                        if not any(f"{kid_.hex}:{key}" in x.label for x in cek_tree.children):
                            cek_tree.add(label)

                    drm.content_keys.update(from_vaults)

                    successful_caches = self.vaults.add_keys(drm.content_keys)
                    self.log.info(
                        f"Cached {len(drm.content_keys)} Key{'' if len(drm.content_keys) == 1 else 's'} to "
                        f"{successful_caches}/{len(self.vaults)} Vaults"
                    )

                if track_kid and track_kid not in drm.content_keys:
                    msg = f"No Content Key for KID {track_kid.hex} was returned in the License"
                    cek_tree.add(f"[logging.level.error]{msg}")
                    if not pre_existing_tree:
                        table.add_row(cek_tree)
                    raise PlayReady.Exceptions.CEKNotFound(msg)

                if cek_tree.children and not pre_existing_tree:
                    table.add_row()
                    table.add_row(cek_tree)

                if export:
                    keys = {}
                    if export.is_file():
                        keys = jsonpickle.loads(export.read_text(encoding="utf8")) or {}
                    if str(title) not in keys:
                        keys[str(title)] = {}
                    if str(track) not in keys[str(title)]:
                        keys[str(title)][str(track)] = {}

                    track_data = keys[str(title)][str(track)]
                    track_data["url"] = track.url
                    track_data["descriptor"] = track.descriptor.name

                    if "keys" not in track_data:
                        track_data["keys"] = {}
                    for kid, key in drm.content_keys.items():
                        track_data["keys"][kid.hex] = key

                    export.write_text(jsonpickle.dumps(keys, indent=4), encoding="utf8")

        elif isinstance(drm, MonaLisa):
            with self.DRM_TABLE_LOCK:
                display_id = drm.content_id or drm.pssh
                pssh_display = self.truncate_pssh_for_display(display_id, "MonaLisa")
                cek_tree = Tree(Text.assemble(("MonaLisa", "cyan"), (f"({pssh_display})", "text"), overflow="fold"))
                pre_existing_tree = next(
                    (x for x in table.columns[0].cells if isinstance(x, Tree) and x.label == cek_tree.label), None
                )
                if pre_existing_tree:
                    cek_tree = pre_existing_tree

                for kid_, key in drm.content_keys.items():
                    label = f"[text2]{kid_.hex}:{key}"
                    if not any(f"{kid_.hex}:{key}" in x.label for x in cek_tree.children):
                        cek_tree.add(label)

                if cek_tree.children and not pre_existing_tree:
                    table.add_row()
                    table.add_row(cek_tree)

    @staticmethod
    def get_cookie_path(service: str, profile: Optional[str]) -> Optional[Path]:
        """Get Service Cookie File Path for Profile."""
        direct_cookie_file = config.directories.cookies / f"{service}.txt"
        profile_cookie_file = config.directories.cookies / service / f"{profile}.txt"
        default_cookie_file = config.directories.cookies / service / "default.txt"

        if direct_cookie_file.exists():
            return direct_cookie_file
        elif profile_cookie_file.exists():
            return profile_cookie_file
        elif default_cookie_file.exists():
            return default_cookie_file

    @staticmethod
    def get_cookie_jar(service: str, profile: Optional[str]) -> Optional[MozillaCookieJar]:
        """Get Service Cookies for Profile."""
        cookie_file = dl.get_cookie_path(service, profile)
        if cookie_file:
            cookie_jar = MozillaCookieJar(cookie_file)
            cookie_data = html.unescape(cookie_file.read_text("utf8")).splitlines(keepends=False)
            for i, line in enumerate(cookie_data):
                if line and not line.startswith("#"):
                    line_data = line.lstrip().split("\t")
                    # Disable client-side expiry checks completely across everywhere
                    # Even though the cookies are loaded under ignore_expires=True, stuff
                    # like python-requests may not use them if they are expired
                    line_data[4] = ""
                    cookie_data[i] = "\t".join(line_data)
            cookie_data = "\n".join(cookie_data)
            cookie_file.write_text(cookie_data, "utf8")
            cookie_jar.load(ignore_discard=True, ignore_expires=True)
            return cookie_jar

    @staticmethod
    def save_cookies(path: Path, cookies: CookieJar):
        if hasattr(cookies, "jar"):
            cookies = cookies.jar

        cookie_jar = MozillaCookieJar(path)
        cookie_jar.load()
        for cookie in cookies:
            cookie_jar.set_cookie(cookie)
        cookie_jar.save(ignore_discard=True)

    @staticmethod
    def get_credentials(service: str, profile: Optional[str]) -> Optional[Credential]:
        """Get Service Credentials for Profile."""
        credentials = config.credentials.get(service)
        if credentials:
            if isinstance(credentials, dict):
                if profile:
                    credentials = credentials.get(profile) or credentials.get("default")
                else:
                    credentials = credentials.get("default")
            if credentials:
                if isinstance(credentials, list):
                    return Credential(*credentials)
                return Credential.loads(credentials)  # type: ignore

    def get_cdm(
        self,
        service: str,
        profile: Optional[str] = None,
        drm: Optional[str] = None,
        quality: Optional[int] = None,
    ) -> Optional[object]:
        """
        Get CDM for a specified service (either Local or Remote CDM).
        Now supports quality-based selection when quality is provided.
        Raises a ValueError if there's a problem getting a CDM.
        """
        cdm_name = config.cdm.get(service) or config.cdm.get("default")
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

                def sort_quality_key(key):
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
                lower_keys = {k.lower(): v for k, v in cdm_name.items()}
                if {"widevine", "playready"} & lower_keys.keys():
                    drm_key = None
                    if drm:
                        drm_key = {
                            "wv": "widevine",
                            "widevine": "widevine",
                            "pr": "playready",
                            "playready": "playready",
                        }.get(drm.lower())
                    cdm_name = lower_keys.get(drm_key or "widevine") or lower_keys.get("playready")
                else:
                    cdm_name = cdm_name.get(profile) or cdm_name.get("default") or config.cdm.get("default")
                if not cdm_name:
                    return None

        from unshackle.core.cdm import load_cdm

        return load_cdm(cdm_name, service_name=service, vaults=self.vaults)
