# Troubleshooting & Debugging

When a download fails, a service misbehaves, or unshackle cannot get a content key, unshackle gives you three tools to find out why:

- **Debug logging**: verbose `DEBUG`-level console output plus a structured, machine-readable log file.
- **[`unshackle env`](#checking-your-environment)**: make sure that unshackle finds the external tools it depends on, and see exactly which directories and config file are in use.
- **Targeted toggles**: `debug_keys` and `debug_requests` for the two areas (DRM keys and raw HTTP traffic) that are deliberately quiet by default.

The sections below cover how to enable each of them, how to read what they give, and which diagnostic fits which failure mode.

---

## Turning on debug logging

There are two independent ways to enable debug logging, and they do slightly different things.

=== "The `-d` / `--debug` flag"

    The root-level flag turns on verbose logging for a single invocation:

    ```console
    unshackle -d dl SERVICE TITLE_ID
    ```

    It must come **before** the subcommand, because it is an option on the root `unshackle` command, not on `dl`.

    `-d` does three things:

    1. Sets the console log level to `DEBUG`, so every module logs its full detail.
    2. Enables the structured JSON debug log file (see [below](#the-structured-json-debug-log)).
    3. Adds source-path annotations to console log lines (which file and line each message came from).

=== "The `debug` config key"

    To make debug logging the default for every run without typing `-d` each time, set it in your `unshackle.yaml`:

    ```yaml
    debug: true
    ```

    or with the [`cfg`](cli-reference.md#cfg) command:

    ```console
    unshackle cfg debug true
    ```

    !!! note "`debug: true` is not identical to `-d`"
        The `debug` config key enables the **JSON debug log file**, but it does **not** raise the console to `DEBUG` level and does not add source-path annotations. Those only happen when you pass the `-d` flag. Use the flag when you want to *watch* the verbose output live. Use the config key when you want only the log file written every time. Passing `-d` is the most complete option, and you can combine both.

!!! warning "Debug logging can be noisy and revealing"
    Debug output is verbose and includes request metadata, service internals, and stack traces. unshackle automatically redacts secrets, URLs, and local paths (see [Redaction](#what-gets-redacted)), but you should still read a log before you share it publicly.

---

## The structured JSON debug log

Beyond the console output, unshackle writes a structured log in [JSON Lines](https://jsonlines.org/) format: one complete JSON object per line. You can filter, examine, and analyse this file with standard tooling (`jq`, `grep`, a text editor). It is safe to attach to a bug report, because unshackle redacts it.

### Where it is written

unshackle makes the debug log during a `dl` run, and during [`import`](cli-reference.md#import), which wraps `dl`. The path is:

```
<logs directory>/unshackle_debug_<service>_<timestamp>.jsonl
```

For example, `unshackle_debug_EXAMPLE_20260703-142530.jsonl`. The `<service>` tag and the timestamp mean each download gets its own file, so runs never overwrite each other.

!!! tip "Find your logs directory"
    The logs directory defaults to a `logs` folder inside the package data directory, but you can move it with the `directories.logs` config key. To see the exact resolved path on your machine, use [`unshackle env info`](#env-info) and read the **Directories** table.

### What a log entry looks like

Every line is a self-contained JSON object. The first line of each log file records the environment:

```json
{"timestamp":"2026-07-03T14:25:30.123456+00:00","session_id":"a1b2c3d4","level":"INFO","operation":"session_start","message":"Debug logging session started","context":{"unshackle_version":"5.3.0","unshackle_code_hash":"1d22a1e","python_version":"3.12.4 ...","platform":"Linux-6.18.5-x86_64","platform_system":"Linux","platform_release":"6.18.5"}}
```

Quote `unshackle_code_hash` in a bug report. It fingerprints the framework source you operate, so it tells us the exact code state even when you did not install from git. A hash that matches no release means that someone edited the source locally.

Subsequent lines record the operations: service calls, DRM licence requests, vault lookups, downloader progress, and errors. Common fields include:

| Field | Meaning |
|---|---|
| `timestamp` | UTC ISO-8601 time of the entry. |
| `session_id` | Short random ID shared by every entry in one run, handy for correlating lines. |
| `level` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |
| `operation` | The action being logged, e.g. `service_call`, `download_init`, `drm_get_widevine_license`, `vault_get_key`, `session_end`. |
| `message` | Human-readable description (redacted). |
| `service` | Service tag the entry belongs to, when applicable. |
| `context` | Arbitrary structured detail for the operation. |
| `request` / `response` | HTTP method/URL and response metadata for network operations. |
| `duration_ms` | How long a timed operation took. |
| `success` | Whether the operation succeeded. |
| `error` | On failures: the exception `type`, `message`, and a full `traceback` array (all redacted). |

### Reading and filtering the log

Because each line is independent JSON, `jq` is the natural tool:

```console
# Show only error-level entries
jq 'select(.level == "ERROR")' unshackle_debug_EXAMPLE_20260703-142530.jsonl

# List every operation and whether it succeeded
jq -c '{operation, success}' unshackle_debug_EXAMPLE_20260703-142530.jsonl

# Pull the traceback out of the first error
jq -r 'select(.error) | .error.traceback[]' unshackle_debug_EXAMPLE_20260703-142530.jsonl

# Show only DRM / licensing operations
jq 'select(.operation | startswith("drm_"))' unshackle_debug_EXAMPLE_20260703-142530.jsonl
```

unshackle logs the binary-tool versions (shaka-packager, mp4decrypt, mkvmerge, FFmpeg, FFprobe) near the start of a `dl` run. Often that is the fastest way to make sure that a muxing or decryption failure is really a wrong-version problem.

---

## Debugging DRM keys with `debug_keys`

By default, unshackle redacts the **content keys** in the JSON debug log. It writes any field that looks like a content key as `[REDACTED]`. This keeps logs shareable. When you specifically debug a key-retrieval or decryption problem and need to see the actual KID/KEY values unshackle fetched, enable `debug_keys`:

```yaml
debug_keys: true
```

```console
unshackle cfg debug_keys true
```

With `debug_keys: true`, the debug log stops masking content key fields. You can then see which content keys came back from a licence server or vault, and compare them against what decryption expected.

!!! warning "Keys are sensitive, turn this back off"
    `debug_keys` writes real content keys into the log file in cleartext. Only enable it while you diagnose a content key problem. Never share a log written with it on, and set it back to `false` when you are done. Note that `debug_keys` only affects the **JSON debug log file**. It does not change what the console prints.

Fields that are *always* redacted regardless of `debug_keys` (anything whose name contains `password`, `token`, `secret`, `auth`, or `cookie`) stay masked, so credentials never leak even in a keys-on log.

---

## Debugging HTTP traffic with `debug_requests`

When you pass `-d`, unshackle raises its own modules to `DEBUG` but deliberately **silences** the noisy low-level HTTP libraries (`urllib3`, `requests`, `rnet`, `httpx`, `httpcore`, `hpack`, `h2`) back down to `WARNING`. Without this, a single download would bury the useful output under thousands of connection-pool and HTTP/2 frame messages.

Sometimes a request fails at the TLS or connection layer, and you need the underlying library's own logs. If you need that low-level traffic, enable `debug_requests`:

```yaml
debug_requests: true
```

```console
unshackle cfg debug_requests true
```

With `debug_requests: true`, unshackle leaves those libraries at `DEBUG` level when you operate it with `-d`, so their request/response chatter appears in the console.

!!! note "`debug_requests` needs `-d` to have any effect"
    The `debug_requests` toggle only decides whether unshackle silences the HTTP libraries when the `-d` flag is active. If you do not operate unshackle with `-d` (verbose console logging), it changes nothing. Combine them: `unshackle -d dl ...` with `debug_requests: true` in config.

---

## What gets redacted

unshackle redacts logged strings so that debug output is safe to share. It applies three passes to every logged message, error, and structured value:

- **Secrets**: unshackle replaces passwords, tokens, API secrets, auth values, and cookies with `[REDACTED]`.
- **URLs**: unshackle collapses every `http(s)` URL to `redacted`. It keeps only the file extension, so a manifest still shows as `redacted.mpd` and a segment as `redacted.m4s`. This hides the CDN, manifest, segment, and licence-server locations, but keeps the *type* of resource.
- **Local paths**: unshackle replaces install-root, virtualenv, and home-directory prefixes with tokens, so your username and machine layout do not appear in logs.

The `redact_paths` config key controls path redaction, and it is **on by default**:

```yaml
redact_paths: true   # default - mask local base directories in logged paths
```

Set `redact_paths: false` if you debug a path problem locally and want to see full, unmasked paths in the console and logs. unshackle always applies secret and URL redaction in the JSON debug log, and this toggle does not change that.

---

## Checking your environment

Many "download failed" problems are really missing external tools or a config file that is not where you think it is. The [`unshackle env`](cli-reference.md#env) command group answers both questions before you dig into logs.

### `env check`

`unshackle env check` verifies every external binary unshackle can use and prints a table of what's installed:

```console
unshackle env check
```

It reports each tool's status (`✓` installed / `✗` missing), whether it is **required** or optional, and what it is for. A summary line tells you the installed-vs-total count and, if anything mandatory is absent, exactly which required tools are missing.

The required tools (unshackle will not get far without them) are:

| Tool | Purpose |
|---|---|
| **FFmpeg** | Media processing |
| **FFprobe** | Media analysis |
| **MKVToolNix** (`mkvmerge`) | MKV muxing |
| **mkvpropedit** | MKV metadata |
| **shaka-packager** | DRM decryption |

Optional tools unlock extra capabilities. For example, `mp4decrypt` (alternative decryptor), `dovi_tool` / `HDR10Plus_tool` (Dolby Vision / HDR10+), `SubtitleEdit` / `CCExtractor` (subtitle conversion and CC extraction), `MPV` / `FFplay` (playback preview), and `git` (service repositories) or `docker` (Gluetun VPN, see [Proxies & VPN](proxies-and-vpn.md)).

!!! tip "First thing to run when something breaks"
    If a decryption step errors, a mux fails, or a Dolby Vision title will not process, use `unshackle env check` first. A red `✗` next to the relevant tool is the answer far more often than a bug in unshackle.

### `env info`

`unshackle env info` shows where unshackle reads configuration from and where every working directory lives:

```console
unshackle env info
```

- If unshackle found a config file, it prints the path it was **loaded from**. If it found none, it shows every location it *would* accept one (see [Config file discovery](#why-is-my-config-not-being-picked-up) below). You can then make the file in the right place.
- It then prints a **Directories** table for every configured path: `downloads`, `temp`, `cache`, `cookies`, `logs`, `exports`, `wvds`, `prds`, the `services` search paths, and more.

This is the authoritative way to answer "where do my logs, downloads and cookies go?" The defaults are relative to the installed package. The table also shows any `directories:` overrides from your config.

### `env clear`

Stale cache or leftover temp files can cause confusing behaviour (an old cached title list, a half-written temp file). `env clear` wipes those directories safely:

```console
unshackle env clear cache            # clear the whole cache directory
unshackle env clear cache EXAMPLE    # clear just one service's cache subdirectory
unshackle env clear temp             # clear the temp working directory
```

Each command reports how many files it removed and how much space it freed, then recreates the empty directory.

!!! tip "Cache-related weirdness"
    If a service keeps returning stale titles or metadata even after the source changed, clear that service's cache with `unshackle env clear cache <SERVICE>`. See [Vaults](vaults.md) for the separate, persistent key-vault store, which `env clear` does **not** touch.

---

## Common failure modes and how to inspect them

!!! example "A required tool is missing"
    **Symptom:** decryption, muxing, or analysis fails early, sometimes with a "binary not found" style error.

    **Inspect:** use `unshackle env check` and look for a red `✗` on a **required** row. Install the missing tool and re-run.

!!! example "Decryption produces garbage or fails"
    **Symptom:** the download completes but the file will not play, or the decrypt step errors.

    **Inspect:** enable `debug_keys` and operate unshackle with `-d`. Then examine the JSON debug log's `drm_*` and `vault_*` operations to see which content keys unshackle fetched, and from where. Also make sure that shaka-packager (or `mp4decrypt`) shows a sane version in the `download_init` binary-versions entry. See [DRM & CDM](drm-and-cdm.md).

!!! example "A network request fails at the connection or TLS layer"
    **Symptom:** timeouts, connection resets, or handshake errors, and unshackle's own logs do not give the full cause.

    **Inspect:** set `debug_requests: true` and operate unshackle with `-d` to show the underlying `urllib3` / `rnet` / `httpx` logs. In the JSON debug log, filter for `service_call` operations to see which request failed. If you use a proxy, also read [Proxies & VPN](proxies-and-vpn.md).

!!! example "The wrong titles/metadata keep coming back"
    **Symptom:** a service returns outdated titles even after the source changed.

    **Inspect:** the title cache is likely serving stale data. Clear it with `unshackle env clear cache <SERVICE>`, or bypass it for one run using the `dl` no-cache / reset-cache options (see [Downloading](downloading.md) and the [CLI Reference](cli-reference.md#dl)).

!!! example "A config value doesn't seem to apply"
    **Symptom:** a setting you edited has no effect.

    **Inspect:** use `unshackle env info` to see **which** config file unshackle loaded, and `unshackle cfg --list` to dump the effective values. It is common to edit a config file in a location unshackle does not read from. See below.

---

## Why is my config not being picked up?

unshackle loads the **first** `unshackle.yaml` it finds, in this order:

1. `unshackle.yaml` inside the package's namespace directory.
2. `unshackle.yaml` in the parent of that directory.
3. `unshackle.yaml` in your OS user-config directory: on Linux `~/.config/unshackle/unshackle.yaml`, on Windows `%LOCALAPPDATA%\unshackle\unshackle.yaml`, on macOS `~/Library/Application Support/unshackle/unshackle.yaml`.

If you have config in more than one of these, an earlier one wins and unshackle silently ignores your edits to a later file. `unshackle env info` prints the path it loaded (or, if none exists, shows all three candidate locations so you can make the file correctly). See [Configuration](cli-reference.md#cfg) for editing values with the `cfg` command.

---

!!! note "Developer note: structured logging in code"
    Service and command code sends entries through the shared `log_event(...)` helper (and the `timed_operation(...)` context manager for timing a block) from `unshackle.core.utilities`. These are no-ops when you disable debug logging, so they are always safe to leave in place. When you add a new feature, call `log_event("my_feature_event", message="...", context={...})`. Do not write the "is the logger enabled?" guard again. unshackle does the redaction and the JSON-serialisation for you, and redacts any field whose name looks like a content key, a secret, or a token. Content keys only appear when the run has `debug_keys` enabled.

### Logging conventions when adding entries

The debug log is tuned for **developers troubleshooting pipeline flow** (maximum signal, minimum noise), not for end users. A few conventions keep it that way. The `log_event` signature does not enforce them, so they are easy to miss and worth stating explicitly.

!!! tip "Log levels are a flow skeleton, not just severity"
    Treat `level` as structure, not decoration. Send exactly **one `INFO` milestone per pipeline stage** so that `jq 'select(.level=="INFO")'` reads back the whole end-to-end flow of a run. Everything internal goes to `DEBUG`. Failures go to `ERROR`. A sparse `INFO` is the whole point. It is the line a developer skims first to see where a run got to before it broke.

    ```console
    # Read the pipeline's high-level flow, one line per stage
    jq 'select(.level == "INFO")' unshackle_debug_EXAMPLE_20260703-142530.jsonl
    ```

!!! warning "No raw dumps"
    Never log a full `Tracks` object, an MPD/manifest body, or a response payload. Log only **counts, ids, sizes, and `safe_display_url(url)`**. This keeps the log low-noise and prevents leaking title or manifest data into the file *before* redaction even runs. Redaction is a backstop, not a licence to dump raw objects.

!!! note "Operation names: `<area>_<event>`, no registry"
    Operation names are plain lowercase strings that follow an `<area>_<event>` convention: `manifest_dash_parse`, `drm_decrypt`, `vault_get_key`, `tool_run`. We write them inline at each call site on purpose. There is **no central registry or enum**. Do not look for one, and do not make one. The absence is a deliberate design choice, not an omission.

!!! note "Message reads alone; data lives in fields"
    Give every entry a single-sentence `message` that stands on its own. Put all structured data (`context`, counts, ids, `duration_ms`) in **fields**, not in the prose. This separation lets the log be both human-scannable and machine-filterable. A message like `"Parsed DASH manifest"` with `context={"video": 4, "audio": 2}` beats `"Parsed manifest with 4 video and 2 audio tracks"`.

!!! tip "Let tool runs log themselves"
    For external-tool calls (FFmpeg, mkvmerge, `dovi_tool`, and friends), route through `run_step` / `ffprobe`, which auto-emit a `tool_run` entry capturing the binary's version, return code, and duration consistently. Only fall back to calling `log_tool_run` manually for a direct `subprocess.run` you genuinely cannot route through `run_step`. Auto-logging is the preferred path precisely because it captures those fields the same way every time.
