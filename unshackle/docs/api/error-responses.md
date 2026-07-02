# Error Responses

All endpoints return consistent error responses:

```json
{
  "status": "error",
  "error_code": "INVALID_PARAMETERS",
  "message": "Invalid vcodec: XYZ. Must be one of: H264, H265, VP9, AV1, VC1, VP8",
  "timestamp": "2026-02-27T18:00:00.000000+00:00",
  "details": { }
}
```

Common error codes:

- `INVALID_INPUT` -- malformed request body
- `INVALID_PARAMETERS` -- invalid parameter values
- `MISSING_SERVICE` -- service tag not provided
- `INVALID_SERVICE` -- service not found or not in the caller's allowlist
- `SERVICE_ERROR` -- service initialization or runtime error
- `AUTH_FAILED` -- authentication failure
- `NOT_FOUND` / `TRACK_NOT_FOUND` / session not found -- job/session/track/title missing
- `INTERNAL_ERROR` -- unexpected server error

When `--debug-api` is enabled, error responses include additional `debug_info` with tracebacks and stderr output.

Authentication errors from the auth middleware are returned as `{"status": 401, "message": "..."}` (not the standard error envelope).
