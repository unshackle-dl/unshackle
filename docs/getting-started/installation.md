# Installation

unshackle is a modular archival tool for movies, TV, and music. It ships as a
single Python package named `unshackle` that installs one console command, also
called `unshackle`. This page covers installing that command, the Python
version it needs, and the external command-line tools it expects to find on your
`PATH`.

!!! note "What you get"
    Installing the package registers a single entry point, the `unshackle`
    command. It covers everything: downloading, key-vault management, device
    provisioning, the REST server, and the helper utilities.

## Requirements at a glance

| Requirement | Recommended version | Needed for |
| --- | --- | --- |
| [Python](https://www.python.org/) | 3.11 to 3.14 | Running unshackle itself |
| [uv](https://docs.astral.sh/uv/) | ≥ 0.5 | Installing and running unshackle |
| [FFmpeg](https://ffmpeg.org/) | ≥ 6.0 | Media processing and analysis |
| [MKVToolNix](https://mkvtoolnix.download/) | ≥ 80 | MKV muxing and metadata editing |
| [shaka-packager](https://github.com/shaka-project/shaka-packager) | 2.6.1 | DRM decryption |
| [Bento4](https://github.com/axiomatic-systems/Bento4) | ≥ 1.6.0-639 | Alternate DRM decryption (`mp4decrypt`) |
| [dovi_tool](https://github.com/quietvoid/dovi_tool) | ≥ 2.1 | Dolby Vision handling |
| [SubtitleEdit](https://github.com/SubtitleEdit/subtitleedit/releases) | ≥ 5.0 | Subtitle conversion (optional) |

The sections below explain each of these and how to confirm your setup is
complete.

## Python version support

unshackle requires **Python 3.11 or newer, up to and including 3.14**
(`requires-python = ">=3.11,<3.15"`). Anything older than 3.11 or 3.15 and
later is unsupported and the installer will refuse to build the environment.

You do not usually need to install a matching Python interpreter by hand, since
`uv` can download and manage one for you. If you are working inside an existing
virtual environment, make sure it is on a supported version.

## Installing with uv

The recommended way to install unshackle is as a `uv` tool. This creates an
isolated environment for the package and puts the `unshackle` command on your
`PATH` without touching your system Python.

```shell title="Install as a uv tool"
uv tool install git+https://github.com/unshackle-dl/unshackle.git
```

Once it finishes, the `unshackle` command is available globally:

```shell
unshackle --help
```

!!! tip "Upgrading and removing"
    Because it is installed from a git URL, re-run the same
    `uv tool install` command (uv will update in place) to pull a newer build,
    and use `uv tool uninstall unshackle` to remove it.

### Running from a clone

If you would rather work from a checkout of the repository (for example to try
an in-development branch or to keep local service code alongside unshackle), use
`uv run` inside the clone. This keeps the project's virtual environment active
for the duration of the command, so you do not have to activate it yourself.

```shell title="Run from a git clone"
git clone https://github.com/unshackle-dl/unshackle.git
cd unshackle
uv run unshackle --help
```

Every command shown elsewhere in these docs works the same way from a clone,
just prefix it with `uv run`:

```shell
uv run unshackle env check
```

!!! note "Developer note"
    Contributors will also want the development and test dependency groups. The
    project defines `dev` (linters, type checkers) and `test` (pytest and
    friends) groups in `pyproject.toml`; install them with
    `uv sync --group dev --group test` inside the clone. End users can ignore
    these entirely.

## External tools on your PATH

unshackle shells out to a number of external command-line programs for media
processing and DRM work. It looks for each one first in the `binaries/` folder
inside the installed package, and then falls back to your system `PATH`, so
installing these tools system-wide (or dropping them into that folder) both
work.

### Required tools

These must be present for core downloading, decryption, and muxing to work:

| Tool | Binary looked up | Why it is needed |
| --- | --- | --- |
| FFmpeg | `ffmpeg` | Media processing: remuxing, conversion, stream handling |
| FFprobe | `ffprobe` | Media analysis: inspecting tracks and container details |
| MKVToolNix | `mkvmerge` | Muxing downloaded tracks into a final MKV |
| mkvpropedit | `mkvpropedit` | Editing MKV metadata after muxing |
| shaka-packager | `shaka-packager` / `packager` | Decrypting DRM-protected content (the default decryptor) |

!!! warning "shaka-packager is the default decryptor"
    unshackle decrypts with shaka-packager unless you explicitly configure a
    different decryptor. If it is missing, protected downloads will fail at the
    decryption step. See the [configuration reference](../reference/configuration/drm.md#decryption) for the
    `decryption` key if you want to switch to `mp4decrypt`.

### Recommended and optional tools

These extend unshackle's capabilities. Install the ones relevant to what you
download. For example, `dovi_tool` only matters if you handle Dolby Vision.

| Tool | Binary looked up | Why it is needed |
| --- | --- | --- |
| Bento4 (`mp4decrypt`) | `mp4decrypt` | Alternative DRM decryptor to shaka-packager |
| dovi_tool | `dovi_tool` | Dolby Vision metadata handling |
| HDR10Plus_tool | `hdr10plus_tool` | HDR10+ metadata handling |
| SubtitleEdit | `seconv`, `SubtitleEdit` | Subtitle conversion (the `SeConv` CLI from 5.x) |
| CCExtractor | `ccextractor` | Extracting closed captions |
| FFplay | `ffplay` | Simple preview player |
| MPV | `mpv` | Advanced preview player (used by `util crop`/`util range` previews) |
| HolaProxy | `hola-proxy` | Hola proxy provider support |
| Caddy | `caddy` | Optional reverse proxy for `unshackle serve --caddy` |
| Docker | `docker` | Gluetun VPN proxy support |
| Git | `git` | Fetching and updating remote service repositories |

!!! tip "SubtitleEdit specifics"
    unshackle looks for `seconv` first, the batch CLI shipped with
    SubtitleEdit 5.x, before falling back to a `SubtitleEdit` (4.x) binary. The
    5.0 GUI has no batch mode, so the `SeConv` CLI is what you want for
    automated subtitle conversion.

## Verifying your installation

After installing, confirm the command runs and that unshackle can see the tools
it depends on.

### Confirm the command is available

```shell
unshackle --help
```

This prints the list of available commands (`dl`, `env`, `kv`,
`serve`, `wvd`, `prd`, `search`, `util`, `cfg`, `import`). If your shell reports
that `unshackle` is not found, the `uv tool` bin directory is probably not on
your `PATH`. Run `uv tool update-shell` and open a new terminal.

### Check external dependencies

The `env check` subcommand inspects your environment and reports which tools it
found:

```shell
unshackle env check
```

=== "Output"

    It prints a table grouped by category (Core, DRM, HDR, Subtitle, Player,
    Network) with a status column: a green check for tools that resolved and a
    red cross for those it could not find, plus a summary line such as
    `All required tools installed ✓` or `Missing required: <names>`.

=== "From a clone"

    ```shell
    uv run unshackle env check
    ```

If a required tool shows as missing, install it and put it on your `PATH` (or in
the package's `binaries/` folder), then re-run the check.

### Inspect environment paths

To see where unshackle will read its configuration from and where it stores
downloads, caches, cookies, and devices, use:

```shell
unshackle env info
```

If no configuration file exists yet, this command lists the locations unshackle
searches for one, so you know where to create it. See the
[configuration guide](configuration-file.md) for the details of that file.

## Next steps

- Set up your [configuration](configuration-file.md) file (`unshackle.yaml`).
- Start [downloading](../guide/downloading.md) content once your tools check out.
