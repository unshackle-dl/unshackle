from __future__ import annotations

import html
import logging
import math
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
from construct import ConstError
from pymediainfo import MediaInfo
from pyplayready.cdm import Cdm as PlayReadyCdm
from pyplayready.device import Device as PlayReadyDevice
from pywidevine.cdm import Cdm as WidevineCdm
from pywidevine.device import Device
from pywidevine.remotecdm import RemoteCdm
from rich.console import Group
from rich.live import Live
from rich.padding import Padding
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskID, TextColumn, TimeRemainingColumn
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from unshackle.core import binaries
from unshackle.core.cdm import CustomRemoteCDM, DecryptLabsRemoteCDM
from unshackle.core.config import config
from unshackle.core.console import console
from unshackle.core.constants import DOWNLOAD_LICENCE_ONLY, AnyTrack, context_settings
from unshackle.core.credential import Credential
from unshackle.core.drm import DRM_T, PlayReady, Widevine
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
from unshackle.core.utils.click_types import (LANGUAGE_RANGE, QUALITY_LIST, SEASON_RANGE, ContextData, MultipleChoice,
                                              SubtitleCodecChoice, VideoCodecChoice)
from unshackle.core.utils.collections import merge_dict
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
        type=VideoCodecChoice(Video.Codec),
        default=None,
        help="Video Codec to download, defaults to any codec.",
    )
    @click.option(
        "-a",
        "--acodec",
        type=click.Choice(Audio.Codec, case_sensitive=False),
        default=None,
        help="Audio Codec to download, defaults to any codec.",
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
    @click.option(
        "--tmdb",
        "tmdb_id",
        type=int,
        default=None,
        help="Use this TMDB ID for tagging instead of automatic lookup.",
    )
    @click.option(
        "--tmdb-name",
        "tmdb_name",
        is_flag=True,
        default=False,
        help="Rename titles using the name returned from TMDB lookup.",
    )
    @click.option(
        "--tmdb-year",
        "tmdb_year",
        is_flag=True,
        default=False,
        help="Use the release year from TMDB for naming and tagging.",
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
    @click.option("--no-cache", "no_cache", is_flag=True, default=False, help="Bypass title cache for this download.")
    @click.option(
        "--reset-cache", "reset_cache", is_flag=True, default=False, help="Clear title cache before fetching."
    )
    @click.option(
        "--best-available",
        "best_available",
        is_flag=True,
        default=False,
        help="Continue with best available quality if requested resolutions are not available.",
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
        tag: Optional[str] = None,
        tmdb_id: Optional[int] = None,
        tmdb_name: bool = False,
        tmdb_year: bool = False,
        *_: Any,
        **__: Any,
    ):
        if not ctx.invoked_subcommand:
            raise ValueError("A subcommand to invoke was not specified, the main code cannot continue.")

        self.log = logging.getLogger("download")

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
        self.tmdb_name = tmdb_name
        self.tmdb_year = tmdb_year

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
                        "tmdb_id": tmdb_id,
                        "tmdb_name": tmdb_name,
                        "tmdb_year": tmdb_year,
                        "cli_params": {
                            k: v
                            for k, v in ctx.params.items()
                            if k not in ["profile", "proxy", "tag", "tmdb_id", "tmdb_name", "tmdb_year"]
                        },
                    },
                )
        else:
            self.debug_logger = None

        if self.profile:
            self.log.info(f"Using profile: '{self.profile}'")

        with console.status("Loading Service Config...", spinner="dots"):
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
            else:
                self.service_config = {}
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
                    self.log.info(
                        f"Loaded PlayReady CDM: {self.cdm.certificate_chain.get_name()} (L{self.cdm.security_level})"
                    )
                    cdm_info = {
                        "type": "PlayReady",
                        "certificate": self.cdm.certificate_chain.get_name(),
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
                            proxy_query = proxy  # Save query before overwriting with URI
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
                                proxy_query = proxy  # Save query before overwriting with URI
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
                    ctx.params["proxy_query"] = proxy
                    ctx.params["proxy_provider"] = requested_provider
                else:
                    self.log.info(f"Using explicit Proxy: {proxy}")
                    # For explicit proxies, store None for query/provider
                    ctx.params["proxy_query"] = None
                    ctx.params["proxy_provider"] = None

        ctx.obj = ContextData(
            config=self.service_config, cdm=self.cdm, proxy_providers=self.proxy_providers, profile=self.profile
        )

        if tag:
            config.tag = tag

        # needs to be added this way instead of @cli.result_callback to be
        # able to keep `self` as the first positional
        self.cli._result_callback = self.result

    def result(
        self,
        service: Service,
        quality: list[int],
        vcodec: Optional[Video.Codec],
        acodec: Optional[Audio.Codec],
        vbitrate: int,
        abitrate: int,
        range_: list[Video.Range],
        channels: float,
        no_atmos: bool,
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
        best_available: bool,
        *_: Any,
        **__: Any,
    ) -> None:
        self.tmdb_searched = False
        self.search_source = None
        start_time = time.time()

        if require_subs and s_lang != ["all"]:
            self.log.error("--require-subs and --s-lang cannot be used together")
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

        with console.status("Authenticating with Service...", spinner="dots"):
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

        with console.status("Fetching Title Metadata...", spinner="dots"):
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

        if (self.tmdb_year or self.tmdb_name) and self.tmdb_id:
            sample_title = titles[0] if hasattr(titles, "__getitem__") else titles
            kind = "tv" if isinstance(sample_title, Episode) else "movie"

            tmdb_year_val = None
            tmdb_name_val = None

            if self.tmdb_year:
                tmdb_year_val = tags.get_year(
                    self.tmdb_id, kind, title_cacher, cache_title_id, cache_region, cache_account_hash
                )

            if self.tmdb_name:
                tmdb_name_val = tags.get_title(
                    self.tmdb_id, kind, title_cacher, cache_title_id, cache_region, cache_account_hash
                )

            if isinstance(titles, (Series, Movies)):
                for t in titles:
                    if tmdb_year_val:
                        t.year = tmdb_year_val
                    if tmdb_name_val:
                        if isinstance(t, Episode):
                            t.title = tmdb_name_val
                        else:
                            t.name = tmdb_name_val
            else:
                if tmdb_year_val:
                    titles.year = tmdb_year_val
                if tmdb_name_val:
                    if isinstance(titles, Episode):
                        titles.title = tmdb_name_val
                    else:
                        titles.name = tmdb_name_val

        console.print(Padding(Rule(f"[rule.text]{titles.__class__.__name__}: {titles}"), (1, 2)))

        console.print(Padding(titles.tree(verbose=list_titles), (0, 5)))
        if list_titles:
            return

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
                if self.tmdb_id:
                    tmdb_title = tags.get_title(
                        self.tmdb_id, kind, title_cacher, cache_title_id, cache_region, cache_account_hash
                    )
                else:
                    self.tmdb_id, tmdb_title, self.search_source = tags.search_show_info(
                        title.title, title.year, kind, title_cacher, cache_title_id, cache_region, cache_account_hash
                    )
                    if not (self.tmdb_id and tmdb_title and tags.fuzzy_match(tmdb_title, title.title)):
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
                movie_id, movie_title, _ = tags.search_show_info(
                    title.name, title.year, "movie", title_cacher, cache_title_id, cache_region, cache_account_hash
                )
                if movie_id:
                    console.print(
                        Padding(
                            f"Search -> {movie_title or '?'} [bright_black](ID {movie_id})",
                            (0, 5),
                        )
                    )
                else:
                    console.print(Padding("Search -> [bright_black]No match found[/]", (0, 5)))

            if self.tmdb_id and getattr(self, "search_source", None) != "simkl":
                kind = "tv" if isinstance(title, Episode) else "movie"
                tags.external_ids(self.tmdb_id, kind, title_cacher, cache_title_id, cache_region, cache_account_hash)

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

            with console.status("Getting tracks...", spinner="dots"):
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
                        title.tracks.select_video(lambda x: x.codec == vcodec)
                        if not title.tracks.videos:
                            self.log.error(f"There's no {vcodec.name} Video Track...")
                            sys.exit(1)

                    if range_:
                        # Special handling for HYBRID - don't filter, keep all HDR10 and DV tracks
                        if Video.Range.HYBRID not in range_:
                            title.tracks.select_video(lambda x: x.range in range_)
                            missing_ranges = [r for r in range_ if not any(x.range == r for x in title.tracks.videos)]
                            for color_range in missing_ranges:
                                self.log.warning(f"Skipping {color_range.name} video tracks as none are available.")

                    if vbitrate:
                        title.tracks.select_video(lambda x: x.bitrate and x.bitrate // 1000 == vbitrate)
                        if not title.tracks.videos:
                            self.log.error(f"There's no {vbitrate}kbps Video Track...")
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

                    if quality:
                        missing_resolutions = []
                        if any(r == Video.Range.HYBRID for r in range_):
                            title.tracks.select_video(title.tracks.select_hybrid(title.tracks.videos, quality))
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
                    if any(r == Video.Range.HYBRID for r in range_):
                        # For hybrid mode, always apply hybrid selection
                        # If no quality specified, use only the best (highest) resolution
                        if not quality:
                            # Get the highest resolution available
                            best_resolution = max((v.height for v in title.tracks.videos), default=None)
                            if best_resolution:
                                # Use the hybrid selection logic with only the best resolution
                                title.tracks.select_video(
                                    title.tracks.select_hybrid(title.tracks.videos, [best_resolution])
                                )
                        # If quality was specified, hybrid selection was already applied above
                    else:
                        selected_videos: list[Video] = []
                        for resolution, color_range in product(quality or [None], range_ or [None]):
                            match = next(
                                (
                                    t
                                    for t in title.tracks.videos
                                    if (
                                        not resolution
                                        or t.height == resolution
                                        or int(t.width * (9 / 16)) == resolution
                                    )
                                    and (not color_range or t.range == color_range)
                                ),
                                None,
                            )
                            if match and match not in selected_videos:
                                selected_videos.append(match)
                        title.tracks.videos = selected_videos

                    # validate hybrid mode requirements
                    if any(r == Video.Range.HYBRID for r in range_):
                        hdr10_tracks = [v for v in title.tracks.videos if v.range == Video.Range.HDR10]
                        dv_tracks = [v for v in title.tracks.videos if v.range == Video.Range.DV]

                        if not hdr10_tracks and not dv_tracks:
                            available_ranges = sorted(set(v.range.name for v in title.tracks.videos))
                            self.log.error("HYBRID mode requires both HDR10 and DV tracks, but neither is available")
                            self.log.error(
                                f"Available ranges: {', '.join(available_ranges) if available_ranges else 'none'}"
                            )
                            sys.exit(1)
                        elif not hdr10_tracks:
                            available_ranges = sorted(set(v.range.name for v in title.tracks.videos))
                            self.log.error("HYBRID mode requires both HDR10 and DV tracks, but only DV is available")
                            self.log.error(f"Available ranges: {', '.join(available_ranges)}")
                            sys.exit(1)
                        elif not dv_tracks:
                            available_ranges = sorted(set(v.range.name for v in title.tracks.videos))
                            self.log.error("HYBRID mode requires both HDR10 and DV tracks, but only HDR10 is available")
                            self.log.error(f"Available ranges: {', '.join(available_ranges)}")
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
                        title.tracks.select_audio(lambda x: x.codec == acodec)
                        if not title.tracks.audio:
                            self.log.error(f"There's no {acodec.name} Audio Tracks...")
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

                        if "best" in processed_lang:
                            unique_languages = {track.language for track in title.tracks.audio}
                            selected_audio = []
                            for language in unique_languages:
                                highest_quality = max(
                                    (track for track in title.tracks.audio if track.language == language),
                                    key=lambda x: x.bitrate or 0,
                                )
                                selected_audio.append(highest_quality)
                            title.tracks.audio = selected_audio
                        elif "all" not in processed_lang:
                            per_language = 1
                            title.tracks.audio = title.tracks.by_language(
                                title.tracks.audio, processed_lang, per_language=per_language, exact_match=exact_lang
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
                    if isinstance(self.cdm, (WidevineCdm, DecryptLabsRemoteCDM)) and not (
                        isinstance(self.cdm, DecryptLabsRemoteCDM) and self.cdm.is_playready
                    ):
                        quality_based_cdm = self.get_cdm(
                            self.service, self.profile, drm="widevine", quality=highest_quality
                        )
                        if quality_based_cdm and quality_based_cdm != self.cdm:
                            self.log.debug(
                                f"Pre-selecting Widevine CDM based on highest quality {highest_quality}p across all video tracks"
                            )
                            self.cdm = quality_based_cdm
                    elif isinstance(self.cdm, (PlayReadyCdm, DecryptLabsRemoteCDM)) and (
                        isinstance(self.cdm, DecryptLabsRemoteCDM) and self.cdm.is_playready
                    ):
                        quality_based_cdm = self.get_cdm(
                            self.service, self.profile, drm="playready", quality=highest_quality
                        )
                        if quality_based_cdm and quality_based_cdm != self.cdm:
                            self.log.debug(
                                f"Pre-selecting PlayReady CDM based on highest quality {highest_quality}p across all video tracks"
                            )
                            self.cdm = quality_based_cdm

            dl_start_time = time.time()

            if skip_dl:
                DOWNLOAD_LICENCE_ONLY.set()

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
                                            if (
                                                isinstance(self.cdm, PlayReadyCdm)
                                                or (
                                                    isinstance(self.cdm, DecryptLabsRemoteCDM) and self.cdm.is_playready
                                                )
                                            )
                                            and hasattr(service, "get_playready_license")
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

                video_track_n = 0

                while (
                    not title.tracks.subtitles
                    and not no_subs
                    and not (hasattr(service, "NO_SUBTITLES") and service.NO_SUBTITLES)
                    and not video_only
                    and not no_video
                    and len(title.tracks.videos) > video_track_n
                    and any(
                        x.get("codec_name", "").startswith("eia_")
                        for x in ffprobe(title.tracks.videos[video_track_n].path).get("streams", [])
                    )
                ):
                    with console.status(f"Checking Video track {video_track_n + 1} for Closed Captions..."):
                        try:
                            # TODO: Figure out the real language, it might be different
                            #       EIA-CC tracks sadly don't carry language information :(
                            # TODO: Figure out if the CC language is original lang or not.
                            #       Will need to figure out above first to do so.
                            video_track = title.tracks.videos[video_track_n]
                            track_id = f"ccextractor-{video_track.id}"
                            cc_lang = title.language or video_track.language
                            cc = video_track.ccextractor(
                                track_id=track_id,
                                out_path=config.directories.temp
                                / config.filenames.subtitle.format(id=track_id, language=cc_lang),
                                language=cc_lang,
                                original=False,
                            )
                            if cc:
                                # will not appear in track listings as it's added after all times it lists
                                title.tracks.add(cc)
                                self.log.info(f"Extracted a Closed Caption from Video track {video_track_n + 1}")
                            else:
                                self.log.info(f"No Closed Captions were found in Video track {video_track_n + 1}")
                        except EnvironmentError:
                            self.log.error(
                                "Cannot extract Closed Captions as the ccextractor executable was not found..."
                            )
                            break
                    video_track_n += 1

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
                                has_decrypted = True
                                events.emit(events.Types.TRACK_REPACKED, track=track)
                            else:
                                self.log.warning(
                                    f"No matching DRM found for track {track} with CDM type {type(self.cdm).__name__}"
                                )
                        if has_decrypted:
                            self.log.info(f"Decrypted tracks with {decrypt_tool}")

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

                    multiplex_tasks: list[tuple[TaskID, Tracks]] = []

                    # Check if we're in hybrid mode
                    if any(r == Video.Range.HYBRID for r in range_) and title.tracks.videos:
                        # Hybrid mode: process DV and HDR10 tracks separately for each resolution
                        self.log.info("Processing Hybrid HDR10+DV tracks...")

                        # Group video tracks by resolution
                        resolutions_processed = set()
                        hdr10_tracks = [v for v in title.tracks.videos if v.range == Video.Range.HDR10]
                        dv_tracks = [v for v in title.tracks.videos if v.range == Video.Range.DV]

                        for hdr10_track in hdr10_tracks:
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

                                # The Hybrid class creates HDR10-DV.hevc, rename it for this resolution
                                default_output = config.directories.temp / "HDR10-DV.hevc"
                                if default_output.exists():
                                    shutil.move(str(default_output), str(hybrid_output_path))

                                # Create a mux task for this resolution
                                task_description = f"Multiplexing Hybrid HDR10+DV {resolution}p"
                                task_id = progress.add_task(f"{task_description}...", total=None, start=False)

                                # Create tracks with the hybrid video output for this resolution
                                task_tracks = Tracks(title.tracks) + title.tracks.chapters + title.tracks.attachments

                                # Create a new video track for the hybrid output
                                hybrid_track = deepcopy(hdr10_track)
                                hybrid_track.path = hybrid_output_path
                                hybrid_track.range = Video.Range.DV  # It's now a DV track
                                hybrid_track.needs_duration_fix = True
                                task_tracks.videos = [hybrid_track]

                                multiplex_tasks.append((task_id, task_tracks))

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

                            task_id = progress.add_task(f"{task_description}...", total=None, start=False)

                            task_tracks = Tracks(title.tracks) + title.tracks.chapters + title.tracks.attachments
                            if video_track:
                                task_tracks.videos = [video_track]

                            multiplex_tasks.append((task_id, task_tracks))

                    with Live(Padding(progress, (0, 5, 1, 5)), console=console):
                        for task_id, task_tracks in multiplex_tasks:
                            progress.start_task(task_id)  # TODO: Needed?
                            audio_expected = not video_only and not no_audio
                            muxed_path, return_code, errors = task_tracks.mux(
                                str(title),
                                progress=partial(progress.update, task_id=task_id),
                                delete=False,
                                audio_expected=audio_expected,
                                title_language=title.language,
                            )
                            muxed_paths.append(muxed_path)
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
                            for video_track in task_tracks.videos:
                                video_track.delete()
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

                else:
                    # dont mux
                    muxed_paths.append(title.tracks.audio[0].path)

                if no_mux:
                    # Handle individual track files without muxing
                    final_dir = config.directories.downloads
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
                    for muxed_path in muxed_paths:
                        media_info = MediaInfo.parse(muxed_path)
                        final_dir = config.directories.downloads
                        final_filename = title.get_filename(media_info, show_service=not no_source)

                        if not no_folder and isinstance(title, (Episode, Song)):
                            final_dir /= title.get_filename(media_info, show_service=not no_source, folder=True)

                        final_dir.mkdir(parents=True, exist_ok=True)
                        final_path = final_dir / f"{final_filename}{muxed_path.suffix}"

                        shutil.move(muxed_path, final_path)
                        tags.tag_file(final_path, title, self.tmdb_id)

                title_dl_time = time_elapsed_since(dl_start_time)
                console.print(
                    Padding(f":tada: Title downloaded in [progress.elapsed]{title_dl_time}[/]!", (0, 5, 1, 5))
                )

            # update cookies
            cookie_file = self.get_cookie_path(self.service, self.profile)
            if cookie_file:
                self.save_cookies(cookie_file, service.session.cookies)

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

        track_quality = None
        if isinstance(track, Video) and track.height:
            track_quality = track.height

        if isinstance(drm, Widevine):
            if not isinstance(self.cdm, (WidevineCdm, DecryptLabsRemoteCDM)) or (
                isinstance(self.cdm, DecryptLabsRemoteCDM) and self.cdm.is_playready
            ):
                widevine_cdm = self.get_cdm(self.service, self.profile, drm="widevine", quality=track_quality)
                if widevine_cdm:
                    if track_quality:
                        self.log.info(f"Switching to Widevine CDM for Widevine {track_quality}p content")
                    else:
                        self.log.info("Switching to Widevine CDM for Widevine content")
                    self.cdm = widevine_cdm

        elif isinstance(drm, PlayReady):
            if not isinstance(self.cdm, (PlayReadyCdm, DecryptLabsRemoteCDM)) or (
                isinstance(self.cdm, DecryptLabsRemoteCDM) and not self.cdm.is_playready
            ):
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

                if need_license and not vaults_only:
                    from_vaults = drm.content_keys.copy()

                    try:
                        drm.get_content_keys(cdm=self.cdm, licence=licence, certificate=certificate)
                    except Exception as e:
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

        cdm_api = next(iter(x.copy() for x in config.remote_cdm if x["name"] == cdm_name), None)
        if cdm_api:
            cdm_type = cdm_api.get("type")

            if cdm_type == "decrypt_labs":
                del cdm_api["name"]
                del cdm_api["type"]

                if "secret" not in cdm_api or not cdm_api["secret"]:
                    if config.decrypt_labs_api_key:
                        cdm_api["secret"] = config.decrypt_labs_api_key
                    else:
                        raise ValueError(
                            f"No secret provided for DecryptLabs CDM '{cdm_name}' and no global "
                            "decrypt_labs_api_key configured"
                        )

                # All DecryptLabs CDMs use DecryptLabsRemoteCDM
                return DecryptLabsRemoteCDM(service_name=service, vaults=self.vaults, **cdm_api)

            elif cdm_type == "custom_api":
                del cdm_api["name"]
                del cdm_api["type"]

                # All Custom API CDMs use CustomRemoteCDM
                return CustomRemoteCDM(service_name=service, vaults=self.vaults, **cdm_api)

            else:
                return RemoteCdm(
                    device_type=cdm_api["Device Type"],
                    system_id=cdm_api["System ID"],
                    security_level=cdm_api["Security Level"],
                    host=cdm_api["Host"],
                    secret=cdm_api["Secret"],
                    device_name=cdm_api["Device Name"],
                )

        prd_path = config.directories.prds / f"{cdm_name}.prd"
        if not prd_path.is_file():
            prd_path = config.directories.wvds / f"{cdm_name}.prd"
        if prd_path.is_file():
            device = PlayReadyDevice.load(prd_path)
            return PlayReadyCdm.from_device(device)

        cdm_path = config.directories.wvds / f"{cdm_name}.wvd"
        if not cdm_path.is_file():
            raise ValueError(f"{cdm_name} does not exist or is not a file")

        try:
            device = Device.load(cdm_path)
        except ConstError as e:
            if "expected 2 but parsed 1" in str(e):
                raise ValueError(
                    f"{cdm_name}.wvd seems to be a v1 WVD file, use `pywidevine migrate --help` to migrate it to v2."
                )
            raise ValueError(f"{cdm_name}.wvd is an invalid or corrupt Widevine Device file, {e}")

        return WidevineCdm.from_device(device)
