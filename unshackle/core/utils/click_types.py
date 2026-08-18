import re
from datetime import date, timedelta
from typing import Any, Optional, Union

import click
from click.shell_completion import CompletionItem
from pywidevine.cdm import Cdm as WidevineCdm

from unshackle.core.tracks.audio import Audio


class VideoCodecChoice(click.Choice):
    """
    A custom Choice type for video codecs that accepts both enum names and values.

    Accepts both:
    - Enum names: avc, hevc, vc1, vp8, vp9, av1
    - Enum values: H.264, H.265, VC-1, VP8, VP9, AV1
    """

    def __init__(self, codec_enum):
        self.codec_enum = codec_enum
        choices = []
        for codec in codec_enum:
            choices.append(codec.name.lower())  # e.g., "avc", "hevc"
            choices.append(codec.value)  # e.g., "H.264", "H.265"
        super().__init__(choices, case_sensitive=False)

    def convert(self, value: Any, param: Optional[click.Parameter] = None, ctx: Optional[click.Context] = None):
        if not value:
            return None

        converted_value = super().convert(value, param, ctx)

        for codec in self.codec_enum:
            if converted_value.lower() == codec.name.lower():
                return codec
            if converted_value == codec.value:
                return codec

        self.fail(f"'{value}' is not a valid video codec", param, ctx)


class MultipleVideoCodecChoice(VideoCodecChoice):
    """
    A multiple-value variant of VideoCodecChoice that accepts comma-separated codecs.

    Accepts both enum names and values, e.g.: ``-v hevc,avc`` or ``-v H.264,H.265``
    """

    name = "multiple_video_codec_choice"

    def convert(
        self, value: Any, param: Optional[click.Parameter] = None, ctx: Optional[click.Context] = None
    ) -> list[Any]:
        if not value:
            return []
        if isinstance(value, list):
            values = value
        elif isinstance(value, str):
            values = value.split(",")
        else:
            self.fail(f"{value!r} is not a supported value.", param, ctx)

        chosen_values: list[Any] = []
        for v in values:
            chosen_values.append(super().convert(v.strip(), param, ctx))
        return chosen_values


class SubtitleCodecChoice(click.Choice):
    """
    A custom Choice type for subtitle codecs that accepts both enum names, values, and common aliases.

    Accepts:
    - Enum names: subrip, substationalpha, substationalphav4, timedtextmarkuplang, webvtt, ftml, fvtt
    - Enum values: SRT, SSA, ASS, TTML, VTT, STPP, WVTT
    - Common aliases: srt (for SubRip)
    """

    def __init__(self, codec_enum):
        self.codec_enum = codec_enum
        choices = []
        aliases = {}

        for codec in codec_enum:
            choices.append(codec.name.lower())  # e.g., "subrip", "webvtt"

            value_lower = codec.value.lower()

            if codec.name == "SubRip":
                if "srt" not in choices:
                    choices.append("srt")
                aliases["srt"] = codec
            elif codec.name == "WebVTT":
                if "vtt" not in choices:
                    choices.append("vtt")
                aliases["vtt"] = codec
                if value_lower != "vtt" and value_lower not in choices:
                    choices.append(value_lower)
            elif codec.name == "SubStationAlpha":
                if "ssa" not in choices:
                    choices.append("ssa")
                aliases["ssa"] = codec
                if value_lower != "ssa" and value_lower not in choices:
                    choices.append(value_lower)
            elif codec.name == "SubStationAlphav4":
                if "ass" not in choices:
                    choices.append("ass")
                aliases["ass"] = codec
                if value_lower != "ass" and value_lower not in choices:
                    choices.append(value_lower)
            elif codec.name == "TimedTextMarkupLang":
                if "ttml" not in choices:
                    choices.append("ttml")
                aliases["ttml"] = codec
                if value_lower != "ttml" and value_lower not in choices:
                    choices.append(value_lower)
            else:
                if value_lower not in choices:
                    choices.append(value_lower)

        choices.append("original")

        self.aliases = aliases
        super().__init__(choices, case_sensitive=False)

    def convert(self, value: Any, param: Optional[click.Parameter] = None, ctx: Optional[click.Context] = None):
        if not value:
            return None

        if str(value).lower() == "original":
            return "original"

        converted_value = super().convert(value, param, ctx)

        if converted_value.lower() in self.aliases:
            return self.aliases[converted_value.lower()]

        for codec in self.codec_enum:
            if converted_value.lower() == codec.name.lower():
                return codec
            if converted_value.lower() == codec.value.lower():
                return codec

        self.fail(f"'{value}' is not a valid subtitle codec", param, ctx)


class ContextData:
    def __init__(self, config: dict, cdm: WidevineCdm, proxy_providers: list, profile: Optional[str] = None):
        self.config = config
        self.cdm = cdm
        self.proxy_providers = proxy_providers
        self.profile = profile


class SeasonRange(click.ParamType):
    name = "ep_range"

    MIN_EPISODE = 0
    MAX_EPISODE = 999
    MIN_PART = 1
    MAX_PART = 99
    MAX_DATE_SPAN = 1000

    DATE_TOKEN = re.compile(r"^(?P<left>\d{4}-\d{2}-\d{2})(:(?P<right>\d{4}-\d{2}-\d{2}))?$")
    TOKEN = re.compile(
        r"^(?:S(?P<season>\d+)(E(?P<episode>\d+)(\.(?P<part>\d+))?)?"
        r"|((?P<disc>\d+)x)?(?P<track>\d+))$",
        re.IGNORECASE,
    )

    def parse_date_token(self, token: str, match: re.Match) -> list[str]:
        """Expand an ISO date token or ':'-separated date range into ISO day keys."""
        try:
            left = date.fromisoformat(match.group("left"))
            right = date.fromisoformat(match.group("right")) if match.group("right") else left
        except ValueError:
            self.fail(f"Invalid date, must be a real YYYY-MM-DD date: {token}")
        if left > right:
            self.fail(f"Invalid range, left side date cannot be later than right side date: {token}")
        span = (right - left).days + 1
        if span > self.MAX_DATE_SPAN:
            self.fail(f"Invalid range, a date range cannot span more than {self.MAX_DATE_SPAN} days: {token}")
        return [(left + timedelta(days=i)).isoformat() for i in range(span)]

    def token_sides(self, match: re.Match) -> tuple[int, Optional[int], Optional[int]]:
        """Read one side of a token as (season, episode, part), episode/part None when absent.

        A music disc x track token shares this key space, disc reading as the season and
        track as the episode. An omitted disc is disc 1, not every disc: a bare number is
        the number the tracklist shows for a single-disc release, and widening it would
        silently add tracks on a multi-disc release.
        """
        if match.group("season") is not None:
            episode = match.group("episode")
            part = match.group("part")
            return (
                int(match.group("season")),
                int(episode) if episode is not None else None,
                int(part) if part is not None else None,
            )
        disc = match.group("disc")
        return int(disc) if disc is not None else 1, int(match.group("track")), None

    def parse_tokens(self, *tokens: str) -> list[str]:
        """
        Parse multiple tokens or ranged tokens as '{s}x{e}' strings.

        An episode split into separately playable parts is addressed as '{s}x{e}.{p}'.
        A part range must stay inside one episode, since how many parts an episode has
        is not knowable here. A part-qualified exclusion cannot be removed from the
        computed keys (they are base keys), so it becomes a '!' key resolved at match time.

        Dated content is addressed by ISO air date. A date range uses ':' only, because
        a date's own '-' separators are not a range separator.

        A music release uses the same keys, its disc reading as the season and its track
        as the episode. A track is addressed as '{d}x{t}', or by its number alone, which
        is a track on disc 1.

        Supports exclusioning by putting a `-` before the token.

        Example:
            >>> sr = SeasonRange()
            >>> sr.parse_tokens("S01E01")
            ["1x1"]
            >>> sr.parse_tokens("S02E01", "S02E03-S02E05")
            ["2x1", "2x3", "2x4", "2x5"]
            >>> sr.parse_tokens("S01-S05", "-S03", "-S02E01")
            ["1x0", "1x1", ..., "2x0", (...), "2x2", (...), "4x0", ..., "5x0", ...]
            >>> sr.parse_tokens("S01E01.1-S01E01.3")
            ["1x1.1", "1x1.2", "1x1.3"]
            >>> sr.parse_tokens("S01E01", "-S01E01.2")
            ["1x1", "!1x1.2"]
            >>> sr.parse_tokens("2026-08-11")
            ["2026-08-11"]
            >>> sr.parse_tokens("2026-08-01:2026-08-03", "-2026-08-02")
            ["2026-08-01", "2026-08-03"]
            >>> sr.parse_tokens("3")
            ["1x3"]
            >>> sr.parse_tokens("1-3", "2x1")
            ["1x1", "1x2", "1x3", "2x1"]
        """
        if len(tokens) == 0:
            return []
        computed: list = []
        exclusions: list = []
        for token in tokens:
            exclude = token.startswith("-")
            if exclude:
                token = token[1:]
            # dates carry their own '-' separators, so they must be read before the range split
            date_match = self.DATE_TOKEN.match(token)
            if date_match:
                (computed if not exclude else exclusions).extend(self.parse_date_token(token, date_match))
                continue
            parsed = [self.TOKEN.match(x) for x in re.split(r"[:-]", token)]
            if len(parsed) > 2:
                self.fail(f"Invalid token, only a left and right range is acceptable: {token}")
            if len(parsed) == 1:
                # the same match object is read with different per-side defaults, so a bare
                # S01 spans from MIN_EPISODE to MAX_EPISODE
                parsed.append(parsed[0])
            if any(x is None for x in parsed):
                self.fail(f"Invalid token, syntax error occurred: {token}")
            from_season, from_episode_raw, from_part = self.token_sides(parsed[0])  # type: ignore[arg-type]
            from_episode = from_episode_raw if from_episode_raw is not None else self.MIN_EPISODE
            to_season, to_episode_raw, to_part = self.token_sides(parsed[1])  # type: ignore[arg-type]
            to_episode = to_episode_raw if to_episode_raw is not None else self.MAX_EPISODE
            if from_season > to_season:
                self.fail(f"Invalid range, left side season cannot be bigger than right side season: {token}")
            if from_season == to_season and from_episode > to_episode:
                self.fail(f"Invalid range, left side episode cannot be bigger than right side episode: {token}")
            if (from_part is None) != (to_part is None):
                self.fail(f"Invalid range, a part must be given on both sides or on neither: {token}")
            if from_part is not None and to_part is not None:
                if (from_season, from_episode) != (to_season, to_episode):
                    self.fail(f"Invalid range, a part range must stay within one episode: {token}")
                if not all(self.MIN_PART <= p <= self.MAX_PART for p in (from_part, to_part)):
                    self.fail(f"Invalid part, must be between {self.MIN_PART} and {self.MAX_PART}: {token}")
                if from_part > to_part:
                    self.fail(f"Invalid range, left side part cannot be bigger than right side part: {token}")
                for p in range(from_part, to_part + 1):
                    (computed if not exclude else exclusions).append(f"{from_season}x{from_episode}.{p}")
                continue
            for s in range(from_season, to_season + 1):
                for e in range(
                    from_episode if s == from_season else 0, (self.MAX_EPISODE if s < to_season else to_episode) + 1
                ):
                    (computed if not exclude else exclusions).append(f"{s}x{e}")
        for exclusion in exclusions:
            if "." in exclusion:
                computed.append(f"!{exclusion}")  # base key stays; resolved at match time
            else:
                # a base exclusion must drop that episode's part keys too, or they still match
                prefix = f"{exclusion}."
                computed = [k for k in computed if k != exclusion and not k.startswith(prefix)]
        return list(set(computed))

    def convert(
        self, value: str, param: Optional[click.Parameter] = None, ctx: Optional[click.Context] = None
    ) -> list[str]:
        return self.parse_tokens(*re.split(r"\s*[,;]\s*", value))


class LanguageRange(click.ParamType):
    name = "lang_range"

    def convert(
        self, value: Union[str, list], param: Optional[click.Parameter] = None, ctx: Optional[click.Context] = None
    ) -> list[str]:
        if isinstance(value, list):
            return value
        if not value:
            return []
        return re.split(r"\s*[,;]\s*", value)


class QualityList(click.ParamType):
    name = "quality_list"

    def convert(
        self,
        value: Union[str, int, list[Union[str, int]]],
        param: Optional[click.Parameter] = None,
        ctx: Optional[click.Context] = None,
    ) -> list[int]:
        if not value:
            return []
        if not isinstance(value, list):
            value = str(value).split(",")
        resolutions = []
        for resolution in value:
            try:
                resolutions.append(int(str(resolution).lower().rstrip("p")))
            except TypeError:
                self.fail(
                    f"Expected string for int() conversion, got {resolution!r} of type {type(resolution).__name__}",
                    param,
                    ctx,
                )
            except ValueError:
                self.fail(f"{resolution!r} is not a valid integer", param, ctx)
        return sorted(resolutions, reverse=True)


class AudioCodecList(click.ParamType):
    """Parses comma-separated audio codecs like 'AAC,EC3'."""

    name = "audio_codec_list"

    def __init__(self, codec_enum):
        self.codec_enum = codec_enum
        self._name_to_codec: dict[str, Audio.Codec] = {}
        for codec in codec_enum:
            self._name_to_codec[codec.name.lower()] = codec
            self._name_to_codec[codec.value.lower()] = codec

        aliases = {
            "eac3": "EC3",
            "ddp": "EC3",
            "vorbis": "OGG",
        }
        for alias, target in aliases.items():
            if target in codec_enum.__members__:
                self._name_to_codec[alias] = codec_enum[target]

    def convert(self, value: Any, param: Optional[click.Parameter] = None, ctx: Optional[click.Context] = None) -> list:
        if not value:
            return []
        if isinstance(value, self.codec_enum):
            return [value]
        if isinstance(value, list):
            if all(isinstance(v, self.codec_enum) for v in value):
                return value
            values = [str(v).strip() for v in value]
        else:
            values = [v.strip() for v in str(value).split(",")]

        codecs = []
        for val in values:
            if not val:
                continue
            key = val.lower()
            if key in self._name_to_codec:
                codecs.append(self._name_to_codec[key])
            else:
                valid = sorted(set(self._name_to_codec.keys()))
                self.fail(f"'{val}' is not valid. Choices: {', '.join(valid)}", param, ctx)
        return list(dict.fromkeys(codecs))  # Remove duplicates, preserve order


class MultipleChoice(click.Choice):
    """
    The multiple choice type allows multiple values to be checked against
    a fixed set of supported values.

    It internally uses and is based off of click.Choice.
    """

    name = "multiple_choice"

    def __repr__(self) -> str:
        return f"MultipleChoice({list(self.choices)})"

    def convert(
        self, value: Any, param: Optional[click.Parameter] = None, ctx: Optional[click.Context] = None
    ) -> list[Any]:
        if not value:
            return []
        if isinstance(value, str):
            values = value.split(",")
        elif isinstance(value, list):
            values = value
        else:
            self.fail(f"{value!r} is not a supported value.", param, ctx)

        chosen_values: list[Any] = []
        for value in values:
            chosen_values.append(super().convert(value, param, ctx))

        return chosen_values

    def shell_complete(self, ctx: click.Context, param: click.Parameter, incomplete: str) -> list[CompletionItem]:
        """
        Complete choices that start with the incomplete value.

        Parameters:
            ctx: Invocation context for this command.
            param: The parameter that is requesting completion.
            incomplete: Value being completed. May be empty.
        """
        incomplete = incomplete.rsplit(",")[-1]
        return super(self).shell_complete(ctx, param, incomplete)


class SlowDelayRange(click.ParamType):
    """Parses a delay range string like '20-40' into a tuple of (min, max) seconds."""

    name = "delay_range"

    def convert(
        self, value: Any, param: Optional[click.Parameter], ctx: Optional[click.Context]
    ) -> Optional[tuple[int, int]]:
        if isinstance(value, tuple):
            return value
        if isinstance(value, bool):
            return (60, 120) if value else None

        match = re.match(r"^(\d+)-(\d+)$", str(value))
        if not match:
            self.fail(f"'{value}' is not a valid range. Use format: MIN-MAX (e.g., 20-40)", param, ctx)

        low, high = int(match.group(1)), int(match.group(2))
        if low < 20:
            self.fail(f"Minimum delay must be at least 20 seconds, got {low}", param, ctx)
        if low > high:
            self.fail(f"Min ({low}) cannot be greater than max ({high})", param, ctx)

        return (low, high)


SEASON_RANGE = SeasonRange()
LANGUAGE_RANGE = LanguageRange()
QUALITY_LIST = QualityList()
AUDIO_CODEC_LIST = AudioCodecList(Audio.Codec)
SLOW_DELAY_RANGE = SlowDelayRange()
