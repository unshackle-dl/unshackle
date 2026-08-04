# REST API Endpoints

This is the full endpoint reference for the unshackle REST API. It covers every route: its method and path, the request body it accepts, the response shape it returns, and the status codes it can produce. Routes are grouped into general, download jobs, server management, and remote-dl sessions.

Start the server with [`unshackle serve`](../../guide/cli-reference.md). By default it binds to `127.0.0.1:8786`, and every route lives under the `/api/` prefix. Authentication, server modes, and the `X-Secret-Key` header are covered in detail on the [Authentication](authentication.md) page. Read that first if you have not already. For an end-to-end walkthrough, see the [Quickstart](quickstart.md).

!!! note "Base URL"
    All examples below assume a base URL of `http://127.0.0.1:8786`. Replace the host and port to match your own `unshackle serve -h <host> -p <port>` invocation.

!!! tip "Interactive docs"
    A live Swagger UI is mounted at **`/api/docs/`** whenever the server is not running in `--remote-only` mode. Its title is "Unshackle REST API" and its version tracks the installed package version. In API-only mode with a key configured, the Swagger UI itself sits behind `X-Secret-Key`; only `/api/health` is exempt.

## Conventions

- **Authentication.** Every request except `GET /api/health` requires the `X-Secret-Key` header when a key is configured. Starting the server with `--no-key` disables the check entirely. See [Authentication](authentication.md).
- **Content type.** Request bodies are JSON (`Content-Type: application/json`); all success responses are JSON unless a `204 No Content` is documented.
- **CORS.** Every response carries permissive CORS headers, and `OPTIONS` preflight requests are answered automatically.
- **Compression.** JSON responses of 256 bytes or more are gzip-compressed when the client sends `Accept-Encoding: gzip`.
- **Service allowlist.** Service-facing endpoints are filtered by the effective allowlist for your API key (the intersection of the global `serve.services` list and your per-key list). Services you are not allowed to use are treated as unknown.

!!! warning "Error responses have two shapes"
    Structured application errors (raised inside a handler) return `{"status": "error", "error_code": "...", "message": "...", "timestamp": "..."}`. The authentication middleware, however, returns a **different** shape: `{"status": 401, "message": "..."}` with an integer `status` and no `error_code`. Clients must handle both. The full error model is documented in the [Error responses](#error-responses) section at the bottom of this page.

---

## General endpoints

These endpoints report server state and enumerate what is available. `GET /api/health`, `GET /api/services`, and `POST /api/search` are also exposed in `--remote-only` mode.

### `GET /api/health`

Health check. **This is the only route exempt from authentication**: you can call it without an `X-Secret-Key` header. It reports the running version and a best-effort update check.

=== "Request"

    ```bash
    curl http://127.0.0.1:8786/api/health
    ```

=== "Response `200`"

    ```json
    {
      "status": "ok",
      "version": "5.3.0",
      "code_hash": "1d22a1e",
      "update_check": {
        "update_available": false,
        "current_version": "5.3.0",
        "latest_version": "5.3.0"
      }
    }
    ```

`code_hash` fingerprints the framework source the server is running, so it identifies the code itself rather than the release. It is the same value for a git checkout and for an installed package built from that checkout, and it changes if anything under `unshackle/core`, `unshackle/commands`, `unshackle/utils`, or `unshackle/vaults` is edited. Service modules are not included. The value is `null` only when the source files cannot be read.

If the update check fails (for example, no network), `update_available` and `latest_version` are `null` while `current_version` still reports the installed version.

| Status | Meaning |
| --- | --- |
| `200` | Server is healthy. |

### `GET /api/services`

List the streaming services available on this server, filtered by your allowlist. Each entry describes the service's tag, matching rules, capability flags, and its CLI parameters (useful for building a UI that accepts service-specific options).

=== "Request"

    ```bash
    curl -H "X-Secret-Key: $KEY" http://127.0.0.1:8786/api/services
    ```

=== "Response `200`"

    ```json
    {
      "services": [
        {
          "tag": "EXAMPLE",
          "aliases": ["EX"],
          "geofence": ["US"],
          "title_regex": "^https?://example\\.com/.+",
          "url": "https://example.com",
          "help": "EXAMPLE\nAuthorization: Cookies\nSecurity: FHD@L3",
          "cli_params": [
            {"name": "title", "kind": "argument", "required": true},
            {
              "name": "movie",
              "kind": "option",
              "opts": ["-m", "--movie"],
              "is_flag": true,
              "default": false,
              "help": "Title is a movie.",
              "type": "boolean",
              "multiple": false
            }
          ],
          "needs_auth": true,
          "has_search": true,
          "has_drm": true,
          "auth_methods": ["cookies"]
        }
      ]
    }
    ```

Field notes:

| Field | Description |
| --- | --- |
| `tag` | Canonical service tag; use this as the `service` value on other endpoints. |
| `aliases` | Alternate tags the service also answers to. |
| `geofence` | Region codes the service is geofenced to (may be empty). |
| `title_regex` | URL/ID pattern the service matches, as a string or list of strings (or `null`). |
| `url` | Service URL, taken from the service's CLI short help. |
| `help` | The service module's docstring. |
| `cli_params` | Array of parameter descriptors. Arguments have `{name, kind: "argument", required}`; options have `{name, kind: "option", opts, is_flag, default, help, type, multiple}` and, for choice options, a `choices` list. |
| `needs_auth` | Whether the service overrides `authenticate()`. |
| `has_search` | Whether the service supports `POST /api/search`. |
| `has_drm` | Whether the service overrides a Widevine or PlayReady license hook. |
| `auth_methods` | Declared or inferred auth methods, e.g. `["cookies"]`, `["credentials"]`. |

| Status | Meaning |
| --- | --- |
| `200` | Service list returned. |
| `500` | Unexpected server error (`INTERNAL_ERROR`). |

### `POST /api/search`

Search a service for titles by query string. The service must implement `search()` (see `has_search` above).

**Request body**

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `service` | string | yes | - | Service tag. |
| `query` | string | yes | - | Search query. |
| `profile` | string | no | `null` | Credential/cookie profile to use. |
| `proxy` | string | no | `null` | Proxy URI or country code. |
| `no_proxy` | boolean | no | `false` | Force-disable all proxy use. |

=== "Request"

    ```bash
    curl -X POST http://127.0.0.1:8786/api/search \
      -H "X-Secret-Key: $KEY" \
      -H "Content-Type: application/json" \
      -d '{"service": "EXAMPLE", "query": "planet earth"}'
    ```

=== "Response `200`"

    ```json
    {
      "results": [
        {
          "id": "12345",
          "title": "Planet Earth",
          "description": "A nature documentary series.",
          "label": "Available",
          "url": "https://example.com/title/12345"
        }
      ],
      "count": 1
    }
    ```

| Status | Error code | Meaning |
| --- | --- | --- |
| `200` | - | Search completed. |
| `400` | `INVALID_PARAMETERS` | Missing `query`. |
| `400` | `INVALID_SERVICE` | Unknown or disallowed service. |
| `400` | `INVALID_PROXY` | Proxy could not be resolved. |
| `502` | `SERVICE_ERROR` | Service failed to initialize, or does not support search. |

### `POST /api/list-titles`

Resolve a title ID into its list of titles (episodes, movie, etc.) without downloading anything.

**Request body**

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `service` | string | yes | Service tag. |
| `title_id` | string | yes | Title identifier. |
| `profile` | string | no | Credential/cookie profile. |
| `proxy` | string | no | Proxy URI or country code. |
| `no_proxy` | boolean | no | Force-disable proxy. |
| `cdm_type` | string | no | Preferred CDM type. |

Service-specific CLI options may also be passed as additional keys.

=== "Request"

    ```bash
    curl -X POST http://127.0.0.1:8786/api/list-titles \
      -H "X-Secret-Key: $KEY" \
      -H "Content-Type: application/json" \
      -d '{"service": "EXAMPLE", "title_id": "12345"}'
    ```

=== "Response `200`"

    ```json
    {
      "titles": [
        {
          "type": "episode",
          "name": "Pilot",
          "id": "ep-1",
          "language": "en",
          "description": "The first episode.",
          "date": "2020-01-01",
          "cover_url": "https://example.com/cover.jpg",
          "year": 2020,
          "series_title": "Example Show",
          "season": 1,
          "number": 1
        }
      ]
    }
    ```

Each serialized title carries `type` (`"episode"`, `"movie"`, or `"other"`), `name`, `id`, `language`, `description`, `date`, and `cover_url`. Episodes and movies add `year`; episodes additionally add `series_title`, `season`, and `number`.

!!! info "`part` on a split episode"
    A service that splits one episode into several separately playable videos reports each
    one as its own episode. Those titles share a `season` and `number` and add a `part`
    integer counting from 1. No other title carries the key, so a client that ignores it
    is unaffected. See [Split episodes](../creating-a-service.md#split-episodes).

| Status | Error code | Meaning |
| --- | --- | --- |
| `200` | - | Titles returned. |
| `400` | `INVALID_INPUT` | Missing `service` or `title_id`. |
| `400` | `INVALID_SERVICE` | Unknown or disallowed service. |
| `401` | `AUTH_FAILED` | Authentication with the service failed. |
| `404` | `NOT_FOUND` | Title not found. |
| `500` | `INTERNAL_ERROR` | Unexpected error. |

### `POST /api/list-tracks`

List the video, audio, and subtitle tracks for a title. For series, you can scope to specific episodes.

!!! tip "Spotting the original-language audio"
    Each audio track carries `is_original`, which marks the track the default `lang` of `orig` selects. Unshackle resolves it with the same language matcher the downloader uses, so it stays correct for regional variants a client cannot separate from tag text alone (`pt` marks `pt-BR` rather than `pt-PT`). Services need not report a title's language, so every track can legitimately come back `false`.

**Request body**

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `service` | string | yes | Service tag. |
| `title_id` | string | yes | Title identifier. |
| `wanted` | string | no | Episode/season range (e.g. `"S01E01-S01E03"`). |
| `season` | int/string | no | Season number (combined with `episode`). |
| `episode` | int/string | no | Episode number (combined with `season`). |
| `part` | int/string | no | Part index of a split episode (combined with `season` and `episode`). |
| `profile`, `proxy`, `no_proxy`, `cdm_type` | - | no | As on `list-titles`. |

When both `season` and `episode` are given, they are combined into a `"{season}x{episode}"` selector. Adding `part` narrows that to `"{season}x{episode}.{part}"`, one part of a [split episode](../creating-a-service.md#split-episodes); without it you get every part of the episode. `part` is read only when `season` and `episode` are both given, and `wanted` takes precedence over all three. To scope a part by range, put it in `wanted` instead (`"S01E01.2"`).

=== "Single title / movie: Response `200`"

    ```json
    {
      "title": { "type": "movie", "name": "Example Movie", "id": "12345", "year": 2021 },
      "video": [
        {
          "id": "v-1", "codec": "H265", "codec_display": "HEVC",
          "bitrate": 8000, "width": 3840, "height": 2160,
          "resolution": "2160p", "fps": "24", "range": "HDR10",
          "range_display": "HDR10", "language": "en", "drm": [], "descriptor": "DASH"
        }
      ],
      "audio": [
        {
          "id": "a-1", "codec": "EC3", "codec_display": "DD+",
          "bitrate": 640, "channels": "5.1", "language": "en", "is_original": true,
          "atmos": false, "descriptive": false, "drm": [], "descriptor": "DASH"
        }
      ],
      "subtitles": [
        { "id": "s-1", "codec": "vtt", "language": "en",
          "forced": false, "sdh": false, "cc": false, "descriptor": "DASH" }
      ]
    }
    ```

=== "Multiple episodes: Response `200`"

    ```json
    {
      "episodes": [
        {
          "title": { "type": "episode", "name": "Pilot", "id": "ep-1", "season": 1, "number": 1 },
          "video": [ ... ],
          "audio": [ ... ],
          "subtitles": [ ... ]
        }
      ],
      "unavailable_episodes": [
        { "title": { "id": "ep-2" }, "error": "..." }
      ]
    }
    ```

Tracks are sorted by bitrate, descending. Episodes that fail to resolve are collected into `unavailable_episodes` rather than failing the whole request. Track objects here do **not** include a download `url`; segment URLs are only exposed through the remote-dl session endpoints.

The `drm` array on a track is a list of `{type, pssh?, kids?, content_keys?, license_url?}` objects.

| Status | Error code | Meaning |
| --- | --- | --- |
| `200` | - | Tracks returned. |
| `400` | `INVALID_INPUT` | Missing `service` or `title_id`. |
| `400` | `INVALID_PARAMETERS` | Invalid `wanted` range. |
| `400` | `INVALID_SERVICE` | Unknown or disallowed service. |
| `404` | `NO_CONTENT` | No matching episodes, or all matches failed. |

---

## Download jobs

Downloads are asynchronous. `POST /api/download` enqueues a job and returns immediately with a `job_id`; the server runs each job in a background worker subprocess. You then poll the job endpoints for progress and results.

!!! info "Job states"
    A job moves through `queued` → `downloading` → one of the terminal states `completed`, `failed`, or `cancelled`. Only queued jobs can be prioritized; only terminal jobs can be retried or removed.

### `POST /api/download`

Create a download job. Requires `service` and `title_id`; every other field is an optional download parameter with a server-side default. On success this returns **`202 Accepted`**, not `200`.

=== "Request"

    ```bash
    curl -X POST http://127.0.0.1:8786/api/download \
      -H "X-Secret-Key: $KEY" \
      -H "Content-Type: application/json" \
      -d '{
        "service": "EXAMPLE",
        "title_id": "12345",
        "quality": [2160],
        "vcodec": "H265",
        "range": ["HDR10"],
        "wanted": ["S01E01"]
      }'
    ```

=== "Response `202`"

    ```json
    {
      "job_id": "b0f7c8e2-4a1d-4e9c-9b7e-2f3a1c0d5e6f",
      "status": "queued",
      "created_time": "2026-07-03T12:00:00+00:00"
    }
    ```

**Common parameters** (all optional). These mirror the [`dl` command](../../guide/downloading.md) flags:

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `profile` | string | `null` | Credential/cookie profile. |
| `quality` | int[] | `[]` | Target resolution(s); best available if empty. |
| `vcodec` | string/string[] | `null` | Video codec filter. |
| `acodec` | string/string[] | `null` | Audio codec filter. |
| `vbitrate` / `abitrate` | int | `null` | Video / audio bitrate in kbps (positive). |
| `vbitrate_range` / `abitrate_range` | string | `null` | `"MIN-MAX"` bitrate window. |
| `range` | string[] | `["SDR"]` | Dynamic range(s). |
| `channels` | number | `null` | Audio channel count. |
| `no_atmos` | boolean | `false` | Exclude Atmos tracks. |
| `wanted` | string[] | `[]` | Episode/season selectors. Accepts the part form, `"S01E01.2"`. |
| `latest_episode` | boolean | `false` | Only the newest episode. |
| `lang` / `v_lang` / `a_lang` / `s_lang` | string[] | `["orig"]` / `[]` / `[]` / `["all"]` | Language filters. |
| `require_subs` | string[] | `[]` | Required subtitle languages. |
| `forced_subs` | boolean | `false` | Include forced subtitles. |
| `forced_s_lang` | string[] | `[]` | Forced subtitle language(s); implies `forced_subs`. |
| `exact_lang` | boolean | `false` | Exact language matching. |
| `sub_format` | string | `null` | Output subtitle format. |
| `video_only` / `audio_only` / `subs_only` / `chapters_only` | boolean | `false` | Track-type restriction (at most one). |
| `no_subs` / `no_audio` / `no_chapters` / `no_video` | boolean | `false` | Skip a track type. |
| `audio_description` | boolean | `false` | Download audio description. |
| `slow` | boolean/string | `null` | Randomized delay between downloads. |
| `split_audio` | boolean | `null` | Separate files per audio codec. |
| `skip_dl` | boolean | `false` | Only fetch keys, do not download. |
| `export` | boolean | `false` | Export manifest/keys/subs to JSON. |
| `cdm_only` | boolean | `null` | Force CDM-only (`true`) or vault-only (`false`) key retrieval. |
| `proxy` / `no_proxy` / `no_proxy_download` | string / bool / bool | `null` / `false` / `false` | Proxy controls. |
| `no_folder` / `no_source` / `no_mux` | boolean | `false` | Output/mux controls. |
| `workers` | int | `null` | Threads per track. |
| `downloads` | int | `1` | Concurrent tracks. |
| `worst` | boolean | `false` | Lowest bitrate within quality. |
| `best_available` | boolean | `false` | Fall back to best available. |
| `repack` | boolean | `false` | Add REPACK tag. |
| `tag` | string | `null` | Release group tag. |
| `tmdb_id` / `imdb_id` / `tvdb_id` / `animeapi_id` | - | `null` | External ID overrides for tagging. |
| `tvdb_order` | `official`, `dvd`, `absolute`, `alternate`, `regional` | `null` | Renumber episodes to a TVDB season order. Falls back to the `tvdb_order` config option. |
| `enrich` | boolean | `false` | Override title/year from external source. |
| `output_dir` | string | `null` | Override output directory. |
| `no_cache` / `reset_cache` | boolean | `false` | Title cache controls. |

**Validation.** Invalid values return `400 INVALID_PARAMETERS`. Notably: `vcodec` must be one of H264/H265/H.264/H.265/AVC/HEVC/VC1/VC-1/VP8/VP9/AV1; `acodec` one of AAC/AC3/EC3/EAC3/DD/DD+/AC4/OPUS/FLAC/ALAC/VORBIS/OGG/DTS; `range` one of SDR/HDR10/HDR10P/DV/HLG/HYBRID (`HDR10+` is accepted); bitrate/worker/download counts must be positive integers; at most one of the `*_only` flags may be set; `no_subs` cannot be combined with `subs_only`, `no_audio` with `audio_only`, or `s_lang` with `require_subs`.

!!! warning "Gated parameters (developer)"
    A few keys are accepted but only permitted when the server config opts in, otherwise the request is rejected with `403 FORBIDDEN`:

    - `cdm` (per-request CDM device name): allowed only if `serve.cdm_overrides` is `true` or a list containing that device name.
    - `credential` (`"user:pass"`) and `credentials` (a map): allowed only if `serve.allow_job_credentials` is truthy (off by default).

    Server-side, effective parameters are merged as defaults → `serve:` config overrides → service-specific defaults → your request body.

| Status | Error code | Meaning |
| --- | --- | --- |
| `202` | - | Job queued. |
| `400` | `INVALID_INPUT` | Missing `service`/`title_id` or malformed JSON. |
| `400` | `INVALID_PARAMETERS` | A parameter failed validation. |
| `400` | `INVALID_SERVICE` | Unknown or disallowed service. |
| `403` | `FORBIDDEN` | A gated parameter is not permitted. |

### `GET /api/download/jobs`

List download jobs, with optional filtering and sorting.

**Query parameters**

| Param | Values | Default | Description |
| --- | --- | --- | --- |
| `status` | `queued`, `downloading`, `completed`, `failed`, `cancelled` | - | Filter by status. |
| `service` | service tag | - | Filter by service. |
| `sort_by` | `created_time`, `started_time`, `completed_time`, `progress`, `status`, `service` | `created_time` | Sort field. |
| `sort_order` | `asc`, `desc` | `desc` | Sort direction. |
| `full` | `true`, `false` | `false` | Include full per-job details. |

=== "Request"

    ```bash
    curl -H "X-Secret-Key: $KEY" \
      "http://127.0.0.1:8786/api/download/jobs?status=downloading&sort_by=progress&sort_order=desc"
    ```

=== "Response `200`"

    ```json
    {
      "jobs": [
        {
          "job_id": "b0f7c8e2-...",
          "status": "downloading",
          "created_time": "2026-07-03T12:00:00+00:00",
          "service": "EXAMPLE",
          "title_id": "12345",
          "title": "Example Show S01E01",
          "progress": 42.5,
          "phase": "downloading",
          "current_title": "Episode 1",
          "completed_tracks": 1,
          "total_tracks": 3,
          "active_tracks": ["v-1"],
          "track_progress": [{"label": "Video", "progress": 42.5, "speed": "5.2 MB/s"}],
          "segments_done": 120,
          "segments_total": 300,
          "speed": "5.2 MB/s",
          "skipped_subtitles": []
        }
      ]
    }
    ```

With `full=true`, each job additionally includes `parameters` (with secrets redacted), `started_time`, `completed_time`, `output_files`, `error_message`, `error_details`, `error_code`, `error_traceback`, and `worker_stderr`.

| Status | Error code | Meaning |
| --- | --- | --- |
| `200` | - | Jobs returned. |
| `400` | `INVALID_PARAMETERS` | Invalid `sort_by` or `sort_order`. |

### `GET /api/download/jobs/{job_id}`

Fetch a single job with full details (equivalent to a `full=true` list entry).

| Status | Error code | Meaning |
| --- | --- | --- |
| `200` | - | Job returned. |
| `404` | `JOB_NOT_FOUND` | No such job. |

### `DELETE /api/download/jobs/{job_id}`

Cancel or remove a job. The behaviour depends on the job's current state:

- **Terminal job** (completed/failed/cancelled): removed from the manager, returning **`204 No Content`** with an empty body.
- **Queued or downloading job**: cancelled, returning `200` `{"status": "success", "message": "Job cancelled"}`.

!!! warning
    A `204` response has no body. Do not attempt to parse JSON from it.

| Status | Error code | Meaning |
| --- | --- | --- |
| `200` | - | Job cancelled. |
| `204` | - | Terminal job removed (empty body). |
| `400` | `INVALID_PARAMETERS` | Job is in a state that cannot be cancelled. |
| `404` | `JOB_NOT_FOUND` | No such job. |

### `POST /api/download/jobs/clear-finished`

Remove every terminal (completed/failed/cancelled) job from the manager. No request body.

=== "Response `200`"

    ```json
    { "removed": 3 }
    ```

### `POST /api/download/jobs/{job_id}/retry`

Enqueue a **new** job that reuses a terminal job's service, title, and parameters. The service allowlist and parameter gates are re-checked. Returns `202` with the new job's ID.

!!! warning "Retry reuses the original, unredacted parameters"
    Retry replays the finished job's stored parameters exactly as they were submitted, including any secrets such as `credential`/`credentials`, not the redacted `parameters` view that `GET /api/download/jobs` and `GET /api/history` expose. This is deliberate: it lets a retried job reproduce a credentialed download that the redacted job record alone never could. It also means anyone who can retry a job can re-run it with its original credentials, even though those credentials are masked everywhere else in the API.

=== "Response `202`"

    ```json
    {
      "job_id": "9c1e2f3a-...",
      "status": "queued",
      "created_time": "2026-07-03T12:05:00+00:00"
    }
    ```

| Status | Error code | Meaning |
| --- | --- | --- |
| `202` | - | New job queued. |
| `404` | `JOB_NOT_FOUND` | No such job. |
| `409` | `CONFLICT` | Job is not in a terminal state. |
| `400` | `INVALID_SERVICE` | Service is no longer allowed. |
| `403` | `FORBIDDEN` | A gated parameter is no longer permitted. |

### `POST /api/download/jobs/{job_id}/priority`

Move a queued job to the front of the download queue.

=== "Response `200`"

    ```json
    { "job_id": "b0f7c8e2-...", "position": "front" }
    ```

| Status | Error code | Meaning |
| --- | --- | --- |
| `200` | - | Job moved to front. |
| `404` | `JOB_NOT_FOUND` | No such job. |
| `409` | `CONFLICT` | Job is not queued. |

---

## Server management

Endpoints for profiles, effective configuration, download history, environment checks, and maintenance actions.

### `GET /api/profiles`

List the named credential profiles configured per service (usable as the `profile` parameter elsewhere). Only services whose credentials are a mapping of profile name to credential are listed; services with a single unnamed credential are omitted. Filtered by your allowlist.

=== "Response `200`"

    ```json
    {
      "profiles": {
        "EXAMPLE": ["default", "premium"],
        "OTHER": ["us-account"]
      }
    }
    ```

### `GET /api/config`

Return a read-only, redacted view of the effective server configuration, suitable for a settings page in a UI. Secrets are never emitted: keys matching `secret`, `password`, `token`, `api_key`, or `credential`, and any userinfo in proxy URLs, are masked.

=== "Response `200`"

    ```json
    {
      "config": {
        "dl": { "...": "redacted default dl parameters" },
        "serve": {
          "max_concurrent_downloads": 2,
          "job_retention_hours": 24,
          "history_limit": 100,
          "services": null,
          "remote_only": false,
          "cdm_overrides": null,
          "allow_job_credentials": false
        },
        "directories": {
          "downloads": "/home/user/Downloads/unshackle",
          "temp": "/home/user/.unshackle/temp",
          "cache": "/home/user/.unshackle/cache"
        },
        "services": ["EXAMPLE", "OTHER"]
      }
    }
    ```

### `GET /api/history`

Read the persisted history of jobs that reached a terminal state, newest first. History is stored in `api_history.jsonl` in the cache directory; corrupt lines are skipped and a missing file yields an empty list.

**Query parameters**

| Param | Type | Default | Description |
| --- | --- | --- | --- |
| `limit` | int (≥ 1) | `100` | Maximum entries to return. |
| `service` | string | - | Case-insensitive service filter. |

=== "Response `200`"

    ```json
    {
      "history": [
        {
          "job_id": "b0f7c8e2-...",
          "service": "EXAMPLE",
          "title_id": "12345",
          "title": "Example Show S01E01",
          "status": "completed",
          "created_time": "2026-07-03T12:00:00+00:00",
          "completed_time": "2026-07-03T12:08:00+00:00",
          "output_files": ["/downloads/Example.S01E01.mkv"],
          "error_message": null,
          "parameters": { "...": "redacted" }
        }
      ],
      "count": 1
    }
    ```

| Status | Error code | Meaning |
| --- | --- | --- |
| `200` | - | History returned. |
| `400` | `INVALID_PARAMETERS` | Invalid `limit`. |

### `DELETE /api/history/{job_id}`

Remove a single persisted history entry. Returns **`204 No Content`** (empty body) on success.

| Status | Error code | Meaning |
| --- | --- | --- |
| `204` | - | Entry removed (empty body). |
| `404` | `NOT_FOUND` | No such entry (or outside your allowlist). |

### `POST /api/maintenance/clear-cache` and `POST /api/maintenance/clear-temp`

Delete and recreate the cache or temp directory. Neither takes a request body. Both are blocked with `409 CONFLICT` if any job is currently `downloading` (the `details` include the offending `active_jobs`).

The `409` guard is about active file I/O, not an arbitrary lock: an in-flight job may still be *reading* cached title data under the cache directory, and active download workers are *writing* segment files under temp. Removing either directory mid-download would corrupt those running jobs, so the endpoints refuse until nothing is downloading.

!!! warning "clear-cache also wipes download history"
    The persistent history read by [`GET /api/history`](#get-apihistory) lives in `api_history.jsonl` **inside the cache directory**. Because clear-cache deletes and recreates that whole directory, calling it also erases your entire download history, not just cached title data. If you want to keep history, do not clear the cache; remove individual entries with [`DELETE /api/history/{job_id}`](#delete-apihistoryjob_id) instead.

=== "Response `200`"

    ```json
    { "cleared": true, "freed_bytes": 1048576 }
    ```

| Status | Error code | Meaning |
| --- | --- | --- |
| `200` | - | Directory cleared. |
| `409` | `CONFLICT` | A download is currently active. |

### `POST /api/maintenance/refresh-services`

git-pull every service repository configured under `directories.services`. `refreshed` is `true` when all repos updated successfully (or none are configured).

=== "Response `200`"

    ```json
    {
      "refreshed": true,
      "repos": [
        { "spec": "github.com/example/services", "updated": true, "changes": ["a1b2c3d Update EXAMPLE"] }
      ]
    }
    ```

### `GET /api/env/check`

Report the install status of the external binaries unshackle depends on (the same set `unshackle env check` inspects), with best-effort version detection.

=== "Response `200`"

    ```json
    {
      "checks": [
        { "name": "ffmpeg", "installed": true, "version": "6.1.1", "required": true },
        { "name": "ccextractor", "installed": false, "version": null, "required": false }
      ]
    }
    ```

---

## Remote-dl sessions

!!! note "Developer feature"
    The `/api/session/*` endpoints power unshackle's **remote download** mode, where a thin local client authenticates once against a remote server, then fetches titles, tracks, and segments and proxies DRM licensing back through it. These endpoints are exposed in `--remote-only` mode, and they are what the built-in `RemoteService` client drives. Most end users never call them directly.

Sessions are held in memory. They expire after `serve.session_ttl` (default 300s), except while awaiting interactive authentication input, which uses a 600s window. A session is bound to the IP that created it, so a request from a different IP is rejected with `403 FORBIDDEN`.

### Session lifecycle overview

```text
POST /api/session/create        → returns session_id, status "authenticating"
GET  /api/session/{id}/prompt   → poll until authenticated (submit answers via POST)
GET  /api/session/{id}/titles   → list titles
POST /api/session/{id}/tracks   → tracks + chapters (with URLs) for one title
POST /api/session/{id}/segments → resolve segment URLs / DRM / headers per track
POST /api/session/{id}/license  → proxy or server-CDM DRM licensing
DELETE /api/session/{id}        → tear down, harvest updated cache
```

### `POST /api/session/create`

Create a session for a service and title. Authentication runs asynchronously in the background. This call returns immediately with `status: "authenticating"`, and you then poll the prompt endpoint. The body accepts `service` and `title_id` (both required) plus a broad set of optional keys (`additionalProperties` is allowed): `credentials` (`{username, password, extra?}`), `cookies` (base64 of zlib-compressed Netscape cookie file), `proxy`, `no_proxy`, `profile`, `cache` (map of forwarded cache files), `client_region`, `cdm_type`, `range_`, `vcodec`, `quality`, `best_available`, and any service CLI options.

=== "Response `200`"

    ```json
    {
      "session_id": "d4e5f6a7-...",
      "service": "EXAMPLE",
      "status": "authenticating"
    }
    ```

| Status | Error code | Meaning |
| --- | --- | --- |
| `200` | - | Session created; authentication in progress. |
| `400` | `INVALID_INPUT` | Missing `service` or `title_id`. |
| `400` | `INVALID_SERVICE` | Unknown or disallowed service. |

### `GET /api/session/{session_id}/prompt`

Poll for the authentication status and any pending interactive prompt (OTP, PIN, device code). Response variants:

=== "Authenticating"

    ```json
    { "status": "authenticating" }
    ```

=== "Pending input"

    ```json
    { "status": "pending_input", "prompt": "Enter the code sent to your email:" }
    ```

=== "Authenticated"

    ```json
    { "status": "authenticated" }
    ```

=== "Failed"

    ```json
    { "status": "failed", "error": "Invalid credentials" }
    ```

| Status | Error code | Meaning |
| --- | --- | --- |
| `200` | - | Status returned. |
| `403` | `FORBIDDEN` | Request IP differs from the session creator. |
| `404` | `SESSION_NOT_FOUND` | No such session. |

### `POST /api/session/{session_id}/prompt`

Submit a response to a pending prompt. Body: `{ "response": "..." }` (required).

=== "Response `200`"

    ```json
    { "status": "accepted" }
    ```

| Status | Error code | Meaning |
| --- | --- | --- |
| `200` | - | Response accepted. |
| `400` | `INVALID_INPUT` | Missing `response`, or no prompt is pending. |
| `404` | `SESSION_NOT_FOUND` | No such session. |

### `GET /api/session/{session_id}/titles`

Fetch the titles for an authenticated session.

=== "Response `200`"

    ```json
    {
      "session_id": "d4e5f6a7-...",
      "titles": [ { "type": "episode", "name": "Pilot", "id": "ep-1" } ]
    }
    ```

Requires an authenticated session; `404 SESSION_NOT_FOUND` otherwise.

### `POST /api/session/{session_id}/tracks`

Get tracks and chapters for a specific title within the session. Body: `{ "title_id": "..." }` (required). Unlike `list-tracks`, track objects here **include** download URLs, along with session headers/cookies and manifest data the client needs to download directly.

=== "Response `200`"

    ```json
    {
      "title": { "type": "episode", "name": "Pilot", "id": "ep-1" },
      "video": [ { "id": "v-1", "url": "https://...", "drm": [ ... ] } ],
      "audio": [ { "id": "a-1", "url": "https://..." } ],
      "subtitles": [ { "id": "s-1", "url": "https://..." } ],
      "chapters": [ { "timestamp": "00:00:00.000", "name": "Intro" } ],
      "attachments": [ { "url": "https://...", "name": "font.ttf", "mime_type": "font/ttf", "description": "" } ],
      "manifests": [ { "type": "dash", "url": "https://...", "data": "<base64 zlib xml>" } ],
      "session_headers": { "User-Agent": "..." },
      "session_cookies": { "sid": "..." },
      "server_cdm_type": "widevine"
    }
    ```

| Status | Error code | Meaning |
| --- | --- | --- |
| `200` | - | Tracks returned. |
| `400` | `INVALID_INPUT` | Missing or unknown `title_id`. |
| `404` | `SESSION_NOT_FOUND` | No such session. |

### `POST /api/session/{session_id}/segments`

Resolve the download URL, DRM info, headers, cookies, and raw track data for selected tracks. Body: `{ "track_ids": ["v-1", "a-1"] }` (required).

=== "Response `200`"

    ```json
    {
      "tracks": {
        "v-1": { "descriptor": "DASH", "url": "https://...", "drm": [ ... ], "headers": { ... }, "cookies": { ... }, "data": { ... } }
      }
    }
    ```

| Status | Error code | Meaning |
| --- | --- | --- |
| `200` | - | Segment info returned. |
| `400` | `INVALID_INPUT` | Missing `track_ids`. |
| `404` | `TRACK_NOT_FOUND` | An unknown track ID was requested. |

### `POST /api/session/{session_id}/license`

Acquire DRM keys. Two modes are supported via the `mode` field.

**Proxy mode** (`mode: "proxy"`, the default) forwards the client's CDM `challenge` to the service's license endpoint and returns the raw license bytes:

=== "Request"

    ```json
    { "track_id": "v-1", "challenge": "<base64 CDM challenge>", "drm_type": "widevine" }
    ```

=== "Response `200`"

    ```json
    { "license": "<base64 license bytes>" }
    ```

**Server-CDM mode** (`mode: "server_cdm"`) runs the full CDM flow on the server using the device configured for your API key, returning content keys directly. Use `track_id` for a single track or `track_ids` for a batch:

=== "Single: Response `200`"

    ```json
    { "keys": { "<kid_hex>": "<key_hex>" } }
    ```

=== "Batch: Response `200`"

    ```json
    { "keys": { "v-1": { "<kid_hex>": "<key_hex>" } }, "drm_type": "widevine" }
    ```

| Field | Description |
| --- | --- |
| `track_id` / `track_ids` | Single track, or a batch of tracks. |
| `challenge` | Base64 CDM challenge (proxy mode). |
| `pssh` | Base64 PSSH (server-CDM mode). |
| `drm_type` | `widevine` (default) or `playready`. |
| `mode` | `proxy` (default) or `server_cdm`. |

| Status | Error code | Meaning |
| --- | --- | --- |
| `200` | - | License/keys returned. |
| `400` | `INVALID_INPUT` | Missing challenge/PSSH, or no CDM device configured for the key. |
| `400` | `INVALID_PARAMETERS` | Unsupported `drm_type`. |
| `404` | `TRACK_NOT_FOUND` | Unknown track ID. |
| `404` | `NO_CONTENT` | Server CDM produced no keys. |
| `502` | `SERVICE_ERROR` | The service aborted during licensing. |

### `GET /api/session/{session_id}`

Return session metadata and validity.

=== "Response `200`"

    ```json
    {
      "session_id": "d4e5f6a7-...",
      "service": "EXAMPLE",
      "valid": true,
      "expires_in": 300,
      "track_count": 4,
      "title_count": 10
    }
    ```

`404 SESSION_NOT_FOUND` if the session does not exist or has expired.

### `DELETE /api/session/{session_id}`

Tear down a session. Its input bridge is cancelled, any updated cache files are harvested, and the session plus its cache directory are deleted.

=== "Response `200`"

    ```json
    {
      "status": "ok",
      "cache": { "tokens.json": "<base64 zlib bytes>" }
    }
    ```

The `cache` field is present only if the session produced cache files. `404 SESSION_NOT_FOUND` if the session does not exist.

---

## Error responses

Every structured error raised inside a handler is serialized to the same JSON envelope:

```json
{
  "status": "error",
  "error_code": "INVALID_PARAMETERS",
  "message": "Invalid video codec: XYZ",
  "timestamp": "2026-07-03T12:00:00+00:00",
  "details": { "field": "vcodec" },
  "retryable": true,
  "debug_info": { "exception_type": "APIError", "traceback": "..." }
}
```

- `details` appears only when non-empty.
- `retryable` appears only when the error is retryable (e.g. network errors, rate limiting).
- `debug_info` appears only when the server was started with `--debug-api`.

!!! warning "Authentication errors differ"
    Errors raised by the API-key middleware use a distinct shape (`{"status": 401, "message": "Secret Key is Empty."}` or `{"status": 401, "message": "Secret Key is Invalid."}`) with an integer `status` and no `error_code`, `timestamp`, or `details`. A malformed JSON body on any POST, by contrast, returns the standard envelope with `error_code: "INVALID_INPUT"`.

### Error codes and default status

| Error code | HTTP status |
| --- | --- |
| `INVALID_INPUT` | 400 |
| `INVALID_SERVICE` | 400 |
| `INVALID_PROXY` | 400 |
| `INVALID_PARAMETERS` | 400 |
| `AUTH_FAILED` | 401 |
| `FORBIDDEN` | 403 |
| `GEOFENCE` | 403 |
| `NOT_FOUND` | 404 |
| `NO_CONTENT` | 404 |
| `JOB_NOT_FOUND` | 404 |
| `SESSION_NOT_FOUND` | 404 |
| `TRACK_NOT_FOUND` | 404 |
| `CONFLICT` | 409 |
| `RATE_LIMITED` | 429 |
| `INTERNAL_ERROR` | 500 |
| `DOWNLOAD_ERROR` | 500 |
| `WORKER_ERROR` | 500 |
| `SERVICE_ERROR` | 502 |
| `DRM_ERROR` | 502 |
| `NETWORK_ERROR` | 503 |
| `SERVICE_UNAVAILABLE` | 503 |

When a generic (uncategorized) exception reaches a handler, unshackle inspects its message and type to pick a code. For example, messages mentioning "connection" or "timeout" become `NETWORK_ERROR` (503, retryable), "rate limit" or "429" become `RATE_LIMITED` (429, retryable), and anything unrecognized collapses to `INTERNAL_ERROR` (500).

---

## See also

- [REST API overview](index.md). What the server is and when to use it.
- [Authentication](authentication.md). Keys, server modes, and the `X-Secret-Key` header.
- [Quickstart](quickstart.md). A first end-to-end request walkthrough.
- [Downloading](../../guide/downloading.md). The `dl` flags mirrored by `POST /api/download`.
