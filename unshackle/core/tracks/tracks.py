from __future__ import annotations

import logging
import subprocess
from functools import partial
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence, Union

from langcodes import Language, closest_supported_match
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn
from rich.table import Table
from rich.tree import Tree

from unshackle.core import binaries
from unshackle.core.config import config
from unshackle.core.console import console
from unshackle.core.constants import LANGUAGE_EXACT_DISTANCE, LANGUAGE_MAX_DISTANCE, AnyTrack, TrackT
from unshackle.core.events import events
from unshackle.core.tracks.attachment import Attachment
from unshackle.core.tracks.audio import Audio
from unshackle.core.tracks.chapters import Chapter, Chapters
from unshackle.core.tracks.subtitle import Subtitle
from unshackle.core.tracks.track import Track
from unshackle.core.tracks.video import Video
from unshackle.core.utilities import get_debug_logger, is_close_match, sanitize_filename
from unshackle.core.utils.collections import as_list, flatten


class Tracks:
    """
    Video, Audio, Subtitle, Chapter, and Attachment Track Store.
    It provides convenience functions for listing, sorting, and selecting tracks.
    """

    TRACK_ORDER_MAP = {Video: 0, Audio: 1, Subtitle: 2, Chapter: 3, Attachment: 4}

    def __init__(
        self,
        *args: Union[
            Tracks, Sequence[Union[AnyTrack, Chapter, Chapters, Attachment]], Track, Chapter, Chapters, Attachment
        ],
    ):
        self.videos: list[Video] = []
        self.audio: list[Audio] = []
        self.subtitles: list[Subtitle] = []
        self.chapters = Chapters()
        self.attachments: list[Attachment] = []

        if args:
            self.add(args)

    def __iter__(self) -> Iterator[AnyTrack]:
        return iter(as_list(self.videos, self.audio, self.subtitles))

    def __len__(self) -> int:
        return len(self.videos) + len(self.audio) + len(self.subtitles)

    def __add__(
        self,
        other: Union[
            Tracks, Sequence[Union[AnyTrack, Chapter, Chapters, Attachment]], Track, Chapter, Chapters, Attachment
        ],
    ) -> Tracks:
        self.add(other)
        return self

    def __repr__(self) -> str:
        return "{name}({items})".format(
            name=self.__class__.__name__, items=", ".join([f"{k}={repr(v)}" for k, v in self.__dict__.items()])
        )

    def __str__(self) -> str:
        rep = {Video: [], Audio: [], Subtitle: [], Chapter: [], Attachment: []}
        tracks = [*list(self), *self.chapters]

        for track in sorted(tracks, key=lambda t: self.TRACK_ORDER_MAP[type(t)]):
            if not rep[type(track)]:
                count = sum(type(x) is type(track) for x in tracks)
                rep[type(track)].append(
                    "{count} {type} Track{plural}{colon}".format(
                        count=count,
                        type=track.__class__.__name__,
                        plural="s" if count != 1 else "",
                        colon=":" if count > 0 else "",
                    )
                )
            rep[type(track)].append(str(track))

        for type_ in list(rep):
            if not rep[type_]:
                del rep[type_]
                continue
            rep[type_] = "\n".join([rep[type_][0]] + [f"├─ {x}" for x in rep[type_][1:-1]] + [f"└─ {rep[type_][-1]}"])
        rep = "\n".join(list(rep.values()))

        return rep

    def tree(self, add_progress: bool = False) -> tuple[Tree, list[partial]]:
        all_tracks = [*list(self), *self.chapters, *self.attachments]

        progress_callables = []

        tree = Tree("", hide_root=True)
        for track_type in self.TRACK_ORDER_MAP:
            tracks = list(x for x in all_tracks if isinstance(x, track_type))
            if not tracks:
                continue
            num_tracks = len(tracks)
            track_type_plural = track_type.__name__ + ("s" if track_type != Audio and num_tracks != 1 else "")
            tracks_tree = tree.add(f"[repr.number]{num_tracks}[/] {track_type_plural}")
            for track in tracks:
                if add_progress and track_type not in (Chapter, Attachment):
                    progress = Progress(
                        SpinnerColumn(finished_text=""),
                        BarColumn(),
                        "•",
                        TimeRemainingColumn(compact=True, elapsed_when_finished=True),
                        "•",
                        TextColumn("[progress.data.speed]{task.fields[downloaded]}"),
                        console=console,
                        speed_estimate_period=10,
                    )
                    task = progress.add_task("", downloaded="-")
                    progress_callables.append(partial(progress.update, task_id=task))
                    track_table = Table.grid()
                    track_table.add_row(str(track)[6:], style="text2")
                    track_table.add_row(progress)
                    tracks_tree.add(track_table)
                else:
                    tracks_tree.add(str(track)[6:], style="text2")

        return tree, progress_callables

    def exists(self, by_id: Optional[str] = None, by_url: Optional[Union[str, list[str]]] = None) -> bool:
        """Check if a track already exists by various methods."""
        if by_id:  # recommended
            return any(x.id == by_id for x in self)
        if by_url:
            return any(x.url == by_url for x in self)
        return False

    def add(
        self,
        tracks: Union[
            Tracks, Sequence[Union[AnyTrack, Chapter, Chapters, Attachment]], Track, Chapter, Chapters, Attachment
        ],
        warn_only: bool = False,
    ) -> None:
        """Add a provided track to its appropriate array and ensuring it's not a duplicate."""
        if isinstance(tracks, Tracks):
            tracks = [*list(tracks), *tracks.chapters, *tracks.attachments]

        duplicates = 0
        for track in flatten(tracks):
            if self.exists(by_id=track.id):
                if not warn_only:
                    raise ValueError(
                        "One or more of the provided Tracks is a duplicate. "
                        "Track IDs must be unique but accurate using static values. The "
                        "value should stay the same no matter when you request the same "
                        "content. Use a value that has relation to the track content "
                        "itself and is static or permanent and not random/RNG data that "
                        "wont change each refresh or conflict in edge cases."
                    )
                duplicates += 1
                continue

            if isinstance(track, Video):
                self.videos.append(track)
            elif isinstance(track, Audio):
                self.audio.append(track)
            elif isinstance(track, Subtitle):
                self.subtitles.append(track)
            elif isinstance(track, Chapter):
                self.chapters.add(track)
            elif isinstance(track, Attachment):
                self.attachments.append(track)
            else:
                raise ValueError("Track type was not set or is invalid.")

        log = logging.getLogger("Tracks")

        if duplicates:
            log.debug(f" - Found and skipped {duplicates} duplicate tracks...")

    def sort_videos(self, by_language: Optional[Sequence[Union[str, Language]]] = None) -> None:
        """Sort video tracks by bitrate, and optionally language."""
        if not self.videos:
            return
        # bitrate
        self.videos.sort(key=lambda x: float(x.bitrate or 0.0), reverse=True)
        # language
        for language in reversed(by_language or []):
            if str(language) in ("all", "best"):
                language = next((x.language for x in self.videos if x.is_original_lang), "")
            if not language:
                continue
            self.videos.sort(key=lambda x: str(x.language))
            self.videos.sort(key=lambda x: not is_close_match(language, [x.language]))

    def sort_audio(self, by_language: Optional[Sequence[Union[str, Language]]] = None) -> None:
        """Sort audio tracks by bitrate, descriptive, and optionally language."""
        if not self.audio:
            return
        # descriptive
        self.audio.sort(key=lambda x: x.descriptive)
        # bitrate (within each descriptive group)
        self.audio.sort(key=lambda x: float(x.bitrate or 0.0), reverse=True)
        # language
        for language in reversed(by_language or []):
            if str(language) in ("all", "best"):
                language = next((x.language for x in self.audio if x.is_original_lang), "")
            if not language:
                continue
            self.audio.sort(key=lambda x: not is_close_match(language, [x.language]))

    def sort_subtitles(self, by_language: Optional[Sequence[Union[str, Language]]] = None) -> None:
        """
        Sort subtitle tracks by various track attributes to a common P2P standard.
        You may optionally provide a sequence of languages to prioritize to the top.

        Section Order:
          - by_language groups prioritized to top, and ascending alphabetically
          - then rest ascending alphabetically after the prioritized groups
          (Each section ascending alphabetically, but separated)

        Language Group Order:
          - Forced
          - Normal
          - Hard of Hearing (SDH/CC)
          (Least to most captions expected in the subtitle)
        """
        if not self.subtitles:
            return
        # language groups
        self.subtitles.sort(key=lambda x: str(x.language))
        self.subtitles.sort(key=lambda x: x.sdh or x.cc)
        self.subtitles.sort(key=lambda x: x.forced, reverse=True)
        # sections
        for language in reversed(by_language or []):
            if str(language) == "all":
                language = next((x.language for x in self.subtitles if x.is_original_lang), "")
            if not language:
                continue
            self.subtitles.sort(key=lambda x: is_close_match(language, [x.language]), reverse=True)

    def select_video(self, x: Callable[[Video], bool]) -> None:
        self.videos = list(filter(x, self.videos))

    def select_audio(self, x: Callable[[Audio], bool]) -> None:
        self.audio = list(filter(x, self.audio))

    def select_subtitles(self, x: Callable[[Subtitle], bool]) -> None:
        self.subtitles = list(filter(x, self.subtitles))

    def select_hybrid(self, tracks, quality):
        hdr10_tracks = [
            v
            for v in tracks
            if v.range == Video.Range.HDR10 and (v.height in quality or int(v.width * 9 / 16) in quality)
        ]
        hdr10 = []
        for res in quality:
            candidates = [v for v in hdr10_tracks if v.height == res or int(v.width * 9 / 16) == res]
            if candidates:
                best = max(candidates, key=lambda v: v.bitrate)  # assumes .bitrate exists
                hdr10.append(best)

        dv_tracks = [v for v in tracks if v.range == Video.Range.DV]
        lowest_dv = min(dv_tracks, key=lambda v: v.height) if dv_tracks else None

        def select(x):
            if x in hdr10:
                return True
            if lowest_dv and x is lowest_dv:
                return True
            return False

        return select

    def by_resolutions(self, resolutions: list[int], per_resolution: int = 0) -> None:
        # Note: Do not merge these list comprehensions. They must be done separately so the results
        # from the 16:9 canvas check is only used if there's no exact height resolution match.
        selected = []
        for resolution in resolutions:
            matches = [  # exact matches
                x for x in self.videos if x.height == resolution
            ]
            if not matches:
                matches = [  # 16:9 canvas matches
                    x for x in self.videos if int(x.width * (9 / 16)) == resolution
                ]
            selected.extend(matches[: per_resolution or None])
        self.videos = selected

    @staticmethod
    def by_language(
        tracks: list[TrackT], languages: list[str], per_language: int = 0, exact_match: bool = False
    ) -> list[TrackT]:
        distance = LANGUAGE_EXACT_DISTANCE if exact_match else LANGUAGE_MAX_DISTANCE
        selected = []
        for language in languages:
            selected.extend(
                [x for x in tracks if closest_supported_match(str(x.language), [language], distance)][
                    : per_language or None
                ]
            )
        return selected

    def mux(
        self,
        title: str,
        delete: bool = True,
        progress: Optional[partial] = None,
        audio_expected: bool = True,
        title_language: Optional[Language] = None,
        skip_subtitles: bool = False,
    ) -> tuple[Path, int, list[str]]:
        """
        Multiplex all the Tracks into a Matroska Container file.

        Parameters:
            title: Set the Matroska Container file title. Usually displayed in players
                instead of the filename if set.
            delete: Delete all track files after multiplexing.
            progress: Update a rich progress bar via `completed=...`. This must be the
                progress object's update() func, pre-set with task id via functools.partial.
            audio_expected: Whether audio is expected in the output. Used to determine
                if embedded audio metadata should be added.
            title_language: The title's intended language. Used to select the best video track
                for audio metadata when multiple video tracks exist.
            skip_subtitles: Skip muxing subtitle tracks into the container.
        """
        if self.videos and not self.audio and audio_expected:
            video_track = None
            if title_language:
                video_track = next((v for v in self.videos if v.language == title_language), None)
                if not video_track:
                    video_track = next((v for v in self.videos if v.is_original_lang), None)

            video_track = video_track or self.videos[0]
            if video_track.language.is_valid():
                lang_code = str(video_track.language)
                lang_name = video_track.language.display_name()

                for video in self.videos:
                    video.needs_repack = True
                    video.data["audio_language"] = lang_code
                    video.data["audio_language_name"] = lang_name

        if not binaries.MKVToolNix:
            raise RuntimeError("MKVToolNix (mkvmerge) is required for muxing but was not found")

        cl = [
            str(binaries.MKVToolNix),
            "--no-date",  # remove dates from the output for security
        ]

        if config.muxing.get("set_title", True):
            cl.extend(["--title", title])

        for i, vt in enumerate(self.videos):
            if not vt.path or not vt.path.exists():
                raise ValueError("Video Track must be downloaded before muxing...")
            events.emit(events.Types.TRACK_MULTIPLEX, track=vt)

            is_default = False
            if title_language:
                is_default = vt.language == title_language
                if not any(v.language == title_language for v in self.videos):
                    is_default = vt.is_original_lang or i == 0
            else:
                is_default = i == 0

            # Prepare base arguments
            video_args = [
                "--language",
                f"0:{vt.language}",
                "--default-track",
                f"0:{is_default}",
                "--original-flag",
                f"0:{vt.is_original_lang}",
                "--compression",
                "0:none",  # disable extra compression
            ]

            # Add FPS fix if needed (typically for hybrid mode to prevent sync issues)
            if hasattr(vt, "needs_duration_fix") and vt.needs_duration_fix and vt.fps:
                video_args.extend(
                    [
                        "--default-duration",
                        f"0:{vt.fps}fps" if isinstance(vt.fps, str) else f"0:{vt.fps:.3f}fps",
                        "--fix-bitstream-timing-information",
                        "0:1",
                    ]
                )

            if hasattr(vt, "range") and vt.range == Video.Range.HLG:
                video_args.extend(
                    [
                        "--color-transfer-characteristics",
                        "0:18",  # ARIB STD-B67 (HLG)
                    ]
                )

            if hasattr(vt, "data") and vt.data.get("audio_language"):
                audio_lang = vt.data["audio_language"]
                audio_name = vt.data.get("audio_language_name", audio_lang)
                video_args.extend(
                    [
                        "--language",
                        f"1:{audio_lang}",
                        "--track-name",
                        f"1:{audio_name}",
                    ]
                )

            cl.extend(video_args + ["(", str(vt.path), ")"])

        for i, at in enumerate(self.audio):
            if not at.path or not at.path.exists():
                raise ValueError("Audio Track must be downloaded before muxing...")
            events.emit(events.Types.TRACK_MULTIPLEX, track=at)
            cl.extend(
                [
                    "--track-name",
                    f"0:{at.get_track_name() or ''}",
                    "--language",
                    f"0:{at.language}",
                    "--default-track",
                    f"0:{at.is_original_lang}",
                    "--visual-impaired-flag",
                    f"0:{at.descriptive}",
                    "--original-flag",
                    f"0:{at.is_original_lang}",
                    "--compression",
                    "0:none",  # disable extra compression
                    "(",
                    str(at.path),
                    ")",
                ]
            )

        if not skip_subtitles:
            for st in self.subtitles:
                if not st.path or not st.path.exists():
                    raise ValueError("Text Track must be downloaded before muxing...")
                events.emit(events.Types.TRACK_MULTIPLEX, track=st)
                default = bool(self.audio and is_close_match(st.language, [self.audio[0].language]) and st.forced)
                cl.extend(
                    [
                        "--track-name",
                        f"0:{st.get_track_name() or ''}",
                        "--language",
                        f"0:{st.language}",
                        "--sub-charset",
                        "0:UTF-8",
                        "--forced-track",
                        f"0:{st.forced}",
                        "--default-track",
                        f"0:{default}",
                        "--hearing-impaired-flag",
                        f"0:{st.sdh}",
                        "--original-flag",
                        f"0:{st.is_original_lang}",
                        "--compression",
                        "0:none",  # disable extra compression (probably zlib)
                        "(",
                        str(st.path),
                        ")",
                    ]
                )

        if self.chapters:
            chapters_path = config.directories.temp / config.filenames.chapters.format(
                title=sanitize_filename(title), random=self.chapters.id
            )
            self.chapters.dump(chapters_path, fallback_name=config.chapter_fallback_name)
            cl.extend(["--chapter-charset", "UTF-8", "--chapters", str(chapters_path)])
        else:
            chapters_path = None

        for attachment in self.attachments:
            if not attachment.path or not attachment.path.exists():
                raise ValueError("Attachment File was not found...")
            cl.extend(
                [
                    "--attachment-description",
                    attachment.description or "",
                    "--attachment-mime-type",
                    attachment.mime_type,
                    "--attachment-name",
                    attachment.name,
                    "--attach-file",
                    str(attachment.path.resolve()),
                ]
            )

        output_path = (
            self.videos[0].path.with_suffix(".muxed.mkv")
            if self.videos
            else self.audio[0].path.with_suffix(".muxed.mka")
            if self.audio
            else self.subtitles[0].path.with_suffix(".muxed.mks")
            if self.subtitles
            else chapters_path.with_suffix(".muxed.mkv")
            if self.chapters
            else None
        )
        if not output_path:
            raise ValueError("No tracks provided, at least one track must be provided.")

        debug_logger = get_debug_logger()
        if debug_logger:
            debug_logger.log(
                level="DEBUG",
                operation="mux_start",
                message="Starting mkvmerge muxing",
                context={
                    "title": title,
                    "output_path": str(output_path),
                    "video_count": len(self.videos),
                    "audio_count": len(self.audio),
                    "subtitle_count": len(self.subtitles),
                    "attachment_count": len(self.attachments),
                    "has_chapters": bool(self.chapters),
                    "video_tracks": [
                        {"id": v.id, "codec": getattr(v, "codec", None), "language": str(v.language)}
                        for v in self.videos
                    ],
                    "audio_tracks": [
                        {"id": a.id, "codec": getattr(a, "codec", None), "language": str(a.language)}
                        for a in self.audio
                    ],
                    "subtitle_tracks": [
                        {"id": s.id, "codec": getattr(s, "codec", None), "language": str(s.language)}
                        for s in self.subtitles
                    ],
                },
            )

        # let potential failures go to caller, caller should handle
        try:
            errors = []
            p = subprocess.Popen([*cl, "--output", str(output_path), "--gui-mode"], text=True, stdout=subprocess.PIPE)
            for line in iter(p.stdout.readline, ""):
                if line.startswith("#GUI#error") or line.startswith("#GUI#warning"):
                    errors.append(line)
                if "progress" in line:
                    progress(total=100, completed=int(line.strip()[14:-1]))

            returncode = p.wait()

            if debug_logger:
                if returncode != 0 or errors:
                    debug_logger.log(
                        level="ERROR",
                        operation="mux_failed",
                        message=f"mkvmerge exited with code {returncode}",
                        context={
                            "returncode": returncode,
                            "output_path": str(output_path),
                            "errors": errors,
                        },
                    )
                else:
                    debug_logger.log(
                        level="DEBUG",
                        operation="mux_complete",
                        message="mkvmerge muxing completed successfully",
                        context={
                            "output_path": str(output_path),
                            "output_exists": output_path.exists() if output_path else False,
                        },
                    )

            return output_path, returncode, errors
        finally:
            if chapters_path:
                chapters_path.unlink()
            if delete:
                for track in self:
                    track.delete()
                for attachment in self.attachments:
                    if attachment.path and attachment.path.exists():
                        attachment.path.unlink()


__all__ = ("Tracks",)
