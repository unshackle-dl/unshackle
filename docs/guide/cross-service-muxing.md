# Cross-service muxing

Some titles are on more than one service, and each service is strong at something
different: one carries the 4K HDR video, another the better dub, a third the fuller
subtitles. Cross-service muxing lets you download the video from one service and pull
the audio, subtitles, or chapters from others, then mux the result into a single file.

!!! warning "This is a power-user feature"
    unshackle does not check that the services carry the same cut of the title. You are
    responsible for pairing services that share the same edit. A different cut, a
    different frame rate, or an extra recap will not line up, and a single offset cannot
    fix it. See [Limitations](#limitations).

## How it works

You download from a **primary** service as normal, the positional `SERVICE URL` at the
end of the command. Each `--cross-*` flag names **another** service tag and URL to take a
track type from:

- **Video** and **chapters** *replace* what the primary provides. There is only ever one
  video source and one set of chapters.
- **Audio** and **subtitles** *merge* with the primary's own tracks. Your normal track
  selection (`-l`, quality, codec, channels) then runs across the combined set, so the
  best version of each language wins whichever service it came from.

```bash
# Download EXAMPLE's video, but take the audio from EXAMPLE2
unshackle dl --cross-audio EXAMPLE2 "https://example2.com/show/1" EXAMPLE "https://example.com/show/1"
```

You can combine several sources in one run:

```bash
# Video from the primary, a dub from one service, subtitles from another
unshackle dl \
    --cross-audio EXAMPLE2 "https://example2.com/show/1" \
    --cross-subtitles EXAMPLE3 "https://example3.com/show/1" \
    EXAMPLE "https://example.com/show/1"
```

The full flag list is in the [CLI reference](cli-reference.md#cross-service-muxing).

## Authentication, DRM, and proxies

Each cross-service behaves like a download of its own. It authenticates with **its own**
credentials, licenses and decrypts with **its own** CDM, and stores keys in **its own**
key vault. A PlayReady-only cross-service works even when the primary uses Widevine,
because it loads the CDM its own configuration asks for.

!!! note "You need credentials for every service"
    A cross-service authenticates the same way it would on a normal download, so its
    profile, cookies, or credentials must be set up. Use `--cross-profile` to pick a
    profile for the cross-services (it defaults to `--profile`).

By default each cross-service resolves **its own** proxy from its configuration, so a
geo-restricted source still reaches the right region. Set `--cross-proxy` to force one
proxy for every cross-service instead.

## Choosing the episode

For a TV title, each cross-service is matched to the **same season and episode** as the
title you download. When a service numbers its episodes differently, override the match:

- `--cross-wanted S01E03` sets a shared override for every cross-service.
- `--cross-audio-wanted`, `--cross-video-wanted`, `--cross-subtitles-wanted`, and
  `--cross-chapters-wanted` are per-type overrides that win over `--cross-wanted`.

If a cross-service has no title matching the request, unshackle warns and keeps the
primary's own tracks for that type.

## Offsets

If a cross-service begins with a short buffer the primary does not have, such as an advert
or a splash screen, its tracks start a few seconds late. Nudge them back into line:

```bash
# EXAMPLE2's audio starts 3 seconds late; shift it earlier
unshackle dl --cross-audio EXAMPLE2 "https://example2.com/show/1" \
    --cross-audio-offset -3s EXAMPLE "https://example.com/show/1"
```

`--cross-audio-offset` and `--cross-subtitle-offset` accept `10s`, `500ms`, or `-5.5s`; a
bare number is milliseconds. The offset is passed to the muxer, which shifts the track's
start. It does not re-time or resample the media, so a single offset only corrects a
constant lead or lag, not drift that grows through the title.

## Limitations

Cross-service muxing assumes the sources are the same cut of the title. The current
release does not:

- **Sync mismatched cuts.** Different edits, ad breaks, or frame rates are out of scope;
  the offset is a manual nudge for a constant lead, not automatic synchronisation.
- **Pin a language to a service.** Merged audio and subtitles are chosen by the normal
  best-per-language selection. You cannot yet force the English from one service when
  another service's English scores higher.
- **Reuse a cross-service across a season.** Each title in a season download loads and
  authenticates its cross-services again, so a login-rate-limited service is contacted
  once per episode.
- **Run a cross-service's own post-download steps.** Only the primary service's
  post-download hooks fire.
