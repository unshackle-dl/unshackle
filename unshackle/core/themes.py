"""
Named CLI color themes.

Each palette maps the same role keys to colors: `text`/`text2` for body copy, `pink` for the
brand accent, the six ANSI hues, and the gray ramp used for rules and dim details. The active
palette drives both the rich Console theme (banner, logs, progress) and the click help screens,
so a single `theme` config value restyles the whole CLI.
"""

from __future__ import annotations

from rich_click import RichHelpConfiguration

DEFAULT_THEME = "catppuccin-mocha"

PALETTES: dict[str, dict[str, str]] = {
    # Colors based on "CatppuccinMocha" from Gogh themes
    "catppuccin-mocha": {
        "bg": "rgb(30,30,46)",
        "text": "rgb(205,214,244)",
        "text2": "rgb(162,169,193)",  # slightly darker
        "black": "rgb(69,71,90)",
        "bright_black": "rgb(88,91,112)",
        "red": "rgb(243,139,168)",
        "green": "rgb(166,227,161)",
        "yellow": "rgb(249,226,175)",
        "blue": "rgb(137,180,250)",
        "pink": "rgb(245,194,231)",
        "cyan": "rgb(148,226,213)",
        "gray": "rgb(166,173,200)",
        "bright_gray": "rgb(186,194,222)",
        "dark_gray": "rgb(54,54,84)",
    },
    # draculatheme.com spec; purple stands in for blue, comment for the dim grays
    "dracula": {
        "bg": "#282a36",
        "text": "#f8f8f2",
        "text2": "#b8bfd8",
        "black": "#21222c",
        "bright_black": "#6272a4",
        "red": "#ff5555",
        "green": "#50fa7b",
        "yellow": "#f1fa8c",
        "blue": "#bd93f9",
        "pink": "#ff79c6",
        "cyan": "#8be9fd",
        "gray": "#6272a4",
        "bright_gray": "#e9e9f4",
        "dark_gray": "#44475a",
    },
    # nordtheme.com: polar night / snow storm / frost / aurora
    "nord": {
        "bg": "#2e3440",
        "text": "#d8dee9",
        "text2": "#9aa4b8",
        "black": "#3b4252",
        "bright_black": "#4c566a",
        "red": "#bf616a",
        "green": "#a3be8c",
        "yellow": "#ebcb8b",
        "blue": "#81a1c1",
        "pink": "#b48ead",
        "cyan": "#88c0d0",
        "gray": "#616e88",
        "bright_gray": "#e5e9f0",
        "dark_gray": "#434c5e",
    },
    # github.com/morhetz/gruvbox dark, bright variants
    "gruvbox": {
        "bg": "#282828",
        "text": "#ebdbb2",
        "text2": "#bdae93",
        "black": "#504945",
        "bright_black": "#665c54",
        "red": "#fb4934",
        "green": "#b8bb26",
        "yellow": "#fabd2f",
        "blue": "#83a598",
        "pink": "#d3869b",
        "cyan": "#8ec07c",
        "gray": "#928374",
        "bright_gray": "#d5c4a1",
        "dark_gray": "#3c3836",
    },
    # Atom One Dark syntax palette
    "one-dark": {
        "bg": "#282c34",
        "text": "#abb2bf",
        "text2": "#818896",
        "black": "#3b4048",
        "bright_black": "#5c6370",
        "red": "#e06c75",
        "green": "#98c379",
        "yellow": "#e5c07b",
        "blue": "#61afef",
        "pink": "#c678dd",
        "cyan": "#56b6c2",
        "gray": "#5c6370",
        "bright_gray": "#c8ccd4",
        "dark_gray": "#3e4451",
    },
    # github.com/enkia/tokyo-night-vscode-theme, night variant
    "tokyo-night": {
        "bg": "#1a1b26",
        "text": "#c0caf5",
        "text2": "#a9b1d6",
        "black": "#15161e",
        "bright_black": "#414868",
        "red": "#f7768e",
        "green": "#9ece6a",
        "yellow": "#e0af68",
        "blue": "#7aa2f7",
        "pink": "#bb9af7",
        "cyan": "#7dcfff",
        "gray": "#565f89",
        "bright_gray": "#a9b1d6",
        "dark_gray": "#292e42",
    },
    # classic Monokai; purple stands in for pink, 66d9ef doubles as blue
    "monokai": {
        "bg": "#272822",
        "text": "#f8f8f2",
        "text2": "#cfcfc2",
        "black": "#2e2e2e",
        "bright_black": "#75715e",
        "red": "#f92672",
        "green": "#a6e22e",
        "yellow": "#e6db74",
        "blue": "#66d9ef",
        "pink": "#ae81ff",
        "cyan": "#a1efe4",
        "gray": "#a59f85",
        "bright_gray": "#cfcfc2",
        "dark_gray": "#3e3d32",
    },
    # flexoki.com dark, 400-level accents
    "flexoki": {
        "bg": "#100f0f",
        "text": "#cecdc3",
        "text2": "#878580",
        "black": "#282726",
        "bright_black": "#403e3c",
        "red": "#d14d41",
        "green": "#879a39",
        "yellow": "#d0a215",
        "blue": "#4385be",
        "pink": "#ce5d97",
        "cyan": "#3aa99f",
        "gray": "#6f6e69",
        "bright_gray": "#b7b5ac",
        "dark_gray": "#343331",
    },
    # ethanschoonover.com/solarized dark; magenta stands in for pink
    "solarized-dark": {
        "bg": "#002b36",
        "text": "#839496",
        "text2": "#586e75",
        "black": "#073642",
        "bright_black": "#586e75",
        "red": "#dc322f",
        "green": "#859900",
        "yellow": "#b58900",
        "blue": "#268bd2",
        "pink": "#d33682",
        "cyan": "#2aa198",
        "gray": "#657b83",
        "bright_gray": "#93a1a1",
        "dark_gray": "#073642",
    },
}

ALIASES = {
    "default": DEFAULT_THEME,
    "catppuccin": DEFAULT_THEME,
    "mocha": DEFAULT_THEME,
    "onedark": "one-dark",
    "tokyonight": "tokyo-night",
    "solarized": "solarized-dark",
}


def resolve_palette(name: object) -> dict[str, str] | None:
    """Look up a palette by theme name or alias, case-insensitively.

    Takes any YAML scalar since config values are unvalidated; non-strings never match.
    Returns a copy: console.py extends the active palette in place, and that must
    not pollute the master definitions.
    """
    key = str(name or "").strip().lower().replace("_", "-")
    palette = PALETTES.get(ALIASES.get(key, key))
    return dict(palette) if palette else None


def build_help_config(palette: dict[str, str]) -> RichHelpConfiguration:
    """Build the rich-click help styling for a palette."""
    return RichHelpConfiguration(
        style_option=palette["text"],
        style_switch=palette["green"],
        style_argument=palette["cyan"],
        style_command=palette["pink"],
        style_metavar=palette["yellow"],
        style_metavar_separator=f"dim {palette['gray']}",
        style_usage=palette["yellow"],
        style_usage_command=palette["text"],
        style_helptext_first_line=palette["text"],
        style_helptext=palette["text2"],
        style_option_help=palette["text"],
        style_command_help=palette["text"],
        style_option_default=f"dim {palette['gray']}",
        style_option_envvar=f"dim {palette['yellow']}",
        style_required_short=palette["red"],
        style_required_long=f"dim {palette['red']}",
        style_errors_suggestion=palette["text2"],
        style_aborted=palette["red"],
        style_options_panel_box=None,
        style_options_panel_border=palette["pink"],
        style_options_panel_title_style=palette["pink"],
        style_commands_panel_box=None,
        style_commands_panel_border=palette["pink"],
        style_commands_panel_title_style=palette["pink"],
        style_errors_panel_box="SQUARE",
        style_errors_panel_border=palette["red"],
        style_options_table_box="HORIZONTALS",
        style_options_table_show_lines=True,
        style_options_table_border_style=palette["dark_gray"],
    )


def apply_help_theme(palette: dict[str, str]) -> None:
    """Point the patched click help renderer at this palette (read at render time)."""
    build_help_config(palette).dump_to_globals()
