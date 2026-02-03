import re
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
        # Build choices from both enum names and values
        choices = []
        for codec in codec_enum:
            choices.append(codec.name.lower())  # e.g., "avc", "hevc"
            choices.append(codec.value)  # e.g., "H.264", "H.265"
        super().__init__(choices, case_sensitive=False)

    def convert(self, value: Any, param: Optional[click.Parameter] = None, ctx: Optional[click.Context] = None):
        if not value:
            return None

        # First try to convert using the parent class
        converted_value = super().convert(value, param, ctx)

        # Now map the converted value back to the enum
        for codec in self.codec_enum:
            if converted_value.lower() == codec.name.lower():
                return codec
            if converted_value == codec.value:
                return codec

        # This shouldn't happen if the parent conversion worked
        self.fail(f"'{value}' is not a valid video codec", param, ctx)


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
        # Build choices from enum names, values, and common aliases
        choices = []
        aliases = {}

        for codec in codec_enum:
            choices.append(codec.name.lower())  # e.g., "subrip", "webvtt"

            # Only add the value if it's different from common aliases
            value_lower = codec.value.lower()

            # Add common aliases and track them
            if codec.name == "SubRip":
                if "srt" not in choices:
                    choices.append("srt")
                aliases["srt"] = codec
            elif codec.name == "WebVTT":
                if "vtt" not in choices:
                    choices.append("vtt")
                aliases["vtt"] = codec
                # Also add the enum value if different
                if value_lower != "vtt" and value_lower not in choices:
                    choices.append(value_lower)
            elif codec.name == "SubStationAlpha":
                if "ssa" not in choices:
                    choices.append("ssa")
                aliases["ssa"] = codec
                # Also add the enum value if different
                if value_lower != "ssa" and value_lower not in choices:
                    choices.append(value_lower)
            elif codec.name == "SubStationAlphav4":
                if "ass" not in choices:
                    choices.append("ass")
                aliases["ass"] = codec
                # Also add the enum value if different
                if value_lower != "ass" and value_lower not in choices:
                    choices.append(value_lower)
            elif codec.name == "TimedTextMarkupLang":
                if "ttml" not in choices:
                    choices.append("ttml")
                aliases["ttml"] = codec
                # Also add the enum value if different
                if value_lower != "ttml" and value_lower not in choices:
                    choices.append(value_lower)
            else:
                # For other codecs, just add the enum value
                if value_lower not in choices:
                    choices.append(value_lower)

        self.aliases = aliases
        super().__init__(choices, case_sensitive=False)

    def convert(self, value: Any, param: Optional[click.Parameter] = None, ctx: Optional[click.Context] = None):
        if not value:
            return None

        # First try to convert using the parent class
        converted_value = super().convert(value, param, ctx)

        # Check aliases first
        if converted_value.lower() in self.aliases:
            return self.aliases[converted_value.lower()]

        # Now map the converted value back to the enum
        for codec in self.codec_enum:
            if converted_value.lower() == codec.name.lower():
                return codec
            if converted_value.lower() == codec.value.lower():
                return codec

        # This shouldn't happen if the parent conversion worked
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

    def parse_tokens(self, *tokens: str) -> list[str]:
        """
        Parse multiple tokens or ranged tokens as '{s}x{e}' strings.

        Supports exclusioning by putting a `-` before the token.

        Example:
            >>> sr = SeasonRange()
            >>> sr.parse_tokens("S01E01")
            ["1x1"]
            >>> sr.parse_tokens("S02E01", "S02E03-S02E05")
            ["2x1", "2x3", "2x4", "2x5"]
            >>> sr.parse_tokens("S01-S05", "-S03", "-S02E01")
            ["1x0", "1x1", ..., "2x0", (...), "2x2", (...), "4x0", ..., "5x0", ...]
        """
        if len(tokens) == 0:
            return []
        computed: list = []
        exclusions: list = []
        for token in tokens:
            exclude = token.startswith("-")
            if exclude:
                token = token[1:]
            parsed = [
                re.match(r"^S(?P<season>\d+)(E(?P<episode>\d+))?$", x, re.IGNORECASE) for x in re.split(r"[:-]", token)
            ]
            if len(parsed) > 2:
                self.fail(f"Invalid token, only a left and right range is acceptable: {token}")
            if len(parsed) == 1:
                parsed.append(parsed[0])
            if any(x is None for x in parsed):
                self.fail(f"Invalid token, syntax error occurred: {token}")
            from_season, from_episode = [
                int(v) if v is not None else self.MIN_EPISODE
                for k, v in parsed[0].groupdict().items()
                if parsed[0]  # type: ignore[union-attr]
            ]
            to_season, to_episode = [
                int(v) if v is not None else self.MAX_EPISODE
                for k, v in parsed[1].groupdict().items()
                if parsed[1]  # type: ignore[union-attr]
            ]
            if from_season > to_season:
                self.fail(f"Invalid range, left side season cannot be bigger than right side season: {token}")
            if from_season == to_season and from_episode > to_episode:
                self.fail(f"Invalid range, left side episode cannot be bigger than right side episode: {token}")
            for s in range(from_season, to_season + 1):
                for e in range(
                    from_episode if s == from_season else 0, (self.MAX_EPISODE if s < to_season else to_episode) + 1
                ):
                    (computed if not exclude else exclusions).append(f"{s}x{e}")
        for exclusion in exclusions:
            if exclusion in computed:
                computed.remove(exclusion)
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
        self, value: Union[str, list[str]], param: Optional[click.Parameter] = None, ctx: Optional[click.Context] = None
    ) -> list[int]:
        if not value:
            return []
        if not isinstance(value, list):
            value = value.split(",")
        resolutions = []
        for resolution in value:
            try:
                resolutions.append(int(resolution.lower().rstrip("p")))
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


SEASON_RANGE = SeasonRange()
LANGUAGE_RANGE = LanguageRange()
QUALITY_LIST = QualityList()
AUDIO_CODEC_LIST = AudioCodecList(Audio.Codec)

# VIDEO_CODEC_CHOICE will be created dynamically when imported
