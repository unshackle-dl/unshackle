"""
CLI colour themes, read from YAML files.

A theme sets the colours of the `ROLES`, can override any Rich style under `styles:`, and can draw the
`GRADIENT_SURFACES` in blended colours under `gradients:`. The shipped themes are in `unshackle/themes/`. A file in `directories.themes` adds a theme, or replaces a shipped
theme with the same name. The active theme drives both the rich Console theme (banner, logs,
progress) and the click help screens.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from rich.color import Color, ColorParseError
from rich.errors import StyleSyntaxError
from rich.style import Style
from rich.theme import Theme
from rich_click import RichHelpConfiguration

from unshackle.core.config import config

DEFAULT_THEME = "unshackle"
ALIASES = {
    "default": DEFAULT_THEME,
    "catppuccin": "catppuccin-mocha",
    "mocha": "catppuccin-mocha",
    "onedark": "one-dark",
    "solarized": "solarized-dark",
    "tokyonight": "tokyo-night",
}

ROLES = (
    "bg",
    "text",
    "text2",
    "black",
    "bright_black",
    "red",
    "green",
    "yellow",
    "blue",
    "pink",
    "cyan",
    "gray",
    "bright_gray",
    "dark_gray",
)
ROLE_ALIASES = {"accent": "pink"}
PRESETS_DIR = Path(__file__).resolve().parent.parent / "themes"
FILE_KEYS = {*ROLES, *ROLE_ALIASES, "extends", "styles", "gradients", "author", "description"}
GRADIENT_SURFACES = ("banner", "bar", "pulse", "rule", "panel")
GRADIENT_OPTIONS = {"direction": ("horizontal", "vertical", "diagonal"), "motion": ("static", "scroll")}

ThemeData = tuple[dict[str, str], dict[str, str], dict[str, Any]]


def normalize_name(name: object) -> str:
    """Normalise a theme name or file stem: lowercase, with `_` read as `-`."""
    return str(name or "").strip().lower().replace("_", "-")


def canonical_name(name: object) -> str:
    """Normalise a theme name and resolve an alias to the theme it names."""
    key = normalize_name(name)
    return ALIASES.get(key, key)


def theme_files(folder: Path) -> dict[str, Path]:
    """Map each theme file's name to its path. It skips alias names, which belong to the shipped themes."""
    try:
        paths = sorted(path for path in folder.iterdir() if path.suffix.lower() in (".yaml", ".yml"))
    except OSError as e:
        if folder.exists():
            logging.getLogger("console").warning("cannot read the themes folder %s: %s", folder, e.strerror or e)
        return {}
    files: dict[str, Path] = {}
    for path in paths:
        name = normalize_name(path.stem)
        if name not in ALIASES and name.isprintable():
            files.setdefault(name, path)
    return files


def shipped_themes() -> dict[str, Path]:
    return theme_files(PRESETS_DIR)


def user_themes() -> dict[str, Path]:
    return theme_files(Path(config.directories.themes))


def load_theme(name: object, user_files: bool = True) -> ThemeData:
    """Resolve a theme name to its role colours and its `styles:` overrides.

    A user file starts from its `extends` theme, else the shipped theme with the same name, else the
    default. That base is always a shipped file, so a theme that extends itself cannot loop.
    It checks every value, so `build_console_theme` always accepts the result. Raises ValueError,
    naming the file and the entry, when the theme is unknown or not valid.
    """
    key = canonical_name(name)
    shipped = shipped_themes()
    users = user_themes() if user_files else {}
    user = users.get(key)
    if user is None:
        if key not in shipped:
            raise ValueError(f"unknown theme {str(name)!r}")
        return apply_theme(shipped[key].name, read_theme_file(shipped[key]), {}, {}, {})

    data = read_theme_file(user)
    extends = data.get("extends")
    if extends is not None and not isinstance(extends, str):
        raise ValueError(f"{user.name}: extends must be a quoted theme name")
    base = canonical_name(extends or (key if key in shipped else DEFAULT_THEME))
    if base not in shipped:
        raise ValueError(f"{user.name}: extends {data['extends']!r}, not a shipped theme")
    return apply_theme(user.name, data, *load_theme(base, user_files=False))


def load_active_theme(name: object) -> tuple[str, ThemeData]:
    """Load the configured theme, or the shipped default when it is unknown or not valid. Returns the name in use."""
    try:
        return canonical_name(name), load_theme(name)
    except ValueError as e:
        logging.getLogger("console").warning("%s, using the %s theme", e, DEFAULT_THEME)
        return DEFAULT_THEME, load_theme(DEFAULT_THEME, user_files=False)


def read_theme_file(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, RecursionError, yaml.YAMLError) as e:
        raise ValueError(f"{path.name}: cannot read: {shorten(' '.join(str(e).split()), 80)}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: not a mapping of theme keys")
    unknown = sorted(str(key) for key in data if key not in FILE_KEYS)
    if unknown:
        raise ValueError(f"{path.name}: unknown key {', '.join(unknown)}")
    return data


def apply_theme(
    file: str, data: dict[str, Any], palette: dict[str, str], styles: dict[str, str], gradients: dict[str, Any]
) -> ThemeData:
    """Lay the keys of a theme file over a base theme, and return the result. The base is not changed."""
    data = dict(data)
    for alias, role in ROLE_ALIASES.items():
        if alias in data:
            if role in data:
                raise ValueError(f"{file}: set {role} or {alias}, not both")
            data[role] = data.pop(alias)

    palette = dict(palette)
    for role in ROLES:
        if role not in data:
            continue
        value = data[role]
        if value is None or value == "":
            raise ValueError(f"{file}: {role} is empty (put hex values in quotes)")
        if not isinstance(value, str):
            raise ValueError(f"{file}: {role} must be a quoted colour")
        palette[role] = parse_colour(file, role, value, {})
    missing = [role for role in ROLES if role not in palette]
    if missing:
        raise ValueError(f"{file}: missing {', '.join(missing)}")

    overrides = data.get("styles") or {}
    if not isinstance(overrides, dict):
        raise ValueError(f"{file}: styles must be a mapping")
    styles = dict(styles)
    for style_name, value in overrides.items():
        if not isinstance(style_name, str) or not isinstance(value, str):
            raise ValueError(f"{file}: styles: quote {shorten(str(style_name))} and its value")
        if style_name in ROLES or style_name in ROLE_ALIASES:
            raise ValueError(f"{file}: set {style_name} at the top level, not under styles")
        resolved = " ".join(palette.get(ROLE_ALIASES.get(word.lower(), word.lower()), word) for word in value.split())
        try:
            parsed = Style.parse(resolved)
        except (StyleSyntaxError, ValueError) as e:
            raise ValueError(f"{file}: styles: {style_name}: {shorten(value)} is not a Rich style") from e
        if parsed.link or parsed.conceal:
            raise ValueError(f"{file}: styles: {style_name}: link and conceal not allowed")
        styles[style_name] = resolved

    overrides = data.get("gradients") or {}
    if not isinstance(overrides, dict):
        raise ValueError(f"{file}: gradients must be a mapping")
    gradients = dict(gradients)
    for key, value in overrides.items():
        if key in GRADIENT_OPTIONS:
            if not isinstance(value, str) or value not in GRADIENT_OPTIONS[key]:
                raise ValueError(f"{file}: gradients: {key} must be {' or '.join(GRADIENT_OPTIONS[key])}")
            gradients[key] = value
        elif key in GRADIENT_SURFACES:
            stops = [value] if isinstance(value, str) else value
            if not isinstance(stops, list) or not stops or not all(isinstance(stop, str) for stop in stops):
                raise ValueError(f"{file}: gradients: {key} must be a list of quoted colours")
            gradients[key] = tuple(parse_colour(file, f"gradients: {key}", stop, palette) for stop in stops)
        else:
            raise ValueError(f"{file}: gradients: unknown key {shorten(str(key))}")
    return palette, styles, gradients


def parse_colour(file: str, label: str, value: str, palette: dict[str, str]) -> str:
    """Check one colour from a theme file. A role name in *palette* becomes that role's colour."""
    value = palette.get(ROLE_ALIASES.get(value.lower(), value.lower()), value)
    value = "".join(value.split())
    try:
        Color.parse(value)
    except (ColorParseError, ValueError) as e:
        raise ValueError(f"{file}: {label}: {shorten(value)} is not a colour") from e
    return value


def shorten(text: str, limit: int = 40) -> str:
    """Quote a value from a theme file for an error message, cut to *limit* characters."""
    return repr(text if len(text) <= limit else text[:limit] + "...")


def build_console_theme(palette: dict[str, str], styles: dict[str, str], set_terminal_bg: bool = False) -> Theme:
    """Assemble the Rich console theme: fixed styles first, then the roles and the names made from them, then `styles:`."""
    on_bg = f" on {palette['bg']}" if set_terminal_bg else ""
    return Theme(
        {
            "bar.back": palette["dark_gray"],
            "bar.complete": palette["pink"],
            "bar.finished": palette["green"],
            "bar.pulse": palette["pink"],
            "inspect.async_def": f"italic {palette['cyan']}",
            "repr.number": f"bold not italic {palette['cyan']}",
            "repr.number_complex": f"bold not italic {palette['cyan']}",
            "rule.line": palette["dark_gray"],
            "rule.text": palette["pink"],
            "tree.line": palette["dark_gray"],
            "status.spinner": palette["pink"],
            "progress.spinner": palette["pink"],
            "ascii.art": palette["pink"] + on_bg,
            "none": palette["text"] + on_bg,
            "grey23": palette["black"],
            "magenta": palette["pink"],
            "bright_red": palette["red"],
            "bright_green": palette["green"],
            "bright_yellow": palette["yellow"],
            "bright_blue": palette["blue"],
            "bright_magenta": palette["pink"],
            "bright_cyan": palette["cyan"],
            **palette,
            **styles,
        }
    )


def build_help_config(palette: dict[str, str]) -> RichHelpConfiguration:
    """Assemble the rich-click help configuration for a palette."""
    return RichHelpConfiguration(
        enable_theme_env_var=False,
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
        padding_helptext=(0, 1, 1, 5),
        padding_usage=(1, 1, 1, 5),
        style_options_panel_padding=(0, 1, 0, 3),
        style_commands_panel_padding=(0, 1, 0, 4),
        panel_title_padding=3,
        padding_errors_panel=(0, 1, 1, 5),
        padding_errors_suggestion=(0, 1, 0, 5),
    )


def apply_help_theme(palette: dict[str, str]) -> None:
    """Point the patched click help renderer at this palette (read at render time)."""
    build_help_config(palette).dump_to_globals()
