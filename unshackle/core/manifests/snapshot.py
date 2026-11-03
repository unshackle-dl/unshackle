"""The manifest snapshot: the DASH and ISM parser state of a title's tracks, saved for --remote and export/import.

``capture`` saves each manifest tree as the service left it, with its filtered periods, plus a
one-track fragment for each track that the full tree cannot identify again. ``restore`` gives
that state back to tracks rebuilt somewhere else, where the service code does not run.
"""

from __future__ import annotations

import base64
import logging
import zlib
from copy import deepcopy
from typing import Any, Optional

from lxml import etree

from unshackle.core.manifests.dash import DASH, MissingLanguageError
from unshackle.core.manifests.ism import ISM
from unshackle.core.tracks import Audio, Subtitle, Tracks, Video
from unshackle.core.tracks.track import Track

log = logging.getLogger("snapshot")

Snapshot = dict[str, list[dict[str, Any]]]

# A peer sets the entry count, so one restore or parse is capped in total. The parsed tree takes about 30x the XML.
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024


def capture(tracks: Tracks, level: int = zlib.Z_DEFAULT_COMPRESSION) -> Snapshot:
    """The snapshot of these tracks, JSON-safe, at zlib ``level`` (0 gives an uncompressed zlib stream)."""
    counts: dict[int, int] = {}
    return {"manifests": whole_manifests(tracks, level, counts), "track_manifests": fragments(tracks, level, counts)}


def restore(tracks: Tracks, snapshot: Any, language: Any = None) -> None:
    """Fill ``track.data`` of each DASH or ISM track the snapshot holds; a track it does not hold is left as it is.

    ``language`` is the parse fallback when no track has a language of its own.
    """
    budget = [MAX_SNAPSHOT_BYTES]
    restore_fragments(tracks, entries(snapshot, "track_manifests"), budget)
    restore_manifests(tracks, entries(snapshot, "manifests"), budget, language)


def parse(snapshot: Any, url: str, language: Any = None) -> Optional[Tracks]:
    """Every track of the saved whole manifest for ``url``, with its filtered periods, else None.

    The URL also matches without its query, since a service can sign the URL it fetches. A saved
    manifest that does not parse gives None, so the caller can fetch the manifest instead.
    """
    saved = [m for m in entries(snapshot, "manifests") if isinstance(m.get("url"), str)]
    entry = next((m for m in saved if m["url"] == url), None) or next(
        (m for m in saved if m["url"].split("?")[0] == url.split("?")[0]), None
    )
    if entry is None:
        return None
    try:
        tracks = to_tracks(entry, language, [MAX_SNAPSHOT_BYTES])
    except MissingLanguageError:
        raise
    except Exception as e:
        log.warning("Failed to re-parse the saved %s manifest from %s: %s", entry.get("type"), url, e)
        return None
    return tracks if tracks is not None and list(tracks) else None


def fragment_ids(snapshot: Any) -> set[str]:
    """The ids of the tracks the snapshot holds a one-track fragment for."""
    return {str(e["track_id"]) for e in entries(snapshot, "track_manifests") if e.get("track_id") is not None}


def entries(snapshot: Any, key: str) -> list[dict[str, Any]]:
    """The dict entries under ``key``; a malformed snapshot gives none."""
    value = snapshot.get(key) if isinstance(snapshot, dict) else None
    return [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []


def pack(element: Any, level: int) -> str:
    xml_bytes = etree.tostring(element, xml_declaration=True, encoding="UTF-8")
    return base64.b64encode(zlib.compress(xml_bytes, level)).decode("ascii")


def unpack(data: str, budget: list[int]) -> Any:
    """The saved tree. ``budget[0]`` is the inflated byte count still allowed; this call takes its share."""
    from unshackle.core.api.compression import safe_inflate

    xml_bytes = safe_inflate(base64.b64decode(data), budget[0])
    budget[0] -= len(xml_bytes)
    return etree.fromstring(xml_bytes)


def counted(manifest: Any, tag: str, counts: dict[int, int]) -> int:
    """The number of ``tag`` elements in ``manifest``, counted once per tree."""
    if id(manifest) not in counts:
        counts[id(manifest)] = len(manifest.findall(tag))
    return counts[id(manifest)]


def whole_manifests(tracks: Tracks, level: int, counts: dict[int, int]) -> list[dict[str, Any]]:
    """One entry per manifest URL, and for DASH its filtered period ids.

    When tracks of one URL point at different trees (a one-track fragment restored on a remote
    client, or a tree a service built for one track), capture saves the tree with the most tracks.
    """
    best: dict[str, tuple[int, str, dict[str, Any]]] = {}
    for track in tracks:
        if not track.url:
            continue
        for kind, tag in (("dash", ".//Representation"), ("ism", ".//QualityLevel")):
            data = track.data.get(kind) or {}
            if data.get("manifest") is not None:
                break
        else:
            continue
        url = str(track.url)
        size = counted(data["manifest"], tag, counts)
        if url not in best or size > best[url][0]:
            best[url] = (size, kind, data)

    out: list[dict[str, Any]] = []
    for url, (_, kind, data) in best.items():
        entry: dict[str, Any] = {"type": kind, "url": url, "data": pack(data["manifest"], level)}
        if kind == "dash":
            entry["filtered_period_ids"] = list(data.get("filtered_period_ids") or [])
        out.append(entry)
    return out


def one_track_period(period: Any, adaptation_set: Any, representation: Any) -> Any:
    """A copy of ``period`` that holds only this AdaptationSet, cut to ``representation`` when there is one."""
    new_period = etree.Element("Period", dict(period.attrib) if period is not None else {})
    for child in period if period is not None else []:
        if child.tag != "AdaptationSet":
            new_period.append(deepcopy(child))
    new_adaptation_set = deepcopy(adaptation_set)
    if representation is not None:
        for sibling in new_adaptation_set.findall("Representation"):
            new_adaptation_set.remove(sibling)
        new_adaptation_set.append(deepcopy(representation))
    new_period.append(new_adaptation_set)
    return new_period


def fragments(tracks: Tracks, level: int, counts: dict[int, int]) -> list[dict[str, Any]]:
    """A one-track MPD for each track the client cannot find again in its whole manifest.

    A service can build an AdaptationSet of its own that never joins the manifest tree, or a
    manifest that holds only its own track, which an attribute match cannot tell apart. The
    fragment starts at the track's period and keeps the track's part of each content period
    after it, so the download still stitches them; the filtered periods are left out.
    """
    out: list[dict[str, Any]] = []
    for track in tracks:
        dash = track.data.get("dash") or {}
        manifest = dash.get("manifest")
        adaptation_set = dash.get("adaptation_set")
        if manifest is None or adaptation_set is None:
            continue
        if adaptation_set.getroottree().getroot() is manifest and counted(manifest, ".//Representation", counts) > 1:
            continue

        root = etree.Element("MPD", dict(manifest.attrib))
        for child in manifest:
            if child.tag != "Period":
                root.append(deepcopy(child))
        period = dash.get("period")
        representation = dash.get("representation")
        root.append(one_track_period(period, adaptation_set, representation))

        periods = manifest.findall("Period")
        if period is not None and period in periods:
            filtered = set(dash.get("filtered_period_ids") or [])
            rep_id = dash.get("representation_id") or (representation.get("id") if representation is not None else None)
            for later in periods[periods.index(period) + 1 :]:
                if not DASH.is_content_period(later, filtered):
                    continue
                found = DASH.resolve_representation(later, rep_id, adaptation_set)
                if found is not None:
                    root.append(one_track_period(later, *found))

        out.append(
            {
                "track_id": str(track.id),
                "type": "dash",
                "url": str(track.url) if track.url else None,
                "data": pack(root, level),
            }
        )
    return out


def restore_fragments(tracks: Tracks, saved: list[dict[str, Any]], budget: list[int]) -> None:
    """Fill ``track.data`` from the one-track fragments, by track id. This runs before the whole-manifest restore."""
    by_id = {str(t.id): t for t in tracks}
    for entry in saved:
        track = by_id.get(str(entry.get("track_id")))
        if track is None or entry.get("type") != "dash" or not isinstance(entry.get("data"), str):
            continue
        try:
            manifest = unpack(entry["data"], budget)
            period = manifest.find("Period")
            adaptation_set = period.find("AdaptationSet")
            representation = adaptation_set.find("Representation")
            if representation is None:
                raise ValueError("fragment has no Representation")
        except Exception as e:
            log.warning("Failed to re-parse the track fragment for track %s: %s", track.id, e)
            continue

        track.data["dash"] = {
            "manifest": manifest,
            "period": period,
            "adaptation_set": adaptation_set,
            "representation": representation,
            "representation_id": representation.get("id"),
        }
        track.descriptor = Track.Descriptor.DASH


def to_tracks(entry: dict[str, Any], language: Any, budget: list[int]) -> Optional[Tracks]:
    """The tracks of one whole-manifest entry, with the service's filtered periods dropped again."""
    root = unpack(entry["data"], budget)
    if entry.get("type") == "dash":
        skip = set(entry.get("filtered_period_ids") or [])
        return DASH(root, entry["url"]).to_tracks(
            language=language, period_filter=(lambda p: p.get("id") in skip) if skip else None
        )
    if entry.get("type") == "ism":
        return ISM(root, entry["url"]).to_tracks(language=language)
    return None


def has_data(track: Any) -> bool:
    return any(track.data.get(key) for key in ("dash", "ism"))


def restore_manifests(tracks: Tracks, saved: list[dict[str, Any]], budget: list[int], fallback: Any = None) -> None:
    """Re-parse each whole manifest and copy ``data`` to the tracks that match, by id or by attributes."""
    targets = [t for t in tracks if not has_data(t)]
    if not targets:
        return
    language = next((t.language for t in tracks if t.language and str(t.language) != "und"), fallback)
    parsed: list[Any] = []
    for entry in saved:
        if not isinstance(entry.get("data"), str) or not isinstance(entry.get("url"), str):
            continue
        try:
            local = to_tracks(entry, language, budget)
        except Exception as e:
            log.warning("Failed to re-parse %s manifest from %s: %s", entry.get("type"), entry.get("url"), e)
            continue
        if local is not None:
            parsed += list(local)

    for track in targets:
        matched = match_track(track, parsed)
        if matched and has_data(matched):
            track.data.update(matched.data)
            track.descriptor = matched.descriptor
            if matched.drm and not track.drm:
                track.drm = matched.drm


def same_bitrate(local: Optional[int], remote: Optional[int]) -> bool:
    """Equal to the nearest kb/s. A server that predates ``bitrate_bps`` rounds to kb/s in transit."""
    if not local or not remote:
        return not local and not remote
    return round(local / 1000) == round(remote / 1000)


def match_track(remote_track: Track, local_tracks: list) -> Optional[Track]:
    """Find the locally-parsed track that matches a remote track by ID or by attributes.

    HDR10 matches HDR10P, because the server's HDR10+ probe relabels a track that the local re-parse calls HDR10.
    """
    hdr10_ranges = {Video.Range.HDR10, Video.Range.HDR10P}
    remote_id = str(remote_track.id)
    for lt in local_tracks:
        if str(lt.id) == remote_id:
            return lt

    if not isinstance(remote_track, (Video, Audio, Subtitle)):
        return None
    for lt in local_tracks:
        if type(lt).__name__ != type(remote_track).__name__:
            continue
        if lt.codec != remote_track.codec or str(lt.language) != str(remote_track.language):
            continue
        if isinstance(remote_track, Video):
            if (
                lt.width == remote_track.width
                and lt.height == remote_track.height
                and (lt.range == remote_track.range or {lt.range, remote_track.range} == hdr10_ranges)
                and same_bitrate(lt.bitrate, remote_track.bitrate)
            ):
                return lt
        elif isinstance(remote_track, Audio):
            if same_bitrate(lt.bitrate, remote_track.bitrate):
                return lt
        elif isinstance(remote_track, Subtitle):
            if lt.forced == remote_track.forced and lt.sdh == remote_track.sdh:
                return lt
    return None
