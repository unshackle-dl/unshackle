"""The dl option table: which dl options the REST API takes, their API defaults, and what a remote client forwards."""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Mapping, Optional, get_args

import click

from unshackle.core.utils.click_types import AUDIO_CODEC_LIST
from unshackle.core.utils.service_params import cast_service_options

log = logging.getLogger("api")

# Config keys accepted as natural names for params whose Python name dodges a builtin.
DL_OPTION_ALIASES = {"range": "range_", "list": "list_"}
DL_OPTION_API_NAMES = {click_name: api_name for api_name, click_name in DL_OPTION_ALIASES.items()}

RemoteKind = Literal["langs", "audio_codecs", "flag"]


@dataclass(frozen=True)
class ApiOption:
    """A dl option that a REST API client can set, with its API default.

    ``remote`` names how a remote client sends the option to the server in ``dl_params``, for a service that
    reads it from ``ctx.parent.params``.
    """

    default: Any
    remote: Optional[RemoteKind] = None


CLI_ONLY = None

DL_OPTIONS: Dict[str, Optional[ApiOption]] = {
    "profile": ApiOption(None),
    "quality": ApiOption([]),
    "vcodec": ApiOption([]),
    "acodec": ApiOption([], remote="audio_codecs"),
    "vbitrate": ApiOption(None),
    "abitrate": ApiOption(None),
    "vbitrate_range": ApiOption(None),
    "abitrate_range": ApiOption(None),
    "range": ApiOption(["SDR"]),
    "channels": ApiOption(None),
    "no_atmos": ApiOption(False),
    "wanted": ApiOption([]),
    "latest_episode": ApiOption(False),
    "latest_episodes": ApiOption(None),
    "lang": ApiOption(["orig"], remote="langs"),
    "v_lang": ApiOption([], remote="langs"),
    "a_lang": ApiOption([], remote="langs"),
    "s_lang": ApiOption(["all"], remote="langs"),
    "require_audio": ApiOption([]),
    "require_video": ApiOption([]),
    "require_subs": ApiOption([]),
    "skip_missing": ApiOption(False),
    "forced_subs": ApiOption(False, remote="flag"),
    "forced_subs_only": ApiOption(False),
    "forced_s_lang": ApiOption([]),
    "exact_lang": ApiOption(False, remote="flag"),
    "sub_format": ApiOption(None),
    "sub_font": ApiOption(None),
    "video_only": ApiOption(False),
    "audio_only": ApiOption(False),
    "subs_only": ApiOption(False),
    "chapters_only": ApiOption(False),
    "no_subs": ApiOption(False),
    "skip_subtitle_errors": ApiOption(False),
    "no_audio": ApiOption(False),
    "no_chapters": ApiOption(False),
    "no_video": ApiOption(False),
    "no_attachments": ApiOption(False),
    "audio_description": ApiOption(False),
    "audio_description_only": ApiOption(False),
    "slow": ApiOption(None),
    "split_audio": ApiOption(None),
    "skip_dl": ApiOption(False),
    "all_drm": ApiOption(False),
    "export": ApiOption(False),
    "cdm_only": ApiOption(None),
    "proxy": ApiOption(None),
    "no_proxy": ApiOption(False),
    "no_proxy_download": ApiOption(False),
    "proxy_download": ApiOption(None),
    "no_folder": ApiOption(False),
    "no_source": ApiOption(False),
    "no_mux": ApiOption(False),
    "sample": ApiOption(None),
    "include_sample": ApiOption(False),
    "workers": ApiOption(None),
    "adaptive_workers": ApiOption(False),
    "download_processes": ApiOption(1),
    "continue_downloads": ApiOption(False),
    "downloads": ApiOption(1),
    "worst": ApiOption(False),
    "best_available": ApiOption(False),
    "generate_dv": ApiOption(False),
    "repack": ApiOption(False),
    "tag": ApiOption(None),
    "tmdb_id": ApiOption(None),
    "imdb_id": ApiOption(None),
    "tvdb_id": ApiOption(None),
    "tvdb_order": ApiOption(None),
    "continuous_episodes": ApiOption(False),
    "anilist_id": ApiOption(None),
    "enrich": ApiOption(False),
    "daily": ApiOption(False),
    "output_dir": ApiOption(None),
    "no_cache": ApiOption(False),
    "reset_cache": ApiOption(False),
    "select_titles": CLI_ONLY,
    "list": CLI_ONLY,
    "list_titles": CLI_ONLY,
    "import_file": CLI_ONLY,
    "cdm_name": CLI_ONLY,
    "remote": CLI_ONLY,
    "server": CLI_ONLY,
    "local_proxy": CLI_ONLY,
    "postscript": CLI_ONLY,
    "no_postscript": CLI_ONLY,
    "merge_video": CLI_ONLY,
    "real_video_bitrate": CLI_ONLY,
    "real_audio_bitrate": CLI_ONLY,
    "speed_limit": CLI_ONLY,
}
"""Every dl option, by its API and config name (``DL_OPTION_ALIASES`` gives the click name where it differs).

An option the REST API takes has an ``ApiOption``; a ``CLI_ONLY`` option never reaches dl from the API.
"""

DEFAULT_DOWNLOAD_PARAMS: Dict[str, Any] = {name: option.default for name, option in DL_OPTIONS.items() if option}
FORWARDED_DL_PARAMS = [
    (name, option)
    for kind in get_args(RemoteKind)
    for name, option in DL_OPTIONS.items()
    if option and option.remote == kind
]


def cast_dl_params(cli: click.Command, params: Dict[str, Any]) -> Dict[str, Any]:
    """Cast each ``DEFAULT_DOWNLOAD_PARAMS`` parameter in ``params`` by its dl click type, under its dl option name.

    A missing, ``None`` or empty-string parameter, and an empty ``range``, take the API default, so
    ``output_dir: ""`` is not the working directory. A parameter outside ``DEFAULT_DOWNLOAD_PARAMS`` never reaches
    dl, so API clients cannot set a new CLI option until the table opens it. Raises ``click.BadParameter``.
    """
    kept: Dict[str, Any] = {}
    to_cast: Dict[str, Any] = {}
    for key, default in DEFAULT_DOWNLOAD_PARAMS.items():
        value = params.get(key)
        if value is None or value == "" or (key == "range" and value == []):
            value = default
        (kept if value is None else to_cast)[DL_OPTION_ALIASES.get(key, key)] = value
    try:
        return {**kept, **cast_service_options(cli.params, click.Context(cli), to_cast)}
    except click.BadParameter as e:
        name = (e.param.name if e.param else None) or ""
        key = DL_OPTION_API_NAMES.get(name, name)
        raise click.BadParameter(f"Invalid {key}: {e.message}") from e


def remote_dl_params(params: Mapping[str, Any]) -> Dict[str, Any]:
    """The ``dl_params`` a remote client sends when it creates a remote session, from its dl context params.

    An empty list is sent as-is, because only an absent key falls back to the dl default on the server.
    """
    sent: Dict[str, Any] = {}
    for key, option in FORWARDED_DL_PARAMS:
        if option.remote == "flag":
            if key in params:
                sent[key] = bool(params[key])
        elif params.get(key) is not None:
            sent[key] = [c.name if option.remote == "audio_codecs" else str(c) for c in params[key]]
    return sent


def forwarded_dl_params(data: Dict[str, Any]) -> Dict[str, Any]:
    """Read the dl track selection that a remote-dl client sends under ``dl_params``.

    Services read these values from ``ctx.parent.params`` to pick the manifests they
    fetch. They travel in their own object so that they never collide with a service option that
    has the same name. An absent or malformed value gets the dl default, so an old client that
    sends no ``dl_params`` gets the same result as a ``dl`` run with no selection flags.
    ``acodec`` holds codec names or aliases, which become ``Audio.Codec`` members the same way
    ``dl -a`` converts them; an unknown name is dropped.
    """
    raw = data.get("dl_params")
    if not isinstance(raw, dict):
        raw = {}

    def str_list(key: str) -> Optional[List[str]]:
        value = raw.get(key)
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return list(value)
        if value is not None:
            log.warning(f"Ignoring dl_params.{key}: it must be an array of strings")
        return None

    params: Dict[str, Any] = {}
    for key, option in FORWARDED_DL_PARAMS:
        if option.remote == "flag":
            params[key] = raw.get(key) is True
        elif option.remote == "langs":
            names = str_list(key)
            params[key] = names if names is not None else list(option.default)
        else:
            names = str_list(key)
            # the dl CLI default is [], and some services test membership in it
            params[key] = list(option.default)
            for name in names or []:
                try:
                    params[key].extend(AUDIO_CODEC_LIST.convert(name))
                except click.BadParameter:
                    # imported here: unshackle.core.api imports this module
                    from unshackle.core.api.sanitize import sanitize_log

                    log.warning(f"Ignoring unknown dl_params.{key} value: {sanitize_log(name)}")
    return params
