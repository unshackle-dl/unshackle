# REST API Quick Start

Unshackle ships with a built-in REST API that exposes the same download engine you drive from the command line. It is served by the `unshackle serve` command and lets you queue downloads, poll their progress, browse services, and manage history over HTTP instead of the terminal.

The full loop runs end to end: start the server, authenticate, submit a download job, and poll it to completion, using real `curl` commands built directly from the API routes.

!!! note "Who this page is for"
    This is a **developer** guide. It assumes you are comfortable with HTTP, JSON, and a tool like `curl`. If you just want to download something, the [command line](../../index.md) is simpler. The REST API is meant for building tools, UIs, and automation on top of unshackle.

---

## Before you begin

You need a working unshackle install with services and CDM devices already configured, exactly as you would for a normal command-line download. If `unshackle dl` works for you, the API will too, since it runs the identical download pipeline.

=== "Installed as a tool"

    ```shell
    uv tool install git+https://github.com/unshackle-dl/unshackle.git
    unshackle --help
    ```

=== "From a clone"

    ```shell
    uv run unshackle --help
    ```

---

## Step 1: Configure an API secret

Every request (except the health check) is authenticated with a secret key sent in the `X-Secret-Key` header. The server reads this key from the `serve` section of your `unshackle.yaml` config.

At minimum you must set `api_secret`:

```yaml title="unshackle.yaml"
serve:
  api_secret: "choose-a-long-random-secret"
```

If you want the API to be able to license and download DRM-protected content, you also need to grant CDM devices to the key. Configure that under `serve.users`, keyed by the secret:

```yaml title="unshackle.yaml"
serve:
  api_secret: "choose-a-long-random-secret"
  users:
    choose-a-long-random-secret:
      devices: ["your_widevine_device"] # Widevine .wvd stems
      playready_devices: ["your_playready_device"] # PlayReady .prd stems
      username: api_user
```

!!! warning "The secret must exist"
    If `api_secret` is not set and you do **not** pass `--no-key`, `unshackle serve` refuses to start and raises an error. Treat the secret like a password. Anyone who has it can queue downloads on your machine.

!!! tip "Disabling auth for local testing"
    Passing `--no-key` to `serve` turns authentication off entirely: every request is allowed and the `X-Secret-Key` header is ignored. This is convenient for a throwaway local test, but never expose a `--no-key` server to a network you do not fully trust.

---

## Step 2: Start the server

Start the REST API with the `serve` command:

```shell
unshackle serve --api-only
```

By default the server binds to `127.0.0.1:8786`. The `--api-only` flag serves just the REST API and omits the Widevine/PlayReady CDM HTTP endpoints; drop it if you also want to expose those.

Common options:

| Option | Default | Effect |
| --- | --- | --- |
| `-h, --host` | `127.0.0.1` | Address to bind to |
| `-p, --port` | `8786` | Port to bind to |
| `--api-only` | off | Serve only the REST API, no CDM endpoints |
| `--no-key` | off | Disable API-key authentication (allow all requests) |
| `--debug-api` | off | Include tracebacks and stderr in error responses |
| `--debug` | off | Enable DEBUG-level logging |
| `--remote-only` | off | Expose only the remote-session subset of routes |
| `--caddy` | off | Also launch Caddy as a reverse proxy (for HTTPS) |

Once it is up you will see the API mounted under `/api/` and, unless you started in remote-only mode, an interactive Swagger UI:

```text
REST API endpoints available at http://127.0.0.1:8786/api/
Swagger UI available at http://127.0.0.1:8786/api/docs/
```

!!! note "Swagger UI is also behind auth"
    In `--api-only` mode (without `--no-key`), the Swagger UI at `/api/docs/` requires the `X-Secret-Key` header just like every other route. Only `/api/health` is exempt from authentication.

For the rest of this page we will assume the server is reachable at `http://127.0.0.1:8786` and store the secret in a shell variable:

```shell
export SECRET="choose-a-long-random-secret"
export BASE="http://127.0.0.1:8786"
```

---

## Step 3: Confirm the server is alive

The health endpoint is the one route that never requires a key. Use it to confirm the server is running and to read its version:

```shell
curl "$BASE/api/health"
```

```json title="200 OK"
{
  "status": "ok",
  "version": "5.3.0",
  "update_check": {
    "update_available": false,
    "current_version": "5.3.0",
    "latest_version": "5.3.0"
  }
}
```

The `update_check` block is best effort. If unshackle cannot reach the update source, `update_available` and `latest_version` come back as `null` rather than failing the request.

---

## Step 4: Authenticate a real request

Every other endpoint needs the `X-Secret-Key` header. A quick way to prove your key works is to list the services the server can see:

```shell
curl "$BASE/api/services" \
  -H "X-Secret-Key: $SECRET"
```

```json title="200 OK (truncated)"
{
  "services": [
    {
      "tag": "EXAMPLE",
      "aliases": ["EX"],
      "geofence": [],
      "title_regex": null,
      "url": "https://example.com",
      "help": "EXAMPLE service documentation...",
      "needs_auth": true,
      "has_search": true,
      "has_drm": true,
      "auth_methods": ["cookies", "credentials"],
      "cli_params": []
    }
  ]
}
```

If the key is missing or wrong, you get a `401` with a plain body:

```json title="401 Unauthorized"
{ "status": 401, "message": "Secret Key is Invalid." }
```

!!! warning "Two error shapes exist"
    The authentication middleware returns the short `{"status": 401, "message": "..."}` shape shown above. Every **other** error the API produces uses the richer structured shape (`error_code`, `timestamp`, `details`, ...) described in [Error handling](#error-handling). A robust client should handle both.

The `tag` value for the service you want is the identifier you pass as `service` in the calls below.

---

## Step 5: Submit a download job

Downloads are asynchronous. You `POST` a job to `/api/download`, the server immediately hands back a `job_id`, and the actual download runs in the background so your request never blocks.

The only required fields are `service` and `title_id`. Everything else mirrors the flags of the `dl` command and has a sensible default, so a minimal job looks like this:

```shell
curl -X POST "$BASE/api/download" \
  -H "X-Secret-Key: $SECRET" \
  -H "Content-Type: application/json" \
  -d '{
        "service": "EXAMPLE",
        "title_id": "12345"
      }'
```

A more realistic job pins the quality and codecs and asks for specific episodes:

```shell
curl -X POST "$BASE/api/download" \
  -H "X-Secret-Key: $SECRET" \
  -H "Content-Type: application/json" \
  -d '{
        "service": "EXAMPLE",
        "title_id": "12345",
        "quality": [1080],
        "vcodec": "H265",
        "range": ["SDR"],
        "wanted": ["S01E01", "S01E02"]
      }'
```

On success the server responds with **`202 Accepted`** and the identifier you will poll:

```json title="202 Accepted"
{
  "job_id": "3f9c1e0a-8d2b-4e6f-9a1c-77b0f2a1c4d5",
  "status": "queued",
  "created_time": "2026-07-03T12:00:00.000000+00:00"
}
```

Capture the `job_id` for the next step:

```shell
JOB=$(curl -s -X POST "$BASE/api/download" \
  -H "X-Secret-Key: $SECRET" \
  -H "Content-Type: application/json" \
  -d '{"service":"EXAMPLE","title_id":"12345","quality":[1080]}' \
  | python -c "import sys, json; print(json.load(sys.stdin)['job_id'])")
```

### Frequently used download fields

The request body accepts the full download parameter set. A few of the most useful:

| Field | Default | Meaning |
| --- | --- | --- |
| `service` | - | Service tag (required) |
| `title_id` | - | Title identifier (required) |
| `profile` | `null` | Named credential/cookie profile to use |
| `quality` | `[]` | Resolution(s), e.g. `[1080]`; empty means best available |
| `vcodec` | `null` | Video codec, e.g. `"H265"` |
| `acodec` | `null` | Audio codec, e.g. `"EC3"` |
| `range` | `["SDR"]` | Dynamic range: `SDR`, `HDR10`, `HDR10+`, `DV`, `HLG`, `HYBRID` |
| `wanted` | `[]` | Episodes to fetch, e.g. `["S01E01"]`; empty means all |
| `lang` | `["orig"]` | Video/audio language(s) |
| `s_lang` | `["all"]` | Subtitle language(s) |
| `downloads` | `1` | Number of tracks to download concurrently |
| `proxy` | `null` | Proxy URI or country code |

Validation is strict: invalid codecs, subtitle formats, dynamic-range names, non-positive counts, or mutually exclusive flags (for example both `video_only` and `audio_only`) come back as `400` with error code `INVALID_PARAMETERS`. See the full reference on the [download reference page](endpoints.md) for every accepted field.

!!! note "Gated fields"
    Passing a per-request CDM (`cdm`) or inline credentials (`credential` / `credentials`) is refused with `403 FORBIDDEN` unless the server operator has explicitly opted in via `serve.cdm_overrides` and `serve.allow_job_credentials`. Both are off by default.

---

## Step 6: Poll the job to completion

A job moves through these states:

```text
queued → downloading → completed | failed | cancelled
```

The last three are **terminal**. Poll the job detail endpoint until you reach one of them. This endpoint always returns the full job record:

```shell
curl "$BASE/api/download/jobs/$JOB" \
  -H "X-Secret-Key: $SECRET"
```

While the download is running you will see `status: "downloading"` and a `progress` value climbing from `0` toward `100`:

```json title="200 OK (in progress)"
{
  "job_id": "3f9c1e0a-8d2b-4e6f-9a1c-77b0f2a1c4d5",
  "status": "downloading",
  "progress": 42.7,
  "phase": "downloading",
  "service": "EXAMPLE",
  "title_id": "12345",
  "title": "Example Show S01E01",
  "current_title": "Example Show S01E01",
  "completed_tracks": 1,
  "total_tracks": 3,
  "segments_done": 512,
  "segments_total": 1200,
  "speed": "8.4 MiB/s",
  "created_time": "2026-07-03T12:00:00.000000+00:00",
  "started_time": "2026-07-03T12:00:03.000000+00:00"
}
```

When it finishes you will see `status: "completed"` and the produced files under `output_files`:

```json title="200 OK (completed)"
{
  "job_id": "3f9c1e0a-8d2b-4e6f-9a1c-77b0f2a1c4d5",
  "status": "completed",
  "progress": 100.0,
  "output_files": ["/path/to/downloads/Example Show S01E01.mkv"],
  "completed_time": "2026-07-03T12:04:31.000000+00:00"
}
```

A simple polling loop in shell:

```shell title="poll.sh"
while true; do
  STATUS=$(curl -s "$BASE/api/download/jobs/$JOB" \
    -H "X-Secret-Key: $SECRET" \
    | python -c "import sys, json; print(json.load(sys.stdin)['status'])")
  echo "job status: $STATUS"
  case "$STATUS" in
    completed|failed|cancelled) break ;;
  esac
  sleep 3
done
```

!!! tip "Polling cadence"
    A few seconds between polls is plenty. The server writes progress updates roughly twice a second internally, but there is no push/streaming channel; you read the current snapshot each time you ask.

If a job ends in `failed`, the full record includes `error_message`, `error_code`, and (when the server was started with `--debug-api`) `error_traceback` and `worker_stderr` to help you diagnose it. Sensitive values in these fields are scrubbed before they are returned.

---

## Step 7: Clean up

Once you have collected the output you can remove the finished job. `DELETE` on a job has two behaviors depending on its state:

```shell
curl -X DELETE "$BASE/api/download/jobs/$JOB" \
  -H "X-Secret-Key: $SECRET"
```

- If the job is **terminal** (completed/failed/cancelled) it is removed from the manager and you get **`204 No Content`** with an empty body.
- If the job is still **queued or downloading** it is cancelled instead, and you get `200` with `{"status": "success", "message": "Job cancelled"}`.

!!! warning "Do not parse JSON on 204"
    A `204` response has no body. This applies to terminal-job removal here and to deleting history entries; check the status code before attempting to decode JSON.

To clear every finished job at once instead of one at a time:

```shell
curl -X POST "$BASE/api/download/jobs/clear-finished" \
  -H "X-Secret-Key: $SECRET"
```

```json title="200 OK"
{ "removed": 3 }
```

---

## Managing the queue

A handful of endpoints round out job control. All require the `X-Secret-Key` header.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/download/jobs` | List all jobs, with optional filtering and sorting |
| `GET` | `/api/download/jobs/{job_id}` | Full detail for one job |
| `DELETE` | `/api/download/jobs/{job_id}` | Cancel a running job, or remove a terminal one |
| `POST` | `/api/download/jobs/{job_id}/retry` | Re-queue a new job from a terminal one |
| `POST` | `/api/download/jobs/{job_id}/priority` | Move a queued job to the front of the queue |
| `POST` | `/api/download/jobs/clear-finished` | Remove all terminal jobs |

Listing supports query parameters for filtering by `status` or `service`, sorting via `sort_by` (`created_time`, `started_time`, `completed_time`, `progress`, `status`, `service`) and `sort_order` (`asc` / `desc`), and requesting full per-job detail with `full=true`:

```shell
curl "$BASE/api/download/jobs?status=downloading&sort_by=progress&sort_order=desc" \
  -H "X-Secret-Key: $SECRET"
```

```json title="200 OK"
{
  "jobs": [
    {
      "job_id": "3f9c1e0a-8d2b-4e6f-9a1c-77b0f2a1c4d5",
      "status": "downloading",
      "service": "EXAMPLE",
      "title_id": "12345",
      "title": "Example Show S01E01",
      "progress": 42.7,
      "phase": "downloading",
      "completed_tracks": 1,
      "total_tracks": 3
    }
  ]
}
```

Retrying reuses the original job's service, title, and parameters to enqueue a brand new job (returning `202` with a fresh `job_id`); it only works on terminal jobs and returns `409 CONFLICT` otherwise. Prioritizing only works on jobs still in the `queued` state.

---

## Beyond a single download

The download loop above is the core of the API, but the same server exposes more:

- **Discovery**: `POST /api/search` finds titles by query, and `POST /api/list-titles` / `POST /api/list-tracks` inspect what a title offers before you commit to downloading it. See [list titles and tracks](endpoints.md).
- **History**: `GET /api/history` reads a persisted log of jobs that reached a terminal state (newest first), and `DELETE /api/history/{job_id}` removes a single entry. See [history](endpoints.md).
- **Introspection**: `GET /api/profiles` lists configured credential profiles per service, `GET /api/config` returns a redacted view of the effective server config, and `GET /api/env/check` reports which external binaries are installed.
- **Maintenance**: `POST /api/maintenance/clear-cache`, `.../clear-temp`, and `.../refresh-services` perform housekeeping (the cache/temp clears are blocked with `409` while a download is active).
- **Remote sessions**: the `/api/session/*` endpoints drive the thin-client "remote download" flow, where the server authenticates and licenses on behalf of a local client. See [remote sessions](remote-sessions.md).

The interactive **Swagger UI at `/api/docs/`** documents every route and lets you try them in the browser (send your `X-Secret-Key` there too).

---

## Error handling

Apart from the auth middleware's short form, every error the API returns uses a single structured envelope:

```json
{
  "status": "error",
  "error_code": "INVALID_PARAMETERS",
  "message": "Human-readable description",
  "timestamp": "2026-07-03T12:00:00Z",
  "details": { }
}
```

`details` appears only when there is something to add, `retryable: true` appears only for transient failures, and a `debug_info` block (exception type and traceback) is included only when the server runs with `--debug-api`.

The `error_code` maps to an HTTP status. The ones you will meet most often:

| Error code | HTTP | Typical cause |
| --- | --- | --- |
| `INVALID_INPUT` | 400 | Malformed JSON body or missing required field |
| `INVALID_SERVICE` | 400 | Unknown or disallowed service tag |
| `INVALID_PARAMETERS` | 400 | A download field failed validation |
| `AUTH_FAILED` | 401 | Service login/credential failure |
| `FORBIDDEN` | 403 | A gated field was used without server opt-in |
| `NOT_FOUND` | 404 | Title or resource does not exist |
| `JOB_NOT_FOUND` | 404 | No job with that `job_id` |
| `CONFLICT` | 409 | Illegal state transition (e.g. retry a running job) |
| `RATE_LIMITED` | 429 | Upstream throttling (retryable) |
| `INTERNAL_ERROR` | 500 | Unexpected server-side failure |
| `SERVICE_ERROR` | 502 | The service raised an error |
| `NETWORK_ERROR` | 503 | Connection/timeout reaching the service (retryable) |

!!! tip "Retry politely"
    Codes marked retryable (`NETWORK_ERROR`, `RATE_LIMITED`, `SERVICE_UNAVAILABLE`) reflect transient conditions. Back off and retry rather than hammering the server.
