# REST API Documentation

The unshackle REST API allows you to control downloads, search services, drive remote downloads from a thin client, and (optionally) co-host the pywidevine/pyplayready CDM. Start the server with `unshackle serve` and access the interactive Swagger UI at `http://localhost:8786/api/docs/`.

The server is built on **aiohttp** (not FastAPI). Implementation lives in `unshackle/commands/serve.py` and `unshackle/core/api/` (`routes.py`, `handlers.py`, `session_store.py`, `input_bridge.py`, `download_manager.py`, `download_worker.py`).

- [Quick Start](api/quick-start.md)
- [Authentication](api/authentication.md)
- [Endpoint Map](api/endpoint-map.md)
- [Endpoints](api/endpoints.md)
- [Remote Service Sessions](api/remote-service-sessions.md)
- [Error Responses](api/error-responses.md)
- [Download Job Lifecycle](api/download-job-lifecycle.md)
- [Client Configuration: `remote_services`](api/client-configuration.md)
