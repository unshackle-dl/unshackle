"""Shared rich-based rendering helpers for music releases.

Data-in / renderable-out: services do their own quality and field extraction,
then hand plain data here. The helpers know nothing about how a ``quality_label``
was derived; they only style and lay it out. Quality-schema parsing stays
per-service while panel/header/artwork rendering lives in one place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Optional

# RenderableType is rich's union of "things that can be printed to a console".
from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from unshackle.core.titles.music import Song


@dataclass
class TrackRow:
    """One row in the track tree, already formatted by the calling service.

    When ``note`` is set the row is treated as unavailable: the quality label is
    styled red, ``note`` is appended muted, and the ``cd``/``hires`` badges are
    suppressed. Otherwise the normal detail line is built and ``cd`` drives a
    "CD" badge.
    """

    song: Song
    quality_label: str
    layout: str
    duration_str: str
    hires: bool = False
    cd: bool = False
    note: str = ""


@dataclass
class MusicHeaderInfo:
    """Plain data holder for the release-header metadata grid."""

    artist: str
    album: str
    year: str
    track_count: int
    quality_label: str
    duration_str: str = ""
    artist_label: str = "Artist"


# Quality strings look like "FLAC 16-bit/44.1kHz"; split the codec token off the
# front and bracket it so the detail line reads "[FLAC] | 16-bit/44.1kHz".
QUALITY_DETAIL_RE = re.compile(r"^(FLAC|OGG|AAC|MP3)\s+(.+)$", flags=re.IGNORECASE)


def format_track_detail_quality(quality_label: str) -> str:
    """Bracket the leading codec token of a quality string for the detail line."""
    text = str(quality_label or "").strip()
    match = QUALITY_DETAIL_RE.match(text)
    if match:
        return f"[{match.group(1).upper()}] | {match.group(2).strip()}"
    return text


def render_artwork_preview(
    session: Any,
    artwork_url: str,
    *,
    width: int = 25,
) -> Optional[RenderableType]:
    """Fetch artwork via ``session`` and render it as half-block coloured cells.

    Pillow (``PIL``) is an optional dependency; any error (missing PIL, network,
    bad image, zero dimensions) soft-fails to ``None`` so callers skip the preview.
    """
    if not artwork_url:
        return None
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        response = session.get(artwork_url, timeout=20)
        response.raise_for_status()
        image = Image.open(BytesIO(response.content)).convert("RGB")
        if not image.width or not image.height:
            return None

        height = max(1, int(width * image.height / image.width * 0.5))
        # Image.Resampling exists on Pillow >= 9.1; fall back gracefully on older.
        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
        image = image.resize((width, height * 2), resampling)

        lines = []
        for y in range(0, image.height, 2):
            line = Text()
            for x in range(image.width):
                top = image.getpixel((x, y))
                bottom = image.getpixel((x, min(y + 1, image.height - 1)))
                top_color = f"#{top[0]:02x}{top[1]:02x}{top[2]:02x}"
                bottom_color = f"#{bottom[0]:02x}{bottom[1]:02x}{bottom[2]:02x}"
                # Lower-half block fg=bottom/bg=top packs two pixel rows per cell.
                line.append("▄", style=f"{bottom_color} on {top_color}")
            lines.append(line)
        return Group(*lines)
    except Exception:
        return None


def render_track_panel(rows: list[TrackRow], total: int) -> Panel:
    """Render the per-track tree (one node + detail line each) in a Panel."""
    track_label = "Track" if total == 1 else "Tracks"
    tree = Tree(f"[repr.number]{total}[/] {track_label}", guide_style="bright_black")
    for row in rows:
        title_line = Text(f"{row.song.track:02}", style="repr.number")
        title_line.append("   ")
        title_line.append(row.song.name, style="bold #009900")
        node = tree.add(title_line, guide_style="bright_black")

        detail = Text()
        if row.note:
            detail.append(row.quality_label, style="red")
            detail.append(" ")
            detail.append(row.note, style="bright_black")
        else:
            detail.append(format_track_detail_quality(row.quality_label))
            if row.layout:
                detail.append(" | ")
                detail.append(row.layout)
            if row.duration_str:
                detail.append(" | ")
                detail.append(row.duration_str)
            if row.cd:
                detail.append(" | ")
                detail.append("CD", style="yellow1")
            if row.hires:
                detail.append(" | ")
                detail.append("Hi-Res", style="gold1")
        node.add(detail, guide_style="bright_black")
    return Panel(tree, title="Available Tracks")


def render_album_header(
    info: MusicHeaderInfo,
    artwork: Optional[RenderableType] = None,
) -> Optional[RenderableType]:
    """Render the release metadata grid, optionally placing artwork beside it."""
    # Table.grid is a borderless table used purely for column alignment.
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="orchid1", no_wrap=True)
    grid.add_column()
    grid.add_row(info.artist_label, Text(info.artist, style="bold #ff0000"))
    grid.add_row("Collection", Text(info.album, style="blue"))
    grid.add_row("Year", Text(info.year, style="blue"))
    grid.add_row("Tracks", Text(str(info.track_count), style="blue"))
    grid.add_row("Quality", Text(info.quality_label, style="blue"))
    if info.duration_str:
        grid.add_row("Length", Text(info.duration_str, style="blue"))

    if not artwork:
        return grid

    header = Table.grid(expand=True, padding=(0, 2))
    header.add_column(no_wrap=True)
    header.add_column(ratio=1)
    header.add_row(artwork, Padding(grid, (3, 0, 0, 0)))
    return header
