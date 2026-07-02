# Creating a Service

This guide walks through writing a new service adapter, section by section, using the bundled
reference implementation at
[`unshackle/services/EXAMPLE/__init__.py`](https://github.com/unshackle-dl/unshackle/blob/main/unshackle/services/EXAMPLE/__init__.py)
as the canonical example. `EXAMPLE` is deliberately exhaustive - it shows every framework feature
in one file so you have one place to copy from. It does not run against a real API.

## File layout

A service is a directory under `unshackle/services/` named by its **tag** (the short code used on
the CLI - `EXAMPLE`, or its aliases `EX`/`DOMAIN`, in the reference service). The directory needs:

```
unshackle/services/TAG/
├── __init__.py     # required - the Service subclass
└── config.yaml     # optional - endpoints, headers, client profiles
```

The class inside `__init__.py` **must** have the same name as the directory (`class TAG(Service):`
in `services/TAG/__init__.py`). `unshackle/core/services.py` discovers services by globbing
`services/*/__init__.py` and looking up a class named after the directory; a mismatch raises
`no class named '<TAG>' found ... the class name must match the directory name` at load time, so
the failure is loud, not silent.

## Lifecycle at a glance

Before the method-by-method breakdown, here's the order the framework calls your service during a
download - your methods are the hooks, the framework drives the loop:

```
unshackle dl TAG <title>
  └─ cli(ctx, title=...)            → constructs TAG(ctx, ...)
       └─ __init__ → super().__init__(ctx)   builds session/cache/track_request, resolves geofence proxy
  1. authenticate(cookies, credential)        login / token exchange (always called; base just loads cookies/credential)
  2. get_titles()                              → Movies / Series / Album   (wrapped by get_titles_cached + title_map)
     · framework applies episode/season filters
  3. per title:
       a. get_tracks(title)                    → Tracks   (parse manifest, post-process, store license data)
       b. get_chapters(title)                  → Chapters
       c. framework selects tracks             resolution / range / codec / language  (best_available softens misses)
       d. per track: download → license callback → decrypt → repackage → mux → move to final filename
```

Two things follow from this order:

- **`get_tracks()` runs before any track selection**, so it must return *everything*; the framework
  filters afterward in step (c).
- **The license callbacks** (`get_widevine_license` etc.) fire in step (d), long after
  `get_tracks()` returned - they only receive `challenge`, `title`, and `track` (nothing else from
  `get_tracks()`), so anything they need (license URL, session token) must be stashed during
  `get_tracks()`.

## Class docstring and class variables

```python
class EXAMPLE(Service):
    """
    Reference service for domain.com - ...

    Version: 2.0.0
    Author: sp4rk.y
    Date: 2026-06-28
    Authorization: Cookies + Credentials
    Geofence: US, UK
    Robustness:
        Widevine:
            L1: 2160p, HDR10, HDR10+, DV
            L3: 1080p, SDR
        PlayReady:
            SL3000: 2160p
        ClearKey: 1080p (DRM-free fallback)

    Tips:
        - Input may be a full URL or a bare ID/slug
    """

    ALIASES = ("EX", "DOMAIN")
    GEOFENCE = ("US", "UK")
    TITLE_RE = r"^(?:https?://(?:www\.)?domain\.com/details/)?(?P<title_id>[^/?#]+)"
    NO_SUBTITLES = False
    VAULT_TAG = "DIFFERENT_NAME"
```

The docstring **is** the `unshackle dl TAG --help` output, so it follows Click's conventions:

- Keep `Version`, `Author`, `date`, `Authorization`, `Geofence`, and `Robustness` - they're the fields
  users and other contributors check first when deciding whether a service still works.

Class variables, all optional except where noted:

| Variable | Purpose |
|---|---|
| `ALIASES: tuple[str, ...]` | Extra CLI tags that resolve to this service, e.g. `dl EX ...` |
| `GEOFENCE: tuple[str, ...]` | ISO country codes required; the framework warns/auto-proxies on mismatch |
| `TITLE_RE: str` | Regex with named groups (e.g. `(?P<title_id>...)`) used in `get_titles()` to accept either a full URL or a bare ID |
| `NO_SUBTITLES: bool` | Convention checked via `hasattr` by `dl.py`, not declared on the base class - set `True` if the service has no subtitle tracks, to skip subtitle handling |
| `VAULT_TAG: Optional[str]` | Store/read keys under a different vault namespace than this service's own tag - lets sibling services (e.g. regional variants of the same backend) share one key vault |
| `AUTH_METHODS: tuple[str, ...]` | Declares which of `"cookies"`/`"credentials"` the service accepts. Optional - when unset, `unshackle serve`'s `/services` endpoint infers it by inspecting `authenticate()`'s source. Set it explicitly if that inference would guess wrong |

See [Service Integration & Authentication](SERVICE_CONFIG.md#service-class-conventions) for the
full list of `self.*` helpers available once `super().__init__(ctx)` has run.

## `cli()` - the Click entry point

```python
@staticmethod
@click.command(name="EXAMPLE", short_help="https://domain.com", help=__doc__)
@click.argument("title", type=str)
@click.option("-m", "--movie", is_flag=True, default=False, help="Treat the title as a movie.")
@click.option(
    "-d", "--device",
    type=click.Choice(["android_tv", "web", "ios"], case_sensitive=False),
    default="android_tv",
    help="Client profile block to use from config.yaml.",
)
@click.pass_context
def cli(ctx: click.Context, **kwargs: Any) -> EXAMPLE:
    return EXAMPLE(ctx, **kwargs)
```

Keep `name="EXAMPLE"` in `@click.command` matching the class/directory name - resolution of
`unshackle dl EXAMPLE ...` is actually driven by the directory name matching the class name (see
File layout above), but a mismatched `name=` produces confusing `--help`/usage output.
`help=__doc__` reuses the class docstring above as the `--help` text. Add `@click.option` entries here for anything a user should be able to toggle per
download (device/client profile, movie-vs-series disambiguation, region, etc.) - they arrive as
keyword arguments into `__init__`.

## `__init__()`

```python
def __init__(self, ctx: click.Context, title: str, movie: bool, device: str):
    self.title = title
    self.movie = movie
    self.device = device

    super().__init__(ctx)

    self.cdm = ctx.obj.cdm
    self.is_playready = is_playready_cdm(self.cdm)
    self.is_widevine = is_widevine_cdm(self.cdm)

    if self.is_playready:
        self.device = "playready_tv"
    elif self.is_widevine:
        self.log.info(" + Widevine CDM detected")

    # The active -p/--profile, used below to key per-profile token caches.
    self.profile = (ctx.parent.params.get("profile") if ctx.parent else None) or "default"

    if any(r != Video.Range.SDR for r in self.track_request.ranges):
        self.track_request.codecs = [Video.Codec.HEVC]

    if self.is_widevine and getattr(self.cdm, "security_level", None) == 3:
        self.track_request.codecs = [Video.Codec.AVC]
        self.track_request.ranges = [Video.Range.SDR]

    if self.config is None:
        raise EnvironmentError("config.yaml is missing for this service.")
```

Order matters here:

1. Store CLI args **before** `super().__init__(ctx)` only if base-class init needs them; otherwise
   it doesn't matter, but storing first is the convention every service follows.
2. `super().__init__(ctx)` wires up `self.config`, `self.log`, `self.session`, `self.cache`,
   `self.title_cache`, `self.current_region`, and builds `self.track_request` from the global `dl`
   flags. Nothing below this line works without it. (`self.request_input` is a base-class method,
   always available - it isn't assigned here.)
3. After that, inspect `ctx.obj.cdm` and classify it with `is_playready_cdm`/`is_widevine_cdm`
   (`unshackle.core.cdm.detect`) - never hand-roll `isinstance` checks, these helpers also handle
   remote/wrapper CDMs.
4. Read or rewrite `self.track_request` for service-level constraints: e.g. force HEVC when any
   non-SDR range was requested, or clamp a Widevine L3 box down to AVC/SDR.

`self.track_request` fields: `codecs: list[Video.Codec]` (empty = no preference),
`ranges: list[Video.Range]` (defaults to `[SDR]`), `best_available: bool`. Read codec/range
preferences from `self.track_request`, not from `ctx.parent.params` directly - it's the same data,
but `track_request` is what `_get_tracks_for_variants()` and the rest of the framework consult.

## `self.session` and `get_session()`

`super().__init__(ctx)` sets `self.session = self.get_session()`. The **base** `get_session()` is a
plain `requests.Session` with `config.headers` applied and a retry adapter mounted (5 retries,
backoff on `429/500/502/503/504`) - it does **not** use rnet/TLS-impersonation by default. If a
service needs TLS fingerprinting to get past bot detection, override `get_session()`:

```python
from unshackle.core.session import session

@staticmethod
def get_session() -> RnetSession:
    return session("Chrome131")  # any rnet.Impersonate preset name, e.g. OkHttp4_12, Edge101, Safari18
```

`RnetSession` is a drop-in `requests.Session` replacement (same `.get`/`.post`/cookie jar
interface), so nothing else in the service needs to change. For custom SSL cipher requirements
instead, mount `unshackle.core.utils.sslciphers.SSLCiphers` on a plain `requests.Session` the same
way.

## `authenticate()`

```python
def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
    super().authenticate(cookies, credential)
    self.session.headers.update({"user-agent": self.config["client"][self.device]["user_agent"]})

    cache = self.cache.get(f"tokens_{self.device}_{self.profile}")
    if cache and cache.data.get("expires_in", 0) > int(datetime.now().timestamp()):
        self.log.info(" + Using cached tokens")
    elif cache and cache.data.get("refresh_token"):
        refresh = self.session.post(url=self.config["endpoints"]["refresh"], data=...).json()
        cache.set(data=refresh, expiration=refresh.get("expires_in"))
    else:
        if cookies:
            ...  # decode a JWT cookie
        elif credential:
            otp = self.request_input("Enter the OTP sent to your device: ")
            ...
        else:
            raise EnvironmentError("Service requires either Cookies or Credentials.")
        token = self.session.post(url=self.config["endpoints"]["login"], data=body).json()
        cache.set(data=token, expiration=token.get("expires_in"))

    self.token = cache.data["token"]
```

Skip this method entirely for services that need no auth at all. Otherwise:

- **Always call `super().authenticate(cookies, credential)` first** - it loads the cookie jar into
  `self.session` and stores `self.credential`.
- Cache tokens with `self.cache.get(key)` / `cache.set(data, expiration=seconds)`. Key the cache
  by whatever varies per session (device, profile, or `credential.sha1`) so multiple profiles
  don't collide.
- Use `self.request_input(prompt)` instead of bare `input()` for anything interactive (OTP, 2FA
  codes) - locally it prompts via the shared Rich console (`console.input`, so the prompt renders
  correctly alongside progress/log output), but under `serve` the attached `InputBridge` relays the
  prompt to the remote client instead of hanging the server.
- Raise `EnvironmentError` if auth is required but neither cookies nor a credential were given.
- Don't hold onto the raw `Credential` object past this method - store the derived token instead.

## `search()`

```python
def search(self) -> Generator[SearchResult, None, None]:
    results = self.session.get(url=self.config["endpoints"]["search"], params={"q": self.title}).json()
    for result in results["entries"]:
        yield SearchResult(
            id_=result["id"],
            title=result["title"],
            description=result.get("description"),
            label="SERIES" if result["programType"] == "series" else result["programType"].upper(),
            url=result.get("url"),
        )
```

Optional - implement it to support `unshackle search TAG <query>`. A generator yielding
`SearchResult` objects; nothing else consumes its return value, so don't collect to a list first.

## `get_titles()`

```python
def get_titles(self) -> Titles_T:
    match = re.match(self.TITLE_RE, self.title)
    if not match:
        raise ValueError("Could not parse a title ID - is the URL/ID correct?")
    title_id = match.group("title_id")

    metadata = self.session.get(url=self.config["endpoints"]["metadata"].format(title_id=title_id)).json()
    original_lang = Language.find(metadata["languages"][0])

    if self.movie or metadata["programType"] == "movie":
        return Movies([
            Movie(
                id_=metadata["id"],
                service=self.__class__,
                name=metadata["title"],
                year=metadata.get("releaseYear") or None,
                language=original_lang,
                data=metadata,
            )
        ])

    episodes = [...]  # flatten seasons into Episode(...) the same way
    return Series(episodes)
```

Required. Returns `Movies`, `Series`, or (for music) `Album` - never a raw list.

- Parse `self.title` with `TITLE_RE` first; raise `ValueError` with a clear message if it doesn't
  match.
- `language=` on each `Movie`/`Episode`/`Song` must be the title's **original** recorded language,
  not the user's preferred language - it's the source of truth `to_tracks()` uses later to flag
  `is_original_lang` on every track, which drives `-l best/all` selection and the "Original" audio
  label.
- `service=self.__class__`, not `service=self` - a common copy-paste mistake.
- Pass the full API response through `data=` on the title. `get_tracks()` and `get_chapters()`
  read it back as `title.data` - this is the standard way to avoid a second request later.

## `get_tracks()`

Two shapes, depending on what the service's API gives you:

**Pattern A - separate manifest per codec/range.** Use the `_get_tracks_for_variants()` helper:

```python
def get_tracks(self, title: Title_T) -> Tracks:
    def _fetch_variant(title, codec, range_):
        vcodec_str = "H265" if codec == Video.Codec.HEVC else "H264"
        return self._fetch_dash_manifest(title, vcodec=vcodec_str, range_=range_)
    return self._get_tracks_for_variants(title, _fetch_variant)
```

The helper walks every `codec x range` combination from `self.track_request`, handles `HYBRID`
(fetches HDR10 and DV separately and merges them), and - when `--best-available` is set - skips
combos the service can't deliver instead of raising.

**Pattern B - one manifest contains every variant.** Just parse and return; `dl.py` filters by
the user's selection:

```python
def get_tracks(self, title: Title_T) -> Tracks:
    playback = self.session.get(url=self.config["endpoints"]["playback"].format(title_id=title.id)).json()
    return HLS.from_url(url=playback["manifest_url"], session=self.session).to_tracks(language=title.language)
```

`ISM` (Microsoft Smooth Streaming) follows the same shape as `HLS` here.

Whichever pattern you use, the manifest fetch is also where you post-process tracks before
returning them - see `_fetch_dash_manifest` in `EXAMPLE` for the full set of fixups:

- **Store DRM/license data** on `self` (e.g. `self.license_data = {...}`) or on `title.data` -
  `get_widevine_license()` only receives `title` and `track`, not the manifest response, so this
  is the only place to capture license URLs/tokens.
- **Stamp `video.range`** - manifests can't always be trusted to signal range correctly; set it
  explicitly to what you asked for.
- **Flip HDR10 → HDR10+** when the service is known to embed HDR10+ SEI but only labels HDR10 in
  the manifest - this is a bitstream feature the HLS parser intentionally does not infer.
- **Set `video.dv_compatible_bitstream = True`** for HEVC tracks carrying DV RPU NALs in a
  container that only signals plain HEVC - `DVFixup` then round-trips the bitstream through
  `dovi_tool` before mux so the output is recognised as Dolby Vision.
- **Drop decoy renditions** (e.g. unencrypted "clear" copies some APIs return alongside the real
  ones).
- **Mark descriptive audio** from the manifest's `Role`/label data (`audio.descriptive = True`).
- **Set `audio.joc`** when the API tells you a track is Dolby Atmos explicitly (the framework
  also detects JOC itself from the bitstream).
- **Build subtitles by hand** when the manifest's own subtitle list is wrong or incomplete - in
  that case compute `is_original_lang` yourself with `is_close_match(sub_lang, [title.language])`,
  the same helper `to_tracks()` uses internally.
- **Add cover art** as an `Attachment` if the API provides one.

```python
def get_tracks(self, title: Title_T) -> Tracks:
    # ... never filter by resolution/codec/language here - return everything,
    # the framework applies the user's selection after this returns.
```

This is the single most common service bug: filtering inside `get_tracks()`. Don't.

## `get_chapters()`

```python
def get_chapters(self, title: Title_T) -> Chapters:
    chapters = []
    seen = set()
    for marker in title.data.get("markers", []):
        ts = marker["start"]            # seconds, ms, or "HH:MM:SS.mmm"
        if ts <= 0 or ts in seen:       # skip a marker at 0 and any duplicate timestamp
            continue
        seen.add(ts)
        chapters.append(Chapter(timestamp=ts, name=marker.get("label")))
    return Chapters(chapters)
```

Return `Chapters()` (or `[]`) if the service has no chapter data - don't skip implementing the
method. Timestamps accept float seconds, int milliseconds, or `"HH:MM:SS.mmm"` strings. Prefer
descriptive names ("Intro", "End Credits") over generic ones ("Chapter 01").

**The opening chapter at `00:00:00.000` is created for you - never add your own.** The moment you
add the first `Chapter`, `Chapters.add()` injects a `Chapter(0)` at the start if one isn't already
present, so every file gets an opening chapter automatically. Two consequences you must handle:

- **Don't add a "Chapter 1" at timestamp 0 yourself** - it's redundant with the auto-inserted one.
- **Guard against duplicate timestamps.** `Chapters.add()` raises `ValueError` if a chapter already
  exists at that exact timestamp, so a marker the API reports at `0` (an intro/recap that starts at
  the very beginning) collides with the auto-inserted opening chapter, and two markers at the same
  time collide with each other. Skip any marker at `0` and de-duplicate timestamps before adding,
  as the `seen` set above does.

A `Chapter` with no `name` is valid - it inserts an unnamed chapter break at that timestamp, which
is the standard way to close out a named range (e.g. mark the end of an "Intro" so the next chapter
boundary lands there).

## DRM license methods

```python
def get_widevine_service_certificate(self, **_: Any) -> Optional[str]:
    return self.config.get("certificate")  # or None to use the common privacy cert

def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[Union[bytes, str]]:
    license_url = self.license_data.get("url") or self.config["endpoints"].get("widevine_license")
    response = self.session.post(url=license_url, data=challenge, ...)
    response.raise_for_status()
    return response.content

def get_playready_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[Union[bytes, str]]:
    response = self.session.post(url=self.config["endpoints"]["playready_license"], data=challenge)
    response.raise_for_status()
    return response.content

def get_clearkey_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[Union[bytes, str, dict]]:
    license_url = self.config["endpoints"].get("clearkey_license")
    if not license_url:
        return None  # fall back to the manifest-provided Laurl, if any
    response = self.session.post(url=license_url, data=challenge)
    response.raise_for_status()
    return response.json()
```

- `get_widevine_service_certificate()` - return the service's privacy cert string to enable
  privacy-mode license requests, or `None` to use the common cert.
- `get_widevine_license()` - POST `challenge` (raw CDM challenge bytes) to the license endpoint,
  return the raw license (bytes), or JSON-unwrap it first if the service wraps it
  (`response.json()["license"]`). `track` is passed in case the service rotates keys per segment
  and you need its per-segment PSSH.
- `get_playready_license()` - same shape as Widevine. If omitted, the base class delegates to
  `get_widevine_license()`, so only implement it when the service has a genuinely separate
  PlayReady endpoint.
- `get_clearkey_license()` - only for DASH `org.w3.clearkey` (W3C EME). `challenge` is the W3C
  JSON license request; return the JWK Set as dict, str, or bytes. **Three integration levels,
  simplest first:**
    1. Manifest carries a `<Laurl>` - implement nothing; the framework POSTs there itself.
    2. Custom endpoint/headers - override this method as shown above.
    3. Obfuscated/bespoke key delivery - fetch and unwrap the key yourself, then pre-populate
       `drm.content_keys[kid] = key_hex` on the track's `ClearKeyCENC` during `get_tracks()`; the
       framework skips the license round-trip entirely when every KID is already keyed.
- HLS AES-128 ClearKey and fully unencrypted content have no license callback at all - the key
  comes straight from the manifest or a side endpoint and is attached to the track's DRM object
  directly.

## Other extension points

A handful of base-class hooks aren't exercised by `EXAMPLE` because it has nothing interesting to
do in them, but are there when you need them:

- **Event hooks** - no-ops on the base class, fire during the download pipeline if overridden:
  `on_segment_downloaded(track, segment: Path)`, `on_track_downloaded(track)`,
  `on_track_decrypted(track, drm, segment=None)`, `on_track_repacked(track)`,
  `on_track_multiplex(track)`. Useful for progress reporting or service-specific cleanup tied to
  pipeline stages.
- **`resolve_server_keys(title)`** - not declared on `Service` at all; `dl.py` checks for it with
  `hasattr` and calls it if present. Use it for services where decryption keys need to be resolved
  server-side ahead of the normal license flow, separate from the per-track license callbacks.
- **`get_titles_cached()` / `apply_title_map()`** - base-class wrappers around your `get_titles()`
  that add the title-metadata cache and `title_map` config rewriting (see
  [`title_map`](SERVICE_CONFIG.md#title_map-dict)). `dl.py` calls `get_titles_cached()`, not
  `get_titles()` directly - implement `get_titles()`, don't override the cached wrapper.

## `config.yaml`

```yaml
endpoints:
  base_url: "https://api.example.com"
  content: "https://api.example.com/content"
  search: "https://api.example.com/search"
  license: "https://license.example.com/v1"
headers:
  User-Agent: "..."
client:
  id: "..."
device:
  platform_name: "..."
```

No enforced schema - `self.config` is just whatever YAML you put here, merged with any
`services.<TAG>` overrides from the user's `unshackle.yaml` (see
[Service Integration & Authentication](SERVICE_CONFIG.md#services-dict)). Conventions:

- **All URLs live here, never hardcoded in `__init__.py`.** Use `{}` or `{name}` placeholders for
  dynamic segments and `.format(...)` at the call site.
- Treat this file as shared/checked-in - don't put per-user secrets in it. Sensitive,
  user-specific values (API keys, device IDs) belong in the user's `unshackle.yaml` under
  `services.<TAG>`, which gets merged into `self.config` at runtime.
- `client:`/`device:` blocks are a common idiom for services with multiple device profiles
  (web/android_tv/ios) - index into them with whatever `-d/--device` option you exposed in `cli()`.

Use `-d`/`--debug` (before the subcommand) for structured JSONL logging while iterating - see
[Structured Debug Logging](DEBUG_LOGGING.md).
