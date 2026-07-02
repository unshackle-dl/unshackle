# unshackle Documentation

This is the **canonical configuration reference** for unshackle. `unshackle/unshackle-example.yaml` is a minimal starter config only - it intentionally omits most options. Come here for the full picture.

## Contents

| Document | Description |
|---|---|
| [Creating a Service](CREATING_A_SERVICE.md) | Section-by-section guide to writing a new service adapter, based on the `EXAMPLE` service |
| [Advanced & System Configuration](ADVANCED_CONFIG.md) | `serve`, debugging, and system-level options not covered elsewhere |
| [REST API Documentation](API.md) | HTTP API exposed by `unshackle serve`: endpoints, auth, remote downloads, CDM hosting |
| [Structured Debug Logging](DEBUG_LOGGING.md) | `-d`/`--debug` JSONL output: schema, redaction rules, `log_event`/`timed_operation` conventions |
| [Download & Processing Configuration](DOWNLOAD_CONFIG.md) | Downloader settings, concurrency, retries, and post-processing behaviour |
| [DRM & CDM Configuration](DRM_CONFIG.md) | Widevine, PlayReady, and ClearKey CDM setup; local and remote CDMs; key vaults |
| [Gluetun VPN Proxy](GLUETUN.md) | Docker-managed VPN proxies via Gluetun; setup, WireGuard ready-detection |
| [Network & Proxy Configuration](NETWORK_CONFIG.md) | Proxy providers, geofence bypass, per-service proxy overrides |
| [Output & Naming Configuration](OUTPUT_CONFIG.md) | Output directory layout, filename templates, folder naming |
| [Service Integration & Authentication](SERVICE_CONFIG.md) | Per-service config blocks, cookies, credentials, and metadata sources |
| [Subtitle Processing Configuration](SUBTITLE_CONFIG.md) | `subtitle:` key options: format conversion, styling, forced-sub rules |
