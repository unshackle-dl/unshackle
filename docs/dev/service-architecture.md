# Service Architecture

This page is the conceptual map of the internal object model that a **service author**
works against. It ties together the moving parts: the `Service` base class, the
`Title` hierarchy a service returns, the `Track` model those titles carry, the
manifest parsers that produce tracks, the DRM hooks that license and decrypt them,
and the request/session objects wired up for you at construction time.

!!! info "Who this page is for"
    This is a **developer reference**. If you only want to *run* downloads, you never
    touch these classes directly; see [Downloading](../guide/downloading.md) and the
    [CLI reference](../guide/cli-reference.md) instead. If you are writing or reading a
    service plugin, this page explains what each object is and how the pieces connect.
    For a guided, step-by-step build, see [Creating a Service](creating-a-service.md);
    for exact signatures, see the auto-generated [Service API](../reference/api/service.md),
    [Titles API](../reference/api/titles.md), and [Tracks API](../reference/api/tracks.md).

## The shape of a download

A service is a translator between one streaming platform and unshackle's generic
pipeline. It never downloads or decrypts anything itself; it just answers questions.
The `dl` command drives the whole flow and only ever calls three required methods on
your service, in order:

```text
Service.get_titles        →  which movies / episodes / songs exist
      │
Service.get_tracks        →  fetch a manifest for one title ...
      │
manifest.to_tracks        →  ... parsed into Video / Audio / Subtitle tracks
      │
Track.download            →  fetch + merge segments (core-driven)
      │
DRM license + decrypt     →  service license hooks → CDM → decrypt tool
      │
Tracks.mux                →  mkvmerge → tagged .mkv
```

1. **`get_titles()`** returns a `Titles` collection: the movies, episodes, or songs
   the platform exposes for the requested ID.
2. **`get_tracks(title)`** returns a `Tracks` object for a single chosen title. In
   practice this means fetching a manifest and handing it to a parser, which builds
   the `Video`/`Audio`/`Subtitle` objects.
3. **`get_chapters(title)`** returns any chapter markers.

Everything after that (track selection, licensing, decryption, repacking, subtitle
conversion, and muxing) is handled by the core using the objects your service
returned. Your job is to produce accurate `Title` and `Track` objects; the framework
does the rest.

## The `Service` base class

Every service subclasses `unshackle.core.service.Service`, an abstract base class.
Two kinds of members matter: **class variables** that describe the service, and
**methods** that the pipeline calls.

### Class variables

Declared at the top of your service class, these are static descriptors:

| Class variable | Type | Purpose |
|---|---|---|
| `ALIASES` | `tuple[str, ...]` | Alternate tags the service answers to (resolved by `Services.get_tag`). |
| `GEOFENCE` | `tuple[str, ...]` | ISO region codes the service requires; empty means no geofence. The first entry is treated as the "main region" for automatic proxy selection. |
| `VAULT_TAG` | `Optional[str]` | Overrides the key-vault namespace; defaults to the service's own tag. |
| `AUTH_METHODS` | `Optional[tuple[str, ...]]` | Accepted auth methods (`"cookies"` / `"credentials"`). When `None`, the REST `/services` endpoint infers them from `authenticate()`. |
| `ANIME` | `bool` | The catalog is anime, so metadata lookups prefer AniList. A title's own `anime` flag overrides it. |
| `DAILY` | `bool` | The catalog is daily/date-based, so episodes are named by air date. A title's own `daily` flag overrides it. |

### Required methods

These are abstract; a service will not load unless it implements all three:

- `get_titles() -> Titles_T`: resolve the requested ID into a title collection.
- `get_tracks(title) -> Tracks`: build the track set for one title.
- `get_chapters(title) -> Chapters`: return chapter markers (may be empty).

### Optional methods and hooks

- **`authenticate(cookies=None, credential=None)`**: the base implementation loads a
  `CookieJar` into `self.session.cookies` and stores `self.credential`. Override it to
  perform a real login handshake.
- **`search()`**: raises `NotImplementedError` by default; implement it to support the
  `search` command.
- **DRM license hooks**: `get_widevine_service_certificate`, `get_widevine_license`,
  `get_playready_license` (delegates to Widevine by default), and
  `get_clearkey_license` (returns `None` by default). See [DRM hooks](#drm-hooks) below.
- **`request_input(prompt)`**: prompts the user; routed through the serve-mode input
  bridge or the rich console.
- **Event callbacks**: `on_segment_downloaded`, `on_track_downloaded`,
  `on_track_decrypted`, `on_track_repacked`, `on_track_multiplex` let a service react to
  pipeline events (for example, MonaLisa decrypts each segment in `on_segment_downloaded`).
- **`resolve_server_keys(title)`**: an escape hatch for services whose decryption keys are
  resolved server-side *ahead of* the normal per-track license flow. Unlike everything else
  here it is **not declared on the `Service` base class at all**: `dl.py` probes for it with
  `hasattr` and calls it (once, after track selection) only if your service defines it. Use
  it when keys must be fetched or injected in bulk before download, separate from the
  per-track license callbacks below.

### What `__init__` wires up for you

You rarely override `__init__`; the base constructor takes the Click context and sets
up the objects your methods rely on:

| Attribute | What it is |
|---|---|
| `self.config` | The per-service config slice (see [Request and session objects](#request-and-session-objects)). |
| `self.log` | A `logging.Logger` named after your class. |
| `self.session` | A ready-to-use HTTP session (headers, retries, and proxy applied). |
| `self.cache` | A generic key/value `Cacher` namespaced to the service. |
| `self.title_cache` | A region- and account-aware `TitleCacher`. |
| `self.cache_dir` | `config.directories.cache / <ServiceName>`. |
| `self.credential` | The active `Credential`, or `None` until authenticated. |
| `self.track_request` | A `TrackRequest` describing the requested codecs, ranges, and best-available flag (see below). |

The constructor also resolves proxies: it reads `--proxy` / `--proxy-query` /
`--proxy-provider`, consults the per-service `proxy_map`, and, if `GEOFENCE` is set and
no explicit proxy was given, does a live IP check and auto-fetches a proxy to
`GEOFENCE[0]` when it detects a geoblock. See [Proxies and VPN](../guide/proxies-and-vpn.md).

!!! note "`TrackRequest`"
    `self.track_request` is a small dataclass built from the CLI: `codecs`
    (from `--vcodec`, empty means "accept any"), `ranges` (from `--range`, default
    `[Video.Range.SDR]`), and `best_available`. Use it in `get_tracks` to decide which
    variants to fetch.

## Titles and the `Title` hierarchy

`get_titles()` returns a **title collection**, one of the `Titles_T` types. Each
collection holds one or more `Title` objects (`Title_T`). There are three parallel
families, one per media kind:

| Media kind | Item (`Title_T`) | Collection (`Titles_T`) |
|---|---|---|
| Movies | `Movie` | `Movies` |
| TV | `Episode` | `Series` |
| Music | `Song` | `Album`, and `Music` (the base collection) |

All items subclass `Title`, which carries the shared identity: a **unique `id`**, the
originating `service` class, an optional original `language`, arbitrary `data`, and a
freshly-constructed `self.tracks = Tracks()` that `get_tracks` will fill. Collections
are sorted lists, so titles stay in a stable order.

!!! warning "The unique ID is validated"
    `Title.__init__` rejects a falsy `id_`, and rejects any ID shorter than 4 characters
    (`"The unique ID is not large enough, clash likely."`). Pick a stable, sufficiently
    unique identifier from the platform.

### Constructors at a glance

Each subclass adds its own required fields on top of `id_` and `service`:

=== "Movie"

    ```python
    Movie(id_, service, name, year=None, language=None, data=None, description=None)
    ```

    `name` is required; `year` is optional but must be a positive integer if given.

=== "Episode"

    ```python
    Episode(id_, service, title, season, number,
            name=None, year=None, language=None, data=None,
            description=None, air_date=None)
    ```

    `title` is the **show** name; `season` and `number` are required (0 is allowed).
    An `air_date` is normalized to a `date` so the `{date}` naming token stays clean,
    and a placeholder episode `name` like `"Episode 4"` is discarded automatically.

=== "Song"

    ```python
    Song(id_, service, name, artist, album, track, disc, year,
         language=None, data=None)
    ```

    `name`, `artist`, `album`, `track`, `disc`, and `year` are all required and validated.

### Title remapping

After titles are fetched (and after any cache), the framework applies
`remap_titles(titles, title_map)` using the per-service `title_map` config. This does an
**exact-string** rename (`Episode`s are matched on their show `title`, `Movie`s and
`Song`s on their `name`), so a platform's odd naming can be normalized to what your
library expects. Titles are also cache-wrapped via `get_titles_cached()`, which is region-
and account-aware; see the [TitleCacher](#request-and-session-objects) notes.

The ordering here is deliberate on both sides. `title_map` is applied **after** the title
cache so that editing the map takes effect immediately, without forcing a cache reset, and
**before** metadata enrichment (`--enrich`) so that an explicit enrich override still wins
over a mapped name.

## Tracks and the `Track` model

A `Tracks` object is the container `get_tracks` returns. It has typed buckets
(`videos`, `audio`, `subtitles`, `chapters`, and `attachments`) plus a `manifest_url`.
Iterating a `Tracks` yields **only** video, audio, and subtitle tracks (not chapters or
attachments), and the canonical display/mux order is Video → Audio → Subtitle → Chapter
→ Attachment.

!!! warning "Track IDs must be deterministic"
    `Tracks.add` refuses duplicate IDs. Track IDs are CRC32-derived from the track's
    identity (URL, codec, language, bitrate) and **must be stable across requests**, never
    random. The manifest parsers already do this correctly; if you build tracks by hand,
    keep IDs deterministic or `add` will raise.

### The base `Track`

`Track` is the base for `Video`, `Audio`, and `Subtitle`. (`Chapter`, `Chapters`, and
`Attachment` are standalone classes, **not** `Track` subclasses.) Every track knows how
it is delivered, via `Track.Descriptor`:

| Descriptor | Delivery |
|---|---|
| `URL` | A single direct URL. |
| `HLS` | HTTP Live Streaming. |
| `DASH` | Dynamic Adaptive Streaming over HTTP. |
| `ISM` | Smooth Streaming. |

Key attributes include `url` (a string or list of strings), `language` (a
`langcodes.Language`), `drm` (an optional iterable of DRM objects), `needs_repack`, and
descriptor-specific state stashed in `track.data["hls"]` / `track.data["dash"]`. When
`name` is omitted, a human-readable name is derived from the language automatically.

### `Video`

`Video` adds `codec`, `range`, `bitrate`, `width`, `height`, `fps`, `scan_type`, and
closed-caption descriptors. The relevant enums:

- **`Video.Codec`**: `AVC` (H.264), `HEVC` (H.265), `VC1`, `VP8`, `VP9`, `AV1`.
- **`Video.Range`**: `SDR`, `HLG`, `HDR10`, `HDR10P` (HDR10+), `DV` (Dolby Vision), and
  `HYBRID`. `HYBRID` is special: it tells the pipeline to select both an HDR10(+) base and
  a DV track and merge the DV RPU onto the HDR10 stream with `dovi_tool` (see
  [Dolby Vision hybrid](#dolby-vision-and-hybrid)).

!!! note "Bitrate units"
    Both `Video` and `Audio` store `bitrate` in **bytes per second** (rounded up), even
    though the string form displays kb/s.

### `Audio`

`Audio` adds `bitrate`, `channels`, `joc` (Joint Object Coding, the Atmos indicator),
and `descriptive`. `Audio.Codec` covers `AAC`, `AC3` (`DD`), `EC3` (`DD+`), `AC4`,
`OPUS`, `OGG` (Vorbis), `DTS`, `ALAC`, and `FLAC`. The `atmos` property is `True` when
`joc` is set (or when `extra` flags it).

### `Subtitle`

`Subtitle` adds three mutually-constrained flags: `cc` (closed captions), `sdh`
(subtitles for the deaf and hard-of-hearing), and `forced`. A track cannot be both `cc`
and `sdh`, and `forced` cannot combine with either. `Subtitle.Codec` includes `SubRip`
(SRT), `WebVTT` (VTT), `SubStationAlpha`/`SubStationAlphav4` (SSA/ASS),
`TimedTextMarkupLang` (TTML), `SAMI`, and the box-encapsulated DASH formats `fTTML`
(STPP) and `fVTT` (WVTT). Subtitles receive post-download normalization
(segment merging, WebVTT sanitization, format conversion, and optional SDH stripping),
all documented for users in [Subtitles](../guide/subtitles.md).

### Chapters and attachments

- **`Chapter(timestamp, name=None)`**: a single marker; the timestamp accepts
  `HH:MM:SS[.mmm]`, integer milliseconds, or float seconds.
- **`Chapters`**: a sorted collection that auto-inserts a `00:00:00.000` chapter and
  reads/writes OGM Simple format.
- **`Attachment`**: a font or image attached to the mux (e.g. fonts an ASS subtitle
  references), built from a local `path` or a `url`.

### Selection, sorting, and muxing

`Tracks` is also the selection engine the `dl` command uses. It offers
`sort_videos` / `sort_audio` / `sort_subtitles` (P2P-standard ordering), predicate-based
`select_*` filters, `by_resolutions`, and `by_language`. When downloads finish, the same
object multiplexes everything: **`Tracks.mux(title, ...)`** builds and runs an
`mkvmerge` command, applying language, default-track, forced, and hearing-impaired flags,
attaching chapters and fonts, and writing a `.muxed.mkv` (or `.mka` / `.mks` for
audio- or subtitle-only sets). As a service author you never call `mux` yourself; you
just return correct tracks.

## Manifest parsers

Most services do not build tracks by hand; they hand a manifest to one of three parsers
in `unshackle.core.manifests`. Each parser has the same three-part contract:

| Format | Class | Notes |
|---|---|---|
| MPEG-DASH (`.mpd`) | `DASH` | Parses `<MPD>`; enumerates tracks from the first content period. |
| HLS (`.m3u8`) | `HLS` | Requires a **variant (master) playlist**, not a media playlist. |
| Smooth Streaming (`.ism`) | `ISM` | Synthesizes init segments from `CodecPrivateData`. |

### The parser contract

1. **Construct**: `DASH.from_url(url, session=...)` or `DASH.from_text(text, url)`
   (and likewise for `HLS` / `ISM`). A `url` is always required, even for `from_text`,
   because relative segment paths are resolved against it.
2. **`to_tracks(...)`**: parses the manifest into a `Tracks` container of `Video`,
   `Audio`, and `Subtitle` objects, each carrying descriptor-specific state.
3. **`download_track(...)`**: a staticmethod the core calls to fetch, decrypt, and merge
   segments. You do not call it; you just return the tracks it will operate on.

A typical `get_tracks` therefore looks like fetching the manifest text with
`self.session` and returning `DASH.from_text(mpd, url).to_tracks(language=title.language)`.

!!! tip "Pass the original language"
    DASH and HLS `to_tracks` raise `ValueError` if a track's language cannot be derived
    and no valid fallback was supplied. Always pass the title's original recorded
    language. For HLS this fallback is used directly for the primary track, since HLS
    variants carry no language.

### Segment-filtering hooks

- **HLS**: set `track.OnSegmentFilter(segment)` to drop unwanted segments (ads/bumpers).
- **DASH**: pass a `period_filter` callable to `to_tracks` to drop whole periods.
  Caching-breadth periods some services emit and non-`content` segment types are dropped automatically.

### Format gotchas worth knowing

- **DASH** enumerates tracks from only the *first* content period, but downloads segments
  from *all* content periods matching the representation. `SegmentTemplate`,
  `SegmentList`, and `SegmentBase` addressing and `$Number%0Nd$` padding are handled.
- **HLS** re-fetches the media playlist at download time, recovers `SUPPLEMENTAL-CODECS`
  from raw text (python-m3u8 drops it) to detect Dolby Vision composite streams, and uses
  the media-sequence-number IV for AES-128 keys that omit an explicit IV.
- **ISM** has no segment list and no init segments: segment URLs are synthesized from the
  `<c>` fragment timeline, and a `moov`/`ftyp` init box is rebuilt from `CodecPrivateData`
  (supported for AVC, HEVC, DV-over-HEVC, AAC, EC-3, and TTML).

## DRM hooks

If a track carries DRM, the framework licenses and decrypts it, but it needs your
service to talk to the platform's license server. unshackle recognizes five DRM systems:

| DRM class | System | License flow |
|---|---|---|
| `Widevine` | Widevine | CDM challenge → license server → content keys (`.wvd` device) |
| `PlayReady` | PlayReady | CDM challenge (WRM header) → license server → content keys (`.prd` device) |
| `ClearKeyCENC` | W3C `org.w3.clearkey` over CENC | JSON KID request → JWK set; no CDM |
| `ClearKey` | HLS AES-128 | key fetched directly from a URI; no CDM, no challenge |
| `MonaLisa` | Proprietary WASM DRM | keys extracted locally; no license server |

!!! warning "`ClearKey` vs `ClearKeyCENC`"
    Despite the shared prefix these are entirely different systems. `ClearKey` is HLS
    AES-128, decrypted in-process. `ClearKeyCENC` is the W3C EME JWK flow over MPEG-CENC,
    decrypted with shaka-packager or mp4decrypt like Widevine.

### The service-side hooks

The framework supplies the CDM and the decryption tool; your service provides the network
calls that the CDM's challenge must be sent through. Override the hooks that your platform
needs:

| Hook | Return | Purpose |
|---|---|---|
| `get_widevine_service_certificate(title, track, challenge)` | cert bytes | Optional privacy-mode service certificate. |
| `get_widevine_license(title, track, challenge)` | license bytes | POST the Widevine challenge to the license server, return the response. |
| `get_playready_license(title, track, challenge)` | license | Same for PlayReady; defaults to delegating to Widevine. |
| `get_clearkey_license(title, track, challenge)` | dict / str / bytes / `None` | For `ClearKeyCENC`; return `None` to let the framework POST to the manifest `Laurl`. |

You do not open CDM sessions or run shaka-packager yourself. The `Widevine`/`PlayReady`
classes drive the CDM handshake and invoke the decryption tool. Your hook is just the
"send this challenge, give me back the license" step.

### How a CDM is chosen and keys are found

CDM selection is config-driven: the `cdm` map resolves a service tag (and optionally the
track quality or DRM type) to a device name, which `load_cdm` instantiates as a local
`.wvd`/`.prd` or a remote CDM. Before ever requesting a license, the framework tries an
in-process key cache and the configured **key vaults**; only unresolved KIDs trigger an
actual license request, and newly licensed keys are written back to every vault. This is
why the same key is rarely re-licensed twice. Users configure all of this in
[DRM and CDM](../guide/drm-and-cdm.md) and [Key vaults](../guide/vaults.md); the API
surface is documented in the [DRM API reference](../reference/api/drm.md).

### Dolby Vision and hybrid

Two DV mechanisms sit on top of the track model. **DV fixup** re-exposes DV signaling on
HLS streams that carry a valid DV RPU but advertise plain HEVC (`dv_compatible_bitstream`).
**Hybrid** processing (`Video.Range.HYBRID`) selects an HDR10(+) base and a DV donor,
extracts the RPU with `dovi_tool`, and injects it onto the base layer, producing a single
stream with both HDR10 and DV metadata. Both are automatic once the right tracks are
selected; a service only needs to expose accurate ranges.

## Request and session objects

Everything a service does over the network flows through objects the base constructor
prepared, all derived from the Click context (`ctx`) that `dl` passes in.

- **`self.config`**: your service's slice of `unshackle.yaml`, i.e.
  `config.services[<ServiceName>]`. This is a plain dict; use it for per-service settings
  like base URLs, an API key, or a `title_map`. See
  [Configuration](../getting-started/configuration-file.md).
- **`self.session`**: a `requests.Session` with `config.headers` applied, a retry policy
  (`total=5`, backoff, retry on 429/5xx), and any resolved proxy already mounted. Pass this
  session into the manifest parsers and DRM calls so headers, cookies, and proxies flow
  everywhere. (unshackle also ships an rnet-based TLS-fingerprinting session used by the
  newer `session()` factory and configured via the `network` config key.)
- **`self.credential`**: the active `Credential` (`username:password[:extra]`), populated
  by `authenticate`. Its `sha1` is used as the account hash for cache keys.
- **`self.cache`**: a generic `Cacher`: JSON key/value storage under the service's cache
  dir, with CRC32 integrity checks, versioning, and optional expiry (auto-derived from a
  JWT `exp` claim when present). Good for auth tokens and API responses.
- **`self.title_cache`**: a `TitleCacher` that wraps `get_titles` results, keyed by title
  ID, region, and account hash. It serves fresh data within `title_cache_time`, falls back
  to stale data within `title_cache_max_retention` when an API call fails, and is bypassed
  by `--no-cache`.
- **`self.track_request`**: the `TrackRequest` describing what the user asked for
  (codecs, ranges, best-available), for use inside `get_tracks`.

!!! example "A minimal service skeleton"
    ```python title="services/EXMP/__init__.py"
    from unshackle.core.service import Service
    from unshackle.core.manifests import DASH
    from unshackle.core.titles import Movie, Movies


    class EXMP(Service):
        ALIASES = ("example",)
        GEOFENCE = ("us",)

        def get_titles(self) -> Movies:
            data = self.session.get(f"https://api.example.com/title/{self.title}").json()
            return Movies([
                Movie(
                    id_=data["id"],
                    service=self.__class__,
                    name=data["name"],
                    year=data.get("year"),
                    language=data.get("language"),
                )
            ])

        def get_tracks(self, title) -> "Tracks":
            mpd_url = self.session.get(...).json()["manifest"]
            mpd = self.session.get(mpd_url).text
            return DASH.from_text(mpd, mpd_url).to_tracks(language=title.language)

        def get_chapters(self, title):
            return []

        def get_widevine_license(self, *, challenge, title, track):
            return self.session.post("https://lic.example.com/wv", data=challenge).content
    ```

    The directory name (`EXMP`) must exactly match the class name; that stem is the
    service tag. Put your service in one of the `directories.services` paths.

## Where to go next

- **[Creating a Service](creating-a-service.md)**: a guided walkthrough that builds on this model.
- **[Service API](../reference/api/service.md)**: exact signatures for the `Service` base class.
- **[Titles API](../reference/api/titles.md)** and **[Tracks API](../reference/api/tracks.md)**: the full object reference.
- **[DRM API](../reference/api/drm.md)**: the DRM and CDM classes in detail.
- **[Downloading](../guide/downloading.md)**: how the flags a user passes map onto track selection.
