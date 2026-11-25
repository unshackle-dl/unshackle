from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from tests.core.test_dash_period_rebase import fragment, init_segment, server, tfdts
from unshackle.core.manifests import HLS
from unshackle.core.tracks import Subtitle
from unshackle.core.tracks.track import DownloadContext
from unshackle.core.utils.webvtt import CUE_TIMES, shift_webvtt, webvtt_block_offsets

__all__ = ["server"]


def block(*cues: str, local: str = "00:00:00.000") -> str:
    head = f"WEBVTT\nX-TIMESTAMP-MAP=MPEGTS:90000,LOCAL:{local}\n\n"
    return head + "".join(f"{cue} --> {cue[:-3]}999\ntext\n\n" for cue in cues)


def starts(text: str) -> list[str]:
    return [m[2] for m in CUE_TIMES.finditer(text)]


def rebase(texts: list[str], bounds_ms: list[int]) -> list[str]:
    return [shift_webvtt(t, o) for t, o in zip(texts, webvtt_block_offsets(texts, bounds_ms), strict=False)]


def test_restarting_blocks_move_to_their_playlist_start() -> None:
    texts = [block("00:00:01.000", "00:06:10.000"), block("00:00:00.023"), block("00:00:30.000")]
    out = rebase(texts, [0, 372933, 717166, 1419000])
    assert [starts(t) for t in out] == [["00:00:01.000", "00:06:10.000"], ["00:06:12.956"], ["00:12:27.166"]]


def test_absolute_cue_times_are_not_changed() -> None:
    # a cue carried over the block boundary starts just before the block, not before its middle
    texts = [block("00:00:01.000"), block("00:09:58.000", "00:10:05.000")]
    assert rebase(texts, [0, 600000, 1200000]) == texts


def test_a_late_first_cue_still_restarts() -> None:
    texts = [block("00:00:01.000"), block("00:00:40.000")]
    assert starts(rebase(texts, [0, 60000, 120000])[1]) == ["00:01:40.000"]


def test_a_cue_carried_over_a_short_block_is_not_a_restart() -> None:
    carried = "WEBVTT\n\n00:00:01.000 --> 00:00:08.000\nt\n\n00:00:20.000 --> 00:00:21.000\nt\n"
    texts = [block("00:00:01.000"), carried]
    assert rebase(texts, [0, 6000, 60000]) == texts


def test_local_time_marks_the_block_start() -> None:
    texts = [block("00:00:01.000"), block("00:00:12.000", local="00:00:10.000")]
    assert starts(rebase(texts, [0, 60000, 120000])[1]) == ["00:01:02.000"]


def test_shift_clamps_at_zero() -> None:
    assert starts(shift_webvtt(block("00:00:01.000"), -5000)) == ["00:00:00.000"]


def test_discontinuity_files_are_rewritten(tmp_path: Path) -> None:
    segments = [SimpleNamespace(discontinuity=d, duration=t) for d, t in ((True, 5), (False, 5), (True, 5))]
    files = [tmp_path / "0.vtt", tmp_path / "1.vtt"]
    files[0].write_bytes(block("00:00:01.000").encode())
    files[1].write_bytes(block("00:00:02.000").replace("\n", "\r\n").encode())

    HLS.discontinuity_append(Subtitle(url="", language="en", codec=Subtitle.Codec.WebVTT), segments, files)

    assert starts(files[0].read_text()) == ["00:00:01.000"]
    assert b"00:00:12.000 --> " in files[1].read_bytes()
    assert b"\r\n" in files[1].read_bytes()


MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",LANGUAGE="en",DEFAULT=YES,URI="subs.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1920x1080,CODECS="avc1.640028",SUBTITLES="subs"
media.m3u8
"""


def playlist(names: list[str], init: str = "") -> bytes:
    head = f'#EXT-X-MAP:URI="{init}"\n' if init else ""
    blocks = "#EXT-X-DISCONTINUITY\n".join(f"{head}#EXTINF:4.0,\n{name}\n" for name in names)
    return f"#EXTM3U\n#EXT-X-VERSION:6\n#EXT-X-TARGETDURATION:4\n{blocks}#EXT-X-ENDLIST\n".encode()


def download(server: ThreadingHTTPServer, tmp_path: Path, codec: Subtitle.Codec | None = None) -> Path:
    url = f"http://127.0.0.1:{server.server_address[1]}/master.m3u8"
    track = HLS.from_text(MASTER, url=url).to_tracks(language="en").subtitles[0]
    if codec:
        track.codec = codec
    ctx = DownloadContext(
        save_path=tmp_path / "out", save_dir=tmp_path / "s0_segments", progress=partial(lambda **kw: None)
    )
    HLS.download_track(track, ctx)
    return tmp_path / "out"


def test_download_track_rebases_webvtt_blocks(server: ThreadingHTTPServer, tmp_path: Path) -> None:
    server.routes["/subs.m3u8"] = playlist(["a.vtt", "b.vtt"])  # type: ignore[attr-defined]
    server.routes["/a.vtt"] = block("00:00:01.000").encode()  # type: ignore[attr-defined]
    server.routes["/b.vtt"] = block("00:00:01.000").encode()  # type: ignore[attr-defined]
    assert starts(download(server, tmp_path).read_text()) == ["00:00:01.000", "00:00:05.000"]


def test_download_track_rebases_fvtt_blocks(server: ThreadingHTTPServer, tmp_path: Path) -> None:
    server.routes["/subs.m3u8"] = playlist(["a.m4s", "b.m4s"], init="init.mp4")  # type: ignore[attr-defined]
    server.routes["/init.mp4"] = init_segment(1000)  # type: ignore[attr-defined]
    server.routes["/a.m4s"] = fragment(0)  # type: ignore[attr-defined]
    server.routes["/b.m4s"] = fragment(0)  # type: ignore[attr-defined]
    assert tfdts(download(server, tmp_path, Subtitle.Codec.fVTT).read_bytes()) == [0, 4000]
