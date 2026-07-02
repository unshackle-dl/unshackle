"""Track-selection tests for HYBRID + DV behaviour.

Covers the selection primitives that back `-r ...,DV,HYBRID` downloads:

- ``Tracks.select_hybrid`` — picks the HDR base layer per resolution and the
  single lowest DV track used as a hybrid ingredient.
- ``Tracks.merge_video_selections`` — de-duplicates the ingredient/deliverable
  overlap so a DV track that is chosen as both is not muxed/downloaded twice.
- ``Tracks.partition_hybrid_videos`` — splits the ladder into hybrid-ingredient
  candidates and the standalone-deliverable pool; HDR10/HDR10+/DV only enter
  the pool when their range was explicitly requested alongside HYBRID.
- ``Tracks.flag_hybrid_ingredients`` — marks ingredient-only tracks with
  ``hybrid_base_only`` so the standalone mux loop skips them.

The remaining ``dl`` glue (the Cartesian deliverable product and the mux loop)
is orchestration inside the Click command; these tests lock down the pure
units it relies on plus the documented end-state of a realistic ATV-style ladder.
"""

from __future__ import annotations

import pytest

from unshackle.core.tracks import Tracks, Video


def make_video(track_id: str, *, range_: Video.Range, height: int, bitrate: int, codec: Video.Codec) -> Video:
    return Video(
        id_=track_id,
        url=f"https://example.test/{track_id}.m3u8",
        language="en",
        codec=codec,
        range_=range_,
        width=int(height * 16 / 9),
        height=height,
        bitrate=bitrate,
    )


@pytest.fixture
def ladder() -> list[Video]:
    """Mirrors the reported ATV ladder: HDR10+, DV and SDR at multiple resolutions."""
    H = Video.Codec.HEVC
    A = Video.Codec.AVC
    return [
        make_video("hdr10p-2160", range_=Video.Range.HDR10P, height=2160, bitrate=25_516_000, codec=H),
        make_video("hdr10p-1080", range_=Video.Range.HDR10P, height=1080, bitrate=9_096_000, codec=H),
        make_video("dv-2160", range_=Video.Range.DV, height=2160, bitrate=25_511_000, codec=H),
        make_video("dv-1080", range_=Video.Range.DV, height=1080, bitrate=9_152_000, codec=H),
        make_video("dv-360", range_=Video.Range.DV, height=360, bitrate=1_328_000, codec=H),
        make_video("sdr-2160", range_=Video.Range.SDR, height=2160, bitrate=21_501_000, codec=H),
        make_video("sdr-1080-avc", range_=Video.Range.SDR, height=1080, bitrate=10_793_000, codec=A),
        make_video("sdr-1080-hevc", range_=Video.Range.SDR, height=1080, bitrate=5_768_000, codec=H),
    ]


def ids(tracks: list[Video]) -> set[str]:
    return {t.id for t in tracks}


# ---------------------------------------------------------------------------
# select_hybrid
# ---------------------------------------------------------------------------


def test_select_hybrid_picks_base_per_resolution_and_lowest_dv(ladder: list[Video]) -> None:
    chosen = list(filter(Tracks().select_hybrid(ladder, [2160, 1080]), ladder))
    # HDR10+ base at each requested resolution, plus the single lowest DV ingredient.
    assert ids(chosen) == {"hdr10p-2160", "hdr10p-1080", "dv-360"}


def test_select_hybrid_ingredient_is_lowest_dv_regardless_of_quality(ladder: list[Video]) -> None:
    # Even when only 2160 is requested, the ingredient is the globally lowest DV.
    chosen = list(filter(Tracks().select_hybrid(ladder, [2160]), ladder))
    assert ids(chosen) == {"hdr10p-2160", "dv-360"}


def test_select_hybrid_prefers_hdr10p_over_hdr10() -> None:
    H = Video.Codec.HEVC
    tracks = [
        make_video("hdr10-2160", range_=Video.Range.HDR10, height=2160, bitrate=20_000_000, codec=H),
        make_video("hdr10p-2160", range_=Video.Range.HDR10P, height=2160, bitrate=20_000_000, codec=H),
        make_video("dv-360", range_=Video.Range.DV, height=360, bitrate=1_000_000, codec=H),
    ]
    chosen = list(filter(Tracks().select_hybrid(tracks, [2160]), tracks))
    assert ids(chosen) == {"hdr10p-2160", "dv-360"}


def test_select_hybrid_base_picks_highest_bitrate_then_worst_flips() -> None:
    H = Video.Codec.HEVC
    tracks = [
        make_video("hdr10p-2160-lo", range_=Video.Range.HDR10P, height=2160, bitrate=18_000_000, codec=H),
        make_video("hdr10p-2160-hi", range_=Video.Range.HDR10P, height=2160, bitrate=25_000_000, codec=H),
        make_video("dv-360", range_=Video.Range.DV, height=360, bitrate=1_000_000, codec=H),
    ]
    best = list(filter(Tracks().select_hybrid(tracks, [2160]), tracks))
    assert ids(best) == {"hdr10p-2160-hi", "dv-360"}

    worst = list(filter(Tracks().select_hybrid(tracks, [2160], worst=True), tracks))
    assert ids(worst) == {"hdr10p-2160-lo", "dv-360"}


def test_select_hybrid_no_dv_selects_only_base() -> None:
    H = Video.Codec.HEVC
    tracks = [make_video("hdr10p-2160", range_=Video.Range.HDR10P, height=2160, bitrate=20_000_000, codec=H)]
    chosen = list(filter(Tracks().select_hybrid(tracks, [2160]), tracks))
    assert ids(chosen) == {"hdr10p-2160"}


# ---------------------------------------------------------------------------
# merge_video_selections (the dedup fix)
# ---------------------------------------------------------------------------


def test_merge_dedups_shared_ingredient_and_deliverable() -> None:
    H = Video.Codec.HEVC
    dv = make_video("dv-1080", range_=Video.Range.DV, height=1080, bitrate=9_000_000, codec=H)
    hdr = make_video("hdr10p-2160", range_=Video.Range.HDR10P, height=2160, bitrate=20_000_000, codec=H)
    sdr = make_video("sdr-2160", range_=Video.Range.SDR, height=2160, bitrate=21_000_000, codec=H)

    # dv is both the hybrid ingredient and an explicit DV deliverable (same object).
    merged = Tracks.merge_video_selections([hdr, dv], [sdr, dv])
    assert [t.id for t in merged] == ["hdr10p-2160", "dv-1080", "sdr-2160"]


def test_merge_preserves_order_and_keeps_distinct_dv() -> None:
    H = Video.Codec.HEVC
    ingredient = make_video("dv-360", range_=Video.Range.DV, height=360, bitrate=1_000_000, codec=H)
    deliverable = make_video("dv-2160", range_=Video.Range.DV, height=2160, bitrate=25_000_000, codec=H)

    merged = Tracks.merge_video_selections([ingredient], [deliverable])
    assert [t.id for t in merged] == ["dv-360", "dv-2160"]


def test_merge_dedup_uses_track_identity_by_id() -> None:
    # Tracks compare equal by id; merge must treat same-id tracks as one.
    H = Video.Codec.HEVC
    a = make_video("same", range_=Video.Range.DV, height=1080, bitrate=9_000_000, codec=H)
    b = make_video("same", range_=Video.Range.DV, height=1080, bitrate=9_000_000, codec=H)
    assert a == b
    assert len(Tracks.merge_video_selections([a], [b])) == 1


# ---------------------------------------------------------------------------
# partition_hybrid_videos
# ---------------------------------------------------------------------------


def test_partition_hybrid_only_keeps_ingredients_out_of_pool(ladder: list[Video]) -> None:
    """`-r HYBRID`: HDR10+/DV are ingredients only; pool holds just SDR."""
    candidates, pool = Tracks.partition_hybrid_videos(ladder, [])
    assert ids(candidates) == {"hdr10p-2160", "hdr10p-1080", "dv-2160", "dv-1080", "dv-360"}
    assert ids(pool) == {"sdr-2160", "sdr-1080-avc", "sdr-1080-hevc"}


def test_partition_admits_hdr10p_to_pool_when_requested(ladder: list[Video]) -> None:
    """`-r HYBRID,HDR10P`: HDR10+ tracks become standalone deliverable candidates."""
    candidates, pool = Tracks.partition_hybrid_videos(ladder, [Video.Range.HDR10P])
    assert {"hdr10p-2160", "hdr10p-1080"} <= ids(pool)
    assert not any(t.range == Video.Range.DV for t in pool)
    # Candidates are unaffected by the requested ranges.
    assert ids(candidates) == {"hdr10p-2160", "hdr10p-1080", "dv-2160", "dv-1080", "dv-360"}


def test_partition_admits_dv_and_hdr10p_when_both_requested(ladder: list[Video]) -> None:
    """`-r HYBRID,HDR10P,DV`: both ranges enter the deliverable pool."""
    _, pool = Tracks.partition_hybrid_videos(ladder, [Video.Range.HDR10P, Video.Range.DV])
    assert {"hdr10p-2160", "hdr10p-1080", "dv-2160", "dv-1080", "dv-360"} <= ids(pool)


def test_partition_hdr10_requested_does_not_admit_hdr10p() -> None:
    H = Video.Codec.HEVC
    tracks = [
        make_video("hdr10-2160", range_=Video.Range.HDR10, height=2160, bitrate=20_000_000, codec=H),
        make_video("hdr10p-2160", range_=Video.Range.HDR10P, height=2160, bitrate=20_000_000, codec=H),
    ]
    _, pool = Tracks.partition_hybrid_videos(tracks, [Video.Range.HDR10])
    assert ids(pool) == {"hdr10-2160"}


# ---------------------------------------------------------------------------
# flag_hybrid_ingredients
# ---------------------------------------------------------------------------


def flagged(tracks: list[Video]) -> set[str]:
    return {t.id for t in tracks if t.hybrid_base_only}


def test_flag_hybrid_only_flags_base_and_ingredient_dv(ladder: list[Video]) -> None:
    """`-r HYBRID`: no deliverables, so the base and the ingredient DV are both
    skipped by the standalone mux loop — only the hybrid output remains."""
    hybrid_selected = list(filter(Tracks().select_hybrid(ladder, [1080]), ladder))
    Tracks.flag_hybrid_ingredients(hybrid_selected, [])
    assert flagged(ladder) == {"hdr10p-1080", "dv-360"}


def test_flag_hybrid_plus_hdr10p_keeps_base_deliverable(ladder: list[Video]) -> None:
    """`-r HYBRID,HDR10P`: the base is also an explicit deliverable, only the
    ingredient DV is skipped — hybrid + standalone HDR10+ are muxed."""
    hybrid_selected = list(filter(Tracks().select_hybrid(ladder, [1080]), ladder))
    base = next(t for t in ladder if t.id == "hdr10p-1080")
    Tracks.flag_hybrid_ingredients(hybrid_selected, [base])
    assert flagged(ladder) == {"dv-360"}


def test_flag_hybrid_plus_hdr10p_and_dv_keeps_both_deliverables(ladder: list[Video]) -> None:
    """`-r HYBRID,HDR10P,DV`: best DV is a deliverable, lowest DV stays
    ingredient-only — hybrid + HDR10+ + DV are muxed."""
    hybrid_selected = list(filter(Tracks().select_hybrid(ladder, [1080]), ladder))
    base = next(t for t in ladder if t.id == "hdr10p-1080")
    best_dv = next(t for t in ladder if t.id == "dv-1080")
    Tracks.flag_hybrid_ingredients(hybrid_selected, [base, best_dv])
    assert flagged(ladder) == {"dv-360"}


def test_flag_single_dv_rendition_as_deliverable_stays_unflagged() -> None:
    """`-r HYBRID,DV` with one DV rendition: the same track is ingredient and
    deliverable, so it must still be muxed standalone."""
    H = Video.Codec.HEVC
    tracks = [
        make_video("hdr10p-1080", range_=Video.Range.HDR10P, height=1080, bitrate=9_000_000, codec=H),
        make_video("dv-1080", range_=Video.Range.DV, height=1080, bitrate=9_000_000, codec=H),
    ]
    hybrid_selected = list(filter(Tracks().select_hybrid(tracks, [1080]), tracks))
    dv = next(t for t in tracks if t.id == "dv-1080")
    Tracks.flag_hybrid_ingredients(hybrid_selected, [dv])
    assert flagged(tracks) == {"hdr10p-1080"}


# ---------------------------------------------------------------------------
# documented end-state for the reported command
# ---------------------------------------------------------------------------


def test_hybrid_plus_dv_deliverable_end_state(ladder: list[Video]) -> None:
    """`-r SDR,HDR10P,DV,HYBRID -q 2160,1080`: hybrid base + lowest DV ingredient,
    de-duplicated against the best-DV-per-resolution deliverables and SDR."""
    quality = [2160, 1080]

    hybrid_selected = list(filter(Tracks().select_hybrid(ladder, quality), ladder))

    # Deliverables: best DV and SDR per requested resolution (what the dl Cartesian picks).
    def best_per_res(range_: Video.Range, codec: Video.Codec | None = None) -> list[Video]:
        out = []
        for res in quality:
            cands = [t for t in ladder if t.range == range_ and t.height == res and (codec is None or t.codec == codec)]
            if cands:
                out.append(max(cands, key=lambda t: t.bitrate))
        return out

    dv_deliverable = best_per_res(Video.Range.DV)
    sdr_deliverable = [t for t in ladder if t.range == Video.Range.SDR]

    final = Tracks.merge_video_selections(hybrid_selected, dv_deliverable + sdr_deliverable)

    # No duplicates, and the lowest DV ingredient coexists with the DV deliverables.
    assert len(final) == len({t.id for t in final})
    assert "dv-360" in ids(final)  # ingredient retained for hybrid build
    assert {"dv-2160", "dv-1080"} <= ids(final)  # standalone DV deliverables
    assert {"hdr10p-2160", "hdr10p-1080"} <= ids(final)
    assert {"sdr-2160", "sdr-1080-avc", "sdr-1080-hevc"} <= ids(final)
