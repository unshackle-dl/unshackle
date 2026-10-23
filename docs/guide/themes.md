# Themes

A theme sets the colours of all CLI output: the banner, logs, track listings, progress bars and
help screens. Every theme is a small theme file in YAML, so you can change any colour, blend
colours into gradients, make a new theme from a shipped one, and give your theme to other people
as one file.

unshackle ships these themes:

| Theme | Look |
|-------|------|
| `unshackle` | The default: soft pastels on a dark blue-grey background. |
| `rainbow` | The `unshackle` colours with every [gradient](#gradients) on. |
| `nord` | Cool arctic blues with an aurora purple accent. |
| `catppuccin-mocha` | Catppuccin Mocha pastels. |
| `dracula` | Dark purple-grey with vivid pink and green. |
| `flexoki` | Warm ink-on-paper tones. |
| `gruvbox` | Retro warm browns and yellows. |
| `monokai` | High-contrast greens and pinks on charcoal. |
| `one-dark` | Muted blues and purples on slate. |
| `solarized-dark` | Teal-black background with precise accents. |
| `tokyo-night` | Deep navy with neon blues and purples. |

Select a theme with the [`theme`](../reference/configuration/misc.md#appearance) config key:

```yaml title="unshackle.yaml"
theme: nord
```

## List and preview themes

```
unshackle env theme          # list every theme, its source and its colours
unshackle env theme nord     # show a full sample of one theme
```

The list marks the active theme and shows the folder unshackle reads your theme files from. A
theme file that is not valid shows in the list with the reason.

In a terminal at least 80 columns wide, the sample of one theme moves for 5 seconds: the banner, rule and panel gradients,
a progress bar and a pulse bar. The sample stops when the progress bar is full. If the sample is
taller than the terminal, only the bars move.

## Make a theme

1. Make a file named `<name>.yaml` (or `<name>.yml`) in your themes folder. The folder is
   [`directories.themes`](../reference/configuration/directories.md); `unshackle env theme` shows
   its path. The file name is the theme name. Case does not matter, and `_` reads as `-`. Do not
   give the file one of the [other names](#when-unshackle-does-not-use-a-theme) of the shipped
   themes, such as `default.yaml`: unshackle does not read it.
2. Set the colours that you want to change. Each role that you do not set comes from the
   `extends` theme.
3. Set `theme: <name>` in `unshackle.yaml`, then preview it with `unshackle env theme <name>`.

!!! warning "Keep the themes folder outside the installed package"
    By default, the themes folder is inside the unshackle package folder. If you installed with
    `uv tool install`, a `uv tool upgrade` deletes the package folder and your themes with it. Set
    [`directories.themes`](../reference/configuration/directories.md) to a folder outside the
    package, for example `~/.config/unshackle/themes`. If a data folder is inside the package,
    `unshackle setup` shows a warning and the `directories:` settings to add.

```yaml title="themes/custom/my-nord.yaml"
extends: nord            # start from this shipped theme
accent: "#88c0d0"        # the brand accent: banner, titles, progress bars
text2: "#9aa4b8"
styles:
  progress.data.speed: bold accent
author: your name        # optional, unshackle does not read it
description: Nord with a frost accent.
```

!!! warning "Put colours in quotes"
    YAML reads `#` as the start of a comment, so an unquoted `pink: #b48ead` is an empty value.
    YAML also reads an unquoted `on`, `off`, `yes` or `no` as true or false. A role or a style that
    YAML does not read as text makes the theme not valid, and unshackle does not use it.

### `extends`

`extends` names the shipped theme that the file starts from, for example `nord`. Without
`extends`, a file named like a shipped theme starts from that theme, and any other file starts
from `unshackle`. A file named `nord.yaml` replaces the shipped `nord` theme, and it can still
use `extends: nord` to start from the shipped colours.

`extends` cannot name one of your own themes, so a theme that you give to another person always
works on their computer.

### Roles

A theme has 14 roles. Each role is one colour: a colour name (`magenta`), 6-digit hex (`"#88c0d0"`),
or `rgb(136,192,208)`. Rich does not accept 3-digit hex such as `#fff`.

| Role | Used for |
|------|----------|
| `text` | Body text, option names, help text. |
| `text2` | Secondary detail lines, the second part of a docstring. |
| `pink` | The brand accent: banner, titles, panel borders on help screens, progress bars. You can also write it as `accent`; do not set both. |
| `red` | Errors and required options. |
| `green` | Success, option switches, a finished progress bar. |
| `yellow` | Warning text, metavars and usage lines. |
| `blue` | Blue text in markup and listings. |
| `cyan` | Numbers and arguments. |
| `gray` | Dim details such as option defaults. |
| `bright_gray` | Light grey details. |
| `dark_gray` | Rules, table lines, the empty part of a progress bar. |
| `black` | Very dark details, and the Rich colour `grey23`. |
| `bright_black` | Panel borders and tree guides in listings. |
| `bg` | The background, used only when [`set_terminal_bg`](../reference/configuration/misc.md#logging-privacy-debug) is on. |

The role names are also markup names in unshackle and in services (`[text2]`, `[yellow]`), so
they do not change between versions. The terminal colour names `magenta`, `bright_red`,
`bright_green`, `bright_yellow`, `bright_blue`, `bright_magenta` and `bright_cyan` take the colour
of the matching role. To change one of them alone, set it under `styles`.

### `styles`

`styles` sets any [Rich style](https://rich.readthedocs.io/en/stable/style.html) by its name,
for example `progress.data.speed`, `bar.complete`, `rule.line` or `logging.level.info`. A value
is a Rich style string, and it can use role names in any case, which take the theme's colours:

```yaml
styles:
  bar.complete: bold accent
  rule.line: dark_gray
  logging.level.warning: bold yellow on black
```

A role name cannot be a style name under `styles`; set the role at the top level of the file. A
style cannot use the `link` or `conceal` attributes, so a shared theme cannot hide output or turn
text into a link. Help screens use the roles only, so `styles` does not change them.
`unshackle cfg` edits only `unshackle.yaml`, so edit the theme file to change a style.

The log level labels (`INFO`, `WARNING`, `ERROR`) keep the terminal's own colours. To theme them,
set them under `styles`, for example `logging.level.info: blue`.

### `gradients`

`gradients` draws a part of the CLI in colours that blend from one to the next, for example a
rainbow banner. Give each part a list of colour stops. A stop is a role name or a colour, as for
a role. The shipped `rainbow` theme sets every part; `unshackle env theme rainbow` shows them
move:

```yaml title="rainbow.yaml"
gradients:
  banner: [red, "#fab387", yellow, green, cyan, blue, pink]
  bar: [red, yellow, green, cyan, blue, pink]
  pulse: [dark_gray, red, yellow, green, blue, pink]
  rule: [red, yellow, green, cyan, blue, pink]
  panel: [red, yellow, green, blue]
  direction: diagonal
  motion: scroll
```

| Key | What it draws |
|-----|---------------|
| `banner` | The ASCII banner on start and on help screens. During `dl`, the banner stays at the top of the screen and moves. |
| `bar` | The full part of a running progress bar. A finished bar keeps `bar.finished`. |
| `pulse` | The moving bar of a task with no known size, such as muxing. Without it, `[dark_gray, pink]`. |
| `rule` | The line of a section rule such as `Service: EXAMPLE`. A rule always blends from left to right. |
| `panel` | The border of a boxed listing such as `Available Tracks`. The title and the lines inside keep their colours. |
| `direction` | How `banner` and `panel` blend: `horizontal` (the default), `vertical` or `diagonal`. |
| `motion` | `static` (the default) keeps the colours fixed along `bar`. `scroll` moves them along the bar. |

A part without a list keeps its normal colour. A single colour (`rule: blue`) draws the part in
that colour. A terminal with fewer colours shows the nearest colours it has.

With a `banner` gradient, `dl` keeps the banner at the top of the screen while it runs. The newest
output shows under the banner and the progress bars show at the bottom. Older lines scroll off the
top into the scrollback. The gradients of the banner, and of the rules and panel borders on the
screen, move. This happens only in a colour terminal at least 80 columns wide, and not for the
help of `dl` (`--help`, `-h` or `-?`). To keep the banner still, remove `banner` from `gradients`.

## Give a theme to other people

Send the theme file. The other person puts it in their themes folder and sets `theme:` to its name.
A theme file holds only colours, style strings and gradient settings, and unshackle reads it as
data, never as code.

## When unshackle does not use a theme

If the theme name is unknown, or the file has an error, unshackle logs a warning with the reason
and uses the shipped `unshackle` theme. The reason names the file and the role, for example
`my-nord.yaml: pink is empty (put hex values in quotes), using the unshackle theme`. A file with an unknown key is not valid, so a misspelt key does
not go unseen.

These names are other names for shipped themes: `default` (`unshackle`), `catppuccin` and
`mocha` (`catppuccin-mocha`), `onedark` (`one-dark`), `solarized` (`solarized-dark`) and
`tokyonight` (`tokyo-night`). unshackle does not read a theme file with one of these names, or a
file whose name has a character that the terminal does not print.

If unshackle cannot read the themes folder, it logs a warning and uses only the shipped themes.
