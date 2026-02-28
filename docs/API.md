# REST API Documentation

The unshackle REST API allows you to control downloads, search services, and manage jobs remotely. Start the server with `unshackle serve` and access the interactive Swagger UI at `http://localhost:8786/api/docs/`.

## Quick Start

```bash
# Start the server (no authentication)
unshackle serve --no-key

# Start with authentication
unshackle serve  # Requires api_secret in unshackle.yaml
```

## Authentication

When `api_secret` is set in `unshackle.yaml`, all API requests require authentication via:

- **Header**: `X-API-Key: your-secret-key-here`
- **Query parameter**: `?api_key=your-secret-key-here`

Use `--no-key` to disable authentication entirely (not recommended for public-facing servers).

```yaml
# unshackle.yaml
serve:
  api_secret: "your-secret-key-here"
```

---

## Endpoints

### GET /api/health

Health check with version and update information.

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

List all available streaming services.

```bash
curl http://localhost:8786/api/services
```

Returns an array of services with `tag`, `aliases`, `geofence`, `title_regex`, `url`, and `help` text.

---

### POST /api/search

Search for titles from a streaming service.

**Required parameters:**
| Parameter | Type | Description |
| --- | --- | --- |
| `service` | string | Service tag (e.g., `NF`, `AMZN`, `ATV`) |
| `query` | string | Search query |

**Optional parameters:**
| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `profile` | string | `null` | Profile for credentials/cookies |
| `proxy` | string | `null` | Proxy URI or country code |
| `no_proxy` | boolean | `false` | Disable all proxy use |

```bash
curl -X POST http://localhost:8786/api/search \
  -H "Content-Type: application/json" \
  -d '{"service": "ATV", "query": "hijack"}'
```

```json
{
  "results": [
    {
      "id": "umc.cmc.1dg08zn0g3zx52hs8npoj5qe3",
      "title": "Hijack",
      "description": null,
      "label": "TV Show",
      "url": "https://tv.apple.com/us/show/hijack/umc.cmc.1dg08zn0g3zx52hs8npoj5qe3"
    }
  ],
  "count": 1
}
```

---

### POST /api/list-titles

Get available titles (seasons/episodes/movies) for a service and title ID.

**Required parameters:**
| Parameter | Type | Description |
| --- | --- | --- |
| `service` | string | Service tag |
| `title_id` | string | Title ID or URL |

```bash
curl -X POST http://localhost:8786/api/list-titles \
  -H "Content-Type: application/json" \
  -d '{"service": "ATV", "title_id": "umc.cmc.1dg08zn0g3zx52hs8npoj5qe3"}'
```

```json
{
  "titles": [
    {
      "type": "episode",
      "name": "Final Call",
      "series_title": "Hijack",
      "season": 1,
      "number": 1,
      "year": 2023,
      "id": "umc.cmc.4levibvvz01hl4zsm0jdk5v2p"
    }
  ]
}
```

---

### POST /api/list-tracks

Get video, audio, and subtitle tracks for a title.

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

```bash
curl -X POST http://localhost:8786/api/list-tracks \
  -H "Content-Type: application/json" \
  -d '{
    "service": "ATV",
    "title_id": "umc.cmc.1dg08zn0g3zx52hs8npoj5qe3",
    "wanted": ["S01E01"]
  }'
```

Returns video, audio, and subtitle tracks with codec, bitrate, resolution, language, and DRM information.

---

### POST /api/download

Start a download job. Returns immediately with a job ID (HTTP 202).

**Required parameters:**
| Parameter | Type | Description |
| --- | --- | --- |
| `service` | string | Service tag |
| `title_id` | string | Title ID or URL |

**Quality and codec parameters:**
| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `quality` | array[int] | best | Resolution(s) (e.g., `[1080, 2160]`) |
| `vcodec` | string or array | any | Video codec(s): `H264`, `H265`/`HEVC`, `VP9`, `AV1`, `VC1` |
| `acodec` | string or array | any | Audio codec(s): `AAC`, `AC3`, `EC3`, `AC4`, `OPUS`, `FLAC`, `ALAC`, `DTS` |
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
| `tmdb_name` | boolean | `false` | Rename titles using TMDB name |
| `tmdb_year` | boolean | `false` | Use TMDB release year |
| `imdb_id` | string | `null` | Use specific IMDB ID (e.g., `tt1375666`) |
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
| `workers` | int | `null` | Max threads per track download |
| `downloads` | int | `1` | Concurrent track downloads |
| `slow` | boolean | `false` | Add 60-120s delay between titles |
| `best_available` | boolean | `false` | Continue if requested quality unavailable |
| `skip_dl` | boolean | `false` | Skip download, only get decryption keys |
| `export` | string | `null` | Export keys to JSON file path |
| `cdm_only` | boolean | `null` | Only use CDM (`true`) or only vaults (`false`) |
| `no_cache` | boolean | `false` | Bypass title cache |
| `reset_cache` | boolean | `false` | Clear title cache before fetching |

**Example:**

```bash
curl -X POST http://localhost:8786/api/download \
  -H "Content-Type: application/json" \
  -d '{
    "service": "ATV",
    "title_id": "umc.cmc.1dg08zn0g3zx52hs8npoj5qe3",
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

List all download jobs with optional filtering and sorting.

**Query parameters:**
| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `status` | string | all | Filter by status: `queued`, `downloading`, `completed`, `failed`, `cancelled` |
| `service` | string | all | Filter by service tag |
| `sort_by` | string | `created_time` | Sort field: `created_time`, `status`, `service` |
| `sort_order` | string | `desc` | Sort order: `asc`, `desc` |

```bash
# List all jobs
curl http://localhost:8786/api/download/jobs

# Filter by status
curl "http://localhost:8786/api/download/jobs?status=completed"

# Filter by service
curl "http://localhost:8786/api/download/jobs?service=ATV"
```

---

### GET /api/download/jobs/{job_id}

Get detailed information about a specific download job including progress, parameters, and error details.

```bash
curl http://localhost:8786/api/download/jobs/504db959-80b0-446c-a764-7924b761d613
```

```json
{
  "job_id": "504db959-80b0-446c-a764-7924b761d613",
  "status": "completed",
  "created_time": "2026-02-27T18:00:00.000000",
  "service": "ATV",
  "title_id": "umc.cmc.1dg08zn0g3zx52hs8npoj5qe3",
  "progress": 100.0,
  "parameters": { ... },
  "started_time": "2026-02-27T18:00:01.000000",
  "completed_time": "2026-02-27T18:00:15.000000",
  "output_files": [],
  "error_message": null,
  "error_details": null
}
```

---

### DELETE /api/download/jobs/{job_id}

Cancel a queued or running download job.

```bash
curl -X DELETE http://localhost:8786/api/download/jobs/504db959-80b0-446c-a764-7924b761d613
```

Returns confirmation on success, or an error if the job has already completed or been cancelled.

---

## Error Responses

All endpoints return consistent error responses:

```json
{
  "status": "error",
  "error_code": "INVALID_PARAMETERS",
  "message": "Invalid vcodec: XYZ. Must be one of: H264, H265, VP9, AV1, VC1, VP8",
  "timestamp": "2026-02-27T18:00:00.000000+00:00",
  "details": { ... }
}
```

Common error codes:
- `INVALID_INPUT` - Malformed request body
- `INVALID_PARAMETERS` - Invalid parameter values
- `MISSING_SERVICE` - Service tag not provided
- `INVALID_SERVICE` - Service not found
- `SERVICE_ERROR` - Service initialization or runtime error
- `AUTH_FAILED` - Authentication failure
- `NOT_FOUND` - Job or resource not found
- `INTERNAL_ERROR` - Unexpected server error

When `--debug-api` is enabled, error responses include additional `debug_info` with tracebacks and stderr output.

---

## Download Job Lifecycle

```
queued -> downloading -> completed
                     \-> failed
queued -> cancelled
downloading -> cancelled
```

Jobs are retained for 24 hours after completion. The server supports up to 2 concurrent downloads by default.
