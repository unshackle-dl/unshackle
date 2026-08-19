# unshackle

**unshackle** is a modular archival tool for **movies, TV, and music**. It fetches
media from streaming manifests, handles the DRM, and muxes everything into clean,
well-tagged files, all driven from a single command-line tool, or served over a
REST API for automation.

It is a fork of [Devine](https://github.com/devine-dl/devine) with DASH, HLS, and
Smooth Streaming (ISM) parsing, both **Widevine** and **PlayReady** DRM, and a
built-in HTTP server.

!!! warning "Use responsibly"
    The [GPL-3.0](https://github.com/unshackle-dl/unshackle/blob/main/LICENSE) licence covers unshackle.
    Do not use it for content you do not have the rights to. Keep the core free and
    open, keep your own services private, and be kind.

## What it does

unshackle does everything except talk to the streaming platform. You write that part
as a service module ([Creating a Service](dev/creating-a-service.md)). Point unshackle
at a title and it will:

- Fetch the title metadata (movies, full series, seasons, episodes, albums, tracks).
- Parse the streaming manifest: **DASH**, **HLS**, or **Smooth Streaming (ISM)**.
- Select the tracks you asked for by resolution, codec, bitrate, range, language,
  and channel layout.
- Get the content keys through a **Widevine** or **PlayReady** CDM, with optional
  **key vaults** that let unshackle reuse a content key instead of licensing it again.
- Decrypt, extract closed captions, repack, and **mux** the result into a Matroska
  (`.mkv`) file with correct language, forced, and default flags.
- Tag and name the output using configurable filename templates.

## Feature highlights

| Area | What you get |
|---|---|
| Media types | Movies, TV series/seasons/episodes, and music albums/tracks |
| Manifests | DASH, HLS, and Smooth Streaming (ISM) |
| DRM | Widevine and PlayReady, with ClearKey support |
| Video | AVC, HEVC, VC-1, VP8, VP9, AV1; SDR, HLG, HDR10, HDR10+, Dolby Vision, and Dolby Vision **hybrid** merging |
| Audio | AAC, AC-3, E-AC-3, AC-4, Opus, Vorbis, DTS, ALAC, FLAC; Dolby Atmos handling; per-channel-layout selection |
| Subtitles | SRT, WebVTT, ASS/SSA, TTML and more, with optional SDH stripping and format conversion |
| Key vaults | SQLite, MySQL, and HTTP/API vaults to store and share content keys |
| Proxies | Basic proxies plus NordVPN, ProtonVPN, Surfshark, Windscribe, ExpressVPN, Gluetun, and Hola providers |
| Services | A plugin API for the service modules you write. unshackle includes none |
| Automation | A REST API with a job queue, live progress, history, and remote-download sessions |

## Installation at a glance

=== "Install as a tool"

    ```shell
    uv tool install git+https://github.com/unshackle-dl/unshackle.git
    unshackle --help
    ```

=== "Run from a clone"

    ```shell
    git clone https://github.com/unshackle-dl/unshackle.git
    cd unshackle
    uv run unshackle --help
    ```

!!! note "External tools"
    unshackle shells out to FFmpeg, MKVToolNix, shaka-packager, Bento4, and
    `dovi_tool` for decryption, repacking, muxing, and Dolby Vision work. See
    [Installation](getting-started/installation.md) for the full list and recommended versions.

## A first download

Every download is three parts: the `dl` command, a **service tag**, and the
service's title argument (a URL, ID, or slug).

```shell title="Download a 1080p title with English audio"
unshackle dl -q 1080 -l en EXAMPLE 81234567
```

The `dl` command carries all the quality, language, and output flags. The service
tag (`EXAMPLE` above) selects which streaming service to talk to, and unshackle passes
the value after it to that service. See the [Quickstart](getting-started/quickstart.md)
to go from a fresh install to your first file.

## Where to go next

- **[Installation](getting-started/installation.md)**. Install unshackle and the external tools it depends on.
- **[Quickstart](getting-started/quickstart.md)**. Configure `unshackle.yaml` and do your first download.
- **[Downloading](guide/downloading.md)**. The full `dl` command guide: quality, codecs, languages, track selection, and output.
- **[Configuration](getting-started/configuration-file.md)**. Where `unshackle.yaml` lives, the directory layout, and how to edit values. Full config key list in the [Configuration Reference](reference/configuration/index.md).
- **[REST API](dev/rest-api/index.md)**. Operate the `serve` HTTP server and drive downloads programmatically.

!!! tip "Getting help"
    Operate `unshackle --help` or `unshackle dl --help` at any time. The community
    [Discord](https://discord.gg/mHYyPaCbFK) is the fastest place to ask questions.
