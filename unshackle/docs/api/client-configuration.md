# Client Configuration: `remote_services`

`remote_services` is a top-level block in `unshackle.yaml` on the **client** machine. It tells `dl` how to reach a remote `unshackle serve` instance instead of running service code locally. The client handles track selection, download, decrypt, and mux; the server handles auth, manifest parsing, and (optionally) DRM licensing.

## CLI flags

| Flag | Description |
| --- | --- |
| `--remote` | Use a remote server instead of local service code. Reads `remote_services` from config. |
| `--server <name>` | Select a named server when multiple are configured. If only one entry exists it is picked automatically. |

```bash
unshackle dl --remote EXAMPLE1 "https://example.com/show/abc123"
unshackle dl --remote --server us-server EXAMPLE1 "https://example.com/show/abc123"
```

## Config structure

```yaml
remote_services:
  my-server:
    url: "http://192.168.1.100:8786"   # base URL of the remote serve instance
    api_key: "your-secret-key-here"    # sent as X-Secret-Key; omit or set "" for --no-key servers

    # server_cdm: the server runs its own CDM and returns KID:KEY pairs (mode: server_cdm).
    # false (default): client runs its own CDM; license challenges are proxied through the server.
    server_cdm: false

    # Per-service overrides applied on the client side after the remote session is set up.
    # Supported keys: decryption, downloader, cdm (and any free-form service config keys).
    services:
      EXAMPLE1:
        decryption: mp4decrypt   # override decryption tool for this service (shaka or mp4decrypt)
```

**`server_cdm`** — when `false` (default) the client builds CDM challenges locally and calls `POST /api/session/{id}/license` with `mode: "proxy"`; the server forwards the challenge to the service's license endpoint and returns raw license bytes. When `true` the client skips local CDM setup and calls with `mode: "server_cdm"`; the server performs the full CDM flow and returns `{ "keys": { "<KID>": "<KEY>" } }`. The server-side user key must have a matching device configured (`devices` / `playready_devices`) for `server_cdm` to work.

## Multiple servers

When more than one server is configured, `--server <name>` is required:

```yaml
remote_services:
  us-server:
    url: "https://us.example.com:8786"
    api_key: "us-key"
    server_cdm: true
    services:
      EXAMPLE1:
        decryption: mp4decrypt

  eu-server:
    url: "https://eu.example.com:8786"
    api_key: "eu-key"
    server_cdm: false
```

```bash
unshackle dl --remote --server us-server EXAMPLE1 "https://example.com/show/abc123"
```

## Cache forwarding

On session open the client sends any local `*.json` cache files for the service (tokens, etc.) to the server so it can skip interactive re-auth. On session close the server returns updated cache files; the client writes them back locally so the next remote session can forward them again.
