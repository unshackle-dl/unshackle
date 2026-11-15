import logging
import struct
from types import SimpleNamespace
from typing import Any

import pytest

from tests.core.test_export_name import video
from unshackle.core.config import Config, config
from unshackle.core.import_service import video_range
from unshackle.core.manifests.snapshot import match_track
from unshackle.core.remote_service import RemoteService
from unshackle.core.tracks import Audio, Video
from unshackle.core.utils import hdr10plus
from unshackle.core.utils.hdr10plus import HDR10PLUS_T35, TS_PACKET, fragment_has_t35


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
    assert fragment_has_t35(data, HDR10PLUS_T35) is True


def test_static_metadata_only_is_plain_hdr10() -> None:
    data = fragment(nal(32, b"vps"), sei(137, b"\x00" * 24), sei(4, b"\xb5\x00\x31GA94"), nal(1, b"\xff" * 64))
    assert fragment_has_t35(data, HDR10PLUS_T35) is False


def test_encrypted_or_truncated_bytes_prove_nothing() -> None:
    assert fragment_has_t35(box(b"moof", b"") + box(b"mdat", b"\xff" * 64), HDR10PLUS_T35) is None
    assert fragment_has_t35(fragment(nal(32, b"vps")), HDR10PLUS_T35) is None
    assert fragment_has_t35(b"\x47" * 188, HDR10PLUS_T35) is None


def hevc(track_id: str, height: int, bitrate: int = 1, range_: Video.Range = Video.Range.HDR10) -> Any:
    return SimpleNamespace(
        id=track_id,
        range=range_,
        hdr10plus=False,
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
    heights = heights or [2160 - 100 * i for i in range(len(pattern))]
    videos = [hevc(f"v{i}{mark}", height, 100 - i) for i, (mark, height) in enumerate(zip(pattern, heights))]
    return videos, probe(monkeypatch, videos)


def probe(monkeypatch: pytest.MonkeyPatch, videos: list[Any]) -> list[str]:
    """Run the probe with a fake fragment: a track id that ends in '+' carries HDR10+. Returns the probed ids."""
    probed: list[str] = []

    def fake_fragment(video: Any, session: Any) -> bytes:
        probed.append(video.id)
        return video.id[-1].encode()

    monkeypatch.setattr(hdr10plus, "first_fragment", fake_fragment)
    monkeypatch.setattr(hdr10plus, "fragment_has_t35", lambda data, t35: t35 == HDR10PLUS_T35 and data == b"+")
    hdr10plus.probe_dynamic_hdr(videos, None, log=logging.getLogger("test"), workers=4)  # type: ignore[arg-type]
    return probed


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


def test_a_dv_ladder_keeps_its_range_and_is_sampled_apart_from_hdr10(monkeypatch: pytest.MonkeyPatch) -> None:
    hdr = [hevc(f"h{i}-", 2160 - 100 * i, 100 - i) for i in range(6)]
    dv = [hevc(f"d{i}+", 2160 - 100 * i, 100 - i, Video.Range.DV) for i in range(6)]
    assert len(probe(monkeypatch, hdr + dv)) == 8
    assert ranges(hdr) == "------"
    assert all(v.range == Video.Range.DV and v.hdr10plus for v in dv)


def test_a_dv_track_with_hdr10plus_lists_as_dv_plus_hdr10p() -> None:
    video = Video(
        url="https://x/v.mp4", language="en", codec=Video.Codec.HEVC, range_=Video.Range.DV, width=3840, height=2160
    )
    assert "[H.265, DV]" in str(video)
    video.hdr10plus = True
    assert "[H.265, DV+HDR10P]" in str(video)
    assert Video.from_dict(video.to_dict()).hdr10plus


def test_on_start_runs_only_when_there_is_a_track_to_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hdr10plus, "first_fragment", lambda video, session: None)
    started: list[int] = []
    sdr = SimpleNamespace(id="s", range=Video.Range.SDR, codec=Video.Codec.AVC, bitrate=1, session=None)
    for videos in ([sdr], [hevc("v", 2160)]):
        hdr10plus.probe_dynamic_hdr(
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
    assert fragment_has_t35(hdr10plus.first_fragment(hevc("v", 2160), None) or b"", HDR10PLUS_T35) is True  # type: ignore[arg-type]


def test_first_segment_of_an_hls_playlist_keeps_its_byte_offset() -> None:
    playlist = '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:6.0,\n#EXT-X-BYTERANGE:5000@1200\nmedia.mp4\n'
    session = SimpleNamespace(get=lambda url, timeout: SimpleNamespace(status_code=200, text=playlist))
    track = SimpleNamespace(url="https://cdn/v/index.m3u8", data={}, descriptor=Video.Descriptor.HLS)
    assert hdr10plus.first_segment(track, session) == ("https://cdn/v/media.mp4", 1200)  # type: ignore[arg-type]


def probe_calls(monkeypatch: pytest.MonkeyPatch, service: Any, **kwargs: Any) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr(
        hdr10plus, "probe_dynamic_hdr", lambda videos, session, *, log, workers, on_start: calls.append(workers)
    )
    hdr10plus.probed_tracks(service, None, **kwargs)
    return calls


def test_probed_tracks_follows_the_config_switch_and_the_workers_count(monkeypatch: pytest.MonkeyPatch) -> None:
    service = SimpleNamespace(
        get_tracks=lambda title: SimpleNamespace(videos=[hevc("v", 2160)], audio=[]), session=None
    )
    assert probe_calls(monkeypatch, service, workers=3) == [3]
    monkeypatch.setattr(config, "hdr10plus_probe", False)
    assert probe_calls(monkeypatch, service, workers=3) == []


def test_probed_tracks_leaves_a_remote_service_to_its_server(monkeypatch: pytest.MonkeyPatch) -> None:
    remote = RemoteService.__new__(RemoteService)
    remote.get_tracks = lambda title: SimpleNamespace(videos=[hevc("v", 2160)], audio=[])  # type: ignore[method-assign]
    assert probe_calls(monkeypatch, remote, workers=3) == []


@pytest.mark.parametrize("workers", [-1, "8"])
def test_a_bad_workers_count_is_an_error(monkeypatch: pytest.MonkeyPatch, workers: Any) -> None:
    service = SimpleNamespace(
        get_tracks=lambda title: SimpleNamespace(videos=[hevc("v", 2160)], audio=[]), session=None
    )
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
    assert fragment_has_t35(hdr10plus.first_fragment(hevc("v", 2160), None) or b"", HDR10PLUS_T35) is True  # type: ignore[arg-type]
    assert len(requests) == 1


def test_a_quoted_dl_workers_value_works_like_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    service = SimpleNamespace(
        get_tracks=lambda title: SimpleNamespace(videos=[hevc("v", 2160)], audio=[]), session=None
    )
    monkeypatch.setitem(config.dl, "workers", "8")
    assert probe_calls(monkeypatch, service) == [8]


def test_a_dv_track_with_an_hdr10_base_lists_as_dv_plus_hdr10() -> None:
    video = Video(url="https://x/v.mp4", language="en", codec=Video.Codec.HEVC, range_=Video.Range.DV, hdr10_base=True)
    assert "[H.265, DV+HDR10]" in str(video)
    assert Video.from_dict(video.to_dict()).hdr10_base


def ts_packet(pid: int, section: bytes) -> bytes:
    body = b"\x00" + section
    return bytes([0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x10]) + body + b"\xff" * (184 - len(body))


def ts(*streams: tuple[int, bytes]) -> bytes:
    """A PAT and a PMT that list each (stream type, registration) on its own PID."""
    pat = b"\x00\xb0\x0d\x00\x01\xc1\x00\x00\x00\x01\xf0\x00" + b"\x00" * 4
    entries = b""
    for index, (stream_type, registration) in enumerate(streams):
        descriptors = b"\x05\x04" + registration if registration else b""
        entries += bytes([stream_type, 0xE1, index, 0xF0, len(descriptors)]) + descriptors
    body = b"\x00\x01\xc1\x00\x00\xe1\x00\xf0\x00" + entries + b"\x00" * 4
    pmt = b"\x02" + bytes([0xB0 | (len(body) >> 8), len(body) & 0xFF]) + body
    return ts_packet(0, pat) + ts_packet(0x1000, pmt)


def test_ts_streams_reads_the_pmt_stream_types_and_registrations() -> None:
    assert hdr10plus.ts_streams(ts((0x24, b"cuvv"), (0x0F, b""))) == [(0x24, b"cuvv"), (0x0F, b"")]
    assert hdr10plus.ts_streams(ts_packet(0, b"\x00" * 16)) is None
    assert hdr10plus.is_ts(ts((0xD5, b""))) and not hdr10plus.is_ts(fragment(nal(1, b"")))


def serve_segments(monkeypatch: pytest.MonkeyPatch, segments: dict[str, bytes]) -> None:
    """Serve each track's first segment through the real first_fragment, keyed by track id."""
    monkeypatch.setattr(hdr10plus, "first_segment", lambda track, session: (track.id, 0))
    monkeypatch.setattr(hdr10plus, "ranged_get", lambda url, session, byte_range: segments[url])


def test_a_cuvv_registered_ts_track_becomes_vivid(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_segments(
        monkeypatch,
        {"vivid": ts((0x24, b"cuvv"), (0x0F, b"")), "plain": ts((0x24, b"")), "dv": ts((0x24, b"cuvv"))},
    )
    videos = [hevc("vivid", 2160), hevc("plain", 1080), hevc("dv", 2160, range_=Video.Range.DV)]
    hdr10plus.probe_dynamic_hdr(videos, None, log=logging.getLogger("test"), workers=4)  # type: ignore[arg-type]
    assert [v.range for v in videos] == [Video.Range.VIVID, Video.Range.HDR10, Video.Range.DV]
    assert not videos[2].hdr10plus


def test_cuva_sei_makes_an_fmp4_hdr10_or_hlg_track_vivid(monkeypatch: pytest.MonkeyPatch) -> None:
    cuva = fragment(nal(32, b"vps"), sei(4, hdr10plus.CUVA_T35 + b"\x00" * 8), nal(1, b"\xff" * 64))
    plus = fragment(nal(32, b"vps"), sei(4, HDR10PLUS_T35 + b"\x00" * 8), nal(1, b"\xff" * 64))
    serve_segments(monkeypatch, {"pq": cuva, "hlg": cuva, "hlg+": plus})
    videos = [
        hevc(i, 2160 - n, range_=r)
        for n, (i, r) in enumerate([("pq", Video.Range.HDR10), ("hlg", Video.Range.HLG), ("hlg+", Video.Range.HLG)])
    ]
    hdr10plus.probe_dynamic_hdr(videos, None, log=logging.getLogger("test"), workers=4)  # type: ignore[arg-type]
    assert [v.range for v in videos] == [Video.Range.VIVID, Video.Range.VIVID, Video.Range.HLG]


def test_ts_streams_skips_program_zero_and_survives_bad_lengths() -> None:
    pat = b"\x00\xb0\x11\x00\x01\xc1\x00\x00" + b"\x00\x00\xe0\x10" + b"\x00\x01\xf0\x00" + b"\x00" * 4
    data = ts_packet(0, pat) + ts((0x24, b"cuvv"))[TS_PACKET:]
    assert hdr10plus.ts_streams(data) == [(0x24, b"cuvv")]
    pmt = bytearray(ts((0x24, b"cuvv"))[TS_PACKET:])
    pmt[5 + 12 + 4] = 0xFF
    pmt[28:] = bytes(len(pmt) - 28)
    assert hdr10plus.ts_streams(ts((0x24, b""))[:TS_PACKET] + bytes(pmt)) == [(0x24, b"cuvv")]


def test_audio_without_a_codec_takes_it_from_the_ts_stream_type(monkeypatch: pytest.MonkeyPatch) -> None:
    fetched: list[tuple[str, str]] = []

    def fake_get(url: str, session: Any, byte_range: str) -> bytes:
        fetched.append((url, byte_range))
        return ts((0xD5, b""))

    monkeypatch.setattr(hdr10plus, "first_segment", lambda track, session: (track.id, 376))
    monkeypatch.setattr(hdr10plus, "ranged_get", fake_get)
    vivid = SimpleNamespace(id="a1", codec=None, session=None)
    named = SimpleNamespace(id="a2", codec=Audio.Codec.AAC, session=None)
    hdr10plus.probe_audio_codecs([vivid, named], None, log=logging.getLogger("test"), workers=4)  # type: ignore[arg-type,list-item]
    assert (vivid.codec, named.codec, fetched) == (
        Audio.Codec.AV3A,
        Audio.Codec.AAC,
        [("a1", f"bytes=376-{376 + 16 * TS_PACKET - 1}")],
    )
    assert "[Audio Vivid]" in str(Audio(url="https://x/a.m3u8", language="zh", codec=Audio.Codec.AV3A))


def test_an_avsa_registration_names_audio_vivid(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_segments(monkeypatch, {"a": ts((0x06, b"AVSA"))})
    track = SimpleNamespace(id="a", codec=None, session=None)
    hdr10plus.probe_audio_codecs([track], None, log=logging.getLogger("test"), workers=1)  # type: ignore[arg-type,list-item]
    assert track.codec == Audio.Codec.AV3A


def test_vivid_imports_by_value_and_matches_its_unprobed_reparse() -> None:
    assert video_range("HDR Vivid") == video_range("vivid") == Video.Range.VIVID
    for local, remote in ((Video.Range.HDR10, Video.Range.VIVID), (Video.Range.HLG, Video.Range.VIVID)):
        assert match_track(video("r", range_=remote), [video("l", range_=local)]) is not None
    assert match_track(video("r", range_=Video.Range.VIVID), [video("l", range_=Video.Range.HDR10P)]) is None


def test_audio_vivid_has_no_music_container(tmp_path: Any) -> None:
    path = tmp_path / "a.ts"
    path.write_bytes(b"\x47")
    track = Audio(url="https://x/a.m3u8", language="zh", codec=Audio.Codec.AV3A)
    track.path = path
    with pytest.raises(ValueError, match="Audio Vivid"):
        track.to_music_container()


def test_probed_tracks_probes_codecless_audio_without_video(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        hdr10plus, "probe_audio_codecs", lambda audio, session, *, log, workers: calls.append(len(audio))
    )
    service = SimpleNamespace(get_tracks=lambda title: SimpleNamespace(videos=[], audio=[SimpleNamespace(codec=None)]))
    service.session = None
    hdr10plus.probed_tracks(service, None, workers=2)  # type: ignore[arg-type]
    assert calls == [1]
