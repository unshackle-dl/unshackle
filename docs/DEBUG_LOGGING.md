# Structured debug logging

unshackle emits structured JSON Lines (JSONL) when `-d`/`--debug` (a **global** flag, before the
subcommand: `unshackle -d dl ...`) or `config.debug` is set. Output lands at
`config.directories.logs / unshackle_debug_<service>_<time>.jsonl`. The logger is built for
**developers troubleshooting pipeline flow** — maximum signal, minimum noise — not end users.

## Adding logging to a new feature

Use the two primitives from `unshackle.core.utilities`. Both are **no-ops when debug logging is
disabled**, so they are always safe to call unguarded — no `if debug_logger:` needed.

```python
from unshackle.core.utilities import log_event, timed_operation

# One-shot event:
log_event("myfeature_done", message="Did the thing", context={"count": n})

# Time a block (logs once at the end with duration_ms; ERROR + exception if it raises):
with timed_operation("myfeature_run", context={"input": str(path)}):
    do_the_work()
```

That's it. Do **not** write `if dl := get_debug_logger(): dl.log(...)` in new code — `log_event`
replaces that boilerplate.

### External tools (ffmpeg, mkvmerge, dovi_tool, …)

Route binary calls through the helpers in `unshackle.core.utils.subprocess`:

- `run_step(args, *, label=..., output=...)` — runs a CLI step and **auto-logs** a `tool_run`
  entry (label, tool, returncode, duration). Prefer this for new tool calls.
- `ffprobe(uri)` — auto-logs its `tool_run`.
- `log_tool_run(label, tool, returncode, *, duration_ms=..., **ctx)` — for a direct
  `subprocess.run` you can't route through `run_step`; call it right after the process returns.

## Conventions

- **Operation names**: `<area>_<event>` lowercase, e.g. `manifest_dash_parse`, `drm_decrypt`,
  `mux_complete`, `vault_get_key`, `tool_run`. Names are plain inline strings (no central registry).
- **Levels = the flow skeleton.** One **INFO** milestone per stage (a dev runs
  `jq 'select(.level=="INFO")'` to read the end-to-end flow); internals at **DEBUG**; failures at
  **ERROR**. Keep INFO sparse.
- **Every entry carries a one-sentence `message`** that reads on its own; structured data
  (`context`, `duration_ms`, counts, ids) lives in fields, not prose.
- **No raw dumps.** Counts, ids, sizes, and `safe_display_url(url)` only — never a full `Tracks`,
  MPD/manifest body, or response payload.
- **Secrets, URLs & paths.** Every logged string passes through `redact_all` =
  `redact_text` (mask password/token/secret/auth/cookie keys + URL userinfo/secret query params)
  → `redact_url` (collapse any http(s) URL to `redacted[.ext]`, hiding CDN/content/manifest/api
  locations while keeping the extension, e.g. `redacted.mpd`) → `redact_path` (strip local base
  dirs: install root → `<unshackle>`, venv → `<venv>`, home → `~`). `key` fields are also redacted
  unless `config.debug_keys`. Pass user data via `context=`/`request=`/kwargs so it is sanitized.
  Net effect: a shared JSONL leaks no account URLs, machine paths, or usernames.
- **Service calls are intentionally not logged** (no per-request POST/GET to services). Manifest
  parsing (`manifest_*_parse`) is the seam for request-level visibility.

## Reading the output

```bash
# Flow skeleton (one line per milestone):
jq -r 'select(.level=="INFO") | "\(.operation)\t\(.message)"' unshackle/logs/unshackle_debug_*.jsonl

# Everything for one correlated operation:
jq 'select(.operation_id=="abc12345")' <log>
```

## Primitives reference

| Symbol | Module | Purpose |
|---|---|---|
| `log_event(op, *, level, message, **ctx)` | `core.utilities` | one-shot structured entry |
| `timed_operation(op, *, level, message, **ctx)` | `core.utilities` | context manager; logs once at end with `duration_ms` (ERROR on raise) |
| `DebugLogger.log_drm_operation / log_vault_query / log_service_call` | `core.utilities` | typed convenience wrappers (accept `message=`/`level=` overrides) |
| `run_step` / `ffprobe` / `log_tool_run` | `core.utils.subprocess` | external-tool `tool_run` logging |
| `get_debug_logger()` | `core.utilities` | low-level accessor (rarely needed directly) |
