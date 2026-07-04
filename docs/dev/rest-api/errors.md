# Error Responses

Every endpoint of the unshackle REST API reports failures in a single, predictable
shape. Whether a request was rejected because a field was missing, a service refused
to authenticate, or the download worker crashed, the server answers with a JSON body
that carries a machine-readable **error code**, a human-readable **message**, and a
matching **HTTP status**. This page documents that format and catalogues every error
code the server can emit.

If you are building a client against the API, read this page alongside the endpoint
reference. Each endpoint lists the specific codes it raises, and the meanings of
those codes are defined here.

!!! note "Where this comes from"
    The error format is defined in `unshackle/core/api/errors.py`, and the wrapper
    that turns raised errors into responses lives in `unshackle/core/api/routes.py`.
    Nothing on this page is invented; it reflects the actual server behaviour.

## The standard error shape

A structured error is always a JSON object with `status` set to the literal string
`"error"`. The full set of fields:

```json title="Error response body"
{
  "status": "error",
  "error_code": "INVALID_PARAMETERS",
  "message": "vcodec must be one of: H264, H265, ...",
  "timestamp": "2026-07-03T12:34:56.789012+00:00",
  "details": { "field": "vcodec" },
  "retryable": true,
  "debug_info": {
    "exception_type": "APIError",
    "traceback": "..."
  }
}
```

| Field | Type | Always present? | Meaning |
|---|---|---|---|
| `status` | string | Yes | Always the literal `"error"`. Lets a client distinguish an error body from a success body at a glance. |
| `error_code` | string | Yes | A stable, machine-readable code from the [catalogue below](#error-code-catalogue). Match on this, not on `message`. |
| `message` | string | Yes | A human-readable description of what went wrong. Wording may change between versions, so do not parse it. |
| `timestamp` | string | Yes | ISO 8601 timestamp (UTC) of when the error response was built. |
| `details` | object | Only when non-empty | Structured, error-specific context (for example the invalid field, a list of available titles, or the set of currently active jobs). |
| `retryable` | boolean | Only when `true` | Present and set to `true` when retrying the same request may succeed later (network blips, rate limiting, transient outages). Absent otherwise. |
| `debug_info` | object | Only with `--debug-api` | Technical diagnostics; see [Debug information](#debug-information). |

!!! tip "Match on `error_code`, branch on HTTP status"
    Treat `error_code` as the primary signal your code reacts to, and the HTTP status
    as a coarse fallback for anything you do not recognise. The two are consistent:
    every code maps to a default status (see the catalogue), and the pairing is
    stable across releases.

### The `details` object

When present, `details` carries extra context specific to the error. It is only
included when it is non-empty, so a client must treat a missing `details` as "no
extra information". Some concrete examples you may encounter:

- A validation failure may include the offending field or the list of valid values.
- A "title not found" error on a remote-dl session includes `available_titles`.
- A maintenance conflict includes `active_jobs`, the jobs blocking the operation.
- Categorised exceptions add a `reason` tag, such as `"network_connectivity"` or
  `"geofence_restriction"`.

### The `retryable` hint

The `retryable` field appears **only when it is `true`**. It is set for conditions
the server believes are transient: network errors, rate limiting, and temporary
service unavailability. When you see it, backing off and retrying is reasonable; when
it is absent, assume the request will keep failing until you change something.

!!! example "A retryable network error"
    ```json
    {
      "status": "error",
      "error_code": "NETWORK_ERROR",
      "message": "Network error occurred: Connection timed out",
      "timestamp": "2026-07-03T12:00:00+00:00",
      "details": { "reason": "network_connectivity" },
      "retryable": true
    }
    ```

## Error code catalogue

Every code below is a member of the `APIErrorCode` enumeration. The **Status** column
is the default HTTP status the server returns for that code. A handler can override
the status in special cases (for example, a network error is always forced to `503`),
but the defaults hold for the overwhelming majority of responses.

### Client errors (4xx)

| Code | Status | Meaning |
|---|---|---|
| `INVALID_INPUT` | 400 | A required field is missing, or the request body could not be parsed. Also used as the generic "your input was malformed" code. |
| `INVALID_SERVICE` | 400 | The named service is unknown, or is not permitted for your API key. |
| `INVALID_PROXY` | 400 | The supplied proxy specification could not be resolved or is malformed. |
| `INVALID_PARAMETERS` | 400 | One or more download or query parameters failed validation (bad codec, bitrate, sort field, and so on). |
| `AUTH_FAILED` | 401 | Authentication with the streaming service failed (bad credentials or cookies). |
| `FORBIDDEN` | 403 | The action is not allowed. Raised by server-side gates (per-key restrictions on CDM or credential overrides) and by session IP binding. |
| `GEOFENCE` | 403 | The content is not available in the applicable region. |
| `NOT_FOUND` | 404 | A requested resource (title, history entry, and so on) does not exist. |
| `NO_CONTENT` | 404 | The request was valid but produced nothing: no matching titles, tracks, episodes, or keys. |
| `JOB_NOT_FOUND` | 404 | The referenced download job does not exist. |
| `SESSION_NOT_FOUND` | 404 | The remote-dl session does not exist or has expired. |
| `TRACK_NOT_FOUND` | 404 | A referenced track ID is not present in the session. |
| `CONFLICT` | 409 | The target resource is in a state that disallows the action (retrying a running job, prioritising a non-queued job, clearing cache during an active download). |
| `RATE_LIMITED` | 429 | The streaming service is rate-limiting requests. Retryable. |

### Server errors (5xx)

| Code | Status | Meaning |
|---|---|---|
| `INTERNAL_ERROR` | 500 | An unexpected server-side error. This is the fallback for any exception the server could not categorise. |
| `DOWNLOAD_ERROR` | 500 | The download process itself failed. |
| `WORKER_ERROR` | 500 | The download worker subprocess errored out. |
| `SERVICE_ERROR` | 502 | The streaming service's API returned an error, or the service does not support the requested operation. |
| `DRM_ERROR` | 502 | DRM or license acquisition failed. Retryable. |
| `NETWORK_ERROR` | 503 | A network connectivity problem (connection, timeout, DNS, TLS). Retryable. |
| `SERVICE_UNAVAILABLE` | 503 | The service is temporarily unavailable or under maintenance. Retryable. |

!!! note "Fallback status"
    If a code is ever emitted without a mapped status, the server falls back to
    `500`. In practice every code above has an explicit mapping.

## How errors are produced

Understanding where an error comes from helps interpret it.

**Explicitly raised errors.** Most endpoints raise a structured `APIError` directly.
For example, a missing `service` field yields `INVALID_INPUT`, and a disallowed
service yields `INVALID_SERVICE`. These carry a precise code, message, and often a
populated `details` object.

**Categorised exceptions.** When an endpoint calls into a streaming service and an
uncategorised exception bubbles up, the server runs it through a classifier that
inspects the exception type and message and maps it to the most appropriate code.
The classification is keyword-based, checked in order:

| If the error mentions… | It becomes | Retryable |
|---|---|---|
| auth, login, credential, unauthorized, forbidden, token | `AUTH_FAILED` (401) | no |
| connection, timeout, network, unreachable, socket, dns, resolve (or `ConnectionError`, `TimeoutError`, `URLError`, `SSLError`) | `NETWORK_ERROR` (503) | yes |
| geofence, region, "not available in", territory | `GEOFENCE` (403) | no |
| "not found", 404, "does not exist", "invalid id" | `NOT_FOUND` (404) | no |
| rate limit, too many requests, 429, throttle | `RATE_LIMITED` (429) | yes |
| drm, license, widevine, playready, decrypt | `DRM_ERROR` (502) | no |
| "service unavailable", 503, maintenance, "temporarily unavailable" | `SERVICE_UNAVAILABLE` (503) | yes |
| invalid, malformed, validation (or `ValueError`, `ValidationError`) | `INVALID_INPUT` (400) | no |
| anything else | `INTERNAL_ERROR` (500) | no |

Categorised errors include a `reason` tag inside `details` (such as
`"network_connectivity"` or `"drm_failure"`) so a client can react without parsing
the message text.

**Invalid JSON bodies.** If a `POST` endpoint receives a body that is not valid JSON,
the server returns `INVALID_INPUT` (400) with the parse error in
`details.error`:

```json title="Malformed request body"
{
  "status": "error",
  "error_code": "INVALID_INPUT",
  "message": "Invalid JSON request body",
  "timestamp": "2026-07-03T12:00:00+00:00",
  "details": { "error": "Expecting value: line 1 column 1 (char 0)" }
}
```

## The authentication error exception

There is one place where the response does **not** follow the standard shape: the API
key authentication middleware. When you send a request to a protected endpoint
without a valid `X-Secret-Key` header, the middleware short-circuits with a legacy
body that uses an integer `status` and no `error_code` or `timestamp`.

=== "Missing key"

    ```json
    { "status": 401, "message": "Secret Key is Empty." }
    ```

=== "Invalid key"

    ```json
    { "status": 401, "message": "Secret Key is Invalid." }
    ```

!!! warning "Handle both shapes"
    A robust client must expect two error formats: the standard structured error
    (string `status` of `"error"`, with `error_code`), and this authentication error
    (integer `status`, no `error_code`). The `GET /api/health` endpoint is exempt
    from authentication, so it never returns the key-error shape.

## Responses with no body

Not every failure or completion has a JSON body. Some endpoints answer with
**`204 No Content`** and an empty body, notably deleting a terminal download job and
deleting a history entry.

!!! warning "Do not parse `204` bodies"
    A `204` response has no body. Clients must check the status code before attempting
    to decode JSON, or the decode will fail on an empty string.

## Debug information

By default, error responses never include tracebacks or internal diagnostics. If the
server was started with the `--debug-api` flag, every structured error gains a
`debug_info` object:

```json title="Error with --debug-api enabled"
{
  "status": "error",
  "error_code": "INTERNAL_ERROR",
  "message": "An unexpected error occurred: ...",
  "timestamp": "2026-07-03T12:00:00+00:00",
  "debug_info": {
    "exception_type": "KeyError",
    "traceback": "Traceback (most recent call last): ..."
  }
}
```

The `debug_info` object always carries `exception_type` and, for exception-derived
errors, a formatted `traceback`. Some endpoints add extra diagnostics such as
captured worker stderr.

!!! warning "Developer use only"
    `--debug-api` exposes internal tracebacks in responses. Enable it only for local
    debugging, never on a server reachable by untrusted clients.

## Client handling recommendations

For anyone writing a consumer of the API, a durable strategy is:

1. **Read the HTTP status first.** It tells you the broad class of outcome and lets
   you handle the `204`/no-body and authentication-error cases before touching the
   body.
2. **Branch on `error_code`.** For structured errors, this is the stable identifier
   to switch on. Do not match on `message`.
3. **Respect `retryable`.** When it is `true`, back off and retry; the reference
   client retries `429`, `500`, `502`, `503`, and `504` with exponential backoff. When
   absent, fix the request rather than repeating it.
4. **Fall back gracefully.** Unrecognised codes should be surfaced using the
   `message` plus the HTTP status.

!!! note "Reference consumer"
    unshackle's own remote-download client treats any status of `400` or above as
    fatal: it logs `Server error [<error_code>]: <message>` and exits. It also wraps
    requests in a retry adapter (five attempts, backing off on `429`, `500`, `502`,
    `503`, and `504`). This is a reasonable template for your own client's error
    handling.
