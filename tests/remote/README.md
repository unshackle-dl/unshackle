# tests/remote/

Test suite for the unshackle remote-services subsystem:

- `unshackle/core/remote_service.py` — `RemoteClient`, `RemoteService`, helpers
- `unshackle/core/api/` — routes, handlers, session_store, input_bridge,
  download_manager, errors, compression
- `unshackle/commands/serve.py` — CLI surface

Two tiers:

| Tier | Where | Network | Default |
|------|-------|---------|---------|
| **unit** | `tests/remote/unit/` | mocked (`responses`, in-process aiohttp) | runs by default |
| **e2e**  | `tests/remote/e2e/`  | hits an `unshackle serve` instance | skipped unless `--live` |

## Install test deps

```powershell
uv sync --group test
```

## Run unit tests

```powershell
uv run pytest tests/remote/unit -v
```

Fast, hermetic, no external calls.

## Run e2e tests

The suite can either:
1. **Spawn its own serve** (default with `--live`): starts
   `uv run unshackle serve --host 127.0.0.1 --port <free> --no-key --remote-only`
   for the session, waits for `/api/health`, tears it down at the end.
2. **Talk to an external serve** you started in another shell: pass
   `--server-url http://host:port`.

Spawn mode is controlled by `--spawn-serve {auto, always, never}` (default
`auto` — spawn only when `--server-url` is empty).

### Auto-spawn (recommended)

```powershell
uv run pytest tests/remote/e2e -v --live
```

### External serve

```powershell
# in shell A
uv run unshackle serve --host 0.0.0.0 --no-key --remote-only

# in shell B
uv run pytest tests/remote/e2e -v --live --server-url http://localhost:8786
```

### With API key

```powershell
uv run pytest tests/remote/e2e -v --live --secret-key your-key
```

### Limit which services run

```powershell
uv run pytest tests/remote/e2e -v --live --services FOO,BAR
```

## Configure e2e scenarios

Copy `tests/remote/e2e/fixtures/fixtures-example.yaml` to
`tests/remote/e2e/fixtures/fixtures.yaml` (gitignored) and fill in the
services you have access to. Schema (see the example file for full docs):

```yaml
services:
  EXAMPLE:
    title_url: "..."           # required
    series_url: "..."          # optional — overrides movie target when set
    target_season: 1
    target_episode: 1
    search_query: "..."
    expected_quality:
      min_height: 1080
      min_codecs: [AVC]
      min_ranges: [SDR]
      min_track_count: 4
    runs_download_test: true   # opt in to the download smoke test
    runs_license_test: false   # opt in to the license test
    license_drm: widevine      # or "playready"
    license_quality: 1080
```

Tests skip a service if its session can't be created (auth missing,
geofence, etc.) or if the matching `runs_*_test` flag is false.

## Run everything

```powershell
uv run pytest tests/remote -v --live
```

## CLI flags added

| Flag | Default | Purpose |
|------|---------|---------|
| `--live` | off | Opt in to e2e tests |
| `--server-url URL` | `""` (or `$UNSHACKLE_SERVE_URL`) | Target external server; empty triggers auto-spawn |
| `--spawn-serve {auto,always,never}` | `auto` | Spawn serve when no URL given |
| `--secret-key KEY` | `""` (or `$UNSHACKLE_SECRET_KEY`) | `X-Secret-Key` header |
| `--services A,B` | (all) | Restrict e2e to these tags |

## Markers

- `unit` — fast, mocked (default)
- `live` — needs `--live`
- `slow` — hits real services; combine with `--live`

Run only fast unit tests:

```powershell
uv run pytest tests/remote -m "unit and not slow"
```

## Adding a new service to e2e

1. Add a block under `services:` in your local `fixtures.yaml`.
2. No new Python needed — every e2e test is parametrized over the YAML.
