"""``ffmpeg_decodes`` must pass good media and fail a wrong content key, judged by a real FFmpeg.

Each test builds its media from FFmpeg lavfi sources, so no media file is kept in the repo.
The encrypted cases use Shaka Packager to encrypt with a clear lead and mp4decrypt to
decrypt with a wrong key, which writes noise in place of the encrypted samples.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import UUID

import pytest

from unshackle.core import binaries
from unshackle.core.drm.verify import KidMap, kid_windows
from unshackle.core.utils.subprocess import ffmpeg_decodes

pytestmark = pytest.mark.skipif(binaries.FFMPEG is None, reason="requires ffmpeg")

KID = "00000000098ac6820000000000000000"
KEY = "13c795e9383efc0387c0bad6a10df73f"
WRONG_KEY = "00000000000000000000000000000000"
# 24 fps with a keyframe every 48 frames: one 2 s GOP for each 2 s fragment.
GOP = ["-x264-params", "keyint=48:min-keyint=48:scenecut=0"]


def ffmpeg(out: Path, source: str, *codec: str) -> Path:
    subprocess.run(
        [str(binaries.FFMPEG), "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", source, *codec, str(out)],
        check=True,
    )
    return out


def video(out: Path, *x264: str, codec: str = "libx264") -> Path:
    return ffmpeg(
        out,
        "testsrc2=size=320x180:rate=24:duration=12",
        "-c:v",
        codec,
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        *x264,
    )


def test_audio_only_passes_fallback(tmp_path: Path) -> None:
    # -skip_frame on a file with no video decoder makes FFmpeg refuse to open it.
    audio = ffmpeg(tmp_path / "a.m4a", "sine=f=440:duration=12", "-c:a", "aac")
    assert ffmpeg_decodes(audio)
    assert ffmpeg_decodes(audio, video=False)


def test_mpegts_video_passes_fallback(tmp_path: Path) -> None:
    ts = video(tmp_path / "v.ts", *GOP)
    assert ffmpeg_decodes(ts)
    assert ffmpeg_decodes(ts, video=True)


def test_open_gop_passes_windows(tmp_path: Path) -> None:
    # The B-frames before each open-GOP keyframe refer to the GOP before the window.
    og = video(tmp_path / "og.mp4", "-x264-params", "open-gop=1:keyint=48:min-keyint=48:scenecut=0:bframes=3")
    assert all(ffmpeg_decodes(og, start=start, seconds=2, video=True) for start in (2.0, 4.0, 6.0, 8.0))


def test_open_gop_hevc_passes_windows(tmp_path: Path) -> None:
    og = video(
        tmp_path / "og.mp4",
        "-x265-params",
        "open-gop=1:keyint=48:min-keyint=48:scenecut=0:log-level=none",
        codec="libx265",
    )
    assert all(ffmpeg_decodes(og, start=start, seconds=2, video=True) for start in (2.0, 4.0, 6.0, 8.0))


# An HEVC keyframe decrypted with a wrong key can decode without an error, so the HEVC case
# proves that the check decodes the frames after it too.
@pytest.fixture(scope="module", params=["h264", "hevc"])
def encrypted(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> tuple[KidMap, Path, Path]:
    """The KID map of a clip encrypted after a clear lead, with its right-key and wrong-key decrypts."""
    if not binaries.ShakaPackager or not binaries.Mp4decrypt:
        pytest.skip("requires shaka packager and mp4decrypt")
    tmp = tmp_path_factory.mktemp("wrong_key")
    if request.param == "hevc":
        src = video(
            tmp / "src.mp4", "-x265-params", "keyint=48:min-keyint=48:scenecut=0:log-level=none", codec="libx265"
        )
    else:
        src = video(tmp / "src.mp4", *GOP)
    enc = tmp / "enc.mp4"
    subprocess.run(
        [
            str(binaries.ShakaPackager),
            f"in={src},stream=video,output={enc}",
            "--enable_raw_key_encryption",
            "--keys",
            f"label=:key_id={KID}:key={KEY}",
            "--clear_lead",
            "4",
            "--fragment_duration",
            "2",
            "--segment_duration",
            "2",
        ],
        check=True,
        capture_output=True,
    )
    right, wrong = tmp / "right.mp4", tmp / "wrong.mp4"
    subprocess.run([str(binaries.Mp4decrypt), "--key", f"{KID}:{KEY}", str(enc), str(right)], check=True)
    subprocess.run([str(binaries.Mp4decrypt), "--key", f"{KID}:{WRONG_KEY}", str(enc), str(wrong)], check=True)
    kid_map = kid_windows(enc)
    assert kid_map and list(kid_map.windows) == [UUID(KID)]
    return kid_map, right, wrong


def windows_pass(kid_map: KidMap, path: Path) -> list[bool]:
    return [ffmpeg_decodes(path, start=s, seconds=e - s, video=kid_map.video) for s, e in kid_map.windows[UUID(KID)]]


def test_right_key_passes_every_window(encrypted: tuple[KidMap, Path, Path]) -> None:
    kid_map, right, _ = encrypted
    assert all(windows_pass(kid_map, right))


def test_wrong_key_fails_every_window(encrypted: tuple[KidMap, Path, Path]) -> None:
    kid_map, _, wrong = encrypted
    assert not any(windows_pass(kid_map, wrong))


def test_wrong_key_passes_the_clear_lead(encrypted: tuple[KidMap, Path, Path]) -> None:
    # The window ends where the encrypted fragment starts, and B-frames put that fragment's
    # keyframe first in decode order: the decoder must not get it.
    kid_map, _, wrong = encrypted
    lead = kid_map.windows[UUID(KID)][0][0]
    assert ffmpeg_decodes(wrong, start=0.0, seconds=lead, video=True)


def test_wrong_key_fails_fallback(encrypted: tuple[KidMap, Path, Path]) -> None:
    assert not ffmpeg_decodes(encrypted[2])
