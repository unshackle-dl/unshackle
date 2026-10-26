"""Download public DASH, HLS and ISM test manifests into tests/fixtures/manifests.

Sources: the DASH-IF test vector database (with the stream counts and codecs it expects),
the example MPDs of the MPEG DASH spec (ISO/IEC 23009-1), the yt-dlp test data, and the
stream lists of the dash.js reference player, the Shaka Player demo and the hls.js tests.
Only the manifest text is stored; index.json maps each file to its source URL, which the
parser needs to resolve relative URLs. A manifest that fails to download keeps its previous
copy. An HLS media playlist is left out, since HLS parses only multivariant playlists.
Run again to refresh: uv run python scripts/fetch_manifest_corpus.py
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests

DASHIF_VECTORS = (
    "https://raw.githubusercontent.com/Dash-Industry-Forum/Test-Assets-Dataset-Public/master/"
    "dataset/data/testvector.json"
)
MPEG_EXAMPLES = "https://api.github.com/repos/MPEGGroup/DASHSchema/git/trees/6th-Ed"
MPEG_RAW = "https://raw.githubusercontent.com/MPEGGroup/DASHSchema/6th-Ed/"
DASHJS_SOURCES = (
    "https://raw.githubusercontent.com/Dash-Industry-Forum/dash.js/development/"
    "samples/dash-if-reference-player/app/data/sources.json"
)
YTDLP_TESTDATA = "https://api.github.com/repos/yt-dlp/yt-dlp/contents/test/testdata/"
HLSJS_STREAMS = "https://raw.githubusercontent.com/video-dev/hls.js/master/tests/test-streams.js"
SHAKA_ASSETS = "https://raw.githubusercontent.com/shaka-project/shaka-player/main/demo/common/assets.js"
OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "manifests"
MAX_BYTES = 500_000
DEADLINE = 60
EXTENSIONS = {"dash": "mpd", "hls": "m3u8", "ism": "ism.xml"}

# url -> reason; manifests the parsers do not support yet, left out until a live service needs them
EXCLUDED: dict[str, str] = {
    "https://playertest.longtailvideo.com/adaptive/vod-with-mp3/manifest.m3u8": "the same variant listed twice gives a duplicate track ID",
    "https://raw.githubusercontent.com/MPEGGroup/DASHSchema/6th-Ed/example_G29-2.mpd": "spec example fragment with no playable Representation",
    "https://raw.githubusercontent.com/MPEGGroup/DASHSchema/6th-Ed/example_K6.4.mpd": "spec example fragment with no playable Representation",
    "https://raw.githubusercontent.com/MPEGGroup/DASHSchema/6th-Ed/example_G4.mpd": "two Representations share an id inside one Period",
    "https://raw.githubusercontent.com/MPEGGroup/DASHSchema/6th-Ed/example_G8.mpd": "Representations with no codecs attribute",
    "https://raw.githubusercontent.com/MPEGGroup/DASHSchema/6th-Ed/example_H3.mpd": "metadata Representation with no mimeType or contentType",
    "https://storage.googleapis.com/shaka-demo-assets/heliocentrism-xlink/heliocentrism.mpd": "xlink:href remote Periods are not resolved",
}

Stream = tuple[str, str, str, Optional[dict[str, Any]]]


def dashif_streams() -> list[Stream]:
    """Return the DASH-IF test vectors; a static one-Period vector also carries what it should parse to.

    The vector's max_num_video and max_num_audio count the Representations of its largest AdaptationSet.
    """
    streams: list[Stream] = []
    for vector in requests.get(DASHIF_VECTORS, timeout=30).json():
        if not vector.get("url"):
            continue
        expect = None
        if vector.get("mpd_type") == "static" and vector.get("num_periods") == 1:
            expect = {k: vector.get(k) for k in ("max_num_video", "max_num_audio", "video_codec", "audio_codec")}
        # the vector names are only "Test Vector N", the URL path says what the vector covers
        name = "/".join(urlparse(vector["url"]).path.split("/")[-3:]).removesuffix(".mpd")
        streams.append(("dashif", name, vector["url"], expect))
    return streams


def mpeg_streams() -> list[Stream]:
    """Return the example MPDs that ship with the MPEG DASH schema."""
    tree = requests.get(MPEG_EXAMPLES, timeout=30).json()["tree"]
    return [
        ("mpeg", item["path"].removesuffix(".mpd"), MPEG_RAW + item["path"], None)
        for item in tree
        if re.fullmatch(r"example_.+\.mpd", item["path"])
    ]


def ytdlp_streams() -> list[Stream]:
    """Return the manifests yt-dlp keeps as parser test data."""
    return [
        ("ytdlp", f"{folder}/{item['name']}", item["download_url"], None)
        for folder in ("mpd", "m3u8", "ism")
        for item in requests.get(YTDLP_TESTDATA + folder, timeout=30).json()
    ]


def hlsjs_streams() -> list[Stream]:
    """Return every URL in the hls.js test stream list, named by the key of its entry."""
    text = requests.get(HLSJS_STREAMS, timeout=30).text
    streams: list[Stream] = []
    for match in re.finditer(r"'(https?://[^']+)'", text):
        keys = re.findall(r"^  (\w+): ", text[: match.start()], re.M)
        streams.append(("hlsjs", keys[-1] if keys else "stream", match.group(1), None))
    return streams


def dashjs_streams() -> list[Stream]:
    data = requests.get(DASHJS_SOURCES, timeout=30).json()
    return [
        ("dashjs", f"{group['name']}/{item['name']}", item["url"], None)
        for group in data["items"]
        for item in group["submenu"]
        if item.get("url")
    ]


def shaka_streams() -> list[Stream]:
    text = requests.get(SHAKA_ASSETS, timeout=30).text
    pattern = re.compile(r"/\* name= \*/\s*'((?:[^'\\]|\\.)*)'.*?/\* manifestUri= \*/\s*'([^']+)'", re.S)
    return [("shaka", name.replace("\\'", "'"), url, None) for name, url in pattern.findall(text)]


def kind_of(head: bytes) -> Optional[str]:
    """Tell the manifest type from the first bytes, since many manifest URLs have no telling extension."""
    text = head.lstrip(b"\xef\xbb\xbf").lstrip()
    if text.startswith(b"#EXTM3U"):
        return "hls"
    if re.search(rb"<MPD[\s>]", text):
        return "dash"
    if re.search(rb"<SmoothStreamingMedia[\s>]", text):
        return "ism"
    return None


def file_name(source: str, name: str, url: str, kind: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower())[:60].strip("-")
    return f"{kind}/{source}-{slug}-{hashlib.sha1(url.encode()).hexdigest()[:8]}.{EXTENSIONS[kind]}"


def fetch(stream: Stream) -> Optional[dict[str, Any]]:
    """Download one manifest into OUT and return its index entry, or None when it is not a manifest."""
    source, name, url, expect = stream
    try:
        with requests.get(url, timeout=20, stream=True) as res:
            res.raise_for_status()
            deadline = time.monotonic() + DEADLINE
            body = b""
            for chunk in res.iter_content(65536):
                body += chunk
                if len(body) >= 4096 and not kind_of(body[:4096]):
                    return None
                if len(body) > MAX_BYTES or time.monotonic() > deadline:
                    print(f"skip {url}: over {MAX_BYTES} bytes or {DEADLINE}s")
                    return None
            final_url = res.url
    except requests.RequestException as e:
        print(f"skip {url}: {e}")
        return None
    kind = kind_of(body)
    if not kind or (kind == "hls" and b"#EXT-X-STREAM-INF" not in body):
        return None
    file = file_name(source, name, url, kind)
    (OUT / file).write_bytes(body)
    # a redirect moves the base that relative segment URLs resolve against
    entry: dict[str, Any] = {"file": file, "kind": kind, "source": source, "name": name, "url": final_url}
    if expect and kind == "dash":
        entry["expect"] = expect
    return entry


def main() -> None:
    streams: dict[str, Stream] = {}
    lists = dashif_streams() + mpeg_streams() + ytdlp_streams() + dashjs_streams() + shaka_streams() + hlsjs_streams()
    for source, name, url, expect in lists:
        url = f"https:{url}" if url.startswith("//") else url
        if url not in EXCLUDED:
            streams.setdefault(url, (source, name, url, expect))

    index_path = OUT / "index.json"
    old = (
        {(e["source"], e["name"]): e for e in json.loads(index_path.read_text("utf-8"))} if index_path.exists() else {}
    )
    for kind in EXTENSIONS:
        (OUT / kind).mkdir(parents=True, exist_ok=True)

    with ThreadPoolExecutor(16) as pool:
        results = list(pool.map(fetch, streams.values()))
    index = [entry for entry in results if entry]
    missing = {(source, name) for source, name, _, _ in streams.values()} - {(e["source"], e["name"]) for e in index}
    kept = [e for key, e in old.items() if key in missing and (OUT / e["file"]).exists()]
    index += kept

    keep = {e["file"] for e in index}
    for kind in EXTENSIONS:
        for path in (OUT / kind).iterdir():
            if f"{kind}/{path.name}" not in keep:
                path.unlink()
    index.sort(key=lambda entry: entry["file"])
    index_path.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(index) - len(kept)} manifests fetched, {len(kept)} kept from the last run, saved to {OUT}")


if __name__ == "__main__":
    main()
