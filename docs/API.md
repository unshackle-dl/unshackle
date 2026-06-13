# REST API Documentation

The unshackle REST API allows you to control downloads, search services, drive remote downloads from a thin client, and (optionally) co-host the pywidevine/pyplayready CDM. Start the server with `unshackle serve` and access the interactive Swagger UI at `http://localhost:8786/api/docs/`.

The server is built on **aiohttp** (not FastAPI). Implementation lives in `unshackle/commands/serve.py` and `unshackle/core/api/` (`routes.py`, `handlers.py`, `session_store.py`, `input_bridge.py`, `download_manager.py`, `download_worker.py`).

## Quick Start

```bash
# Start the server (no authentication)
unshackle serve --no-key

# Start with authentication (api_secret in unshackle.yaml)
unshackle serve

# Serve only the REST API (no pywidevine/pyplayready CDM)
unshackle serve --api-only

# Serve only the remote-dl session endpoints (CORS/Cloudflare friendly)
unshackle serve --remote-only

# Disable just one CDM
unshackle serve --no-widevine
unshackle serve --no-playready

# Verbose error responses (tracebacks/stderr in JSON)
unshackle serve --debug-api
```

`serve` flags:

| Flag | Description |
| --- | --- |
| `-h, --host` | Bind host (default `127.0.0.1`) |
| `-p, --port` | Bind port (default `8786`) |
| `--caddy` | Also launch Caddy using `Caddyfile` next to the unshackle config |
| `--api-only` | REST API only; skip the bundled pywidevine/pyplayready CDM endpoints |
| `--no-widevine` | Disable Widevine CDM endpoints |
| `--no-playready` | Disable PlayReady CDM endpoints |
| `--no-key` | Disable API key authentication entirely |
| `--debug-api` | Include tracebacks/stderr in error responses |
| `--debug` | Enable DEBUG-level logging for API operations |
| `--remote-only` | Expose only `/api/health`, `/api/services`, `/api/search`, and `/api/session/*` (implies `--api-only`) |

## Authentication

When `api_secret` is set in `unshackle.yaml`, all API requests require the **`X-Secret-Key`** header. There is no query-parameter fallback. `/api/health` is always reachable without authentication. `--no-key` disables auth entirely (not recommended for public-facing servers).

```yaml
# unshackle.yaml
serve:
  api_secret: "your-master-secret"          # falls back to global users map below
  remote_only: false                         # also toggleable via --remote-only
  services: ["EXAMPLE1", "EXAMPLE2"]         # optional global service allowlist
  users:
    user-secret-1:
      username: alice
      devices: ["my_widevine_l3"]            # Widevine WVD names this user may use
      playready_devices: ["my_pr_sl2000"]    # PlayReady PRD names; defaults to [] (no access)
      services: ["EXAMPLE1"]                  # optional per-user allowlist (intersected with global)
    user-secret-2:
      username: bob
      devices: []
      playready_devices: []
```

### Service allowlists

`config.serve.services` is the global allowlist; `users.<key>.services` further narrows it per key. The effective set is the intersection. Endpoints affected: `/api/services`, `/api/search`, `/api/list-titles`, `/api/list-tracks`, `/api/download`, and all `/api/session/*` routes.

### CDM access (server-side decryption)

There is no separate "tier" flag. Whether the server can return KID:KEY for a session-mode download depends solely on the device lists configured for the calling user key:

- Empty `devices` and `playready_devices` -> server can only proxy CDM challenges; the client must run its own CDM and parse the license.
- Populated lists -> the client may set `mode: "server_cdm"` on `/api/session/{id}/license` and receive `{ "keys": { "<track_id>": { "<KID>": "<KEY>" } } }` instead of raw license bytes.

Per-service CDM type can be pinned via `config.cdm` (`widevine`/`playready`) or per-service `cdm_type`; otherwise the server picks the type the user has devices for.

### Server-side `dl` defaults

Any flag accepted by `/api/download` (see the table below) can be declared under `serve:` in `unshackle.yaml` and the API will apply it as a default. Request-body values still win. Useful for raising concurrency without changing every client call:

```yaml
serve:
  api_secret: "..."
  users: { ... }
  downloads: 4        # parallel tracks per download job
  workers: 16         # threads per track segment fetch
  best_available: true
  no_proxy_download: false
```

Layering order: built-in defaults < `serve.*` overrides < service-specific click defaults < request body.

---

## Endpoint Map

Standard endpoints (suppressed in `--remote-only` mode are marked R):

| Method | Path | R |
| --- | --- | :-: |
| GET    | `/api/health` | ok |
| GET    | `/api/services` | ok |
| POST   | `/api/search` | ok |
| POST   | `/api/list-titles` | hidden |
| POST   | `/api/list-tracks` | hidden |
| POST   | `/api/download` | hidden |
| GET    | `/api/download/jobs` | hidden |
| GET    | `/api/download/jobs/{job_id}` | hidden |
| DELETE | `/api/download/jobs/{job_id}` | hidden |
| POST   | `/api/session/create` | ok |
| GET    | `/api/session/{session_id}` | ok |
| DELETE | `/api/session/{session_id}` | ok |
| GET    | `/api/session/{session_id}/titles` | ok |
| POST   | `/api/session/{session_id}/tracks` | ok |
| POST   | `/api/session/{session_id}/segments` | ok |
| POST   | `/api/session/{session_id}/license` | ok |
| GET    | `/api/session/{session_id}/prompt` | ok |
| POST   | `/api/session/{session_id}/prompt` | ok |

CDM endpoints (`/{wvd}/...`, `/playready/{prd}/...`) are exposed unless `--api-only` / `--remote-only` / `--no-widevine` / `--no-playready` is set, and use pywidevine / pyplayready's own auth scheme.

---

## Endpoints

### GET /api/health

Health check with version and update information. Always reachable without auth.

```bash
curl http://localhost:8786/api/health
```

```json
{
  "status": "ok",
  "version": "4.0.0",
  "update_check": {
    "update_available": false,
    "current_version": "4.0.0",
    "latest_version": null
  }
}
```

---

### GET /api/services

List all available streaming services (filtered by the effective allowlist for the caller).

```bash
curl -H "X-Secret-Key: $KEY" http://localhost:8786/api/services
```

Returns `{"services": [...]}`. Each entry has `tag`, `aliases`, `geofence`, `title_regex`, `url` (from `cli.short_help`), `help` (full docstring), and `cli_params` describing the service-level Click parameters.

---

### POST /api/search

Search for titles from a streaming service.

**Required parameters:**
| Parameter | Type | Description |
| --- | --- | --- |
| `service` | string | Service tag |
| `query` | string | Search query |

**Optional parameters:**
| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `profile` | string | `null` | Profile for credentials/cookies |
| `proxy` | string | `null` | Proxy URI or country code |
| `no_proxy` | boolean | `false` | Disable all proxy use |

```bash
curl -X POST http://localhost:8786/api/search \
  -H "X-Secret-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"service": "EXAMPLE1", "query": "example show"}'
```

```json
{
  "results": [
    {
      "id": "abc123def456",
      "title": "Example Show",
      "description": null,
      "label": "TV Show",
      "url": "https://example.com/show/abc123def456"
    }
  ],
  "count": 1
}
```

---

### POST /api/list-titles

Get available titles (seasons/episodes/movies) for a service and title ID. Disabled in `--remote-only` mode.

**Required parameters:**
| Parameter | Type | Description |
| --- | --- | --- |
| `service` | string | Service tag |
| `title_id` | string | Title ID or URL |

```bash
curl -X POST http://localhost:8786/api/list-titles \
  -H "X-Secret-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"service": "EXAMPLE1", "title_id": "abc123def456"}'
```

---

### POST /api/list-tracks

Get video, audio, and subtitle tracks for a title. Disabled in `--remote-only` mode.

**Required parameters:**
| Parameter | Type | Description |
| --- | --- | --- |
| `service` | string | Service tag |
| `title_id` | string | Title ID or URL |

**Optional parameters:**
| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `wanted` | array | all | Episode filter (e.g., `["S01E01"]`) |
| `profile` | string | `null` | Profile for credentials/cookies |
| `proxy` | string | `null` | Proxy URI or country code |
| `no_proxy` | boolean | `false` | Disable all proxy use |

Returns video, audio, and subtitle tracks with codec, bitrate, resolution, language, and DRM information.

---

### POST /api/download

Start a download job. Returns immediately with a job ID (HTTP 202). Disabled in `--remote-only` mode.

**Required parameters:**
| Parameter | Type | Description |
| --- | --- | --- |
| `service` | string | Service tag |
| `title_id` | string | Title ID or URL |

**Quality and codec parameters:**
| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `quality` | array[int] | best | Resolution(s) (e.g., `[1080, 2160]`) |
| `vcodec` | string or array | any | Video codec(s): `H264`, `H265`/`HEVC`, `VP9`, `AV1`, `VC1`, `VP8` |
| `acodec` | string or array | any | Audio codec(s): `AAC`, `AC3`, `EC3`, `AC4`, `OPUS`, `FLAC`, `ALAC`, `DTS`, `OGG` |
| `vbitrate` | int | highest | Video bitrate in kbps |
| `abitrate` | int | highest | Audio bitrate in kbps |
| `range` | array[string] | `["SDR"]` | Color range(s): `SDR`, `HDR10`, `HDR10+`, `HLG`, `DV`, `HYBRID` |
| `channels` | float | any | Audio channels (e.g., `5.1`, `7.1`) |
| `no_atmos` | boolean | `false` | Exclude Dolby Atmos tracks |
| `split_audio` | boolean | `null` | Create separate output per audio codec |
| `sub_format` | string | `null` | Output subtitle format: `SRT`, `VTT`, `ASS`, `SSA`, `TTML` |

**Episode selection:**
| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `wanted` | array[string] | all | Episodes (e.g., `["S01E01", "S01E02-S01E05"]`) |
| `latest_episode` | boolean | `false` | Download only the most recent episode |

**Language parameters:**
| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `lang` | array[string] | `["orig"]` | Language for video and audio (`orig` = original) |
| `v_lang` | array[string] | `[]` | Language override for video tracks only |
| `a_lang` | array[string] | `[]` | Language override for audio tracks only |
| `s_lang` | array[string] | `["all"]` | Language for subtitles |
| `require_subs` | array[string] | `[]` | Required subtitle languages (skip if missing) |
| `forced_subs` | boolean | `false` | Include forced subtitle tracks |
| `exact_lang` | boolean | `false` | Exact language matching (no variants) |

**Track selection:**
| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `video_only` | boolean | `false` | Only download video tracks |
| `audio_only` | boolean | `false` | Only download audio tracks |
| `subs_only` | boolean | `false` | Only download subtitle tracks |
| `chapters_only` | boolean | `false` | Only download chapters |
| `no_video` | boolean | `false` | Skip video tracks |
| `no_audio` | boolean | `false` | Skip audio tracks |
| `no_subs` | boolean | `false` | Skip subtitle tracks |
| `no_chapters` | boolean | `false` | Skip chapters |
| `audio_description` | boolean | `false` | Include audio description tracks |

**Output and tagging:**
| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `tag` | string | `null` | Override group tag |
| `repack` | boolean | `false` | Add REPACK tag to filename |
| `tmdb_id` | int | `null` | Use specific TMDB ID for tagging |
| `imdb_id` | string | `null` | Use specific IMDB ID (e.g., `tt1375666`) |
| `animeapi_id` | string | `null` | Anime database ID via AnimeAPI (e.g., `mal:12345`) |
| `enrich` | boolean | `false` | Override show title and year from external source |
| `no_folder` | boolean | `false` | Disable folder creation for TV shows |
| `no_source` | boolean | `false` | Remove source tag from filename |
| `no_mux` | boolean | `false` | Do not mux tracks into container |
| `output_dir` | string | `null` | Override output directory |

**Download behavior:**
| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `profile` | string | `null` | Profile for credentials/cookies |
| `proxy` | string | `null` | Proxy URI or country code |
| `no_proxy` | boolean | `false` | Disable all proxy use |
| `no_proxy_download` | boolean | `false` | Bypass proxy for segment downloads only. Manifest, license, and auth still use proxy |
| `workers` | int | `null` | Max threads per track download |
| `downloads` | int | `1` | Concurrent track downloads |
| `slow` | boolean or string | `null` | Add randomized delay between titles. `true` = 60-120s, or `"MIN-MAX"` string (e.g., `"20-40"`). Min must be >= 20 |
| `best_available` | boolean | `false` | Continue if requested quality unavailable |
| `worst` | boolean | `false` | Select the lowest bitrate track within the specified quality. Requires `quality` |
| `skip_dl` | boolean | `false` | Skip download, only get decryption keys |
| `export` | boolean | `false` | Export manifest, track URLs, keys, and subtitles to JSON in the exports directory |
| `cdm_only` | boolean | `null` | Only use CDM (`true`) or only vaults (`false`) |
| `no_cache` | boolean | `false` | Bypass title cache |
| `reset_cache` | boolean | `false` | Clear title cache before fetching |

**Example:**

```bash
curl -X POST http://localhost:8786/api/download \
  -H "X-Secret-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "service": "EXAMPLE1",
    "title_id": "abc123def456",
    "wanted": ["S01E01"],
    "quality": [1080, 2160],
    "vcodec": ["H265"],
    "acodec": ["AAC", "EC3"],
    "range": ["HDR10", "SDR"],
    "split_audio": true,
    "lang": ["en"]
  }'
```

```json
{
  "job_id": "504db959-80b0-446c-a764-7924b761d613",
  "status": "queued",
  "created_time": "2026-02-27T18:00:00.000000"
}
```

---

### GET /api/download/jobs

List all download jobs with optional filtering and sorting. Disabled in `--remote-only` mode.

**Query parameters:**
| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `status` | string | all | Filter by status: `queued`, `downloading`, `completed`, `failed`, `cancelled` |
| `service` | string | all | Filter by service tag |
| `sort_by` | string | `created_time` | Sort field: `created_time`, `started_time`, `completed_time`, `progress`, `status`, `service` |
| `sort_order` | string | `desc` | Sort order: `asc`, `desc` |

```bash
curl -H "X-Secret-Key: $KEY" "http://localhost:8786/api/download/jobs?status=completed"
```

---

### GET /api/download/jobs/{job_id}

Get detailed information about a specific download job including progress, parameters, and error details.

```json
{
  "job_id": "504db959-80b0-446c-a764-7924b761d613",
  "status": "completed",
  "created_time": "2026-02-27T18:00:00.000000",
  "service": "EXAMPLE1",
  "title_id": "abc123def456",
  "progress": 100.0,
  "parameters": { },
  "started_time": "2026-02-27T18:00:01.000000",
  "completed_time": "2026-02-27T18:00:15.000000",
  "output_files": [],
  "error_message": null,
  "error_details": null
}
```

---

### DELETE /api/download/jobs/{job_id}

Cancel a queued or running download job. Returns 400 if the job has already terminated.

---

## Remote Service Sessions

These endpoints back the `RemoteService` adapter in `unshackle/core/remote_service.py`. They let a thin `dl` client (or any consumer) authenticate against a service on the server, fetch titles/tracks/manifests, and either proxy CDM challenges or have the server resolve KID:KEY directly. The `dl` command's `RemoteService` adapter replaces the old `remote_dl` command. These endpoints are the only `/api/*` routes available in `--remote-only` mode (in addition to `health`, `services`, and `search`).

### POST /api/session/create

Authenticate against a service and open a session. Body fields:

| Field | Type | Description |
| --- | --- | --- |
| `service` | string | Service tag (required) |
| `title_id` | string | Title ID/URL (required) |
| `credentials` | object | Auth credentials forwarded to `Service.authenticate` |
| `cookies` | string | Cookie blob (Netscape or JSON) |
| `proxy` | string | Proxy URI or country code |
| `no_proxy` | bool | Force-disable proxies |
| `profile` | string | Profile name |
| `cache` | object | Optional pre-warmed title cache payload |

If the service requires interactive input during authentication, poll `GET /api/session/{id}/prompt` and submit responses via `POST /api/session/{id}/prompt` until status is `authenticated`.

**Request:**

```json
{
  "service": "EXAMPLE1",
  "title_id": "abc123def456",
  "credentials": {"username": "alice", "password": "hunter2"},
  "cookies": "# Netscape HTTP Cookie File\n...",
  "proxy": "us",
  "no_proxy": false,
  "profile": "default",
  "cache": {}
}
```

**Response (202-style; auth runs asynchronously):**

```json
{
  "session_id": "f1c4a8b2-9c7e-4d2a-bf91-2d3e4f5a6b7c",
  "service": "EXAMPLE1",
  "status": "authenticating"
}
```

### GET /api/session/{session_id}

Returns session metadata. 404 if expired or unknown.

```json
{
  "session_id": "f1c4a8b2-9c7e-4d2a-bf91-2d3e4f5a6b7c",
  "service": "EXAMPLE1",
  "valid": true,
  "expires_in": 3600,
  "track_count": 0,
  "title_count": 0
}
```

### DELETE /api/session/{session_id}

Tears down the session, cancels any pending prompts, and returns any updated per-session cache files (base64-encoded, zlib-compressed) so the client can re-warm next time.

```json
{
  "status": "ok",
  "cache": {
    "tokens": "eJzLSM3JyVcozy/KSVGo5AIAGgQEvQ=="
  }
}
```

### GET /api/session/{session_id}/titles

Returns the resolved titles list.

```json
{
  "session_id": "f1c4a8b2-9c7e-4d2a-bf91-2d3e4f5a6b7c",
  "titles": [
    {
      "type": "episode",
      "name": "Pilot",
      "series_title": "Example Show",
      "season": 1,
      "number": 1,
      "year": 2024,
      "id": "ep-0001",
      "language": "en"
    },
    {
      "type": "movie",
      "name": "Example Movie",
      "year": 2024,
      "id": "mov-0001",
      "language": "en"
    }
  ]
}
```

### POST /api/session/{session_id}/tracks

**Request:**

```json
{"title_id": "ep-0001"}
```

**Response:**

```json
{
  "title": {
    "type": "episode",
    "name": "Pilot",
    "series_title": "Example Show",
    "season": 1,
    "number": 1,
    "year": 2024,
    "id": "ep-0001",
    "language": "en"
  },
  "video": [
    {
      "id": "v-1080p-h264",
      "codec": "H264",
      "codec_display": "H.264",
      "bitrate": 6000,
      "width": 1920,
      "height": 1080,
      "resolution": "1920x1080",
      "fps": "23.976",
      "range": "SDR",
      "range_display": "SDR",
      "language": "en",
      "drm": [
        {
          "type": "widevine",
          "pssh": "AAAAW3Bzc2gAAAAA7e+...",
          "kids": ["abcdef0123456789abcdef0123456789"],
          "license_url": "https://license.example.com/widevine"
        }
      ],
      "descriptor": "DASH",
      "url": "https://cdn.example.com/manifest.mpd"
    }
  ],
  "audio": [
    {
      "id": "a-en-eac3",
      "codec": "EC3",
      "codec_display": "Dolby Digital Plus",
      "bitrate": 640,
      "channels": "5.1",
      "language": "en",
      "atmos": false,
      "descriptive": false,
      "drm": null,
      "descriptor": "DASH",
      "url": "https://cdn.example.com/manifest.mpd"
    }
  ],
  "subtitles": [
    {
      "id": "s-en-vtt",
      "codec": "WebVTT",
      "language": "en",
      "forced": false,
      "sdh": false,
      "cc": false,
      "descriptor": "DASH",
      "url": "https://cdn.example.com/subs/en.vtt"
    }
  ],
  "chapters": [
    {"timestamp": "00:00:00.000", "name": "Chapter 1"}
  ],
  "attachments": [],
  "manifests": [
    {
      "type": "dash",
      "url": "https://cdn.example.com/manifest.mpd",
      "data": "eJzNVk1v2zAM/Ss..."
    }
  ],
  "session_headers": {
    "User-Agent": "Mozilla/5.0 ..."
  },
  "session_cookies": {
    "session": "abc123"
  },
  "server_cdm_type": "widevine"
}
```

### POST /api/session/{session_id}/segments

**Request:**

```json
{"track_ids": ["v-1080p-h264", "a-en-eac3"]}
```

**Response:**

```json
{
  "tracks": {
    "v-1080p-h264": {
      "descriptor": "DASH",
      "url": "https://cdn.example.com/manifest.mpd",
      "drm": [
        {
          "type": "widevine",
          "pssh": "AAAAW3Bzc2gAAAAA7e+...",
          "kids": ["abcdef0123456789abcdef0123456789"],
          "license_url": "https://license.example.com/widevine"
        }
      ],
      "headers": {"User-Agent": "Mozilla/5.0 ..."},
      "cookies": {"session": "abc123"},
      "data": {}
    },
    "a-en-eac3": {
      "descriptor": "DASH",
      "url": "https://cdn.example.com/manifest.mpd",
      "drm": null,
      "headers": {"User-Agent": "Mozilla/5.0 ..."},
      "cookies": {"session": "abc123"},
      "data": {}
    }
  }
}
```

### POST /api/session/{session_id}/license

Two modes, selected by the `mode` field.

**`mode: "proxy"` (default)** -- forward a client-built CDM challenge to the service's license endpoint.

Request:

```json
{
  "mode": "proxy",
  "track_id": "v-1080p-h264",
  "challenge": "CAESxQEK...",
  "drm_type": "widevine",
  "pssh": "AAAAW3Bzc2gAAAAA7e+..."
}
```

Response:

```json
{"license": "CAIS3wIK..."}
```

**`mode: "server_cdm"`** -- the server uses its own CDM to license the track and extract keys. Single-track form takes `track_id`; batch form takes `track_ids`. Requires the calling user key to have a matching device (`devices` for Widevine, `playready_devices` for PlayReady) in `unshackle.yaml`.

Request (batch):

```json
{
  "mode": "server_cdm",
  "track_ids": ["v-1080p-h264", "a-en-eac3"],
  "drm_type": "widevine"
}
```

Response:

```json
{
  "keys": {
    "v-1080p-h264": {
      "abcdef0123456789abcdef0123456789": "00112233445566778899aabbccddeeff"
    },
    "a-en-eac3": {
      "abcdef0123456789abcdef0123456789": "00112233445566778899aabbccddeeff"
    }
  },
  "drm_type": "widevine"
}
```

### GET /api/session/{session_id}/prompt

Polled by the client during interactive authentication (OTP, PIN, device codes). Backed by the `InputBridge` in `unshackle/core/api/input_bridge.py`; `Service.request_input()` blocks server-side until the client posts a response.

Pending input:

```json
{"status": "pending_input", "prompt": "Enter OTP code: "}
```

Other states:

```json
{"status": "authenticating"}
```

```json
{"status": "authenticated"}
```

```json
{"status": "failed", "error": "Invalid credentials"}
```

### POST /api/session/{session_id}/prompt

Unblocks the server-side `request_input()` call.

Request:

```json
{"response": "123456"}
```

Response:

```json
{"status": "accepted"}
```

---

## Error Responses

All endpoints return consistent error responses:

```json
{
  "status": "error",
  "error_code": "INVALID_PARAMETERS",
  "message": "Invalid vcodec: XYZ. Must be one of: H264, H265, VP9, AV1, VC1, VP8",
  "timestamp": "2026-02-27T18:00:00.000000+00:00",
  "details": { }
}
```

Common error codes:

- `INVALID_INPUT` -- malformed request body
- `INVALID_PARAMETERS` -- invalid parameter values
- `MISSING_SERVICE` -- service tag not provided
- `INVALID_SERVICE` -- service not found or not in the caller's allowlist
- `SERVICE_ERROR` -- service initialization or runtime error
- `AUTH_FAILED` -- authentication failure
- `NOT_FOUND` / `TRACK_NOT_FOUND` / session not found -- job/session/track/title missing
- `INTERNAL_ERROR` -- unexpected server error

When `--debug-api` is enabled, error responses include additional `debug_info` with tracebacks and stderr output.

Authentication errors from the auth middleware are returned as `{"status": 401, "message": "..."}` (not the standard error envelope).

---

## Download Job Lifecycle

```
queued -> downloading -> completed
                     \-> failed
queued -> cancelled
downloading -> cancelled
```

Jobs are retained for 24 hours after completion (override via top-level `download_job_retention_hours` in `unshackle.yaml`). The server runs up to 2 concurrent download jobs by default; override via top-level `max_concurrent_downloads`. This is independent of `serve.downloads`, which controls parallel tracks **within** a single job.

Remote sessions are managed by `SessionStore` (`unshackle/core/api/session_store.py`); idle sessions and their `InputBridge` instances are cleaned up by a background loop started/stopped with the app lifecycle.
