# Installation

unshackle is a modular archival tool for movies, TV, and music. It ships as a
single Python package named `unshackle` that installs one console command, also
called `unshackle`. This page covers the install script, the `unshackle setup`
command, the Python version unshackle needs, and the external command-line tools
it expects to find.

!!! note "What you get"
    Installing the package registers a single entry point, the `unshackle`
    command. It covers everything: downloading, key-vault management, device
    provisioning, the REST server, and the helper utilities.

## Install from a git clone (recommended)

A git clone with the install script is the recommended way to install unshackle.
A clone keeps unshackle, its external tools, its config, its data folders, and
your services together in one folder that you can see.

The repository has an install script for Linux (`install.sh`) and for Windows
(`install.bat`). The script installs `uv` if it is not found, installs unshackle,
and then runs [`unshackle setup`](#what-unshackle-setup-does).

=== "Linux"

    ```shell
    git clone https://github.com/unshackle-dl/unshackle.git
    cd unshackle
    sh install.sh
    ```

=== "Windows"

    ```bat
    git clone https://github.com/unshackle-dl/unshackle.git
    cd unshackle
    .\install.bat
    ```

In a clone, the script runs `uv sync` and then `uv run unshackle setup`. The last
lines of setup tell you to check your tools with `uv run unshackle env check`.
Run unshackle from the clone with `uv run unshackle ...`.

To install without a clone, see
[Advanced: install as a uv tool](#advanced-install-as-a-uv-tool).

## What `unshackle setup` does

`unshackle setup` prepares a new installation. The install script runs it for
you, and you can run it yourself at any time:

=== "From a clone"

    ```shell
    uv run unshackle setup
    ```

=== "Installed as a uv tool"

    ```shell
    unshackle setup
    ```

The command does these steps:

1. It shows which [external tools](#external-tools-on-your-path) are missing,
   and where to get the ones that it cannot download.
2. It asks once for each tool or group of tools to download. The required
   tools come first. shaka-packager and `mp4decrypt` share one DRM question, so
   `mp4decrypt` comes with the required tools, but `env check` still shows it
   as optional. The HDR and subtitle groups are optional. The prompts show the
   version that setup downloads. On Windows, the subtitle question also shows
   the download size of CCExtractor, which is much larger than the other tools.
   It puts the downloads in a `binaries` folder
   (see [Where setup puts files](#where-setup-puts-files)).
3. If no `unshackle.yaml` exists, it asks for a downloads folder and writes a
   minimal config file.
4. It makes the data folders for cookies, WVDs, PRDs, and services.
5. It asks for `.wvd` and `.prd` devices to import. You can give a file or a
   folder of devices, and it asks again until you give an empty answer. It
   validates each file, copies it into the WVDs or PRDs folder, and prints one
   line for each file. If setup wrote a new config in this run, it sets
   `cdm.default` to the imported device. If you imported more than one, it
   asks which one is the default.
6. It prints a summary of the tools that it found. If it wrote a config, it
   gives the path of that config and a link to the
   [configuration reference](../reference/configuration/index.md). Then it
   tells you to run `env check` next.

You can run the command again. It skips the tools that it finds and never
overwrites an existing config.

!!! note "Not every tool has a download"
    Setup downloads tools only on Linux and Windows, and only when the tool
    has a portable build for your system. For other tools, setup prints where
    to get them, for example a package manager. On macOS, setup downloads
    nothing and prints these links for each missing tool.

    On Linux, setup cannot download MKVToolNix, which is a required tool.
    Install the `mkvtoolnix` package with your package manager (for example
    `apt`, `dnf`, or `pacman`).

!!! warning "CCExtractor on Linux needs FUSE"
    On Linux, setup downloads CCExtractor as an AppImage, which needs FUSE to
    run. Setup runs the AppImage once after the download. If it does not run, setup removes it and prints the
    manual install link. Install FUSE with your package manager and run setup
    again, or install the `ccextractor` package instead.

### Where setup puts files

| Item | From a git clone | From a `uv` tool install |
| --- | --- | --- |
| External tools | `unshackle/binaries` in the clone | `binaries` in the user data directory |
| New config (`unshackle.yaml`) | `unshackle/unshackle.yaml` in the clone | The user config directory |
| Data folders (cookies, WVDs, PRDs, cache, logs, services, temp) | The default folders in the clone | The user data directory |
| Downloads | The folder that you give at the prompt | The folder that you give at the prompt |

On Windows, CCExtractor and its libraries go into their own `ccextractor`
subfolder of the tools folder.

On Linux, the user data directory is usually `~/.local/share/unshackle` and the
user config directory is usually `~/.config/unshackle`. On Windows, both are
`%LOCALAPPDATA%\unshackle`. Run `uv run unshackle env info` to see the paths that
unshackle uses. Its `User_Binaries` row shows the `binaries` folder in the user
data directory.

Setup writes a new config only when it finds no `unshackle.yaml` in any of the
[config locations](configuration-file.md). It keeps an existing config where it
is. In a clone, the new config sets only `directories.downloads`, so the data
folders stay at their defaults in the clone.

A `uv` tool upgrade replaces the package folder. For a `uv` tool install, setup
puts the tools in the user data directory and writes a `directories` block that
moves the data folders there, so an upgrade keeps them. If an existing config
keeps data folders inside the package of a `uv` tool install, setup shows a
warning and prints the `directories` lines to add.

## Requirements at a glance

| Requirement | Recommended version | Needed for |
| --- | --- | --- |
| [Python](https://www.python.org/) | 3.11 to 3.14 | Running unshackle itself |
| [uv](https://docs.astral.sh/uv/) | ≥ 0.5 | Installing and running unshackle |
| [FFmpeg](https://ffmpeg.org/) | ≥ 6.0 | Media processing and analysis |
| [MKVToolNix](https://mkvtoolnix.download/) | ≥ 80 | MKV muxing and metadata editing |
| [shaka-packager](https://github.com/shaka-project/shaka-packager) | 2.6.1, or ≥ 3.10.0 | DRM decryption |
| [Bento4](https://github.com/axiomatic-systems/Bento4) | ≥ 1.6.0-639 | Alternate DRM decryption (`mp4decrypt`) |
| [dovi_tool](https://github.com/quietvoid/dovi_tool) | ≥ 2.1 | Dolby Vision handling |
| [SubtitleEdit](https://github.com/SubtitleEdit/subtitleedit/releases) | ≥ 5.0 | Subtitle conversion (optional) |

The sections below tell you about each of these, and how to make sure that your
setup is complete.

## Python versions

unshackle requires **Python 3.11 or newer, up to and including 3.14**
(`requires-python = ">=3.11,<3.15"`). Anything older than 3.11 or 3.15 and
later is unsupported, and the installer refuses to assemble the environment.

You do not usually need to install a matching Python interpreter by hand, since
`uv` can download and manage one for you. If you work inside an existing
virtual environment, make sure that it is on a supported version.

## Installing by hand

If you do not use the install script, install unshackle with `uv` and then run
`unshackle setup`, or install the [external tools](#external-tools-on-your-path)
yourself.

### Running from a clone

Use `uv run` inside the clone. It keeps the project's virtual environment active
for the duration of the command, so you do not have to activate it yourself. It
also installs the dependencies on the first run.

```shell title="Run from a git clone"
git clone https://github.com/unshackle-dl/unshackle.git
cd unshackle
uv run unshackle setup
```

Every command shown elsewhere in these docs works the same way from a clone.
Prefix it with `uv run`:

```shell
uv run unshackle env check
```

### Advanced: install as a uv tool

This way is for users who know `uv` tools and want the `unshackle` command on
their `PATH` without a clone.

!!! note "Where a uv tool install keeps files"
    `uv tool install` puts unshackle into the tool folder of `uv`, not into a
    folder that you choose. `unshackle setup` therefore puts the external
    tools, the config, and the data folders in your user data and config
    directories (see [Where setup puts files](#where-setup-puts-files)). A git
    clone is the easier setup: its tools, config, data, and services stay in
    one folder that you can see. On the first run from a `uv` tool install,
    `unshackle setup` tells you this and asks if you want to continue.

Install the tool and run setup:

```shell title="Install as a uv tool"
uv tool install git+https://github.com/unshackle-dl/unshackle.git
unshackle setup
```

The install scripts also do this when you run one alone, without a clone: when
the script does not find the unshackle `pyproject.toml` next to it, it runs
`uv tool install` and then `unshackle setup`. When it finishes, it tells you to
open a new terminal, because the `PATH` change only applies to new terminals.
Run `unshackle env check` there.

!!! tip "Upgrading and removing"
    Because you installed it from a git URL, re-run the same
    `uv tool install` command (uv will update in place) to get a newer version,
    and use `uv tool uninstall unshackle` to remove it.

!!! note "Developer note"
    Contributors will also want the development and test dependency groups. The
    project defines `dev` (linters, type checkers) and `test` (pytest and
    friends) groups in `pyproject.toml`. Install them with
    `uv sync --group dev --group test` inside the clone. End users can ignore
    these entirely.

## External tools on your PATH

unshackle shells out to a number of external command-line programs for media
processing and DRM work. It looks for each one first in the `binaries/` folder
inside the installed package, then in the `binaries` folder of your user data
directory, and then on your system `PATH`. `unshackle setup` installs tools into
the first folder in a clone and into the second for a `uv` tool install. Installing a tool system-wide or putting it into one of those folders
both work.

`unshackle setup` installs the required tools and some optional ones for you.
Use the tables below when you install a tool by hand.

### Required tools

These must be present for core downloading, decryption, and muxing to work:

| Tool | Binary looked up | Why it is needed |
| --- | --- | --- |
| FFmpeg | `ffmpeg` | Media processing: remuxing, conversion, track handling |
| FFprobe | `ffprobe` | Media analysis: inspecting tracks and container details |
| MKVToolNix | `mkvmerge` | Muxing downloaded tracks into a final MKV |
| mkvpropedit | `mkvpropedit` | Editing MKV metadata after muxing |
| shaka-packager | `shaka-packager` / `packager` | Decrypting DRM-protected content (the default decryptor) |

!!! warning "shaka-packager is the default decryptor"
    unshackle decrypts with shaka-packager unless you explicitly configure a
    different decryptor. If it is missing, protected downloads will fail at the
    decryption step. See the [configuration reference](../reference/configuration/drm.md#decryption) for the
    `decryption` config key if you want to switch to `mp4decrypt`.

!!! danger "Do not use shaka-packager 3.0 through 3.9.x"
    These versions stop with a segmentation fault on a single-track MP4 whose
    internal `track_id` is 2 or more. A track that unshackle downloads from a
    DASH or HLS manifest usually has such a `track_id`.

### Recommended and optional tools

These extend unshackle's capabilities. Install the ones relevant to what you
download. For example, `dovi_tool` only matters if you download Dolby Vision video.

| Tool | Binary looked up | Why it is needed |
| --- | --- | --- |
| Bento4 (`mp4decrypt`) | `mp4decrypt` | Alternative DRM decryptor to shaka-packager |
| MP4Box (GPAC) | `MP4Box` | Muxing DTS:X Profile 2 (DTS-UHD) audio |
| dovi_tool | `dovi_tool` | Dolby Vision metadata handling |
| HDR10Plus_tool | `hdr10plus_tool` | HDR10+ metadata handling |
| SubtitleEdit | `seconv`, `SubtitleEdit` | Subtitle conversion (the `SeConv` CLI from 5.x) |
| CCExtractor | `ccextractor`, `ccextractorwin`, `ccextractorwinfull` | Extracting closed captions |
| FFplay | `ffplay` | Simple preview player |
| MPV | `mpv` | Advanced preview player (used by `util crop`/`util range` previews) |
| HolaProxy | `hola-proxy` | Hola proxy provider support |
| Caddy | `caddy` | Optional reverse proxy for `unshackle serve --caddy` |
| Docker | `docker` | Gluetun VPN proxy support |
| Git | `git` | Fetching and updating remote service repositories |

!!! info "MP4Box is only used for DTS:X Profile 2"
    Titles with DTS:X Profile 2 (DTS-UHD) audio mux to MP4; everything else muxes to MKV
    with `mkvmerge`. Install with `apt install gpac`.

!!! tip "SubtitleEdit specifics"
    unshackle looks for `seconv` first, the batch CLI shipped with
    SubtitleEdit 5.x, before falling back to a `SubtitleEdit` (4.x) binary. The
    5.0 GUI has no batch mode, so the `SeConv` CLI is what you want for
    automated subtitle conversion.

## Verifying your installation

After installation, make sure that unshackle can see the tools it depends on.

### Examine the external dependencies

The `env check` subcommand is the first check. It inspects your environment and
reports which tools it found:

=== "From a clone"

    ```shell
    uv run unshackle env check
    ```

=== "Installed as a uv tool"

    ```shell
    unshackle env check
    ```

It prints a table grouped by category (Core, DRM, HDR, Subtitle, Player,
Network) with a status column: a green check for tools that resolved and a red
cross for those it could not find, plus a summary line such as
`All required tools installed ✓` or `Missing required: <names>`.

If a required tool shows as missing, `env check` tells you to run
`unshackle setup`. You can also install the tool by hand and put it on your
`PATH` (or in one of the `binaries` folders), then re-run the check.

!!! tip "`unshackle` is not found after a uv tool install"
    The bin directory of `uv` tools is probably not on your `PATH`. Run
    `uv tool update-shell` and open a new terminal.

### Inspect environment paths

To see where unshackle will read its configuration from and where it stores
downloads, caches, cookies, and devices, use:

```shell
uv run unshackle env info
```

If no configuration file exists yet, this command lists the locations unshackle
searches for one, so you know where to make it. See the
[configuration guide](configuration-file.md) for the details of that file.

## Next steps

- Set up your [configuration](configuration-file.md) file (`unshackle.yaml`), or
  let `unshackle setup` write a first one.
- Start [downloading](../guide/downloading.md) titles when `env check` finds every required tool.
