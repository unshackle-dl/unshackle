import logging
import struct
from types import SimpleNamespace
from typing import Any

import pytest

from unshackle.core.config import Config, config
from unshackle.core.remote_service import RemoteService
from unshackle.core.tracks import Video
from unshackle.core.utils import hdr10plus
from unshackle.core.utils.hdr10plus import HDR10PLUS_T35, fragment_has_hdr10plus


def box(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", 8 + len(body)) + kind + body


def nal(nal_type: int, payload: bytes) -> bytes:
    data = bytes([nal_type << 1, 1]) + payload
    return struct.pack(">I", len(data)) + data


def sei(payload_type: int, payload: bytes) -> bytes:
    return nal(39, bytes([payload_type, len(payload)]) + payload + b"\x80")


def fragment(*nals: bytes) -> bytes:
    return box(b"moof", b"\x00" * 16) + box(b"mdat", b"".join(nals))


def test_hdr10plus_sei_before_the_slice_is_found() -> None:
    data = fragment(nal(32, b"vps"), sei(137, b"\x00" * 24), sei(4, HDR10PLUS_T35 + b"\x00" * 8), nal(1, b"\xff" * 64))
    assert fragment_has_hdr10plus(data) is True


def test_static_metadata_only_is_plain_hdr10() -> None:
    data = fragment(nal(32, b"vps"), sei(137, b"\x00" * 24), sei(4, b"\xb5\x00\x31GA94"), nal(1, b"\xff" * 64))
    assert fragment_has_hdr10plus(data) is False


def test_encrypted_or_truncated_bytes_prove_nothing() -> None:
    assert fragment_has_hdr10plus(box(b"moof", b"") + box(b"mdat", b"\xff" * 64)) is None
    assert fragment_has_hdr10plus(fragment(nal(32, b"vps"))) is None
    assert fragment_has_hdr10plus(b"\x47" * 188) is None


def hevc(track_id: str, height: int, bitrate: int = 1) -> Any:
    return SimpleNamespace(
        id=track_id,
        range=Video.Range.HDR10,
        codec=Video.Codec.HEVC,
        width=height * 16 // 9,
        height=height,
        bitrate=bitrate,
        session=None,
    )


def ladder(
    monkeypatch: pytest.MonkeyPatch, pattern: str, heights: list[int] | None = None
) -> tuple[list[Any], list[str]]:
    """HEVC HDR10 tracks, highest bitrate first; '+' marks the ones whose fragment carries HDR10+."""
    probed: list[str] = []

    def fake_fragment(video: Any, session: Any) -> bytes:
        probed.append(video.id)
        return video.id[-1].encode()

    monkeypatch.setattr(hdr10plus, "first_fragment", fake_fragment)
    monkeypatch.setattr(hdr10plus, "fragment_has_hdr10plus", lambda data: data == b"+")
    heights = heights or [2160 - 100 * i for i in range(len(pattern))]
    videos = [hevc(f"v{i}{mark}", height, 100 - i) for i, (mark, height) in enumerate(zip(pattern, heights))]
    hdr10plus.probe_hdr10plus(videos, None, log=logging.getLogger("test"), workers=4)  # type: ignore[arg-type]
    return videos, probed


def ranges(videos: list[Any]) -> str:
    return "".join("+" if v.range == Video.Range.HDR10P else "-" for v in videos)


def test_hdr10plus_at_both_ends_relabels_the_whole_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    videos, probed = ladder(monkeypatch, "++---++")
    assert ranges(videos) == "+++++++"
    assert len(probed) == 4


def test_a_mixed_sample_probes_every_track(monkeypatch: pytest.MonkeyPatch) -> None:
    videos, probed = ladder(monkeypatch, "+-+-+-+-")
    assert ranges(videos) == "+-+-+-+-"
    assert len(probed) == 8


def test_plain_hdr10_ends_stop_after_the_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    videos, probed = ladder(monkeypatch, "--++--")
    assert ranges(videos) == "------"
    assert len(probed) == 4


def test_two_encodes_at_one_resolution_are_all_probed(monkeypatch: pytest.MonkeyPatch) -> None:
    videos, probed = ladder(monkeypatch, "++-++", heights=[2160, 1440, 1080, 1080, 720])
    assert ranges(videos) == "++-++"
    assert len(probed) == 5


def test_on_start_runs_only_when_there_is_a_track_to_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hdr10plus, "first_fragment", lambda video, session: None)
    started: list[int] = []
    sdr = SimpleNamespace(id="s", range=Video.Range.SDR, codec=Video.Codec.AVC, bitrate=1, session=None)
    for videos in ([sdr], [hevc("v", 2160)]):
        hdr10plus.probe_hdr10plus(
            videos,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            log=logging.getLogger("test"),
            workers=4,
            on_start=lambda: started.append(1),
        )
    assert started == [1]


def test_first_fragment_skips_the_header_boxes_of_a_one_file_track(monkeypatch: pytest.MonkeyPatch) -> None:
    media = fragment(nal(32, b"vps"), sei(4, HDR10PLUS_T35 + b"\x00" * 8), nal(1, b"\xff" * 64))
    data = box(b"ftyp", b"isom") + box(b"moov", b"\x00" * 100_000) + box(b"sidx", b"\x00" * 40) + media

    def fake_get(url: str, session: Any, byte_range: str) -> bytes:
        start, end = (int(x) for x in byte_range.removeprefix("bytes=").split("-"))
        return data[start : end + 1]

    monkeypatch.setattr(hdr10plus, "first_segment", lambda track, session: ("https://cdn/v.mp4", 0))
    monkeypatch.setattr(hdr10plus, "ranged_get", fake_get)
    assert fragment_has_hdr10plus(hdr10plus.first_fragment(hevc("v", 2160), None) or b"") is True  # type: ignore[arg-type]


def test_first_segment_of_an_hls_playlist_keeps_its_byte_offset() -> None:
    playlist = '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:6.0,\n#EXT-X-BYTERANGE:5000@1200\nmedia.mp4\n'
    session = SimpleNamespace(get=lambda url, timeout: SimpleNamespace(status_code=200, text=playlist))
    track = SimpleNamespace(url="https://cdn/v/index.m3u8", data={}, descriptor=Video.Descriptor.HLS)
    assert hdr10plus.first_segment(track, session) == ("https://cdn/v/media.mp4", 1200)  # type: ignore[arg-type]


def probe_calls(monkeypatch: pytest.MonkeyPatch, service: Any, **kwargs: Any) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr(
        hdr10plus, "probe_hdr10plus", lambda videos, session, *, log, workers, on_start: calls.append(workers)
    )
    hdr10plus.probed_tracks(service, None, **kwargs)
    return calls


def test_probed_tracks_follows_the_config_switch_and_the_workers_count(monkeypatch: pytest.MonkeyPatch) -> None:
    service = SimpleNamespace(get_tracks=lambda title: SimpleNamespace(videos=[hevc("v", 2160)]), session=None)
    assert probe_calls(monkeypatch, service, workers=3) == [3]
    monkeypatch.setattr(config, "hdr10plus_probe", False)
    assert probe_calls(monkeypatch, service, workers=3) == []


def test_probed_tracks_leaves_a_remote_service_to_its_server(monkeypatch: pytest.MonkeyPatch) -> None:
    remote = RemoteService.__new__(RemoteService)
    remote.get_tracks = lambda title: SimpleNamespace(videos=[hevc("v", 2160)])  # type: ignore[method-assign]
    assert probe_calls(monkeypatch, remote, workers=3) == []


@pytest.mark.parametrize("workers", [-1, "8"])
def test_a_bad_workers_count_is_an_error(monkeypatch: pytest.MonkeyPatch, workers: Any) -> None:
    service = SimpleNamespace(get_tracks=lambda title: SimpleNamespace(videos=[hevc("v", 2160)]), session=None)
    with pytest.raises(ValueError, match="workers"):
        probe_calls(monkeypatch, service, workers=workers)


def test_a_quoted_hdr10plus_probe_value_is_an_error() -> None:
    with pytest.raises(SystemExit, match="hdr10plus_probe"):
        Config(hdr10plus_probe="false")


def test_a_fragment_inside_the_first_window_needs_one_request(monkeypatch: pytest.MonkeyPatch) -> None:
    media = fragment(nal(32, b"vps"), sei(4, HDR10PLUS_T35 + b"\x00" * 8), nal(1, b"\xff" * 64))
    data = box(b"sidx", b"\x00" * 40) + media
    requests: list[str] = []

    def fake_get(url: str, session: Any, byte_range: str) -> bytes:
        requests.append(byte_range)
        return data

    monkeypatch.setattr(hdr10plus, "first_segment", lambda track, session: ("https://cdn/v.mp4", 0))
    monkeypatch.setattr(hdr10plus, "ranged_get", fake_get)
    assert fragment_has_hdr10plus(hdr10plus.first_fragment(hevc("v", 2160), None) or b"") is True  # type: ignore[arg-type]
    assert len(requests) == 1


def test_a_quoted_dl_workers_value_works_like_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    service = SimpleNamespace(get_tracks=lambda title: SimpleNamespace(videos=[hevc("v", 2160)]), session=None)
    monkeypatch.setitem(config.dl, "workers", "8")
    assert probe_calls(monkeypatch, service) == [8]
