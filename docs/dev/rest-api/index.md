# REST API Overview

unshackle ships with a built-in HTTP server. Alongside the command-line tool, you
can operate `unshackle serve` and drive the same download engine over a REST API:
searching services, listing titles and tracks, queueing downloads, watching live
progress, and browsing history, all as JSON over HTTP.

This page explains what the API is for, how to start the server, where it listens,
and how the endpoints are organised. Later pages cover each area in depth.

!!! info "Who this section is for"
    The REST API is for people who make automation, dashboards, or remote
    clients on top of unshackle. If you only download the occasional title, the
    [command-line tool](../../guide/cli-reference.md) does everything the API does
    and is simpler to use. Reach for the API when you want a long-running service, a
    job queue, or a web front-end.

## What the API is for

The server exposes the same capabilities as the `dl` command, but as discrete HTTP
calls you can script against:

- **Discovery**: show the services available on the server, use a service's search,
  and enumerate the titles and tracks behind a title ID.
- **Downloading**: submit a download as a background *job*, then follow its status and
  live progress (by polling, or by subscribing to its Server-Sent Events stream),
  cancel it, retry it, or bump it to the front of the queue.
- **History and housekeeping**: read the log of finished jobs, inspect the server's
  effective (redacted) configuration, examine external tool versions, and clear caches
  or temp files.
- **Remote-download sessions**: a thin local client can authenticate against a
  service *on the server*, then pull titles, tracks, and segment info back, and proxy
  DRM licensing. This is how one machine with the credentials and CDMs can serve
  downloads to another. See [Remote Sessions](remote-sessions.md).

Because every download runs as a queued job in a separate job worker process, the API is
well suited to running unattended: submit work, come back later, and read the result.

## Running the server

Start the server with the `serve` command:

```shell title="Start the REST API on the default host and port"
unshackle serve
```

By default the server binds to `127.0.0.1:8786` and requires an API key on every
request. It also serves the local Widevine and PlayReady CDM endpoints (for
pywidevine / pyplayready clients) in addition to the REST API.

!!! warning "An API key is required by default"
    Unless you pass `--no-key`, the server needs an API secret configured under the
    `serve` section of your config. If none is set, `unshackle serve` refuses to
    start. See [Authentication](authentication.md) for how to set `api_secret` and
    how the `X-Secret-Key` header works.

### Common options

The `serve` command accepts the following flags:

| Option | Default | Effect |
|---|---|---|
| `-h`, `--host` | `127.0.0.1` | Address to bind to. Use `0.0.0.0` to accept connections from other machines. |
| `-p`, `--port` | `8786` | Port to bind to. |
| `--api-only` | off | Serve only the REST API, without the Widevine/PlayReady CDM HTTP endpoints. |
| `--no-widevine` | off | Disable the Widevine CDM endpoints. Cannot be combined with `--api-only`. |
| `--no-playready` | off | Disable the PlayReady CDM endpoints. Cannot be combined with `--api-only`. |
| `--no-key` | off | Disable API-key authentication entirely; every request is allowed. |
| `--remote-only` | off | Expose only the remote-session subset of routes. Implies `--api-only`. |
| `--debug-api` | off | Include tracebacks and worker stderr in error responses. |
| `--debug` | off | Enable debug-level logging. |
| `--caddy` | off | Also launch [Caddy](https://caddyserver.com/) as a reverse proxy, using the `Caddyfile` in your user-config directory. |

```shell title="Serve the REST API only, on all interfaces"
unshackle serve --api-only --host 0.0.0.0 --port 8786
```

!!! danger "Binding to a public interface"
    `--no-key` disables authentication completely. Anyone who can reach the port can
    find titles, download them, and read your configuration. Only combine it with a host like
    `127.0.0.1`, or keep the default key-based auth when binding to `0.0.0.0`. Put a
    TLS-terminating reverse proxy (Caddy, nginx) in front for anything internet-facing.

### Server modes

The routes the server mounts depend on how you start the server:

=== "Default (CDM + REST)"

    The server mounts both the pywidevine/pyplayready CDM endpoints and the full REST API.
    This is the mode you get when you operate `unshackle serve` with no mode flags.

=== "`--api-only`"

    The server mounts only the REST API. It does not mount the CDM HTTP endpoints.
    Authentication uses the single configured API key (or none, with `--no-key`).

=== "`--remote-only`"

    The server mounts only the remote-session subset of routes (health, services, search,
    and everything under `/api/session/*`). This mode implies `--api-only`, and the
    interactive Swagger UI is **not** mounted.

## Base URL

All REST endpoints live under the `/api` prefix. With the default bind address the
base URL is:

```
http://127.0.0.1:8786/api
```

Every path in this documentation is relative to the host and port. For example, the
health check is `GET http://127.0.0.1:8786/api/health`.

!!! tip "Interactive API docs"
    Unless you started the server with `--remote-only`, an interactive Swagger UI is
    available at [`/api/docs/`](http://127.0.0.1:8786/api/docs/). The server generates
    it from the same route table it serves, so it always matches the version you
    operate. Note that in `--api-only` mode the docs page is itself behind the
    API key. Only `/api/health` is exempt from authentication.

A quick smoke test that needs no API key:

```shell title="Confirm the server is up"
curl http://127.0.0.1:8786/api/health
```

```json title="Response"
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

## Endpoint map

Every REST path uses JSON request and response bodies and lives under `/api`. The
table below is a map of the surface. The [Endpoints](endpoints.md) page gives each
request and response in full.

### Discovery

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Liveness and version check. Never requires a key. |
| `GET` | `/api/services` | List the services available on this server. |
| `POST` | `/api/search` | Run a service's search for a query. |
| `POST` | `/api/list-titles` | List the titles behind a service title ID. |
| `POST` | `/api/list-tracks` | List the video, audio, and subtitle tracks for a title. |

### Downloads and jobs

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/download` | Queue a download; returns a `job_id`. |
| `GET` | `/api/download/jobs` | List jobs, with filtering and sorting. |
| `POST` | `/api/download/jobs/clear-finished` | Remove all finished jobs. |
| `GET` | `/api/download/jobs/{job_id}` | Full detail for one job. |
| `GET` | `/api/download/jobs/{job_id}/events` | Stream one job's progress as Server-Sent Events. |
| `DELETE` | `/api/download/jobs/{job_id}` | Cancel a running job, or remove a finished one. |
| `POST` | `/api/download/jobs/{job_id}/retry` | Re-queue a finished job's parameters as a new job. |
| `POST` | `/api/download/jobs/{job_id}/priority` | Move a queued job to the front. |

### History and server info

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/history` | Read the persisted log of finished jobs. |
| `DELETE` | `/api/history/{job_id}` | Delete a history entry. |
| `GET` | `/api/profiles` | Named credential profiles per service. |
| `GET` | `/api/config` | The server's effective, redacted configuration. |
| `GET` | `/api/env/check` | Presence and versions of external tools. |

### Maintenance

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/maintenance/clear-cache` | Clear the cache directory. |
| `POST` | `/api/maintenance/clear-temp` | Clear the temp directory. |
| `POST` | `/api/maintenance/refresh-services` | `git pull` each configured service repository. |

### Remote-download sessions

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/session/create` | Start a server-side, authenticated service session. |
| `GET` | `/api/session/{session_id}` | Session info and time-to-live. |
| `DELETE` | `/api/session/{session_id}` | Tear down a session. |
| `GET` | `/api/session/{session_id}/titles` | Titles for the session's title ID. |
| `POST` | `/api/session/{session_id}/tracks` | Tracks (with URLs) for a chosen title. |
| `POST` | `/api/session/{session_id}/segments` | Per-track segment/download details. |
| `POST` | `/api/session/{session_id}/license` | Proxy or server-side DRM licensing. |
| `GET` | `/api/session/{session_id}/prompt` | Poll for interactive auth status/prompt. |
| `POST` | `/api/session/{session_id}/prompt` | Submit an answer to an auth prompt. |

!!! note "Remote-only routes"
    The health, services, search, and `/api/session/*` routes are the ones exposed
    when the server runs with `--remote-only`. Everything else (downloads, jobs,
    history, config, maintenance) is only available in the full or `--api-only` modes.

## Authentication in one line

With the default configuration, send your API secret in the `X-Secret-Key` header on
every request except `/api/health`:

```shell title="An authenticated request"
curl -X POST http://127.0.0.1:8786/api/search \
  -H "X-Secret-Key: your-api-secret" \
  -H "Content-Type: application/json" \
  -d '{"service": "EXAMPLE", "query": "example"}'
```

A missing or unknown API key returns `401`. [Authentication](authentication.md) gives
the full model, including per-key service allowlists.

## Where to go next

<div class="grid cards" markdown>

- **[Quick Start](quickstart.md)**: start the server, set an API key, and operate your
  first end-to-end download over the API.
- **[Authentication](authentication.md)**: the `X-Secret-Key` header, `--no-key`,
  per-key service allowlists, and CORS.
- **[Endpoints](endpoints.md)**: every request body, query parameter, and response
  shape, endpoint by endpoint.
- **[Job Lifecycle](job-lifecycle.md)**: how a download moves from `queued` to a
  terminal state, live progress, cancellation, and retries.
- **[Remote Sessions](remote-sessions.md)**: the `/api/session/*` flow for driving a
  service from a thin remote client.
- **[Error Responses](errors.md)**: the standard error envelope, error codes, and
  their HTTP statuses.

</div>
