"""The dl front door: one checked run plan from a dl click parse or a REST API job, and what the run gave back."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Collection, Mapping, Optional, Union

import click
from click.core import ParameterSource

from unshackle.core import providers
from unshackle.core.dl_options import DL_OPTION_API_NAMES, cast_dl_params
from unshackle.core.downloaders import parse_speed_limit
from unshackle.core.providers.anilist import parse_anilist_ref
from unshackle.core.providers.tvdb import SEASON_TYPES
from unshackle.core.remote_protocol import APIErrorCode
from unshackle.core.utilities import inherited_exclusions, kind_only_conflict, partition_exclusions

ID_PROVIDER_KEYS = {"tmdb": "tmdb_api_key", "tvdb": "tvdb_api_key", "omdb": "omdb_api_key"}
PLAN_API_NAMES = {**DL_OPTION_API_NAMES, "cdm_name": "cdm", "server": "remote"}


class PlanError(click.ClickException):
    """A dl option value or combination that dl refuses, in one line."""

    error_code = APIErrorCode.INVALID_PARAMETERS


class RunStopped(SystemExit):
    """A run that a missing track or language stopped, with the reason dl logged."""

    def __init__(self, reason: str) -> None:
        super().__init__(1)
        self.reason = reason


@dataclass
class RunOutcome:
    """What a dl run gave back: the files it wrote, the subtitles it skipped, and a download failure it reported."""

    files: list[Path] = field(default_factory=list)
    skipped_subtitles: list[Any] = field(default_factory=list)
    error: Optional[BaseException] = None


@dataclass(frozen=True)
class RunPlan:
    """The checked dl parameters of one run, by dl click name, with the source of each value."""

    params: dict[str, Any]
    sources: dict[str, ParameterSource]

    def context(self, cli: click.Command, service: str) -> click.Context:
        """A dl context for this plan, so dl and a service read the values and sources as on the CLI."""
        ctx = click.Context(cli)
        ctx.invoked_subcommand = service
        ctx.params = dict(self.params)
        for name, source in self.sources.items():
            ctx.set_parameter_source(name, source)
        return ctx


def validate_metadata_ids(
    tmdb_id: Optional[int],
    imdb_id: Optional[str],
    tvdb_id: Optional[int],
    anilist_id: Optional[Union[int, str]] = None,
) -> None:
    """Reject ID flags that conflict, or that no configured metadata provider could ever find.

    --anilist stays outside the mutual exclusion: AniList knows no western IDs, so pairing
    it with one of the other three is how an anime title gets both.
    """
    if anilist_id and not parse_anilist_ref(anilist_id):
        raise PlanError("--anilist takes an AniList ID like 21, or a MyAnimeList ID like mal:21.")

    supplied = [
        (flag, value) for flag, value in (("--tmdb", tmdb_id), ("--imdb", imdb_id), ("--tvdb", tvdb_id)) if value
    ]
    if len(supplied) > 1:
        raise PlanError(f"{', '.join(flag for flag, _ in supplied)} cannot be used together; give one ID")
    if not supplied:
        return

    flag, _ = supplied[0]
    id_kind = flag.lstrip("-")
    ordered = {cls.NAME: cls for kind in ("tv", "movie") for cls in providers.provider_order(kind)}
    consumers = [cls for cls in ordered.values() if cls.ID_KIND == id_kind]
    if any(cls().is_available() for cls in consumers):
        return

    if consumers:
        keys = sorted({ID_PROVIDER_KEYS[cls.NAME] for cls in consumers if cls.NAME in ID_PROVIDER_KEYS})
        raise PlanError(f"{flag} needs the {consumers[0].NAME} provider: set {' or '.join(keys)} in your config")
    names = sorted({cls.NAME for cls in providers.REGISTRY.values() if cls.ID_KIND == id_kind})
    raise PlanError(f"{flag} needs the {' or '.join(names)} provider, which your metadata_providers config leaves out.")


def bitrate_range(value: Optional[str], flag: str, example: str) -> Optional[tuple[int, int]]:
    """A ``MIN-MAX`` bitrate range as (low, high), in either order."""
    if not value:
        return None
    try:
        low, high = (int(part) for part in value.split("-"))
    except ValueError:
        raise PlanError(f"Invalid {flag}: expected MIN-MAX, such as {example}") from None
    return min(low, high), max(low, high)


def check_options(params: Mapping[str, Any]) -> None:
    """Refuse the dl option values and combinations that dl cannot run, from cast params by dl click name."""
    from unshackle.core.config import config
    from unshackle.core.tracks import Video
    from unshackle.core.tracks.selection import generated_dv_ranges

    validate_metadata_ids(params.get("tmdb_id"), params.get("imdb_id"), params.get("tvdb_id"), params.get("anilist_id"))
    tvdb_order = (params.get("tvdb_order") or config.tvdb_order or "").lower()
    if tvdb_order and tvdb_order not in SEASON_TYPES:
        raise PlanError(f"tvdb_order must be one of {', '.join(SEASON_TYPES)}, not {tvdb_order!r}")
    if params.get("enrich") and not any(params.get(k) for k in ("tmdb_id", "imdb_id", "tvdb_id", "anilist_id")):
        raise PlanError("--enrich requires --tmdb, --imdb, --tvdb, or --anilist to provide a metadata source.")
    if params.get("sample") and params.get("include_sample"):
        raise PlanError("--sample and --include-sample cannot be used together.")

    included: dict[str, list[str]] = {}
    excluded: dict[str, list[str]] = {}
    for name, flag in (
        ("lang", "-l/--lang"),
        ("v_lang", "-vl/--v-lang"),
        ("a_lang", "-al/--a-lang"),
        ("s_lang", "-sl/--s-lang"),
        ("forced_s_lang", "-fsl/--forced-s-lang"),
    ):
        included[name], excluded[name] = partition_exclusions(params.get(name))
        if any(token.lower() == "all" for token in excluded[name]):
            raise PlanError(f"{flag}: 'all' cannot be excluded")
    for flag, key, own, fallback in (
        ("--require-audio", "require_audio", "a_lang", "lang"),
        ("--require-video", "require_video", "v_lang", "lang"),
        ("--require-subs", "require_subs", "s_lang", None),
    ):
        excludes = inherited_exclusions(included[own], excluded[own], excluded[fallback] if fallback else [])
        required, own_excl = partition_exclusions(params.get(key))
        clash = own_excl + [t for t in required if any(t.lower() == e.lower() for e in excludes)]
        if clash:
            raise PlanError(f"{flag}: {clash[0]} cannot be required and excluded at the same time.")

    for only, drop in (("subs_only", "no_subs"), ("audio_only", "no_audio")):
        if params.get(only) and params.get(drop):
            raise PlanError(f"--{only.replace('_', '-')} cannot be used with --{drop.replace('_', '-')}.")
    conflict = kind_only_conflict(params)
    if conflict:
        first, second = (f"--{name.replace('_', '-')}" for name in conflict)
        raise PlanError(f"{first} cannot be used with {second}: {second} drops the tracks that {first} selects.")

    try:
        parse_speed_limit(params.get("speed_limit"))
    except ValueError as e:
        raise PlanError(str(e)) from None

    for kind, example in (("v", "6000-7000"), ("a", "128-256")):
        if params.get(f"{kind}bitrate_range"):
            if params.get(f"{kind}bitrate"):
                raise PlanError(f"Cannot use both --{kind}bitrate and --{kind}bitrate-range at the same time.")
            bitrate_range(params[f"{kind}bitrate_range"], f"--{kind}bitrate-range", example)

    if params.get("select_titles") and params.get("wanted"):
        raise PlanError("--select-titles and -w/--wanted cannot be used together")

    ranges, _ = generated_dv_ranges(params.get("range_") or [], bool(params.get("generate_dv")))
    if Video.Range.HYBRID in ranges:
        from unshackle.core.binaries import DoviTool

        if not DoviTool:
            raise PlanError("Unable to run hybrid mode: dovi_tool not found")


def api_plan(params: Mapping[str, Any], client_keys: Optional[Collection[str]] = None) -> RunPlan:
    """The checked run plan of a REST API job from its parameters, by API name.

    A value counts as typed on the command line when its API name is in ``client_keys`` (every parameter when None),
    so a service's ``dl`` config replaces only the values the client did not send.
    """
    from unshackle.commands.dl import dl

    try:
        cast = cast_dl_params(dl.cli, dict(params))
    except click.BadParameter as e:
        raise PlanError(e.message) from None
    cast.update(cdm_name=params.get("cdm"), remote=bool(params.get("remote")), server=params.get("remote"))
    check_options(cast)
    sent = params.keys() if client_keys is None else client_keys
    sources = {
        name: ParameterSource.COMMANDLINE if PLAN_API_NAMES.get(name, name) in sent else ParameterSource.DEFAULT
        for name in cast
    }
    return RunPlan(cast, sources)
