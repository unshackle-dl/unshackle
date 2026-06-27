import sys
import math
import click
from click.core import ParameterSource
from typing import List, Dict, Any, Optional

from rich.tree import Tree
from rich.text import Text
from rich.prompt import Prompt, Confirm
from rich.rule import Rule
from rich.table import Table
from rich.padding import Padding

from unshackle.core.console import console
from unshackle.core.tracks import Video, Audio
from unshackle.core.config import config
from unshackle.core.utils.selector import Selector
from unshackle.core.services import Services
from unshackle.core.titles import Series

class BitrateMatcher(int):
    """Utility class to match stream bitrates within a specified tolerance range."""
    def __new__(cls, target_bps, tolerance=0.25, *args, **kwargs):
        base_val = target_bps[0] if isinstance(target_bps, list) else target_bps
        return super(BitrateMatcher, cls).__new__(cls, base_val // 1000)

    def __init__(self, target_bps: int, tolerance=0.25):
        self.target_bps = target_bps
        self.targets = target_bps if isinstance(target_bps, list) else [target_bps]
        self.tolerance = tolerance

    def __eq__(self, other):
        if other is None:
            return False
        try:
            val = float(other)
            # Normalize scale differences (bps vs. kbps)
            actual_bps = val if val > 100000 else val * 1000
            
            for target in self.targets:
                if abs(actual_bps - target) <= (target * self.tolerance):
                    return True
            return False
        except (ValueError, TypeError):
            return False

def run_service_selector() -> str:
    """Displays a numbered list of available services and returns the selected tag."""
    tags = [t for t in Services.get_tags() if t != "EXAMPLE"]
    
    if not tags:
        console.print("[bold red]Error: No active services found![/]")
        sys.exit(1)

    console.print(Padding(Rule("[rule.text]Select Available Service"), (0, 2, 1, 2)))
    
    num_tags = len(tags)
    num_cols = 4
    num_rows = math.ceil(num_tags / num_cols)
    
    table = Table(show_header=False, box=None, padding=(0, 2))
    for _ in range(num_cols):
        table.add_column()

    for r in range(num_rows):
        row_cells = []
        for c in range(num_cols):
            # Map index to column-major order (vertical sorting)
            idx = r + (c * num_rows)
            if idx < num_tags:
                row_cells.append(f"[bold magenta]{idx + 1:2}[/] - {tags[idx]}")
            else:
                row_cells.append("")
        table.add_row(*row_cells)
    
    console.print(table)
    
    idx_str = Prompt.ask(
        "\n  [bold]Select Service Index[/]", 
        choices=[str(i) for i in range(1, num_tags + 1)], 
        show_choices=False
    )
    console.print()
    return tags[int(idx_str) - 1]

def parse_indices(input_str: str, max_val: int) -> List[int]:
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

def run_service_extra_options(service_cmd: Any, ctx: Any) -> Dict[str, Any]:
    service_kwargs = {}
    
    # Exclude parameters that are managed globally by the interactive engine
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
        
        # Bypass interactive prompt if option was already specified in the CLI
        source = ctx.get_parameter_source(param.name)
        if source == ParameterSource.COMMANDLINE:
            service_kwargs[param.name] = ctx.params.get(param.name)
            continue

        # Filter for options suitable for interactive prompt configuration
        if isinstance(param.type, click.Choice) or param.is_flag or param.default is not None:
            interactive_params.append(param)

    if not interactive_params:
        return service_kwargs

    # Prompt user whether to configure service-specific options
    console.print(Padding(Rule("[rule.text]Service Specific Options"), (0, 2, 1, 2)))
    
    msg = f"  [bold cyan]{len(interactive_params)}[/] service-specific options available. Configure them?"
    if not Confirm.ask(msg, default=False):
        return service_kwargs

    # Interactive options setup
    for param in interactive_params:
        param_label = param.name.replace('_', ' ').title()
        
        if param.help:
            console.print(f"\n  [bold yellow]󰋖[/] [white]{param.help}[/]")

        # Handle Click Choice options
        if isinstance(param.type, click.Choice):
            choices = param.type.choices
            for i, choice in enumerate(choices, 1):
                is_default = " [dim](default)[/]" if choice == param.default else ""
                console.print(f"    [bold magenta]{i:2}[/] - {choice}{is_default}")
            
            default_idx = "1"
            if param.default in choices:
                default_idx = str(choices.index(param.default) + 1)

            idx_str = Prompt.ask(
                f"\n  Select [bold cyan]{param_label}[/] Index",
                choices=[str(i) for i in range(1, len(choices) + 1)],
                default=default_idx,
                show_choices=False
            )
            service_kwargs[param.name] = choices[int(idx_str) - 1]

        # Handle Boolean flags
        elif param.is_flag:
            service_kwargs[param.name] = Confirm.ask(
                f"  Enable [bold cyan]{param_label}[/]?",
                default=param.default
            )

        # Handle standard text/numeric options with a default value
        else:
            service_kwargs[param.name] = Prompt.ask(
                f"  Enter [bold cyan]{param_label}[/]",
                default=str(param.default)
            )
                
    return service_kwargs

def run_interactive_session(service: Any, titles: Any, log: Any, current_params: Dict[str, Any] = None) -> Dict[str, Any]:
    int_cfg = getattr(config, "interactive", {})
    params = current_params or {}
    
    is_series = isinstance(titles, (Series, list)) and not hasattr(titles, 'name')
    
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
    
    # Enforce priority: configuration settings and CLI arguments override defaults
    for idx, (label, key) in enumerate(behavior_map):
        should_be_checked = False
        if int_cfg.get(key) is True:
            should_be_checked = True
        if params.get(key) is True:
            should_be_checked = True
        if should_be_checked:
            selector.selected_indices.add(idx)
    
    console.print(Padding(Rule("[rule.text]Phase 1: Customizations"), (1, 2, 1, 2)))
    selected_indices = selector.run()
    
    if selected_indices is None:
        sys.exit(0)
    
    selections = {
        "quality": [], 
        "vcodec": [], 
        "range_": [], 
        "vbitrate": None, 
        "v_mode": "best",
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
        console.print(f"  {icon} [{style}]{label}[/]")

    # Early exit if dry run (list-only) mode is enabled
    if selections.get("list_"):
        return selections

    try:
        # Phase 2: Reference title selection for track previewing
        is_multi_input = hasattr(titles, "__iter__") and len(titles) > 1
        
        if is_multi_input and not selections["latest_episode"]:
            console.print(Padding(Rule("[rule.text]Phase 2: Reference Title Selection"), (1, 2, 1, 2)))
            
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
                milestones.append(f"  S{s_num:<2} [dim]({start}-{end})[/]")

            m_table = Table(show_header=False, box=None, padding=(0, 2))
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
            
            console.print("  [dim]Pick a number as a reference to preview tracks.[/]\n")
            console.print(m_table)
            
            ref_idx = int(Prompt.ask(
                "\n  Enter Reference Index", 
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

        # Fetch metadata to populate track selection options
        console.print(Padding(Rule(header_text), (1, 2, 1, 2)))
        with console.status("[bold cyan]Fetching metadata...[/]"):
            fetched = service.get_tracks(target)
            if fetched:
                if not hasattr(target, 'tracks') or target.tracks is None:
                    target.tracks = fetched
                else:
                    target.tracks.add(fetched, warn_only=True)

        # Phase 3: Video filtering based on available codecs and dynamic range
        console.print(Padding(Rule("[rule.text]Phase 3: Video Filters"), (0, 2, 1, 2)))
        v_pool = target.tracks.videos

        # Filter by video codec
        codecs = list(Video.Codec)
        codec_options = [f"[bold white]  1. Any / Default [/] [[bold white]{len(v_pool):>3}[/]]"]
        for idx, c in enumerate(codecs, 2):
            count = len([v for v in v_pool if v.codec == c])
            style = "white" if count > 0 else "dim"
            label = f"{c.name} ({c.value})"
            codec_options.append(f"[{style}] {idx:2}. {label:<14}[/] [[bold]{count:>3}[/]]")
            
        c_table = Table(show_header=False, box=None, padding=(0, 1))
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
            
        console.print(" [bold cyan]Select Video Codec Filter:[/]")
        console.print(c_table)
        f_idx_str = Prompt.ask("\n  Select Codec Index", choices=[str(i) for i in range(1, len(codec_options) + 1)], default="1", show_choices=False)
        if f_idx_str != "1":
            selected_codec = codecs[int(f_idx_str) - 2]
            selections["vcodec"] = [selected_codec]
            service.track_request.codecs = selections["vcodec"]
            # Narrow down video pool for subsequent range filters
            v_pool = [v for v in v_pool if v.codec == selected_codec]

        # Filter by video dynamic range
        ranges = list(Video.Range)
        range_options = [f"[bold white]  1. Any / Default [/] [[bold white]{len(v_pool):>3}[/]]"]
        for idx, r in enumerate(ranges, 2):
            count = len([v for v in v_pool if v.range == r])
            style = "white" if count > 0 else "dim"
            label = r.name
            range_options.append(f"[{style}] {idx:2}. {label:<14}[/] [[bold]{count:>3}[/]]")

        r_table = Table(show_header=False, box=None, padding=(0, 2))
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
            
        console.print("\n [bold cyan]Select Dynamic Range Filter:[/]")
        console.print(r_table)
        r_idx_str = Prompt.ask(
            "\n  Select Range Index", 
            choices=[str(i) for i in range(1, len(range_options) + 1)], 
            default="1", 
            show_choices=False
        )
        if r_idx_str != "1":
            selected_range = ranges[int(r_idx_str) - 2]
            selections["range_"] = [selected_range]

        # Phase 4: Video track selection
        display_v = target.tracks.videos
        if selections.get("vcodec"):
            display_v = [v for v in display_v if v.codec in selections["vcodec"]]
        if selections.get("range_"):
            display_v = [v for v in display_v if v.range in selections["range_"]]

        v_tree = Tree("\n[bold cyan]Available Video Tracks:[/]", guide_style="dim")
        range_priority = {"SDR": 0, "HDR10": 1, "DV": 2}
        codec_priority = {"AVC": 0, "HEVC": 1, "AV1": 2}

        def get_range_val(t):
            if getattr(t, 'dv', False): return "DV"
            if getattr(t, 'hdr10', False): return "HDR10"
            return str(t.range).split('.')[-1]

        def get_codec_val(t):
            return str(t.codec).split('.')[-1]

        # Sort by: Height -> Width -> Range priority -> Codec priority -> Bitrate
        display_v = sorted(display_v, key=lambda x: (
            x.height, 
            x.width, 
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
            
            label = Text.assemble(
                (f" {i:3} - ", "white"), 
                ("[", "bold white"), 
                (f"{codec_name:<4}", c_style),
                (" | ", "bold white"), 
                (f"{v_range:<5}", r_style), 
                ("]", "bold white"),
                (f" @ {t.bitrate//1000:>5}kbps{fps_str}", "white")
            )
            current_branch.add(label)
        
        console.print(v_tree)
        
        v_idx = int(Prompt.ask("\n  Select Video Index", default="1")) - 1
        v_sel = display_v[v_idx]
        
        is_single_output = not is_multi_input or selections["latest_episode"]
        
        # Calculate dl.py-compatible quality resolution height mapping
        magic_quality = int(v_sel.width * 9 / 16)
        
        selections["vcodec"] = [v_sel.codec]
        selections["range_"] = [v_sel.range]
        selections["quality"] = [magic_quality]

        if is_single_output:
            selections["v_mode"] = "exact"
            selections["vbitrate"] = BitrateMatcher(v_sel.bitrate, tolerance=0.05)
        else:
            # Determine profile position for batch mode stability
            profile_tracks = sorted(
                [
                    v for v in display_v 
                    if v.height == v_sel.height and v.width == v_sel.width 
                    and v.codec == v_sel.codec and v.range == v_sel.range
                ], 
                key=lambda x: x.bitrate, 
                reverse=True
            )
            
            selections["vbitrate"] = None  # Prevent strict bitrate matching in batch mode
            if v_sel.bitrate == profile_tracks[0].bitrate:
                selections["v_mode"] = "best"
            else:
                selections["v_mode"] = "worst"

        # Phase 5: Audio filtering
        console.print(Padding(Rule("[rule.text]Phase 5: Audio Filters"), (1, 2, 1, 2)))
        from unshackle.core.tracks import Audio
        
        a_pool = target.tracks.audio
        
        # Filter out descriptive tracks early if not explicitly requested
        if not selections.get("audio_description"):
            a_pool = [a for a in a_pool if not getattr(a, 'descriptive', False)]

        # Filter by audio codec
        a_codecs = list(Audio.Codec)
        a_codec_options = [f"[bold white]  1. Any / Default [/] [[bold white]{len(a_pool):>3}[/]]"]
        
        for idx, c in enumerate(a_codecs, 2):
            count = len([a for a in a_pool if a.codec == c])
            style = "white" if count > 0 else "dim"
            label = f"{c.name} ({c.value})"
            a_codec_options.append(f"[{style}] {idx:2}. {label:<14}[/] [[bold]{count:>3}[/]]")
            
        ac_table = Table(show_header=False, box=None, padding=(0, 1))
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
            
        console.print(" [bold green]Select Audio Codec Filter:[/]")
        console.print(ac_table)
        
        af_idx_str = Prompt.ask("\n  Select Audio Codec Index", choices=[str(i) for i in range(1, len(a_codec_options) + 1)], default="1", show_choices=False)
        if af_idx_str != "1":
            selected_a_codec = a_codecs[int(af_idx_str) - 2]
            selections["acodec"] = [selected_a_codec]
            a_pool = [a for a in a_pool if a.codec == selected_a_codec]

        # Phase 5.5: Audio track selection
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
                if getattr(t, 'atmos', False): tags.append("Atmos")
                if getattr(t, 'descriptive', False): tags.append("AD")
                
                # Safe channel count retrieval
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
            
            console.print(a_tree)
            
            # Handle track selection and prevent engine crashes from duplicates
            a_input = Prompt.ask("\n  Select Audio indices (eg.: 2 6-9)", default="1")
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
            
            console.print(Padding(Rule("[rule.text]Phase 6: Available Subtitle Tracks"), (1, 2, 1, 2)))
            
            s_table = Table(show_header=False, box=None, padding=(0, 2))
            for _ in range(3):
                s_table.add_column(no_wrap=True)
            
            items = []
            for i, t in enumerate(display_s, 1):
                tags = []
                if getattr(t, 'forced', False): tags.append("Forced")
                if getattr(t, 'sdh', False) or getattr(t, 'cc', False): tags.append("SDH")
                
                tag_str = ""
                if tags:
                    tag_str = f" [dim]({', '.join(tags)})[/]"
                items.append(f"  {i:2} - {t.language}{tag_str}")

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
            
            console.print(s_table)
            
            s_input = Prompt.ask("\n  Select Subtitle indices (eg.: 2 6-9 or ENTER for none)", default="")
            if s_input:
                s_idxs = parse_indices(s_input, len(display_s))
                for i in s_idxs:
                    if getattr(display_s[i], 'forced', False):
                        selections["forced_subs"] = True
                selections["s_lang"] = list(set(str(display_s[i].language) for i in s_idxs))

        # Prepare selections for engine delivery
        selections["v_lang"] = ["all"]
        if not selections["a_lang"]:
            selections["a_lang"] = ["all"]
            
        selections["no_subs"] = not bool(selections["s_lang"])
        
        if selections.get("latest_episode", False):
            selections["select_titles"] = False

        return selections

    except Exception as e:
        console.print_exception()
        sys.exit(1)