# Quick Start

This page takes you from a fresh install to your first downloaded file. You will
make the one piece of configuration unshackle insists on, supply the service
module and CDM it needs to fetch and decrypt media, operate a basic `unshackle dl`
command, and learn where the finished file lands.

!!! note "Before you start"
    This guide assumes unshackle is already installed and on your `PATH`. If
    `unshackle --help` does not print a help screen, work through
    [Installation](installation.md) first.

!!! warning "unshackle includes no services"
    unshackle handles manifests, DRM, downloading, and muxing. It does not include a
    module for any streaming platform, so you write that part yourself
    ([step 3](#3-write-a-service)). `EXAMPLE` in this guide stands in for the tag you
    give yours.

## 1. Examine your environment

unshackle drives several external tools (FFmpeg, MKVToolNix, shaka-packager, and
others) to decrypt, repack, and mux media. Make sure that they are visible before
you download anything:

```shell
unshackle env check
```

This prints a dependency table. The **required** tools (FFmpeg, FFprobe,
MKVToolNix, mkvpropedit, and shaka-packager) must show a green check. Optional
tools such as `dovi_tool` (Dolby Vision) and CCExtractor (closed captions) only
matter for the features that use them.

!!! tip
    The summary line at the bottom reports `installed/total` and lists anything
    required that is still missing, so you know exactly what to install next.

## 2. Make a minimal config

unshackle reads a single configuration file named `unshackle.yaml`. To see where it
looks for that file, and where it *would* accept one if you have not made it yet,
use this command:

```shell
unshackle env info
```

If no config exists, this prints the candidate locations. In the order unshackle
examines them, they are:

1. `unshackle.yaml` inside the unshackle package folder.
2. `unshackle.yaml` in that folder's parent.
3. `unshackle.yaml` in your OS user-config directory
   (`~/.config/unshackle/` on Linux, `%LOCALAPPDATA%\unshackle\` on Windows,
   `~/Library/Application Support/unshackle/` on macOS).

The **first** file that exists wins. Make `unshackle.yaml` in one of those
locations.

### The one config key you must set

unshackle refuses to start a download unless it knows how to name the output file.
That means `output_template` is the one setting a first run genuinely requires.
Everything else has a sensible default. A minimal, working config looks like this:

```yaml title="unshackle.yaml"
output_template:
  movies: "{title} ({year}) {quality} {source}"
  series: "{title} {season_episode} {episode_name?} {quality} {source}"
```

unshackle fills in each `{variable}` from the title and the tracks you downloaded.
[Output and Naming](../guide/output-and-naming.md) gives the full set of valid
variables, including `resolution`, `video`, `audio`, `hdr`, `edition`, `tag`, and more.

!!! note "Spaces or dots?"
    unshackle auto-detects your naming style from the template: if the separators
    between variables are mostly spaces, it uses spaces. If they are mostly dots,
    it makes scene-style `Title.S01E01.1080p` names. Write the template in the
    style you want the filenames to look.

!!! warning "Editing config from the CLI strips comments"
    You can read and set keys with `unshackle cfg` (for example
    `unshackle cfg tag MYGROUP` or `unshackle cfg --list`), but writing a value
    rewrites the file and **removes any comments** it contained. If you keep notes
    in your config, edit the file by hand instead.

## 3. Write a service

The service tag you pass to `dl` (like `EXAMPLE`) maps to a **service module**, the
plugin that talks to one streaming platform. unshackle includes none, so you write
the one you need. [Creating a Service](../dev/creating-a-service.md) covers how.

Put the finished module in your `directories.services` folder (`unshackle env info`
shows the path). The tag then works with `dl`, `search`, and the other service
commands.

## 4. Add a CDM for DRM

Most streaming services encrypt their titles. To fetch content keys, unshackle
needs a **CDM**, a Widevine device (`.wvd`) or a PlayReady device (`.prd`). Add a
Widevine device you already have with:

```shell
unshackle wvd add /path/to/device.wvd
```

This validates the file and moves it into your WVDs directory. Then point services
at it in your config. The `cdm` map uses the service tag as its config key, with a
`default` that covers everything else:

```yaml title="unshackle.yaml"
cdm:
  default: my_device        # the .wvd file's name, without the extension
  EXAMPLE: my_other_device       # override for a specific service
```

PlayReady works the same way with `.prd` files created and managed by the
[`prd`](../guide/cli-reference.md#prd) command. If a title is DRM-free, unshackle needs no CDM.

!!! tip
    Operate `unshackle wvd parse my_device` to inspect a device's security level
    and contents, and `unshackle env info` to show where unshackle stores WVDs and PRDs.

## 5. Supply authentication

Services that require a login read either **cookies** or **credentials**.

- **Cookies**: export the service's cookies to a Netscape-format text file and
  place it in your cookies directory. unshackle looks for, in order:
  `cookies/{SERVICE}.txt`, then `cookies/{SERVICE}/{profile}.txt`, then
  `cookies/{SERVICE}/default.txt`. So unshackle finds a file at `cookies/EXAMPLE.txt`
  automatically for the `EXAMPLE` service.

    unshackle repairs the usual export damage in memory and does not change your
    file: a missing header line, a byte-order mark, spaces in place of tabs, and
    rows a text editor broke. unshackle refuses a JSON export. Export the cookies
    again in Netscape format.

- **Credentials**: store a username and password per service in your config:

    ```yaml title="unshackle.yaml"
    credentials:
      EXAMPLE: "email@example.com:your-password"
    ```

Use the `-p/--profile` flag to switch between multiple accounts for the same
service. Whether a given service needs cookies, credentials, or nothing at all
depends on the service module.

## 6. Find a title (optional)

If you have a URL or ID already, skip this. Otherwise, find a title on the service
and note the `id` it prints, because that is what you feed to `dl`:

```shell
unshackle search EXAMPLE "My Show"
```

You can also show what a service gives for a given title without downloading:

```shell
unshackle dl --list-titles EXAMPLE 81234567     # show seasons/episodes
unshackle dl --list EXAMPLE 81234567            # show available tracks
```

## 7. Do your first download

A download always has three parts:

```
unshackle dl  <FLAGS>  <SERVICE-TAG>  <TITLE>
```

- **`dl`** carries every quality, language, track, and output flag.
- **`<SERVICE-TAG>`** picks which service to talk to (e.g. `EXAMPLE`).
- **`<TITLE>`** is the URL, ID, or slug the service understands.

A good first command asks for 1080p with English audio:

```shell title="Your first download"
unshackle dl -q 1080 -l en EXAMPLE 81234567
```

unshackle will fetch the title, select the tracks that match your flags, and get
the content keys through your CDM (and any key vaults). It then decrypts, muxes, and
tags the result.

### Handy first flags

| Flag | Meaning |
|---|---|
| `-q`, `--quality` | Target resolution(s), e.g. `-q 1080` or `-q 1080,720`. Defaults to best available. |
| `-l`, `--lang` | Language(s) for video and audio, e.g. `-l en` or `-l orig,en`. `orig` = the title's original language. Defaults to `orig`. |
| `-sl`, `--s-lang` | Subtitle language(s); defaults to `all`. |
| `-v`, `--vcodec` | Video codec, e.g. `-v H.265`. Defaults to any. |
| `-r`, `--range` | Dynamic range, e.g. `-r HDR10` or `-r DV`. Defaults to `SDR`. |
| `-w`, `--wanted` | Which episodes, e.g. `-w S01` or `-w S01E01-S01E03`. For music, which tracks, e.g. `-w 1-5` or `-w 2x3` for disc 2 track 3. |
| `-o`, `--output` | Override the output directory for this run. |
| `--list` | List the tracks that would be downloaded, then stop. |

!!! example "A few realistic variations"
    ```shell
    # A whole first season in the best available quality
    unshackle dl -w S01 EXAMPLE 81234567

    # 2160p HDR10 with the original-language audio plus English subtitles
    unshackle dl -q 2160 -r HDR10 -l orig -sl en EXAMPLE 81234567

    # Just the newest episode of an ongoing show
    unshackle dl --latest-episode EXAMPLE 81234567
    ```

See [Downloading](../guide/downloading.md) for the complete flag reference, including
codec, bitrate, channel-layout, and track-type selection.

## 8. Where the output lands

By default, unshackle writes finished files to the `downloads` directory.
`unshackle env info` shows its exact path. The built-in default is a `downloads`
folder one level above the installed `unshackle` package. Override it for one
command with `-o`:

```shell
unshackle dl -q 1080 -o /mnt/media/incoming EXAMPLE 81234567
```

- unshackle writes **movies** as a single `.mkv` file named from your `movies` template.
- unshackle groups **TV episodes** into a per-show / per-season folder (from your
  `series` template) unless you pass `--no-folder`.
- unshackle builds the filename from your `output_template` and adds IMDb/TMDB/TVDB IDs to
  the file's metadata tags when available.

To change the default output location permanently, set it in your config:

```yaml title="unshackle.yaml"
directories:
  downloads: /mnt/media/incoming
```

## Where to go next

- **[Downloading](../guide/downloading.md)**. The full `dl` command: quality, codecs,
  languages, track selection, hybrid Dolby Vision, and output control.
- **[Configuration](configuration-file.md)**. Every `unshackle.yaml` config key, plus
  directories, key vaults, proxies, and naming templates.
- **[REST API](../dev/rest-api/index.md)**. Operate the `serve` server to drive downloads
  over HTTP.

!!! tip "You are never far from help"
    `unshackle --help`, `unshackle dl --help`, and `unshackle <command> --help`
    show every option. When in doubt, add `--list` to a `dl`
    command to preview what it would do before it downloads anything.
