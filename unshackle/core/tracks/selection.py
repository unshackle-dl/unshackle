"""Track selection: which tracks of a title a dl run keeps, or why it skips the title."""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from unshackle.core.console import console
from unshackle.core.constants import AnyTrack
from unshackle.core.titles import Episode, Movie, Title_T
from unshackle.core.tracks.attachment import Attachment
from unshackle.core.tracks.audio import Audio
from unshackle.core.tracks.chapter import Chapter
from unshackle.core.tracks.chapters import Chapters
from unshackle.core.tracks.tracks import Tracks, at_resolution, select_videos
from unshackle.core.tracks.video import Video
from unshackle.core.utilities import (
    as_requested,
    embedded_audio_langs,
    find_missing_langs,
    matched_language_tags,
    matching_languages,
    missing_required_langs,
    orig_token,
    plain_subtitle_tags,
    resolve_sort_langs,
)

log = logging.getLogger("download")

HYBRID_BASE_RANGES = (Video.Range.HDR10P, Video.Range.HDR10)


def generated_dv_ranges(
    range_: Sequence[Video.Range], generate_dv: bool
) -> tuple[list[Video.Range], list[Video.Range]]:
    """Turn ``-r HDR10 --generate-dv`` (or HDR10P) into a HYBRID run on that base alone.

    Returns the ranges to run with, and the base ranges the user named. The second list is empty when
    the flag is off, HYBRID or DV is in the ranges, or no base range is. The service still gets the
    ranges the user gave, so it fetches no DV track.
    """
    bases = [r for r in range_ if r in HYBRID_BASE_RANGES]
    if not generate_dv or not bases or Video.Range.HYBRID in range_ or Video.Range.DV in range_:
        return list(range_), []
    return [Video.Range.HYBRID, *(r for r in range_ if r not in bases)], bases


@dataclass(frozen=True)
class Skip:
    """The title has no track the selection asks for.

    A kind-only miss (-ado, -fso, no track left) skips without --skip-missing; the caller decides the rest.
    """

    reason: str
    kind_only: bool = False


@dataclass(frozen=True)
class Kept:
    """The tracks the title keeps, and the ranges it downloads: a failed HYBRID falls back to the other ranges."""

    tracks: Tracks
    ranges: list[Video.Range]


@dataclass(frozen=True, kw_only=True)
class TrackSelection:
    """The selection flags of one dl run, as result() normalizes them, applied to each title in a fixed order."""

    quality: list[int]
    vcodec: list[Video.Codec]
    acodec: list[Audio.Codec]
    vbitrate: int | None
    abitrate: int | None
    vbitrate_min: int | None
    vbitrate_max: int | None
    abitrate_min: int | None
    abitrate_max: int | None
    range_: list[Video.Range]
    generated_dv_bases: list[Video.Range]
    channels: float | None
    no_atmos: bool
    lang: list[str]
    v_lang: list[str] | None
    a_lang: list[str] | None
    s_lang: list[str] | None
    video_excl: list[str]
    audio_excl: list[str]
    s_lang_excl: list[str]
    fsl_excl: list[str]
    forced_s_lang: list[str]
    require_audio: list[str]
    require_video: list[str]
    require_subs: list[str]
    forced_subs: bool
    forced_subs_only: bool
    exact_lang: bool
    video_only: bool
    audio_only: bool
    subs_only: bool
    chapters_only: bool
    no_subs: bool
    no_audio: bool
    no_chapters: bool
    no_video: bool
    no_attachments: bool
    audio_description: bool
    audio_description_only: bool
    worst: bool
    best_available: bool
    generate_dv: bool

    def select(self, title: Title_T, subtitles_unsupported: bool) -> Kept | Skip:
        """Narrow title.tracks to the tracks the flags ask for, or return the reason to skip the title.

        subtitles_unsupported is the service's NO_SUBTITLES read after get_tracks, which can set it. The
        required-language gate runs first, on the tracks before any filter, so a miss there is never kind-only.
        """
        v_lang, a_lang, s_lang, range_ = self.v_lang, self.a_lang, self.s_lang, self.range_
        vbitrate_min, vbitrate_max, abitrate_min, abitrate_max = (
            self.vbitrate_min,
            self.vbitrate_max,
            self.abitrate_min,
            self.abitrate_max,
        )
        channels = self.channels
        keep_videos = True
        keep_audio = True
        keep_subtitles = True
        keep_chapters = True

        if self.video_only or self.audio_only or self.subs_only or self.chapters_only:
            keep_videos = self.video_only
            keep_audio = self.audio_only
            keep_subtitles = self.subs_only
            keep_chapters = self.chapters_only

        if subtitles_unsupported:
            console.log("Skipping subtitles - service does not support subtitle downloads")
            keep_subtitles = False
            s_lang = None
        elif self.no_subs:
            console.log("Skipped subtitles as --no-subs was used...")
            keep_subtitles = False
            s_lang = None

        if self.no_video:
            console.log("Skipped video as --no-video was used...")
            keep_videos = False
            v_lang = None

        if self.no_audio:
            console.log("Skipped audio as --no-audio was used...")
            keep_audio = False
            a_lang = None

        if self.no_chapters:
            console.log("Skipped chapters as --no-chapters was used...")
            keep_chapters = False

        if self.no_attachments:
            console.log("Skipped attachments as --no-attachments was used...")

        missing_required: list[str] = []
        for kind, required, available in (
            (
                "audio",
                self.require_audio if keep_audio else [],
                [a.language for a in title.tracks.audio if a.descriptive or not self.audio_description_only]
                + embedded_audio_langs(title.tracks.videos, keep_videos and not self.audio_description_only),
            ),
            ("video", self.require_video if keep_videos else [], [v.language for v in title.tracks.videos]),
            (
                "subtitle",
                self.require_subs if keep_subtitles else [],
                [
                    t.language
                    for t in title.tracks.subtitles
                    if (t.forced if self.forced_subs_only else self.forced_subs or not t.forced)
                ],
            ),
        ):
            if missing := missing_required_langs(required, available, title.language, exact=self.exact_lang):
                missing_required.append(f"Required {kind} language(s) not found: {', '.join(missing)}")
        if missing_required:
            return Skip("; ".join(missing_required))

        if not keep_videos:
            title.tracks.videos = []
        if not keep_audio:
            title.tracks.audio = []
        if not keep_subtitles:
            title.tracks.subtitles = []
        if not keep_chapters:
            title.tracks.chapters = Chapters()

        with console.status("Selecting tracks...", spinner="dots"):
            video_excl_r = resolve_sort_langs(self.video_excl, title.language, exact=self.exact_lang)
            audio_excl_r = resolve_sort_langs(self.audio_excl, title.language, exact=self.exact_lang)
            s_excl_r = resolve_sort_langs(self.s_lang_excl, title.language, exact=self.exact_lang)
            fsl_excl_r = resolve_sort_langs(self.fsl_excl, title.language, exact=self.exact_lang)

            if isinstance(title, (Movie, Episode)):
                if keep_videos and self.vcodec:
                    title.tracks.select_video(lambda x: x.codec in self.vcodec)
                    missing_codecs = [c for c in self.vcodec if not any(x.codec == c for x in title.tracks.videos)]
                    for codec in missing_codecs:
                        log.warning(f"Skipping {codec.name} video tracks as none are available.")
                    if not title.tracks.videos:
                        return Skip(f"There's no {', '.join(c.name for c in self.vcodec)} Video Track...")
                elif keep_videos:
                    title.tracks.select_video(lambda x: x.codec != Video.Codec.LCEVC)

                if keep_videos and self.generated_dv_bases:
                    title.tracks.videos = keep_generated_dv_bases(title.tracks.videos, self.generated_dv_bases)
                    if not any(v.range in self.generated_dv_bases for v in title.tracks.videos):
                        return Skip(f"There's no {', '.join(r.name for r in self.generated_dv_bases)} Video Track...")

                if keep_videos and range_:
                    # Special handling for HYBRID - don't filter, keep all HDR10 and DV tracks
                    if Video.Range.HYBRID not in range_:
                        dv_has_hdr10p = any(x.hdr10plus for x in title.tracks.videos)
                        title.tracks.select_video(lambda x: x.range in range_)
                        missing_ranges = [r for r in range_ if not any(x.range == r for x in title.tracks.videos)]
                        for color_range in missing_ranges:
                            log.warning(f"Skipping {color_range.name} video tracks as none are available.")
                        if Video.Range.HDR10P in missing_ranges and dv_has_hdr10p:
                            log.warning("A DV track carries HDR10+: use -r DV or -r HYBRID.")
                        if not title.tracks.videos:
                            return Skip(f"There's no {', '.join(r.name for r in range_)} Video Track...")

                if keep_videos and self.vbitrate:
                    if any(r == Video.Range.HYBRID for r in range_):
                        # In HYBRID mode, only apply bitrate filter to non-DV tracks
                        # DV tracks are kept regardless since they're only used for RPU metadata
                        title.tracks.select_video(
                            lambda x: (
                                x.range == Video.Range.DV or bool(x.bitrate and x.bitrate // 1000 == self.vbitrate)
                            )
                        )
                        if not any(x.range != Video.Range.DV for x in title.tracks.videos):
                            return Skip(f"There's no {self.vbitrate}kbps Video Track...")
                    else:
                        title.tracks.select_video(lambda x: bool(x.bitrate and x.bitrate // 1000 == self.vbitrate))
                        if not title.tracks.videos:
                            return Skip(f"There's no {self.vbitrate}kbps Video Track...")

                if keep_videos and vbitrate_min is not None and vbitrate_max is not None:
                    title.tracks.select_video(
                        lambda x: bool(x.bitrate and vbitrate_min <= x.bitrate // 1000 <= vbitrate_max)
                    )
                    if not title.tracks.videos:
                        return Skip(f"No Video Track in {vbitrate_min}-{vbitrate_max}kbps range...")

                if keep_videos and video_excl_r:
                    drop = matched_language_tags(
                        video_excl_r, [t.language for t in title.tracks.videos], self.exact_lang, every_variant=True
                    )
                    title.tracks.select_video(lambda x: str(x.language) not in drop)
                    if not title.tracks.videos:
                        return Skip(f"Every Video Track was excluded by -{', -'.join(video_excl_r)}...")

                effective_video_lang = v_lang or self.lang
                video_languages = [tok for tok in effective_video_lang if tok != "best"]
                processed_video_lang = video_languages
                if keep_videos and video_languages and "all" not in video_languages:
                    processed_video_lang = resolve_sort_langs(video_languages, title.language, exact=self.exact_lang)
                    v_orig_token = orig_token(video_languages, title.language, self.exact_lang)
                    if "orig" in video_languages and not title.language:
                        log.warning("Original language not available for title, skipping 'orig' selection for video")
                    missing_v_langs = find_missing_langs(
                        processed_video_lang,
                        [v.language for v in title.tracks.videos],
                        exact=self.exact_lang,
                    )
                    if missing_v_langs:
                        missing_str = as_requested(missing_v_langs, v_orig_token)
                        if self.best_available:
                            remaining_v_langs = [tok for tok in processed_video_lang if tok not in missing_v_langs]
                            if remaining_v_langs:
                                log.warning(
                                    f"{missing_str} not found in video tracks, "
                                    f"continuing with: {as_requested(remaining_v_langs, v_orig_token)}"
                                )
                                processed_video_lang = remaining_v_langs
                            else:
                                return Skip(f"{missing_str} not found in video tracks and no fallback available")
                        else:
                            return Skip(missing_str + " not found in video tracks")
                    title.tracks.videos = title.tracks.by_language(
                        title.tracks.videos, processed_video_lang, exact_match=self.exact_lang
                    )
                    if not title.tracks.videos:
                        # empty only when 'orig' was the sole request and did not resolve
                        return Skip(
                            f"There's no {as_requested(processed_video_lang, v_orig_token) or 'orig'} Video Track..."
                        )
                video_requests = None if {"best", "all"} & set(effective_video_lang) else processed_video_lang

                has_hybrid = any(r == Video.Range.HYBRID for r in range_)
                non_hybrid_ranges: list[Video.Range] = [r for r in range_ if r != Video.Range.HYBRID]
                effective_quality = self.quality
                if keep_videos and self.quality:
                    missing_resolutions = []
                    if has_hybrid:
                        hybrid_candidate_tracks, non_hybrid_tracks = Tracks.partition_hybrid_videos(
                            title.tracks.videos, non_hybrid_ranges
                        )
                        # only ranges that can actually be delivered count towards "present"
                        deliverable_pool = hybrid_candidate_tracks + [
                            v for v in non_hybrid_tracks if v.range in non_hybrid_ranges
                        ]
                        for resolution in self.quality:
                            if any(at_resolution(v, resolution) for v in deliverable_pool):
                                continue
                            missing_resolutions.append(resolution)

                        if len(missing_resolutions) == len(self.quality) and self.best_available:
                            # every requested resolution missing: leave the pool untouched so the
                            # best-available hybrid pass below selects the top resolution instead
                            effective_quality = []
                        else:
                            hybrid_filter = title.tracks.select_hybrid(
                                hybrid_candidate_tracks, self.quality, worst=self.worst, generate_dv=self.generate_dv
                            )
                            hybrid_selected = list(filter(hybrid_filter, hybrid_candidate_tracks))

                            if non_hybrid_ranges and non_hybrid_tracks:
                                non_hybrid_selected = [
                                    v for v in non_hybrid_tracks if any(at_resolution(v, res) for res in self.quality)
                                ]
                                title.tracks.videos = Tracks.merge_video_selections(
                                    hybrid_selected, non_hybrid_selected
                                )
                            else:
                                title.tracks.videos = hybrid_selected
                    else:
                        pre_quality_videos = list(title.tracks.videos)
                        title.tracks.by_resolutions(self.quality)

                        for resolution in self.quality:
                            if any(at_resolution(v, resolution) for v in title.tracks.videos):
                                continue
                            missing_resolutions.append(resolution)

                        if len(missing_resolutions) == len(self.quality) and self.best_available:
                            # every requested resolution missing: fall back to the best available
                            title.tracks.videos = pre_quality_videos
                            effective_quality = []

                    if missing_resolutions:
                        res_list = ""
                        if len(missing_resolutions) > 1:
                            res_list = ", ".join([f"{x}p" for x in missing_resolutions[:-1]]) + " or "
                        res_list = f"{res_list}{missing_resolutions[-1]}p"
                        plural = "s" if len(missing_resolutions) > 1 else ""

                        if self.best_available:
                            log.warning(
                                f"There's no {res_list} Video Track{plural}, continuing with available qualities..."
                            )
                        else:
                            return Skip(f"There's no {res_list} Video Track{plural}...")

                pre_hybrid_videos: list[Video] = list(title.tracks.videos) if has_hybrid else []
                if keep_videos and has_hybrid:
                    hybrid_candidate_tracks, non_hybrid_tracks = Tracks.partition_hybrid_videos(
                        title.tracks.videos, non_hybrid_ranges
                    )

                    hybrid_quality = effective_quality
                    if not hybrid_quality:
                        height_pool = (
                            [
                                *select_hybrid_bases(hybrid_candidate_tracks),
                                *(v for v in hybrid_candidate_tracks if v.finished_hybrid),
                            ]
                            if self.worst
                            else hybrid_candidate_tracks
                        )
                        base_heights = [v.height for v in height_pool if v.height]
                        hybrid_quality = [(min if self.worst else max)(base_heights)] if base_heights else []
                    hybrid_selected = []
                    if hybrid_quality:
                        hybrid_filter = title.tracks.select_hybrid(
                            hybrid_candidate_tracks, hybrid_quality, worst=self.worst, generate_dv=self.generate_dv
                        )
                        hybrid_selected = list(filter(hybrid_filter, hybrid_candidate_tracks))
                        for res in hybrid_quality:
                            if not any(at_resolution(v, res) for v in hybrid_selected):
                                log.warning(f"Skipping {res}p: nothing at that resolution can make a hybrid")

                    non_hybrid_selected = []
                    if non_hybrid_ranges and non_hybrid_tracks:
                        non_hybrid_selected = select_videos(
                            non_hybrid_tracks,
                            effective_quality,
                            non_hybrid_ranges,
                            self.vcodec,
                            video_requests,
                            self.exact_lang,
                            self.worst,
                        )

                    title.tracks.videos = Tracks.merge_video_selections(hybrid_selected, non_hybrid_selected)
                    Tracks.flag_hybrid_ingredients(hybrid_selected, non_hybrid_selected)
                elif keep_videos:
                    title.tracks.videos = select_videos(
                        title.tracks.videos,
                        effective_quality,
                        range_,
                        self.vcodec,
                        video_requests,
                        self.exact_lang,
                        self.worst,
                    )

                if keep_videos and any(r == Video.Range.HYBRID for r in range_):
                    gate_failure = hybrid_gate_failure(title.tracks.videos, self.generate_dv)
                    if not gate_failure and not any(
                        v.range == Video.Range.DV or v.native_hybrid for v in title.tracks.videos
                    ):
                        log.info("No Dolby Vision track: Dolby Vision will be generated")

                    if gate_failure:
                        other_ranges: list[Video.Range] = [r for r in range_ if r != Video.Range.HYBRID]
                        if self.best_available and other_ranges:
                            log.warning(f"{gate_failure}; continuing with {', '.join(r.name for r in other_ranges)}")
                            range_ = other_ranges
                            title.tracks.videos = select_videos(
                                pre_hybrid_videos,
                                effective_quality,
                                other_ranges,
                                self.vcodec,
                                video_requests,
                                self.exact_lang,
                                self.worst,
                            )
                        else:
                            return Skip(gate_failure)

                fsl = resolve_sort_langs(self.forced_s_lang, title.language, exact=self.exact_lang)
                if keep_subtitles and s_excl_r:
                    # applies to forced subs too: an excluded language is unwanted in any form
                    drop = matched_language_tags(
                        s_excl_r, [t.language for t in title.tracks.subtitles], self.exact_lang, every_variant=True
                    )
                    title.tracks.select_subtitles(lambda x: str(x.language) not in drop)
                if keep_subtitles and s_lang and "all" not in s_lang:
                    s_orig_token = orig_token(s_lang, title.language, self.exact_lang)
                    s_lang = resolve_sort_langs(s_lang, title.language, exact=self.exact_lang)
                    # the pool selection draws from: forced tracks count only when -fs keeps them
                    missing_langs = find_missing_langs(
                        s_lang,
                        [sub.language for sub in title.tracks.subtitles if self.forced_subs or not sub.forced],
                        exact=self.exact_lang,
                    )
                    if missing_langs:
                        remaining = [tok for tok in s_lang if tok not in missing_langs]
                        tail = (
                            f"continuing with: {as_requested(remaining, s_orig_token)}"
                            if remaining
                            else "continuing without subtitles"
                        )
                        log.warning(f"{as_requested(missing_langs, s_orig_token)} not found in subtitle tracks, {tail}")

                    if s_lang and title.tracks.subtitles:
                        plain_tags = plain_subtitle_tags(s_lang, title.tracks.subtitles, self.exact_lang)
                        forced_tags = matched_language_tags(
                            [*s_lang, *fsl], [x.language for x in title.tracks.subtitles if x.forced], self.exact_lang
                        )
                        title.tracks.select_subtitles(
                            lambda x: str(x.language) in (forced_tags if x.forced else plain_tags)
                        )

                if keep_subtitles:
                    # fsl, not self.forced_s_lang: unresolvable orig must keep all forced, not drop them
                    if fsl:
                        fsl_tags = matched_language_tags(
                            fsl, [x.language for x in title.tracks.subtitles if x.forced], self.exact_lang
                        )
                        title.tracks.select_subtitles(lambda x: not x.forced or str(x.language) in fsl_tags)
                        if not any(x.forced for x in title.tracks.subtitles):
                            log.warning(
                                f"No forced subtitle tracks matched --forced-s-lang ({', '.join(self.forced_s_lang)})"
                            )
                    elif not self.forced_subs:
                        title.tracks.select_subtitles(lambda x: not x.forced)
                    if fsl_excl_r:
                        drop = matched_language_tags(
                            fsl_excl_r,
                            [t.language for t in title.tracks.subtitles if t.forced],
                            self.exact_lang,
                            every_variant=True,
                        )
                        title.tracks.select_subtitles(lambda x: not (x.forced and str(x.language) in drop))
                    if self.forced_subs_only:
                        title.tracks.select_subtitles(lambda x: x.forced)
                        if not title.tracks.subtitles:
                            return Skip("There's no forced subtitle track for the selected languages.", kind_only=True)

            if keep_audio and self.audio_description_only:
                title.tracks.select_audio(lambda x: x.descriptive)
                if not title.tracks.audio:
                    return Skip("There's no audio description track.", kind_only=True)
            # might have no audio tracks if part of the video, e.g. transport stream hls
            if keep_audio and len(title.tracks.audio) > 0:
                if not self.audio_description:
                    title.tracks.select_audio(lambda x: not x.descriptive)
                    if not title.tracks.audio:
                        return Skip("Audio description only, use --audio-description...")
                if self.acodec:
                    title.tracks.select_audio(lambda x: x.codec in self.acodec)
                    if not title.tracks.audio:
                        codec_names = ", ".join(c.name for c in self.acodec)
                        return Skip(f"No audio tracks matching codecs: {codec_names}")
                if channels:
                    title.tracks.select_audio(
                        lambda x: bool(x.channels and math.ceil(Audio.channel_total(x.channels)) == math.ceil(channels))
                    )
                    if not title.tracks.audio:
                        return Skip(f"There's no {channels} Audio Track...")
                if self.no_atmos:
                    title.tracks.audio = [x for x in title.tracks.audio if not x.atmos]
                    if not title.tracks.audio:
                        return Skip("No non-Atmos audio tracks available...")
                if self.abitrate:
                    title.tracks.select_audio(lambda x: bool(x.bitrate and x.bitrate // 1000 == self.abitrate))
                    if not title.tracks.audio:
                        return Skip(f"There's no {self.abitrate}kbps Audio Track...")
                if abitrate_min is not None and abitrate_max is not None:
                    title.tracks.select_audio(
                        lambda x: bool(x.bitrate and abitrate_min <= x.bitrate // 1000 <= abitrate_max)
                    )
                    if not title.tracks.audio:
                        return Skip(f"No Audio Track in {abitrate_min}-{abitrate_max}kbps range...")
                if audio_excl_r:
                    drop = matched_language_tags(
                        audio_excl_r, [t.language for t in title.tracks.audio], self.exact_lang, every_variant=True
                    )
                    title.tracks.select_audio(lambda x: str(x.language) not in drop)
                    if not title.tracks.audio:
                        return Skip(f"Every Audio Track was excluded by -{', -'.join(audio_excl_r)}...")
                audio_languages = a_lang or self.lang
                if audio_languages:
                    processed_lang = resolve_sort_langs(audio_languages, title.language, exact=self.exact_lang)
                    a_orig_token = orig_token(audio_languages, title.language, self.exact_lang)
                    if "orig" in audio_languages and not title.language:
                        log.warning("Original language not available for title, skipping 'orig' selection")

                    embedded_langs = embedded_audio_langs(
                        title.tracks.videos, keep_videos and not self.audio_description_only
                    )
                    audio_noun = "audio description tracks" if self.audio_description_only else "audio tracks"

                    if not any(tok in processed_lang for tok in ("best", "all")):
                        missing_a_langs = find_missing_langs(
                            processed_lang,
                            [*(a.language for a in title.tracks.audio), *embedded_langs],
                            exact=self.exact_lang,
                        )
                        if missing_a_langs:
                            missing_str = as_requested(missing_a_langs, a_orig_token)
                            if self.best_available:
                                remaining = [tok for tok in processed_lang if tok not in missing_a_langs]
                                if remaining:
                                    log.warning(
                                        f"{missing_str} not found in {audio_noun}, "
                                        f"continuing with: {as_requested(remaining, a_orig_token)}"
                                    )
                                    processed_lang = remaining
                                else:
                                    return Skip(f"{missing_str} not found in {audio_noun} and no fallback available")
                            else:
                                return Skip(f"{missing_str} not found in {audio_noun}")

                    title.tracks.audio = select_best_audio(
                        title.tracks.audio, processed_lang, self.acodec, self.audio_description, self.exact_lang
                    )
                    if not title.tracks.audio and not embedded_langs:
                        # empty when 'orig' was the sole request, did not resolve, and the
                        # video carries no audio of its own to fall back on
                        return Skip(f"There's no {as_requested(processed_lang, a_orig_token) or 'orig'} Audio Track...")

            kept_tracks: list[AnyTrack | Chapter | Attachment] = []
            if keep_videos:
                kept_tracks.extend(title.tracks.videos)
            if keep_audio:
                kept_tracks.extend(title.tracks.audio)
            if keep_subtitles:
                kept_tracks.extend(title.tracks.subtitles)
            if keep_chapters:
                kept_tracks.extend(title.tracks.chapters)
            if not self.no_attachments:
                kept_tracks.extend(title.tracks.attachments)

            title.tracks = Tracks(kept_tracks, manifest_url=title.tracks.manifest_url)
            if not (title.tracks.videos or title.tracks.audio or title.tracks.subtitles or title.tracks.chapters):
                return Skip("There's no track left for the selected options.", kind_only=True)
        return Kept(title.tracks, range_)


def select_best_audio(
    audio: list[Audio],
    languages: list[str],
    acodec: list[Audio.Codec],
    audio_description: bool,
    exact_lang: bool,
) -> list[Audio]:
    """Pick the best audio per (language, codec) group from a sort_audio-ordered list.

    "Best" is the first track of each order-preserving candidate filter, so the
    sort_audio ranking (descriptive-last, Atmos, codec_priority, bitrate) decides the
    winner. With audio_description both the best standard and best descriptive track per
    group are kept. "best"/"all" tokens select every language present in sorted order.
    """
    select_all = any(tok in ("best", "all") for tok in languages)
    if select_all:
        target_langs: list[Any] = list(dict.fromkeys(t.language for t in audio))
    else:
        target_langs = languages
    codecs: list[Audio.Codec | None] = list(acodec) if len(acodec) > 1 else [None]

    selected: list[Audio] = []
    for language in target_langs:
        for codec in codecs:
            if select_all:
                candidates = [t for t in audio if t.language == language and (codec is None or t.codec == codec)]
            else:
                # match against every codec at once, so the variant a tag means does not depend on codec
                tags = matching_languages(language, [t.language for t in audio], exact_lang)
                candidates = [t for t in audio if str(t.language) in tags and (codec is None or t.codec == codec)]
            if not candidates:
                continue
            if audio_description:
                standards = [t for t in candidates if not t.descriptive]
                if standards:
                    selected.append(standards[0])
                descs = [t for t in candidates if t.descriptive]
                if descs:
                    selected.append(descs[0])
            else:
                selected.append(candidates[0])
    # Built via per-tag by_language calls, so overlapping tags can append
    # the same track across iterations; dedupe by id.
    unique: dict[str, Audio] = {}
    for t in selected:
        unique.setdefault(t.id, t)
    return list(unique.values())


def select_hybrid_bases(videos: Sequence[Video]) -> list[Video]:
    """The HDR10 or HDR10+ track that is the hybrid base for each height, tallest first.

    Only HEVC tracks qualify, because dovi_tool injects the RPU into an HEVC stream. HDR10+ comes before
    HDR10 at one height.
    """
    bases: dict[int, Video] = {}
    for video in sorted(
        (v for v in videos if v.range in HYBRID_BASE_RANGES and v.codec in (Video.Codec.HEVC, None)),
        key=lambda v: (v.height or 0, v.range == Video.Range.HDR10P),
        reverse=True,
    ):
        bases.setdefault(video.height or 0, video)
    return list(bases.values())


def keep_generated_dv_bases(videos: Sequence[Video], bases: Sequence[Video.Range]) -> list[Video]:
    """The videos without the DV tracks and without a hybrid base range the user did not name."""
    return [v for v in videos if v.range != Video.Range.DV and (v.range not in HYBRID_BASE_RANGES or v.range in bases)]


def hybrid_gate_failure(videos: Sequence[Video], generate_dv: bool) -> str | None:
    """The message that stops HYBRID for these selected videos, or None when it can go on."""
    if any(v.native_hybrid for v in videos):
        return None
    hdr_tracks = [v for v in videos if v.range in HYBRID_BASE_RANGES]
    base_tracks = [v for v in hdr_tracks if v.codec in (Video.Codec.HEVC, None)]
    dv_tracks = [v for v in videos if v.range == Video.Range.DV]
    ranges = ", ".join(sorted(set(v.range.name for v in videos))) or "none"
    missing = f"HYBRID needs HDR10/HDR10+ and DV tracks, available ranges: {ranges}"

    if not hdr_tracks:
        return missing
    if not base_tracks:
        codecs = sorted(set(v.codec.name for v in hdr_tracks if v.codec))
        return f"HYBRID needs an HEVC HDR10/HDR10+ base, available codecs: {', '.join(codecs)}"
    if not dv_tracks and not generate_dv:
        return f"{missing}; add --generate-dv to make DV"
    return None
