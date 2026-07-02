# Endpoints

## GET /api/health

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

## GET /api/services

List all available streaming services (filtered by the effective allowlist for the caller).

```bash
curl -H "X-Secret-Key: $KEY" http://localhost:8786/api/services
```

Returns `{"services": [...]}`. Each entry has `tag`, `aliases`, `geofence`, `title_regex`, `url` (from `cli.short_help`), `help` (full docstring), and `cli_params` describing the service-level Click parameters.

---

## POST /api/search

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

## POST /api/list-titles

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

## POST /api/list-tracks

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

## POST /api/download

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

## GET /api/download/jobs

List all download jobs with optional filtering and sorting. Disabled in `--remote-only` mode.

**Query parameters:**
| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `status` | string | all | Filter by status: `queued`, `downloading`, `completed`, `failed`, `cancelled` |
| `service` | string | all | Filter by service tag |
| `sort_by` | string | `created_time` | Sort field: `created_time`, `started_time`, `completed_time`, `progress`, `status`, `service` |
| `sort_order` | string | `desc` | Sort order: `asc`, `desc` |
| `full` | string | `false` | `true` to include full job details per job (parameters, timestamps, `output_files`, error fields) |

```bash
curl -H "X-Secret-Key: $KEY" "http://localhost:8786/api/download/jobs?status=completed"
```

---

## GET /api/download/jobs/{job_id}

Get detailed information about a specific download job including progress, parameters, and error details.

```json
{
  "job_id": "504db959-80b0-446c-a764-7924b761d613",
  "status": "downloading",
  "created_time": "2026-02-27T18:00:00.000000",
  "service": "EXAMPLE1",
  "title_id": "abc123def456",
  "title": "Example Show",
  "progress": 74.9,
  "phase": "downloading video 1080p SDR",
  "current_title": "S01E04",
  "completed_tracks": 4,
  "total_tracks": 13,
  "active_tracks": ["video 1080p SDR"],
  "track_progress": [
    { "label": "video 1080p SDR", "progress": 74.9, "speed": "DASH 265.5 MB/s" }
  ],
  "segments_done": 75.0,
  "segments_total": 100.0,
  "speed": "DASH 265.5 MB/s",
  "parameters": { },
  "started_time": "2026-02-27T18:00:01.000000",
  "completed_time": null,
  "output_files": [
    "/downloads/Example.Show.S01E01.mkv",
    "/downloads/Example.Show.S01E02.mkv",
    "/downloads/Example.Show.S01E03.mkv"
  ],
  "error_message": null,
  "error_details": null
}
```

**Progress fields:**

| Field | Type | Description |
| --- | --- | --- |
| `progress` | number | Overall progress for the title currently downloading, 0–100. Resets per episode on multi-episode jobs |
| `phase` | string | Human-readable current step, e.g. `downloading video 1080p SDR`, `muxing` |
| `title` | string | Human-readable show/movie name, available once the download starts |
| `current_title` | string | `SxxEyy` code of the episode downloading now (episodic jobs only) |
| `completed_tracks` / `total_tracks` | int | Track counts for the current title |
| `active_tracks` | array[string] | Labels of tracks downloading now |
| `track_progress` | array | Per-track `{label, progress, speed}` for each concurrently active track |
| `segments_done` / `segments_total` | number | Segment counts of the track downloading now |
| `speed` | string | Transfer speed of the track downloading now |
| `output_files` | array[string] | Final file paths, appended as each title finishes muxing |

The list endpoint (`GET /api/download/jobs`) returns the same progress fields per job but omits `parameters`, timestamps, `output_files`, and error details.

---

## DELETE /api/download/jobs/{job_id}

Cancel a queued or running download job, or remove a finished one.

- `queued` / `downloading` job: cancelled, returns `200` with `{"status": "success", "message": "Job cancelled"}`.
- `completed` / `failed` / `cancelled` job: removed from the manager entirely, returns `204` (no body).
- Unknown job: `404`.

---

## POST /api/download/jobs/clear-finished

Remove all `completed`, `failed`, and `cancelled` jobs from the manager. Disabled in `--remote-only` mode.

```json
{ "removed": 3 }
```

---

## POST /api/download/jobs/{job_id}/retry

Enqueue a new job reusing a finished job's service, title, and original (unredacted) parameters. Only allowed on `completed`, `failed`, or `cancelled` jobs — returns `409` otherwise, `404` if unknown. The original job is left in place.

Creation-time authorization is re-applied at retry time: the job's service must pass the caller's service allowlist (otherwise `400` invalid service), and jobs whose original parameters carried a `cdm` override or per-job credentials return `403` if `serve.cdm_overrides` / `serve.allow_job_credentials` no longer permit them.

Returns `202` with the same shape as `POST /api/download`:

```json
{
  "job_id": "b7e9c1f2-...",
  "status": "queued",
  "created_time": "2026-02-27T18:00:00.000000"
}
```

---

## POST /api/download/jobs/{job_id}/priority

Move a `queued` job to the front of the download queue so a worker picks it up next. Returns `409` if the job is not queued, `404` if unknown.

```json
{ "job_id": "504db959-...", "position": "front" }
```

---

## GET /api/profiles

List named credential profiles per service, usable as the `profile` parameter on search/list/download. Only services whose credentials are configured as a `profile-name: credential` mapping appear (a `default` key is included if present); a service with a single plain (unnamed) credential is omitted entirely. Filtered by the caller's service allowlist. Disabled in `--remote-only` mode.

```bash
curl -H "X-Secret-Key: $KEY" http://localhost:8786/api/profiles
```

```json
{ "profiles": { "EXAMPLE1": ["default", "uk", "us"], "EXAMPLE2": ["alice"] } }
```

---

## GET /api/config

Read-only, redacted view of the effective server configuration for a UI settings page. Secrets (`api_secret`, `users`, credentials, tokens) are never included; secret-looking keys inside `dl` are masked with `***`. Disabled in `--remote-only` mode.

```bash
curl -H "X-Secret-Key: $KEY" http://localhost:8786/api/config
```

```json
{
  "config": {
    "dl": { "lang": "orig", "sub_format": "srt", "downloads": 12 },
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
      "downloads": "/srv/downloads",
      "temp": "/srv/temp",
      "cache": "/srv/cache"
    },
    "services": ["EXAMPLE1", "EXAMPLE2"]
  }
}
```

| Field | Description |
| --- | --- |
| `dl` | Default `dl` parameters from the config file (redacted) |
| `serve.services` | Global service allowlist, or `null` when all services are allowed |
| `serve.cdm_overrides` | Permitted per-request CDM device names (list), `true`, or `null` |
| `services` | Available service tags, filtered by the caller's allowlist |

---

## GET /api/history

Persistent download history. Every job that reaches a terminal state (`completed`, `failed`, `cancelled`) is appended as one JSON line to `api_history.jsonl` in the cache directory, surviving restarts and job cleanup. Only the newest `serve.history_limit` entries are retained (default `100`, `0` for unlimited; see Advanced Config). Newest first. Corrupt lines are skipped; a missing file yields an empty list. Entries are filtered by the caller's service allowlist. Disabled in `--remote-only` mode.

**Query parameters:**
| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `limit` | int | `100` | Maximum entries returned (must be >= 1) |
| `service` | string | all | Filter by service tag (case-insensitive) |

```bash
curl -H "X-Secret-Key: $KEY" "http://localhost:8786/api/history?limit=50&service=EXAMPLE1"
```

```json
{
  "history": [
    {
      "job_id": "504db959-80b0-446c-a764-7924b761d613",
      "service": "EXAMPLE1",
      "title_id": "abc123def456",
      "title": "Example Show",
      "status": "completed",
      "created_time": "2026-02-27T18:00:00.000000",
      "completed_time": "2026-02-27T18:12:34.000000",
      "output_files": ["/downloads/Example.Show.S01E01.mkv"],
      "parameters": { "wanted": ["S01E01"], "quality": [1080], "skip_dl": false },
      "error_message": null
    }
  ],
  "count": 1
}
```

`error_message` is redacted with the same secret-scrubbing applied to job responses. `parameters` is the redacted download selection the job ran with (quality/vcodec/range/wanted/skip_dl, etc.). `completed_time` is `null` for jobs cancelled while still queued.

---

## DELETE /api/history/{job_id}

Remove a single history entry by `job_id` (the file is rewritten without it). Returns `204` on success, `404` if no matching entry exists or it falls outside the caller's service allowlist. Corrupt lines in the history file are preserved.

```bash
curl -X DELETE -H "X-Secret-Key: $KEY" "http://localhost:8786/api/history/504db959-80b0-446c-a764-7924b761d613"
```

---

## POST /api/maintenance/clear-cache

Delete the contents of the cache directory (the directory is recreated empty). `freed_bytes` is best effort. This also removes `api_history.jsonl`, so persistent download history is cleared. Returns `409` with the active job ids if any job is currently downloading, since in-flight jobs may be reading the cache. Disabled in `--remote-only` mode.

```bash
curl -X POST -H "X-Secret-Key: $KEY" http://localhost:8786/api/maintenance/clear-cache
```

```json
{ "cleared": true, "freed_bytes": 1234567 }
```

---

## POST /api/maintenance/clear-temp

Same as clear-cache, for the temp directory. Also refuses with `409` while any job is downloading — active workers write segment files under temp.

```json
{ "cleared": true, "freed_bytes": 0 }
```

---

## POST /api/maintenance/refresh-services

Force-sync (git pull, hard reset to upstream) every service repo configured in `directories.services`. `refreshed` is `true` when all repos synced (or none are configured). Disabled in `--remote-only` mode.

```bash
curl -X POST -H "X-Secret-Key: $KEY" http://localhost:8786/api/maintenance/refresh-services
```

```json
{
  "refreshed": true,
  "repos": [
    { "spec": "https://github.com/example/services", "updated": true, "changes": ["~ EXAMPLE1 (modified)"] }
  ]
}
```

An empty `changes` array means the repo was already up to date. A repo that failed to sync has `updated: false` (and flips `refreshed` to `false`).

---

## GET /api/env/check

Install status of the binaries `unshackle env check` inspects (FFmpeg, MKVToolNix, shaka-packager, etc.). `version` is a best-effort probe and may be `null` even when installed. Disabled in `--remote-only` mode.

```bash
curl -H "X-Secret-Key: $KEY" http://localhost:8786/api/env/check
```

```json
{
  "checks": [
    { "name": "FFmpeg", "installed": true, "version": "7.1.0", "required": true },
    { "name": "mp4decrypt", "installed": false, "version": null, "required": false }
  ]
}
```
