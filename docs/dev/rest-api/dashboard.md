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
| `/api/dashboard/status` | Version, `code_hash`, bind host and port, `mode` (`full`, `api_only`, `remote_only`), uptime, `requests_total`, `requests_rejected` (401s), `requests_by_key` (label → count), loaded service count, session count, `max_sessions` (`null` when the operator configured no limit), `session_ttl`, job counts by status. The service count includes services that failed to import - use `/api/dashboard/services` for the number that loaded. |
| `/api/dashboard/sessions` | Every live remote session: `id`, `owner` (username or masked key), `creator_ip`, `service`, `title_id`, `titles`, `tracks`, `title` (the first resolved title's display name), `auth_status`, `auth_error`, `server_account` (the server profile lent to the remote session, else `null`), `client`, `actions`, `created_at` and `last_accessed` (ISO 8601) with `created_ts` and `last_accessed_ts` (Unix epoch, the same clock as log `ts`), `age_seconds`, `idle_seconds`, `log_seq`. `client` is whatever the remote client sent as `client` in its session create request: the CLI sends `version` and `platform`, an older client sends nothing and the field is `{}`. `log_seq` is the last sequence number in the remote session's service log - poll `/api/dashboard/sessions/{id}/logs` when it changes. `actions` is the session's request log, newest last, capped at 500: `ts`, `method`, `action` (`titles`, `tracks`, `segments`, `license`, `prompt`, …), `query`, `status`, `ms`, `bytes_in`, `bytes_out`. |
| `/api/dashboard/jobs` | Every download job with full detail, regardless of owner. Empty in `--remote-only` mode. |
| `/api/dashboard/logs` | `{"seq": N, "records": [...]}`: the last 1000 log records. `?since=<seq>` returns only newer records; `?level=WARNING` sets the minimum level; `?logger=serve` keeps one logger and its children (`aiohttp.access` is the noisiest). `ts` is a Unix epoch in seconds. |
| `/api/dashboard/sessions/{id}/logs` | One remote session's service log: `{"session_id", "last_seq", "records": [...]}`. Each record has `seq`, `ts`, `level`, `message`. `?since=<seq>` returns only newer records. 404 when the remote session is unknown. |
| `/api/dashboard/keys` | Every configured API key: what it may do and what it has done. One row per key in `serve.users`, plus `serve.api_secret` when it is set. |
| `/api/dashboard/services` | Every discovered service and its load state, including the ones that failed to import. Not filtered by any allowlist. |
| `/api/dashboard/health` | Preflight: whether this instance could finish a download. Cached 30 seconds. |
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
| `service` | `action` (`staged`, `applied` or `failed`) and `tags`, the services it applies to. A `failed` action carries `errors` and an empty `tags`. Refetch `/api/dashboard/services` when one arrives, instead of polling for a reload. |

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

## Remote session logs

A remote session mirrors the service's own `self.log` into a 500-record buffer at `INFO`,
whatever level the server prints at. The remote session summary carries only a truncated
`auth_error`; the buffer holds the real reason a login failed.

```
GET /api/dashboard/sessions/{session_id}/logs?since=12
{"session_id": "...", "last_seq": 14,
 "records": [{"seq": 13, "ts": 1756909254.1, "level": "ERROR", "message": "login rejected: MFA required"}]}
```

The field name is `records`, the same one `/api/dashboard/logs` uses, so one dashboard reads
both logs the same way. Each record has the shape the client route `/api/session/{id}/logs`
returns. `last_seq` is the buffer's own last sequence number, so an empty page still reports
the true value.

Reading is a cursor read, not a drain: the client draining the same buffer still gets every
record. It also does not refresh the remote session's idle timer, so polling a remote session here
never keeps it alive or makes it look active.

!!! note "`log_seq` is a poll trigger, not a push"
    A remote session summary reaches the dashboard only when something publishes a `session`
    event - a request the remote session makes, or an auth transition. A log line on its own
    publishes nothing. Poll this route when `log_seq` changes, and on a timer while a drawer
    is open.

## Keys

```
GET /api/dashboard/keys
[{"id": "3ed054be9371", "role": "user", "label": "tier1", "services": ["ATV", "DSNP"],
  "server_cdm": false, "server_accounts": false, "server_proxy": false,
  "tier": "bot", "rate_limit": 600, "window_used": 412,
  "requests": 41300, "rejected": 4, "bytes_out": 103079215104, "last_seen": 1756909188.2},
 {"id": "7c1d0f52a884", "role": "dashboard", "label": "dash…", "services": [],
  "server_cdm": false, "server_accounts": false, "server_proxy": false,
  "tier": null, "rate_limit": null, "window_used": 0,
  "requests": 8140, "rejected": 0, "bytes_out": 41200311, "last_seen": 1756909350.9}]
```

`id` is a hash of the API key, so it stays stable across restarts and carries no key material.
Two API keys with no `username` merge into one label in `requests_by_key` but stay separate
here, so use `id` as the identity and `label` only for display.

| Field | Meaning |
|-------|---------|
| `role` | Which key this is: `user` (a `serve.users` entry), `admin` (`serve.api_secret`) or `dashboard` (`serve.dashboard.key`). Identity, not capability - read the grant fields for what the API key may do. |
| `services` | The effective allowlist, the global list intersected with the API key's. `null` means unrestricted; `[]` means the API key reaches no service route at all. |
| `server_cdm`, `server_accounts` | `false`, `true`, or the list of service tags the grant covers. |
| `server_proxy` | Boolean. Only a literal `true` grants it, and an API key with no `users` entry does not get it. |
| `rate_limit` | Requests per hour, from the API key's own value or its tier's. `null` means no limit. |
| `window_used` | Requests counted in the current hourly window. |
| `rejected` | Responses of 400 or worse that the server attributes to this API key, 429s included. The global `requests_rejected` stays 401-only. |
| `bytes_out` | Response bodies only. An event stream reports no body length, so it counts as zero. |
| `last_seen` | `null` until the API key makes its first request. |

Only a configured API key gets a row of counters. A request on an open route such as `/api/health` with an unknown key counts as `anonymous`, so an unauthenticated caller cannot grow the table.

Every API key the server counts has a row here, the dashboard key included, so a
`requests_by_key` bucket other than `anonymous` always matches one. A "traffic by key" panel
reads the rows here, which are unambiguous, and takes `anonymous` from `requests_by_key`. Read
`role` before you draw the bars: the `dashboard` row is the panel measuring its own polling.

The dashboard key gets `services: []` and no grants unless the operator also gave it a
`serve.users` entry, because it reaches nothing outside `/api/dashboard/`. An API key listed
in more than one place still gets exactly one row.

### Rate limits

An API key over its limit gets `429` with a `Retry-After` header:

```yaml
serve:
  tiers:
    bot: { rate_limit: 600 }   # requests per hour
  users:
    "<api key>":
      username: bot-relay
      tier: bot
      # rate_limit: 900        # optional, wins over the tier
```

The server keeps a fixed window, not a sliding one: it opens on the first request and resets
an hour later. An API key with no tier and no override has no limit. A `tier` that names no
entry under `serve.tiers`, or a `rate_limit` that is not a positive whole number, stops the
server at startup, because the alternative is an API key that silently gets no limit. The rate
limit never applies to the dashboard key, and never to `/api/health`.

## Services

```
GET /api/dashboard/services
[{"tag": "MAX", "state": "staged", "error": null,
  "commit": "9cc230c98f13b83f6c6fe831595b9702d3ba2786", "staged_commit": "5f0aa17...",
  "staged_since": 1756908900.0, "sessions": 1, "jobs": 1,
  "aliases": ["HBO"], "geofence": ["US"]}]
```

`state` is `loaded`, `staged` or `failed`. A service that failed to import keeps its error
here instead of disappearing, and `/status` still counts it in the service total - count the
`state` values here for the number that loaded.

A `staged` service has an update on disk that a busy service blocks from the import: the repo
pull already happened, so `commit` (what runs now) and `staged_commit` (what waits) differ. It swaps in when the last job for that tag finishes. Both commits are `null`
for a service from a plain local directory rather than a git repo.

Watch the `service` event rather than polling for the swap.

## Health

```
GET /api/dashboard/health
{"generated_at": 1756909350.4, "status": "ok",
 "checks": [{"id": "ffmpeg", "label": "ffmpeg", "status": "ok",
             "detail": "7.1 · /usr/bin/ffmpeg", "ms": 4.1}]}
```

An instance can answer every request and still fail every download, because `mp4decrypt` is
missing or a vault's credentials expired. This finds that before a job does.

`status` is `ok`, `degraded` (an optional dependency is missing) or `failing` (a required one
is). Player binaries are not checked: a headless server never has one and it plays nothing.
Each check is `ok`, `warn` or `fail`. Check ids are the binary names, `cdm`,
`vault:<name>` (the vault's configured `name`, so two vaults of the same type stay apart), and
`proxies`.

The server caches the result for 30 seconds and keeps every probe shallow: a vault answers one
lookup that should miss, and no probe allocates anything. A failing probe's `detail` has the
vault's password or token masked. This is a panel, not a liveness probe - do not poll it faster
than the cache: every refresh that the cache does not answer costs one real lookup against each
configured vault, which a vault that meters or rate limits its callers will count.

!!! warning "An unreachable vault costs a slow, partial answer"
    The probes run one after another, so a cold read takes as long as the probes that finish
    inside the deadline. The endpoint gives up after 15 seconds and answers with the checks that
    did complete, plus one `probe` check with the status `fail` naming how many finished. A
    database vault that accepts the connection and then stalls is what this deadline is for: its
    driver applies no timeout of its own, so without one the request would never answer.

    The probe that timed out keeps running in its thread until its driver returns, and the
    server caches the partial answer for the full 30 seconds, so callers cannot stack up more
    of them. A second caller waits behind the first, because the server shares one refresh
    rather than running it twice.

!!! warning "Proxy providers are only checked at construction"
    The proxy check builds each proxy provider but never asks it for a proxy, because for some
    proxy providers that allocates a connection. A proxy provider that only fails on its first
    real use still reads `ok`.

## Counters reset on restart

Stats, the log ring and the event history are all in memory. A restart drops every number to
zero. `started_at` in the `stats` frame (with `version` and `code_hash`) is the signal: it
arrives in the first frame after a reconnect, so a dashboard can tell a restart from a bug
without any extra field.
