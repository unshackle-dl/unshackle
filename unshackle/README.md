<p align="center">
    <img width="16" height="16" alt="no_encryption" src="https://github.com/user-attachments/assets/6ff88473-0dd2-4bbc-b1ea-c683d5d7a134" /> unshackle
    <br/>
    <sup><em>Movie, TV, and Music Archival Software</em></sup>
    <br/>
      <a href="https://discord.gg/mHYyPaCbFK">
        <img src="https://img.shields.io/discord/1395571732001325127?label=&logo=discord&logoColor=ffffff&color=7289DA&labelColor=7289DA" alt="Discord">
    </a>
</p>

<p align="center">
  <a href="#install">Install</a> &nbsp;·&nbsp;
  <a href="https://docs.unshackle.dev">Docs</a> &nbsp;·&nbsp;
  <a href="https://discord.gg/mHYyPaCbFK">Discord</a>
</p>

---

A modular archival tool for movies, TV, and music. Fork of [Devine](https://github.com/devine-dl/devine/) with DASH/HLS/ISM parsing, Widevine & PlayReady DRM, and a REST API.

<p align="center">
  <a href="https://asciinema.org/a/ldMiqYFFTgPAOxW7">
    <img src="https://asciinema.org/a/ldMiqYFFTgPAOxW7.svg" alt="unshackle demo" width="700">
  </a>
</p>

## Install

```shell
uv tool install git+https://github.com/unshackle-dl/unshackle.git
unshackle --help
```

> [!TIP]
> Prefer `uv run unshackle ...` inside a clone to keep the virtual environment active.

### Requirements

External tools on your `PATH` (recommended versions):

- [Python](https://www.python.org/) - 3.11 - 3.14
- [uv](https://docs.astral.sh/uv/) - ≥ 0.5
- [FFmpeg](https://ffmpeg.org/) - ≥ 6.0
- [MKVToolNix](https://mkvtoolnix.download/) - ≥ 80
- [shaka-packager](https://github.com/shaka-project/shaka-packager/releases/tag/v2.6.1) - 2.6.1
- [Bento4](https://github.com/axiomatic-systems/Bento4) - ≥ 1.6.0-639
- [dovi_tool](https://github.com/quietvoid/dovi_tool) - ≥ 2.1

Optional:

- [SubtitleEdit](https://github.com/SubtitleEdit/subtitleedit/releases) - ≥ 5.0 (`SeConv` CLI)

## License

[GPL-3.0](LICENSE). Do not use unshackle for content you lack the rights to. Keep the core free and open; keep service code private. Be kind.
