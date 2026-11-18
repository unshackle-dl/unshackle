"""Service-independent completion archive matching and persistence."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from unshackle.core.download_archive import PARTIAL_MODES, ArchiveEntry, DownloadArchive, archive_entries
from unshackle.core.titles import Episode
from unshackle.core.tracks import Tracks, Video


class EXAMPLE:
    pass


def episode(height=720, **video_options):
    title = Episode(id_="example-episode-1", service=EXAMPLE, title="Example", season=1, number=1)
    title.tracks = Tracks(
        Video(
            id_="video",
            url="https://example.invalid/video",
            language="en",
            height=height,
            width=height * 16 // 9 if height else None,
            codec=Video.Codec.AVC,
            range_=Video.Range.SDR,
            **video_options,
        )
    )
    return title


def test_exact_resolved_variant_not_requested_quality(tmp_path):
    archive = DownloadArchive(tmp_path)
    entries = archive_entries("EXAMPLE", episode(), {})
    assert not archive.contains(entries)
    archive.record(entries)
    assert DownloadArchive(tmp_path).contains(archive_entries("EXAMPLE", episode(), {}))
    for change in (
        {"quality": 1080},
        {"range": "DV"},
        {"codec": "H.265"},
        {"service": "EXAMPLE2"},
        {"title_id": "other-id"},
    ):
        assert not archive.contains([replace(entries[0], **change)])
    # A lower explicit request is also a distinct variant.
    assert not archive.contains([replace(entries[0], quality=480)])


def test_all_variants_must_be_recorded(tmp_path):
    archive = DownloadArchive(tmp_path)
    entries = archive_entries("EXAMPLE", episode(), {})
    higher = replace(entries[0], quality=1080)
    archive.record(entries)
    assert not archive.contains([*entries, higher])
    archive.record([higher])
    assert archive.contains([*entries, higher])


@pytest.mark.parametrize("mode", PARTIAL_MODES)
def test_partial_and_non_download_modes_never_check_or_record(mode):
    assert archive_entries("EXAMPLE", episode(), {mode: True}) == []


def test_unknown_video_metadata_never_matches():
    assert archive_entries("EXAMPLE", episode(height=None), {}) == []
    title = episode()
    for attribute in ("codec", "range"):
        old = getattr(title.tracks.videos[0], attribute)
        setattr(title.tracks.videos[0], attribute, None)
        assert archive_entries("EXAMPLE", title, {}) == []
        setattr(title.tracks.videos[0], attribute, old)
    title.id = {"unsupported": "id"}
    assert archive_entries("EXAMPLE", title, {}) == []


def test_scopes_and_typed_ids_do_not_cross(tmp_path):
    cli = DownloadArchive(tmp_path)
    first = DownloadArchive(tmp_path, "api-" + "a" * 64)
    second = DownloadArchive(tmp_path, "api-" + "b" * 64)
    entries = [ArchiveEntry("EXAMPLE", 1234, 720, "SDR", "H.264")]
    first.record(entries)
    assert first.contains(entries)
    assert not second.contains(entries)
    assert not cli.contains(entries)
    assert not first.contains([replace(entries[0], title_id="1234")])
    for scope in ("../cli", "api-unowned", "api-../../cli", "/tmp/receipt"):
        with pytest.raises(ValueError):
            DownloadArchive(tmp_path, scope)


def test_history_and_file_deletion_have_no_effect(tmp_path):
    archive = DownloadArchive(tmp_path)
    entries = archive_entries("EXAMPLE", episode(), {})
    output = tmp_path / "output.mkv"
    history = tmp_path / "api_history.jsonl"
    output.touch()
    history.touch()
    archive.record(entries)
    output.unlink()
    history.unlink()
    assert DownloadArchive(tmp_path).contains(entries)
    text = archive.path.read_text()
    assert "output.mkv" not in text and "api_history" not in text


def test_concurrent_append_and_truncated_receipt(tmp_path):
    archive = DownloadArchive(tmp_path)
    entries = [ArchiveEntry("EXAMPLE", f"episode-{n}", 720, "SDR", "H.264") for n in range(20)]
    archive.path.parent.mkdir(parents=True)
    archive.path.write_text('{"version":1,"service":"unfinished')
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda entry: DownloadArchive(tmp_path).record([entry]), entries))
    assert archive.contains(entries)
    archive.record(entries)
    valid = [json.loads(line) for line in archive.path.read_text().splitlines()[1:] if line]
    assert len(valid) == len(entries)


def test_existing_reader_observes_another_writer(tmp_path):
    archive = DownloadArchive(tmp_path)
    first = ArchiveEntry("EXAMPLE", "episode-1", 720, "SDR", "H.264")
    second = replace(first, title_id="episode-2")
    archive.record([first])
    assert archive.contains([first])
    assert not archive.contains([second])
    DownloadArchive(tmp_path).record([second])
    assert archive.contains([first, second])
