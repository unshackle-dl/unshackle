from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

import pytest

from tests.core.test_kid_windows import A, B, box, clear_audio, encv, moof, movie, sinf
from tests.core.test_zero_kid_fallback import _playready, _widevine
from tests.test_segment_decrypt import read_argv
from unshackle.core import binaries
from unshackle.core.config import config
from unshackle.core.drm import ClearKeyCENC
from unshackle.core.drm.key_args import shaka_noise, track_key_args
from unshackle.core.drm.segment_decrypt import SegmentDecrypter, can_use
from unshackle.core.drm.verify import kidless_track_kids, mp4decrypt_or_shaka

KEY = "b5df2ae3f63b8dda9049d6e41422e075"
OTHER_KEY = "11" * 16
POSIX_STUBS = pytest.mark.skipif(sys.platform == "win32", reason="the decrypter stubs are shell scripts")


def two_sinf(kid: UUID, piff_first: bool) -> bytes:
    """A sample entry with a PIFF and a CENC sinf for the same KID."""
    return box(b"encv", bytes(78), sinf(kid, piff_first), sinf(kid, not piff_first))


def track_zero(entry: bytes) -> bytes:
    """A movie whose tkhd carries track_ID 0."""
    data = movie(entry)
    tkhd = data.index(b"tkhd")
    return data[: tkhd + 16] + bytes(4) + data[tkhd + 20 :]


@pytest.mark.parametrize(
    ("init", "kids"),
    [
        pytest.param(movie(two_sinf(A, piff_first=True)), {1: A}, id="piff-then-cenc"),
        pytest.param(movie(encv(A, piff=True)), {1: A}, id="piff-only"),
        pytest.param(movie(two_sinf(A, piff_first=False)), {}, id="cenc-then-piff"),
        pytest.param(movie(encv(A)), {}, id="cenc-only"),
        pytest.param(clear_audio(), {}, id="clear"),
        pytest.param(track_zero(two_sinf(A, piff_first=True)), {}, id="track-id-0"),
        pytest.param(b"\x00\x00\x00\x01moov\x00\x00", {}, id="truncated-large-size"),
        pytest.param(movie(two_sinf(A, piff_first=True))[:-20], {}, id="cut-short"),
    ],
)
def test_kidless_track_kids(init: bytes, kids: dict[int, UUID]) -> None:
    assert kidless_track_kids(init) == kids


def test_kidless_track_kids_survives_deep_nesting() -> None:
    nested = b""
    for _ in range(3000):
        nested = box(b"trak", nested)
    assert kidless_track_kids(nested) == {}


def test_track_key_needs_the_track_kid() -> None:
    assert track_key_args({A: KEY, B: OTHER_KEY}, {2: A}) == ["--key", f"2:{KEY}"]
    assert track_key_args({B: OTHER_KEY}, {2: A}) == []


DRMS: list[Any] = [
    pytest.param(lambda: _widevine(A), id="widevine"),
    pytest.param(lambda: _playready(A), id="playready"),
    pytest.param(lambda: ClearKeyCENC(kids=[A]), id="clearkey"),
]


def write_movie(path: Path, entry: bytes, fragment: bytes = b"") -> Path:
    path.write_bytes(movie(entry) + fragment + box(b"mdat", bytes(16)))
    return path


@POSIX_STUBS
@pytest.mark.parametrize("make", DRMS)
def test_whole_file_decrypt_keys_a_piff_first_track_by_id(
    tmp_path: Path, stub_mp4decrypt: Path, make: Callable[[], Any]
) -> None:
    path = write_movie(tmp_path / "track.mp4", two_sinf(A, piff_first=True))
    drm = make()
    drm.content_keys[A] = KEY

    drm.decrypt_with_mp4decrypt(path)

    assert f"1:{KEY}" in read_argv(stub_mp4decrypt)[0]


@POSIX_STUBS
@pytest.mark.parametrize("make", DRMS)
def test_decrypt_falls_back_to_shaka_on_a_passthrough(
    tmp_path: Path, stub_mp4decrypt: Path, monkeypatch: pytest.MonkeyPatch, make: Callable[[], Any]
) -> None:
    path = write_movie(tmp_path / "track.mp4", two_sinf(A, piff_first=True), moof(1, 0))
    drm = make()
    drm.content_keys[B] = OTHER_KEY
    shaka: list[Path] = []
    monkeypatch.setattr(type(drm), "decrypt_with_shaka_packager", lambda self, p: shaka.append(p))
    monkeypatch.setattr(config, "decryption", "mp4decrypt", raising=False)

    drm.decrypt(path)

    assert shaka == [path]


@POSIX_STUBS
def test_segment_decrypter_keys_from_the_init_segment(tmp_path: Path, stub_mp4decrypt: Path) -> None:
    drm = _widevine(A)
    drm.content_keys[A] = KEY

    decrypter = SegmentDecrypter(drm, movie(two_sinf(A, piff_first=True)), tmp_path, workers=1)
    decrypter.pool.shutdown()

    assert f"1:{KEY}" in decrypter.key_args


@pytest.mark.parametrize(("key_kid", "usable"), [(A, True), (B, False)], ids=["track-kid-keyed", "other-kid-only"])
def test_segment_decrypt_needs_a_key_for_a_kidless_track(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key_kid: UUID, usable: bool
) -> None:
    monkeypatch.setattr(config, "decrypt_segments", True, raising=False)
    monkeypatch.setattr(binaries, "Mp4decrypt", tmp_path / "mp4decrypt")
    drm = _widevine(A)
    drm.content_keys[key_kid] = KEY

    assert can_use(drm, "mp4decrypt", movie(two_sinf(A, piff_first=True))) is usable
    assert can_use(drm, "mp4decrypt", movie(encv(A))) is True


class StubDRM:
    """Records which decrypter ran; mp4decrypt writes ``mp4decrypt_output`` over the file."""

    def __init__(self, mp4decrypt_output: bytes) -> None:
        self.output = mp4decrypt_output
        self.calls: list[str] = []

    def decrypt_with_mp4decrypt(self, path: Path) -> None:
        self.calls.append("mp4decrypt")
        path.write_bytes(self.output)

    def decrypt_with_shaka_packager(self, path: Path) -> None:
        self.calls.append("shaka")


ENCRYPTED = movie(encv(A)) + moof(1, 0)


@pytest.mark.parametrize(
    ("output", "calls"),
    [
        pytest.param(ENCRYPTED, ["mp4decrypt", "shaka"], id="copied-through"),
        pytest.param(movie(encv(A)) + moof(1, 1000), ["mp4decrypt"], id="stale-encrypted-moov"),
        pytest.param(movie(box(b"avc1", bytes(78))) + moof(1, 1000), ["mp4decrypt"], id="decrypted"),
        pytest.param(b"not an mp4", ["mp4decrypt"], id="unreadable"),
    ],
)
def test_mp4decrypt_or_shaka(tmp_path: Path, output: bytes, calls: list[str]) -> None:
    drm = StubDRM(output)
    path = tmp_path / "track.mp4"
    path.write_bytes(ENCRYPTED)

    mp4decrypt_or_shaka(drm, path)

    assert drm.calls == calls


@pytest.fixture
def stub_packager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[int], None]:
    """A shaka-packager that writes its output, logs an ERROR line and exits with the code given."""

    def install(code: int) -> None:
        script = tmp_path / "packager"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "output = sys.argv[1].split('output=')[1].split(',')[0]\n"
            "open(output, 'wb').write(b'decrypted')\n"
            "sys.stderr.write('[1103/153702:ERROR:box_definitions.cc(641)] Not implemented reached\\n')\n"
            f"sys.exit({code})\n"
        )
        script.chmod(0o755)
        monkeypatch.setattr(binaries, "ShakaPackager", script)

    return install


@POSIX_STUBS
@pytest.mark.parametrize("make", DRMS)
def test_shaka_error_line_with_exit_0_is_a_decrypt(
    tmp_path: Path, stub_packager: Callable[[int], None], make: Callable[[], Any]
) -> None:
    stub_packager(0)
    path = tmp_path / "track.mp4"
    path.write_bytes(b"encrypted")
    drm = make()
    drm.content_keys[A] = KEY

    drm.decrypt_with_shaka_packager(path)

    assert path.read_bytes() == b"decrypted"


@POSIX_STUBS
@pytest.mark.parametrize("make", DRMS)
def test_shaka_non_zero_exit_raises(
    tmp_path: Path, stub_packager: Callable[[int], None], make: Callable[[], Any]
) -> None:
    stub_packager(2)
    path = tmp_path / "track.mp4"
    path.write_bytes(b"encrypted")
    drm = make()
    drm.content_keys[A] = KEY

    with pytest.raises(subprocess.CalledProcessError):
        drm.decrypt_with_shaka_packager(path)


@pytest.mark.parametrize(
    ("line", "noise"),
    [
        pytest.param(
            "I1103 21:41:56.557273   16212 packager_main.cc:591] Excluding 'pssh' boxes", True, id="glog-info"
        ),
        pytest.param("I0503 01:02:03.000000   1 demuxer.cc:105] Demuxer::Run()", True, id="glog-info-early-month"),
        pytest.param("W1103 21:41:56.000000   1 x.cc:1] warning", True, id="glog-warning"),
        pytest.param(
            "[1103/214121:WARNING:track_run_iterator.cc(699)] Seeing non-zero composition offset", True, id="warning"
        ),
        pytest.param("[1103/153402:INFO:demuxer.cc(170)] Initialize Demuxer", True, id="info"),
        pytest.param("[1103/153402:ERROR:box_definitions.cc(641)] Not implemented reached", False, id="error"),
        pytest.param("E1103 21:41:56.000000   1 x.cc:1] failed", False, id="glog-error"),
        pytest.param("Packaging completed successfully.", False, id="plain"),
    ],
)
def test_shaka_noise(line: str, noise: bool) -> None:
    assert shaka_noise(line) is noise
