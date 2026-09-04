"""Container selection for audio Matroska cannot name.

DTS:X ships in two shapes. Profile 2 (DTS-UHD, sample entry dtsx/dtsy) has no Matroska
CodecID, so mkvmerge stores it as an A_QUICKTIME passthrough and no reader can name the
codec; those titles have to go to MP4. Profile 1 rides inside DTS-HD Master Audio
(dtsc/dtse/dtsh/
dtsl), which Matroska maps to A_DTS and stores properly, so it must stay in Matroska.
"""

from __future__ import annotations

import io
import struct
import subprocess
from pathlib import Path

import pytest

from unshackle.core import binaries
from unshackle.core.tracks import Audio, Subtitle, Tracks, Video
from unshackle.core.tracks.track import (
    has_dts_uhd_sample_entry,
    iter_top_level_boxes,
    strip_duplicate_init_boxes,
)


def box(box_type: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I", 8 + len(payload)) + box_type + payload


def write_mp4(path: Path, sample_entry: bytes) -> Path:
    """An MP4 whose moov holds one audio sample entry of the given 4CC."""
    entry = box(sample_entry, b"\x00" * 20)
    path.write_bytes(box(b"ftyp", b"isom") + box(b"moov", box(b"trak", box(b"stsd", entry))))
    return path


@pytest.mark.parametrize("fourcc", [b"dtsx", b"dtsy"])
def test_dts_uhd_sample_entries_are_detected(tmp_path: Path, fourcc: bytes) -> None:
    assert has_dts_uhd_sample_entry(write_mp4(tmp_path / "a.mp4", fourcc)) is True


@pytest.mark.parametrize("fourcc", [b"dtsc", b"dtse", b"dtsh", b"dtsl", b"ec-3", b"mp4a"])
def test_other_dts_profiles_stay_in_matroska(tmp_path: Path, fourcc: bytes) -> None:
    """
    DTS:X Profile 1 is DTS-HD Master Audio underneath and Matroska stores it as
    A_DTS, so it must not be pulled out of Matroska by the DTS:X container rule.
    """
    assert has_dts_uhd_sample_entry(write_mp4(tmp_path / "a.mp4", fourcc)) is False


def test_a_file_with_no_moov_is_not_detected(tmp_path: Path) -> None:
    path = tmp_path / "a.mp4"
    path.write_bytes(box(b"ftyp", b"isom") + box(b"mdat", b"dtsx" * 8))
    assert has_dts_uhd_sample_entry(path) is False, "a 4CC in mdat is not a sample entry"


def build_tracks(tmp_path: Path, audio_entry: bytes) -> Tracks:
    video = Video(id_="v1", url="https://x/v", codec=Video.Codec.AVC, language="en", width=1920, height=1080, fps=24)
    audio = Audio(id_="a1", url="https://x/a", codec=Audio.Codec.DTSX, language="en", bitrate=448000)
    video.path = write_mp4(tmp_path / "v.mp4", b"avc1")
    audio.path = write_mp4(tmp_path / "a.mp4", audio_entry)
    return Tracks([video, audio])


class FakePopen:
    """Stands in for MP4Box, recording the command it was given."""

    stdout_text = ""

    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.stdout = io.StringIO(FakePopen.stdout_text or "ISO File Writing: |==  | (50/100)\n")

    def wait(self) -> int:
        return 0


def test_dts_uhd_audio_routes_the_mux_to_mp4(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[FakePopen] = []

    def fake_popen(args, **kwargs):
        p = FakePopen(args, **kwargs)
        captured.append(p)
        Path(args[-1]).write_bytes(b"muxed")
        return p

    monkeypatch.setattr(binaries, "MP4Box", Path("MP4Box"), raising=False)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    tracks = build_tracks(tmp_path, b"dtsx")
    out, returncode, errors = tracks.mux("Title", delete=False, output_path=tmp_path / "out.muxed.mkv")

    assert out.suffix == ".mp4", "a DTS-UHD title must not be delivered as Matroska"
    assert returncode == 0 and not errors
    command = captured[0].args
    assert command[0] == "MP4Box"
    assert command[-2:] == ["-new", str(out)]
    assert f"{tmp_path / 'a.mp4'}:lang=en" in command
    # the child must not inherit the terminal that the live progress tree is drawing on
    assert captured[0].kwargs["stdout"] is subprocess.PIPE
    assert captured[0].kwargs["stderr"] is subprocess.STDOUT


def test_other_audio_still_muxes_with_mkvmerge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binaries, "MP4Box", None, raising=False)
    monkeypatch.setattr("unshackle.core.tracks.tracks.binaries.MKVToolNix", None)

    tracks = build_tracks(tmp_path, b"dtsh")
    # reaching the mkvmerge requirement proves the DTS-UHD branch was not taken; MP4Box
    # is None here too, so the MP4 branch would have raised about MP4Box instead
    with pytest.raises(RuntimeError, match="MKVToolNix"):
        tracks.mux("Title", delete=False, output_path=tmp_path / "out.muxed.mkv")


def test_a_missing_mp4box_names_the_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binaries, "MP4Box", None, raising=False)
    tracks = build_tracks(tmp_path, b"dtsx")
    with pytest.raises(RuntimeError, match="gpac"):
        tracks.mux("Title", delete=False, output_path=tmp_path / "out.muxed.mkv")


def test_substation_alpha_subtitles_are_left_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MP4 has no ASS format and tx3g would drop the styling that picked it."""
    captured: list[FakePopen] = []

    def fake_popen(args, **kwargs):
        p = FakePopen(args, **kwargs)
        captured.append(p)
        Path(args[-1]).write_bytes(b"muxed")
        return p

    monkeypatch.setattr(binaries, "MP4Box", Path("MP4Box"), raising=False)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    tracks = build_tracks(tmp_path, b"dtsx")
    ass = Subtitle(id_="s1", url="https://x/s", codec=Subtitle.Codec.SubStationAlphav4, language="en")
    ass.path = tmp_path / "s.ass"
    ass.path.write_text("[Script Info]\n")
    vtt = Subtitle(id_="s2", url="https://x/s2", codec=Subtitle.Codec.WebVTT, language="es")
    vtt.path = tmp_path / "s.vtt"
    vtt.path.write_text("WEBVTT\n")
    tracks.add([ass, vtt])

    tracks.mux("Title", delete=False, output_path=tmp_path / "out.muxed.mkv")
    command = " ".join(captured[0].args)
    assert "s.ass" not in command
    assert "s.vtt:lang=es" in command


def test_mp4box_errors_come_back_to_the_caller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed mux does not raise; the caller inspects the return code and lines."""

    class FailingPopen(FakePopen):
        def __init__(self, args, **kwargs):
            super().__init__(args, **kwargs)
            self.stdout = io.StringIO("Error importing track: not supported\n")

        def wait(self) -> int:
            return 1

    monkeypatch.setattr(binaries, "MP4Box", Path("MP4Box"), raising=False)
    monkeypatch.setattr(subprocess, "Popen", FailingPopen)

    tracks = build_tracks(tmp_path, b"dtsx")
    _, returncode, errors = tracks.mux("Title", delete=False, output_path=tmp_path / "out.muxed.mkv")
    assert returncode == 1
    assert errors == ["Error importing track: not supported"]


def init_pair(sample_entry: bytes, stamp: bytes = b"\x00\x00\x00\x01") -> bytes:
    """An ftyp+moov pair; the stamp stands in for the fetch time a packager writes."""
    stsd = box(b"stsd", box(sample_entry, b"\x00" * 20))
    return box(b"ftyp", b"isom") + box(b"moov", box(b"mvhd", stamp) + box(b"trak", stsd))


def fragment(payload: bytes) -> bytes:
    return box(b"moof", b"\x00" * 8) + box(b"mdat", payload)


def test_repeated_inits_are_dropped_when_the_stsd_matches(tmp_path: Path) -> None:
    """
    An HLS discontinuity gives each period its own init, so a merged track holds several.
    Only the stsd decides how samples decode, and a packager stamps each init with the
    fetch time, so copies that differ only in that stamp are safe to drop.
    """
    src = tmp_path / "a.mp4"
    src.write_bytes(
        init_pair(b"dtsx", b"\x00\x00\x00\x01")
        + fragment(b"one")
        + init_pair(b"dtsx", b"\x00\x00\x00\x99")
        + fragment(b"two")
    )
    dst = tmp_path / "clean.mp4"

    assert strip_duplicate_init_boxes(src, dst) == 2, "the second ftyp and moov should be dropped"
    types = [t for t, _, _ in iter_top_level_boxes(dst)]
    assert types == [b"ftyp", b"moov", b"moof", b"mdat", b"moof", b"mdat"]
    assert dst.read_bytes().count(b"mdat") == 2, "no sample data may be lost"


def test_a_changed_stsd_is_never_dropped(tmp_path: Path) -> None:
    """A later init describing a different format is a real change; dropping it corrupts."""
    src = tmp_path / "a.mp4"
    src.write_bytes(init_pair(b"dtsx") + fragment(b"one") + init_pair(b"ec-3") + fragment(b"two"))
    dst = tmp_path / "clean.mp4"

    assert strip_duplicate_init_boxes(src, dst) == 0
    assert not dst.exists(), "the caller must keep the original when the format changes"


def test_a_single_init_is_left_alone(tmp_path: Path) -> None:
    src = tmp_path / "a.mp4"
    src.write_bytes(init_pair(b"dtsx") + fragment(b"one"))
    dst = tmp_path / "clean.mp4"
    assert strip_duplicate_init_boxes(src, dst) == 0
    assert not dst.exists()


def test_an_unreadable_input_aborts_instead_of_shipping_a_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    MP4Box answers a file its ISOM reader refused by importing it through ffmpeg, which
    writes the samples under a placeholder 4CC. mkvmerge-style return codes treat 1 as a
    warning, so that shipped a whole title with an unnameable audio track.
    """

    class RefusingPopen(FakePopen):
        def __init__(self, args, **kwargs):
            super().__init__(args, **kwargs)
            self.stdout = io.StringIO("[IsoMedia] error while opening /tmp/a.mp4, error=Invalid IsoMedia File\n")

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(binaries, "MP4Box", Path("MP4Box"), raising=False)
    monkeypatch.setattr(subprocess, "Popen", RefusingPopen)

    tracks = build_tracks(tmp_path, b"dtsx")
    _, returncode, errors = tracks.mux("Title", delete=False, output_path=tmp_path / "out.muxed.mkv")

    assert returncode >= 2, "a refused input must abort the run, not pass as a warning"
    assert errors
