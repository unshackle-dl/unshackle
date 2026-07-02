# Quick Start

```bash
# Start the server (no authentication)
unshackle serve --no-key

# Start with authentication (api_secret in unshackle.yaml)
unshackle serve

# Serve only the REST API (no pywidevine/pyplayready CDM)
unshackle serve --api-only

# Serve only the remote-dl session endpoints (CORS/Cloudflare friendly)
unshackle serve --remote-only

# Disable just one CDM
unshackle serve --no-widevine
unshackle serve --no-playready

# Verbose error responses (tracebacks/stderr in JSON)
unshackle serve --debug-api
```

`serve` flags:

| Flag | Description |
| --- | --- |
| `-h, --host` | Bind host (default `127.0.0.1`) |
| `-p, --port` | Bind port (default `8786`) |
| `--caddy` | Also launch Caddy using `Caddyfile` next to the unshackle config |
| `--api-only` | REST API only; skip the bundled pywidevine/pyplayready CDM endpoints |
| `--no-widevine` | Disable Widevine CDM endpoints |
| `--no-playready` | Disable PlayReady CDM endpoints |
| `--no-key` | Disable API key authentication entirely |
| `--debug-api` | Include tracebacks/stderr in error responses |
| `--debug` | Enable DEBUG-level logging for API operations |
| `--remote-only` | Expose only `/api/health`, `/api/services`, `/api/search`, and `/api/session/*` (implies `--api-only`) |
