# Developer Dashboard

The dashboard endpoints show the operator of a `serve` instance what the server is doing:
version and uptime, request counts, live remote sessions, download jobs, and the recent
log lines. They exist so you can watch a headless server (`serve --quiet`) from a web page
instead of a terminal.

The dashboard endpoints are for the person who runs the server, not for API users. They
have their own key: a user key or a tier key never opens them.

## Enable

Set a dashboard key under `serve.dashboard` in `unshackle.yaml`:

```yaml
serve:
  api_secret: ...
  dashboard:
    key: a-long-random-string
```

Without `dashboard.key` the server does not register the routes, and they answer `404`.
`serve --no-key` turns off user authentication only; the dashboard key stays required.
Once the dashboard key is set, every `/api/dashboard/` request must carry it in
`X-Secret-Key`.
Every other endpoint refuses the dashboard key, and the dashboard refuses user keys. The
routes are available in `--remote-only` mode too.

Run the server headless with `--quiet`: no banner, no Rich output, one plain log line per
event on stderr, and `api.*` loggers stay at `INFO` so remote session activity reaches the
log ring. Add `--debug` to raise the level to `DEBUG` and also write the full log to
`directories.logs` as `unshackle_serve_<time>.log`. Nothing else the dashboard shows reaches
the disk: stats, the log ring and the event stream live in memory and reset on restart, so a
dashboard UI that wants history must keep its own copy.

```
unshackle serve --remote-only --quiet
```

## Endpoints

All are `GET`.

| Path | Returns |
|------|---------|
| `/api/dashboard/status` | Version, `code_hash`, bind host and port, `mode` (`full`, `api_only`, `remote_only`), uptime, `requests_total`, `requests_rejected` (401s), `requests_by_key` (label → count), loaded service count, session count, `max_sessions`, `session_ttl`, job counts by status. |
| `/api/dashboard/sessions` | Every live remote session: `id`, `owner` (username or masked key), `creator_ip`, `service`, `title_id`, `titles`, `tracks`, `title` (the first resolved title's display name), `auth_status`, `auth_error`, `client`, `actions`, `created_at` and `last_accessed` (ISO 8601) with `created_ts` and `last_accessed_ts` (Unix epoch, the same clock as log `ts`), `age_seconds`, `idle_seconds`. `client` is whatever the remote client sent as `client` in its session create request (the current CLI sends nothing, so it is `{}`). `actions` is the session's request log, newest last, capped at 500: `ts`, `method`, `action` (`titles`, `tracks`, `segments`, `license`, `prompt`, …), `query`, `status`, `ms`, `bytes_in`, `bytes_out`. |
| `/api/dashboard/jobs` | Every download job with full detail, regardless of owner. Empty in `--remote-only` mode. |
| `/api/dashboard/logs` | `{"seq": N, "records": [...]}`: the last 1000 log records. `?since=<seq>` returns only newer records; `?level=WARNING` sets the minimum level; `?logger=serve` keeps one logger and its children (`aiohttp.access` is the noisiest). `ts` is a Unix epoch in seconds. |
| `/api/dashboard/events` | Server-Sent Events stream. With `?since=<seq>` it returns `{"seq", "stats", "events": [...]}` instead: every event after `seq` (the server keeps the last 20000, log records included, so a poll every few seconds is safe even under a busy access log) as one JSON burst, so a UI that polls every few seconds still sees a remote session that opened and closed in between. |

`requests_rejected` counts 401 responses; those never appear in `requests_by_key`. The
label is the `username` from the `users` entry when one is set, else the first four
characters of the API key.

## Event stream

`/api/dashboard/events` sends a `stats` event (the same body as `/status`) on connect and
then every 5 seconds, so the connection stays alive and the counters stay current. Between
those it sends:

| Event | Data |
|-------|------|
| `log` | One log record: `seq`, `ts`, `level`, `logger`, `msg`. |
| `session` | `action` (`create`, `update` on every request the session makes and when titles or tracks load, or `delete`) plus the full remote session summary, so each event is a complete snapshot. A `delete` also carries `reason`: `closed` (the client ended it), `expired` (TTL sweep) or `evicted` (`max_sessions` reached). A session opened by `--skip-dl` can live only a few seconds, so a UI that wants history must keep its own record of these events. |
| `job` | `event` (`queued`, `progress`, `status`, or the terminal status) plus the full job object. |

Every frame carries an `id:` (the event seq). After a dropped connection an `EventSource`
reconnects with `Last-Event-ID` and the server replays the events it missed. The connection
never closes on its own.

A browser `EventSource` cannot set headers, so this route also accepts the dashboard key as
`?secret_key=`. The dashboard key then appears in proxy and access logs and in browser history, so
use the query parameter only for `/events` and send the header everywhere else.

```js
const es = new EventSource(`${server}/api/dashboard/events?secret_key=${key}`);
es.addEventListener("log", (e) => console.log(JSON.parse(e.data).msg));
```

CORS is open (`Access-Control-Allow-Origin: *`), so a dashboard page served from another
origin works without a proxy.
