from __future__ import annotations

import math
from typing import Any, Optional

import click
from click.core import ParameterSource
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from unshackle.core.config import config
from unshackle.core.console import console
from unshackle.core.services import Services
from unshackle.core.titles import Episode, Series
from unshackle.core.tracks import Audio, Video
from unshackle.core.utils.selector import Selector, tui_body, tui_confirm, tui_header, tui_prompt


class BitrateMatcher(int):
    """Match stream bitrates within a tolerance range."""

    __hash__ = int.__hash__

    def __new__(cls, target_bps: Any, tolerance: float = 0.25, *args: Any, **kwargs: Any) -> BitrateMatcher:
        base_val = target_bps[0] if isinstance(target_bps, list) else target_bps
        int_val = (base_val // 1000) if base_val else 0
        return super().__new__(cls, int_val)

    def __init__(self, target_bps: Any, tolerance: float = 0.25) -> None:
        self.target_bps = target_bps
        self.targets = target_bps if isinstance(target_bps, list) else [target_bps]
        self.tolerance = tolerance

    def __eq__(self, other: Any) -> bool:
        if other is None:
            return False
        try:
            val = float(other)
        except (ValueError, TypeError):
            return False
        # Engine compares `track.bitrate // 1000` (kbps) against this matcher; targets are bps, so normalise up.
        actual_bps = val if val > 100000 else val * 1000
        for target in self.targets:
            if target is None:
                continue
            if abs(actual_bps - target) <= (target * self.tolerance):
                return True
        return False

def run_service_selector() -> str:
    """Prompts for a service with the same Selector UI as --select-titles and returns the tag."""
    tags = [t for t in Services.get_tags() if t != "EXAMPLE"]

    if not tags:
        console.print("[bold red]Error: No active services found![/]")
        raise click.Abort()

    tui_header("Select Available Service", pad=(0, 2, 1, 2))

    selector = Selector(
        options=tags,
        cursor_style="cyan",
        page_size=min(len(tags), 15),
        single_select=True,
    )
    picked = selector.run()
    if not picked:
        raise click.Abort()
    return tags[picked[0]]

def parse_indices(input_str: str, max_val: int) -> list[int]:
    """Parses user input string (e.g. '1-3, 5') into 0-based indices."""
    indices = set()
    parts = input_str.replace(',', ' ').split()

    for part in parts:
        if '-' in part:
            try:
                nums = list(map(int, part.split('-')))
                # Support reversed ranges (e.g., '3-1')
                start, end = min(nums), max(nums)
                for i in range(start, end + 1):
                    if 1 <= i <= max_val:
                        indices.add(i - 1)
            except ValueError:
                continue
        else:
            try:
                i = int(part)
                if 1 <= i <= max_val:
                    indices.add(i - 1)
            except ValueError:
                continue
    return sorted(list(indices))


def get_standard_quality_tier(v: Any) -> int:
    """Maps heights to standard engine quality tiers."""
    h = getattr(v, 'height', 0)

    if h > 1500:
        return 2160
    if h > 800:
        return 1080
    if h > 600:
        return 720
    if h > 0:
        return 480

    return h

def run_service_extra_options(service_cmd: Any, ctx: Any) -> dict[str, Any]:
    service_kwargs = {}

    # Params the interactive engine manages globally; skip them here.
    global_engine_params = [
        'vcodec', 'video_codec', 'acodec', 'audio_codec',
        'quality', 'range_', 'title', 'url', 'help', 'interactive'
    ]

    params = getattr(service_cmd.cli, "params", [])
    if not params:
        return service_kwargs

    interactive_params = []
    for param in params:
        if not isinstance(param, click.Option) or param.name in global_engine_params:
            continue

        # Already given on the CLI; skip the prompt.
        source = ctx.get_parameter_source(param.name)
        if source == ParameterSource.COMMANDLINE:
            service_kwargs[param.name] = ctx.params.get(param.name)
            continue

        # Only options we can prompt for interactively.
        if isinstance(param.type, click.Choice) or param.is_flag or param.default is not None:
            interactive_params.append(param)

    if not interactive_params:
        return service_kwargs

    tui_header("Service Specific Options")

    msg = f"[bold cyan]{len(interactive_params)}[/] service-specific options available. Configure them?"
    if not tui_confirm(msg, default=False):
        return service_kwargs

    for param in interactive_params:
        param_label = param.name.replace('_', ' ').title()

        tui_header(param_label, pad=(1, 2, 0, 2))
        if param.help:
            tui_body(param.help)

        if isinstance(param.type, click.Choice):
            choices = list(param.type.choices)
            default_idx = choices.index(param.default) if param.default in choices else 0
            options = [f"{c}[dim] (default)[/]" if c == param.default else c for c in choices]

            selector = Selector(
                options=options,
                cursor_style="cyan",
                page_size=min(len(options), 8),
                single_select=True,
            )
            selector.cursor_index = default_idx

            picked = selector.run()
            chosen = picked[0] if picked else default_idx
            service_kwargs[param.name] = choices[chosen]

        elif param.is_flag:
            selector = Selector(options=[param_label], cursor_style="cyan", page_size=1)
            if param.default is True:
                selector.selected_indices.add(0)
            service_kwargs[param.name] = 0 in set(selector.run())

        else:
            service_kwargs[param.name] = tui_prompt(
                f"Enter [bold cyan]{param_label}[/]",
                default=str(param.default)
            )

    return service_kwargs

def run_interactive_session(
    service: Any, titles: Any, log: Any, current_params: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    int_cfg = getattr(config, "interactive", {})
    params = current_params or {}

    # Detect episodes explicitly; Movies is a list subclass and must not count as a series
    is_series = isinstance(titles, Series)
    if not is_series and isinstance(titles, (list, tuple)):
        is_series = any(isinstance(t, Episode) for t in titles)

    behavior_map = [
        ("Disable Muxing", "no_mux"),
        ("Forced Subtitles Only", "forced_subs"),
        ("Include Audio Description", "audio_description"),
        ("Skip Download (Retrieve Keys Only)", "skip_dl"),
        ("Dry Run (List Tracks Only)", "list_"),
        ("Export Session to JSON", "export")
    ]

    if is_series:
        behavior_map.append(("Latest Episode Only", "latest_episode"))

    selector = Selector(
        options=[opt[0] for opt in behavior_map],
        cursor_style="cyan",
        page_size=len(behavior_map)
    )

    # Config and CLI args override defaults
    for idx, (label, key) in enumerate(behavior_map):
        should_be_checked = False
        if int_cfg.get(key) is True:
            should_be_checked = True
        if params.get(key) is True:
            should_be_checked = True
        if should_be_checked:
            selector.selected_indices.add(idx)

    tui_header("Phase 1: Customizations")
    selected_indices = selector.run()

    if selected_indices is None:
        raise click.Abort()

    selections = {
        "quality": [],
        "vcodec": [],
        "range_": [],
        "vbitrate": None,
        "v_mode": "best",
        "acodec": [],
        "abitrate": None,
        "a_lang": [],
        "s_lang": [],
        "select_titles": True,
        "no_subs": False,
        "latest_episode": False,
    }

    for idx, (label, key) in enumerate(behavior_map):
        selections[key] = idx in selected_indices

    for label, key in behavior_map:
        icon = "[bold green]✓[/]" if selections[key] else "[dim] [/]"
        style = "bold green" if selections[key] else "dim"
        tui_body(f"{icon} [{style}]{label}[/]")

    try:
        is_multi_input = hasattr(titles, "__iter__") and len(titles) > 1

        if is_multi_input and not selections["latest_episode"]:
            tui_header("Phase 2: Reference Title Selection")

            season_data = {}
            for idx, t in enumerate(titles):
                s_num = getattr(t, 'season', 'Unknown')
                if s_num not in season_data:
                    season_data[s_num] = {"start": idx + 1, "count": 0}
                season_data[s_num]["count"] += 1

            milestones = []
            for s_num, data in season_data.items():
                start = data["start"]
                end = start + data["count"] - 1
                milestones.append(f"S{s_num:<2} [dim]({start}-{end})[/]")

            m_table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2))
            for _ in range(3):
                m_table.add_column(no_wrap=True)

            m_rows = math.ceil(len(milestones) / 3)
            for r in range(m_rows):
                row_cells = []
                for c in range(3):
                    idx = r + (c * m_rows)
                    if idx < len(milestones):
                        row_cells.append(milestones[idx])
                    else:
                        row_cells.append("")
                m_table.add_row(*row_cells)

            tui_body("[dim]Pick a number as a reference to preview tracks.[/]\n")
            tui_body(m_table)

            ref_idx = int(tui_prompt(
                "\nEnter Reference Index",
                choices=[str(i) for i in range(1, len(titles) + 1)],
                default="1",
                show_choices=False
            )) - 1
            target = titles[ref_idx]
            header_text = f"Track Info: {target}"
        else:
            if is_multi_input and selections["latest_episode"]:
                target = titles[-1]
            else:
                target = titles[0] if hasattr(titles, "__iter__") else titles
            header_text = f"Track Info: {target}"

        tui_header(header_text)
        with console.status("[bold cyan]Fetching metadata...[/]"):
            fetched = service.get_tracks(target)
            if fetched:
                if not hasattr(target, 'tracks') or target.tracks is None:
                    target.tracks = fetched
                else:
                    target.tracks.add(fetched, warn_only=True)

        tui_header("Phase 3: Video Filters", pad=(0, 2, 1, 2))
        v_pool = target.tracks.videos

        codecs = list(Video.Codec)
        codec_options = [f"[bold white] 1. Any / Default [/] [[bold white]{len(v_pool):>3}[/]]"]
        for idx, c in enumerate(codecs, 2):
            count = len([v for v in v_pool if v.codec == c])
            style = "white" if count > 0 else "dim"
            label = f"{c.name} ({c.value})"
            codec_options.append(f"[{style}]{idx:2}. {label:<14}[/] [[bold]{count:>3}[/]]")

        c_table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 1))
        for _ in range(2):
            c_table.add_column()
        c_rows = math.ceil(len(codec_options) / 2)
        for r in range(c_rows):
            row_cells = []
            for c in range(2):
                idx = r + (c * c_rows)
                if idx < len(codec_options):
                    row_cells.append(codec_options[idx])
                else:
                    row_cells.append("")
            c_table.add_row(*row_cells)

        tui_body("[bold cyan]Select Video Codec Filter:[/]")
        tui_body(c_table)
        f_idx_str = tui_prompt("\nSelect Codec Index", choices=[str(i) for i in range(1, len(codec_options) + 1)], default="1", show_choices=False)
        if f_idx_str != "1":
            selected_codec = codecs[int(f_idx_str) - 2]
            selections["vcodec"] = [selected_codec]
            service.track_request.codecs = selections["vcodec"]
            # Narrow the pool for the range filter.
            v_pool = [v for v in v_pool if v.codec == selected_codec]

        ranges = list(Video.Range)
        range_options = [f"[bold white] 1. Any / Default [/] [[bold white]{len(v_pool):>3}[/]]"]
        for idx, r in enumerate(ranges, 2):
            count = len([v for v in v_pool if v.range == r])
            style = "white" if count > 0 else "dim"
            label = r.name
            range_options.append(f"[{style}]{idx:2}. {label:<14}[/] [[bold]{count:>3}[/]]")

        r_table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 1))
        for _ in range(2):
            r_table.add_column()
        r_rows = math.ceil(len(range_options) / 2)
        for r in range(r_rows):
            row_cells = []
            for c in range(2):
                idx = r + (c * r_rows)
                if idx < len(range_options):
                    row_cells.append(range_options[idx])
                else:
                    row_cells.append("")
            r_table.add_row(*row_cells)

        tui_body("\n[bold cyan]Select Dynamic Range Filter:[/]")
        tui_body(r_table)
        r_idx_str = tui_prompt(
            "\nSelect Range Index",
            choices=[str(i) for i in range(1, len(range_options) + 1)],
            default="1",
            show_choices=False
        )
        if r_idx_str != "1":
            selected_range = ranges[int(r_idx_str) - 2]
            selections["range_"] = [selected_range]

        # Dry run still needs codec/range to drive the request; dl lists the tracks itself.
        if selections.get("list_"):
            if selections.get("vcodec"):
                service.track_request.codecs = selections["vcodec"]
            if selections.get("range_"):
                service.track_request.ranges = selections["range_"]
            return selections

        # get_tracks is request-gated, so re-fetch for the chosen codec/range
        req_codecs = selections.get("vcodec")
        req_ranges = selections.get("range_")
        if req_codecs or req_ranges:
            prev_tracks = target.tracks
            service.track_request.codecs = req_codecs or []
            service.track_request.ranges = req_ranges or list(Video.Range)
            with console.status("[bold cyan]Refetching tracks for selected codec/range...[/]"):
                refetched = service.get_tracks(target)
            if refetched:
                target.tracks = refetched
            # Fall back to the pre-refetch pool if nothing matched
            check_v = target.tracks.videos
            if req_codecs:
                check_v = [v for v in check_v if v.codec in req_codecs]
            if req_ranges:
                check_v = [v for v in check_v if v.range in req_ranges]
            if not check_v:
                console.print("[yellow]No video tracks matched that codec/range; showing what was available.[/]")
                target.tracks = prev_tracks

        display_v = target.tracks.videos
        if selections.get("vcodec"):
            display_v = [v for v in display_v if v.codec in selections["vcodec"]]
        if selections.get("range_"):
            display_v = [v for v in display_v if v.range in selections["range_"]]

        v_tree = Tree("\n[bold cyan]Available Video Tracks:[/]", guide_style="dim")
        range_priority = {"SDR": 0, "HDR10": 1, "DV": 2}
        codec_priority = {"AVC": 0, "HEVC": 1, "AV1": 2}

        def get_range_val(t: Any) -> str:
            if getattr(t, 'dv', False):
                return "DV"
            if getattr(t, 'hdr10', False):
                return "HDR10"
            return str(t.range).split('.')[-1]

        def get_codec_val(t: Any) -> str:
            return str(t.codec).split('.')[-1]

        # Sort by: Height -> Width -> Range priority -> Codec priority -> Bitrate
        display_v = sorted(display_v, key=lambda x: (
            x.height or 0,
            x.width or 0,
            range_priority.get(get_range_val(x), 0),
            codec_priority.get(get_codec_val(x), 9),
            x.bitrate or 0
        ))

        codec_colors = {"AVC": "green", "HEVC": "yellow", "AV1": "cyan"}
        range_colors = {"SDR": "dim", "HDR10": "bold orange1", "DV": "bold magenta"}

        last_tier = None
        current_branch = v_tree

        for i, t in enumerate(display_v, 1):
            current_tier = f"{t.width}x{t.height}" if t.width else f"{t.height}p"
            if current_tier != last_tier:
                current_branch = v_tree.add(f"[bold rgb(21,131,209)]── {current_tier} ──[/]")
                last_tier = current_tier

            codec_name = get_codec_val(t)
            c_style = codec_colors.get(codec_name, "white")
            v_range = get_range_val(t)
            r_style = range_colors.get(v_range, "white")
            fps_str = f", {t.fps:.3f} FPS" if getattr(t, 'fps', None) else ""
            bitrate_str = f"{t.bitrate//1000:>5}kbps" if t.bitrate else "  VBR"

            label = Text.assemble(
                (f" {i:3} - ", "white"),
                ("[", "bold white"),
                (f"{codec_name:<4}", c_style),
                (" | ", "bold white"),
                (f"{v_range:<5}", r_style),
                ("]", "bold white"),
                (f" @ {bitrate_str}{fps_str}", "white")
            )
            current_branch.add(label)

        if not display_v:
            tui_body("[yellow]No video tracks available after filtering; skipping video selection.[/]")
        else:
            tui_body(v_tree)

            v_choices = [str(i) for i in range(1, len(display_v) + 1)]
            v_idx = int(tui_prompt(
                "\nSelect Video Index", choices=v_choices, default="1", show_choices=False
            )) - 1
            v_sel = display_v[v_idx]

            is_single_output = not is_multi_input or selections["latest_episode"]

            # Map the selected track's real height to a dl.py quality tier
            selections["vcodec"] = [v_sel.codec]
            selections["range_"] = [v_sel.range]
            if v_sel.height:
                selections["quality"] = [get_standard_quality_tier(v_sel)]

            if is_single_output:
                selections["v_mode"] = "exact"
                if v_sel.bitrate:
                    selections["vbitrate"] = BitrateMatcher(v_sel.bitrate, tolerance=0.05)
            else:
                # Determine profile position for batch mode stability
                profile_tracks = sorted(
                    [
                        v for v in display_v
                        if v.height == v_sel.height and v.width == v_sel.width
                        and v.codec == v_sel.codec and v.range == v_sel.range
                    ],
                    key=lambda x: x.bitrate or 0,
                    reverse=True
                )

                selections["vbitrate"] = None  # Prevent strict bitrate matching in batch mode
                if profile_tracks and (v_sel.bitrate or 0) == (profile_tracks[0].bitrate or 0):
                    selections["v_mode"] = "best"
                else:
                    selections["v_mode"] = "worst"

        tui_header("Phase 5: Audio Filters")

        a_pool = target.tracks.audio

        # Drop descriptive tracks unless requested.
        if not selections.get("audio_description"):
            a_pool = [a for a in a_pool if not getattr(a, 'descriptive', False)]

        a_codecs = list(Audio.Codec)
        a_codec_options = [f"[bold white] 1. Any / Default [/] [[bold white]{len(a_pool):>3}[/]]"]

        for idx, c in enumerate(a_codecs, 2):
            count = len([a for a in a_pool if a.codec == c])
            style = "white" if count > 0 else "dim"
            label = f"{c.name} ({c.value})"
            a_codec_options.append(f"[{style}]{idx:2}. {label:<14}[/] [[bold]{count:>3}[/]]")

        ac_table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 1))
        for _ in range(2):
            ac_table.add_column()

        ac_rows = math.ceil(len(a_codec_options) / 2)
        for r in range(ac_rows):
            row_cells = []
            for c in range(2):
                idx = r + (c * ac_rows)
                if idx < len(a_codec_options):
                    row_cells.append(a_codec_options[idx])
                else:
                    row_cells.append("")
            ac_table.add_row(*row_cells)

        tui_body("[bold green]Select Audio Codec Filter:[/]")
        tui_body(ac_table)

        af_idx_str = tui_prompt("\nSelect Audio Codec Index", choices=[str(i) for i in range(1, len(a_codec_options) + 1)], default="1", show_choices=False)
        if af_idx_str != "1":
            selected_a_codec = a_codecs[int(af_idx_str) - 2]
            selections["acodec"] = [selected_a_codec]
            a_pool = [a for a in a_pool if a.codec == selected_a_codec]

        if a_pool:
            display_a = a_pool

            # Sort by: Original language first -> Language alphabetical -> Channels -> Bitrate
            display_a = sorted(display_a, key=lambda x: (
                not getattr(x, 'is_original_lang', False),
                str(x.language),
                float(x.channels or 0),
                x.bitrate or 0
            ))

            a_tree = Tree("\n[bold green]Available Audio Tracks:[/]", guide_style="dim")
            last_lang = None
            current_branch = a_tree

            for i, t in enumerate(display_a, 1):
                lang_str = str(t.language).upper()

                if lang_str != last_lang:
                    is_orig = getattr(t, 'is_original_lang', False)
                    orig_tag = " [yellow](Original)[/]" if is_orig else ""
                    current_branch = a_tree.add(f"[bold rgb(21,131,209)]── {lang_str}{orig_tag} ──[/]")
                    last_lang = lang_str

                codec_label = t.codec.value if hasattr(t.codec, 'value') else str(t.codec).split('.')[-1]

                tags = []
                if getattr(t, 'atmos', False):
                    tags.append("Atmos")
                if getattr(t, 'descriptive', False):
                    tags.append("AD")

                try:
                    channels_str = f"{float(t.channels):.1f}" if t.channels is not None else "2.0"
                except (ValueError, TypeError):
                    channels_str = "2.0"

                parts = [
                    (f" {i:3} - ", "white"),
                    ("[", "bold white"),
                    (f"{codec_label:<4}", "cyan"),
                    ("]", "bold white"),
                    (f" {channels_str} ch | {t.bitrate//1000 if t.bitrate else 'VBR'}kbps", "white")
                ]
                if tags:
                    parts.append((f" ({', '.join(tags)})", "dim"))

                current_branch.add(Text.assemble(*parts))

            tui_body(a_tree)

            a_input = tui_prompt("\nSelect Audio indices (eg.: 2 6-9)", default="1")
            a_idxs = parse_indices(a_input, len(display_a))

            selected_raw = [display_a[i] for i in a_idxs]
            unique_map = {}
            seen_combos = set()

            for t in selected_raw:
                # Deduplicate based on base language (e.g., prevent es-419 vs es-ES collisions)
                lang_base = str(t.language).split('-')[0].split('_')[0].lower()
                combo = (lang_base, t.bitrate)

                if combo not in seen_combos:
                    unique_map[combo] = t
                    seen_combos.add(combo)
                else:
                    # Prioritize descriptive audio tracks over standard duplicate requests
                    if getattr(t, 'descriptive', False) and not getattr(unique_map[combo], 'descriptive', False):
                        unique_map[combo] = t
                        log.info(f"Prioritizing AD track for {t.language}")
                    else:
                        log.warning(f"Skipping duplicate audio request to prevent engine crash: {t.language}")

            unique_tracks = list(unique_map.values())
            selections["a_lang"] = [str(t.language) for t in unique_tracks]
            selections["acodec"] = list(set(t.codec for t in unique_tracks))

            if any(getattr(t, 'descriptive', False) for t in unique_tracks):
                selections["audio_description"] = True

            a_bitrates = [t.bitrate for t in unique_tracks if getattr(t, 'bitrate', None)]
            if a_bitrates:
                selections["abitrate"] = BitrateMatcher(list(set(a_bitrates)))

        # Phase 6: Subtitles
        if not selections["skip_dl"] and target.tracks.subtitles:
            display_s = sorted(target.tracks.subtitles, key=lambda x: str(x.language))

            tui_header("Phase 6: Available Subtitle Tracks")

            s_table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2))
            for _ in range(3):
                s_table.add_column(no_wrap=True)

            items = []
            for i, t in enumerate(display_s, 1):
                tags = []
                if getattr(t, 'forced', False):
                    tags.append("Forced")
                if getattr(t, 'sdh', False) or getattr(t, 'cc', False):
                    tags.append("SDH")

                tag_str = ""
                if tags:
                    tag_str = f" [dim]({', '.join(tags)})[/]"
                items.append(f"{i:2} - {t.language}{tag_str}")

            s_rows = math.ceil(len(items) / 3)
            for r in range(s_rows):
                row_cells = []
                for c in range(3):
                    idx = r + (c * s_rows)
                    if idx < len(items):
                        row_cells.append(items[idx])
                    else:
                        row_cells.append("")
                s_table.add_row(*row_cells)

            tui_body(s_table)

            s_input = tui_prompt("\nSelect Subtitle indices (eg.: 2 6-9 or ENTER for none)", default="")
            if s_input:
                s_idxs = parse_indices(s_input, len(display_s))
                for i in s_idxs:
                    if getattr(display_s[i], 'forced', False):
                        selections["forced_subs"] = True
                selections["s_lang"] = list(set(str(display_s[i].language) for i in s_idxs))

        selections["v_lang"] = ["all"]
        if not selections["a_lang"]:
            selections["a_lang"] = ["all"]

        selections["no_subs"] = not bool(selections["s_lang"])

        if selections.get("latest_episode", False):
            selections["select_titles"] = False

        return selections

    except Exception:
        console.print_exception()
        raise click.Abort()
