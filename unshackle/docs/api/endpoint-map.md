# Endpoint Map

Standard endpoints (suppressed in `--remote-only` mode are marked R):

| Method | Path | R |
| --- | --- | :-: |
| GET    | `/api/health` | ok |
| GET    | `/api/services` | ok |
| POST   | `/api/search` | ok |
| POST   | `/api/list-titles` | hidden |
| POST   | `/api/list-tracks` | hidden |
| POST   | `/api/download` | hidden |
| GET    | `/api/download/jobs` | hidden |
| GET    | `/api/download/jobs/{job_id}` | hidden |
| DELETE | `/api/download/jobs/{job_id}` | hidden |
| POST   | `/api/session/create` | ok |
| GET    | `/api/session/{session_id}` | ok |
| DELETE | `/api/session/{session_id}` | ok |
| GET    | `/api/session/{session_id}/titles` | ok |
| POST   | `/api/session/{session_id}/tracks` | ok |
| POST   | `/api/session/{session_id}/segments` | ok |
| POST   | `/api/session/{session_id}/license` | ok |
| GET    | `/api/session/{session_id}/prompt` | ok |
| POST   | `/api/session/{session_id}/prompt` | ok |

CDM endpoints (`/{wvd}/...`, `/playready/{prd}/...`) are exposed unless `--api-only` / `--remote-only` / `--no-widevine` / `--no-playready` is set, and use pywidevine / pyplayready's own auth scheme.
