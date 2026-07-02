# Remote Service Sessions

These endpoints back the `RemoteService` adapter in `unshackle/core/remote_service.py`. They let a thin `dl` client (or any consumer) authenticate against a service on the server, fetch titles/tracks/manifests, and either proxy CDM challenges or have the server resolve KID:KEY directly. The `dl` command's `RemoteService` adapter replaces the old `remote_dl` command. These endpoints are the only `/api/*` routes available in `--remote-only` mode (in addition to `health`, `services`, and `search`).

## POST /api/session/create

Authenticate against a service and open a session. Body fields:

| Field | Type | Description |
| --- | --- | --- |
| `service` | string | Service tag (required) |
| `title_id` | string | Title ID/URL (required) |
| `credentials` | object | Auth credentials forwarded to `Service.authenticate` |
| `cookies` | string | Cookie blob (Netscape or JSON) |
| `proxy` | string | Proxy URI or country code |
| `no_proxy` | bool | Force-disable proxies |
| `profile` | string | Profile name |
| `cache` | object | Optional pre-warmed title cache payload |

If the service requires interactive input during authentication, poll `GET /api/session/{id}/prompt` and submit responses via `POST /api/session/{id}/prompt` until status is `authenticated`.

**Request:**

```json
{
  "service": "EXAMPLE1",
  "title_id": "abc123def456",
  "credentials": {"username": "alice", "password": "hunter2"},
  "cookies": "# Netscape HTTP Cookie File\n...",
  "proxy": "us",
  "no_proxy": false,
  "profile": "default",
  "cache": {}
}
```

**Response (202-style; auth runs asynchronously):**

```json
{
  "session_id": "f1c4a8b2-9c7e-4d2a-bf91-2d3e4f5a6b7c",
  "service": "EXAMPLE1",
  "status": "authenticating"
}
```

## GET /api/session/{session_id}

Returns session metadata. 404 if expired or unknown.

```json
{
  "session_id": "f1c4a8b2-9c7e-4d2a-bf91-2d3e4f5a6b7c",
  "service": "EXAMPLE1",
  "valid": true,
  "expires_in": 3600,
  "track_count": 0,
  "title_count": 0
}
```

## DELETE /api/session/{session_id}

Tears down the session, cancels any pending prompts, and returns any updated per-session cache files (base64-encoded, zlib-compressed) so the client can re-warm next time.

```json
{
  "status": "ok",
  "cache": {
    "tokens": "eJzLSM3JyVcozy/KSVGo5AIAGgQEvQ=="
  }
}
```

## GET /api/session/{session_id}/titles

Returns the resolved titles list.

```json
{
  "session_id": "f1c4a8b2-9c7e-4d2a-bf91-2d3e4f5a6b7c",
  "titles": [
    {
      "type": "episode",
      "name": "Pilot",
      "series_title": "Example Show",
      "season": 1,
      "number": 1,
      "year": 2024,
      "id": "ep-0001",
      "language": "en"
    },
    {
      "type": "movie",
      "name": "Example Movie",
      "year": 2024,
      "id": "mov-0001",
      "language": "en"
    }
  ]
}
```

## POST /api/session/{session_id}/tracks

**Request:**

```json
{"title_id": "ep-0001"}
```

**Response:**

```json
{
  "title": {
    "type": "episode",
    "name": "Pilot",
    "series_title": "Example Show",
    "season": 1,
    "number": 1,
    "year": 2024,
    "id": "ep-0001",
    "language": "en"
  },
  "video": [
    {
      "id": "v-1080p-h264",
      "codec": "H264",
      "codec_display": "H.264",
      "bitrate": 6000,
      "width": 1920,
      "height": 1080,
      "resolution": "1920x1080",
      "fps": "23.976",
      "range": "SDR",
      "range_display": "SDR",
      "language": "en",
      "drm": [
        {
          "type": "widevine",
          "pssh": "AAAAW3Bzc2gAAAAA7e+...",
          "kids": ["abcdef0123456789abcdef0123456789"],
          "license_url": "https://license.example.com/widevine"
        }
      ],
      "descriptor": "DASH",
      "url": "https://cdn.example.com/manifest.mpd"
    }
  ],
  "audio": [
    {
      "id": "a-en-eac3",
      "codec": "EC3",
      "codec_display": "Dolby Digital Plus",
      "bitrate": 640,
      "channels": "5.1",
      "language": "en",
      "atmos": false,
      "descriptive": false,
      "drm": null,
      "descriptor": "DASH",
      "url": "https://cdn.example.com/manifest.mpd"
    }
  ],
  "subtitles": [
    {
      "id": "s-en-vtt",
      "codec": "WebVTT",
      "language": "en",
      "forced": false,
      "sdh": false,
      "cc": false,
      "descriptor": "DASH",
      "url": "https://cdn.example.com/subs/en.vtt"
    }
  ],
  "chapters": [
    {"timestamp": "00:00:00.000", "name": "Chapter 1"}
  ],
  "attachments": [],
  "manifests": [
    {
      "type": "dash",
      "url": "https://cdn.example.com/manifest.mpd",
      "data": "eJzNVk1v2zAM/Ss..."
    }
  ],
  "session_headers": {
    "User-Agent": "Mozilla/5.0 ..."
  },
  "session_cookies": {
    "session": "abc123"
  },
  "server_cdm_type": "widevine"
}
```

## POST /api/session/{session_id}/segments

**Request:**

```json
{"track_ids": ["v-1080p-h264", "a-en-eac3"]}
```

**Response:**

```json
{
  "tracks": {
    "v-1080p-h264": {
      "descriptor": "DASH",
      "url": "https://cdn.example.com/manifest.mpd",
      "drm": [
        {
          "type": "widevine",
          "pssh": "AAAAW3Bzc2gAAAAA7e+...",
          "kids": ["abcdef0123456789abcdef0123456789"],
          "license_url": "https://license.example.com/widevine"
        }
      ],
      "headers": {"User-Agent": "Mozilla/5.0 ..."},
      "cookies": {"session": "abc123"},
      "data": {}
    },
    "a-en-eac3": {
      "descriptor": "DASH",
      "url": "https://cdn.example.com/manifest.mpd",
      "drm": null,
      "headers": {"User-Agent": "Mozilla/5.0 ..."},
      "cookies": {"session": "abc123"},
      "data": {}
    }
  }
}
```

## POST /api/session/{session_id}/license

Two modes, selected by the `mode` field.

**`mode: "proxy"` (default)** -- forward a client-built CDM challenge to the service's license endpoint.

Request:

```json
{
  "mode": "proxy",
  "track_id": "v-1080p-h264",
  "challenge": "CAESxQEK...",
  "drm_type": "widevine",
  "pssh": "AAAAW3Bzc2gAAAAA7e+..."
}
```

Response:

```json
{"license": "CAIS3wIK..."}
```

**`mode: "server_cdm"`** -- the server uses its own CDM to license the track and extract keys. Single-track form takes `track_id`; batch form takes `track_ids`. Requires the calling user key to have a matching device (`devices` for Widevine, `playready_devices` for PlayReady) in `unshackle.yaml`.

Request (batch):

```json
{
  "mode": "server_cdm",
  "track_ids": ["v-1080p-h264", "a-en-eac3"],
  "drm_type": "widevine"
}
```

Response:

```json
{
  "keys": {
    "v-1080p-h264": {
      "abcdef0123456789abcdef0123456789": "00112233445566778899aabbccddeeff"
    },
    "a-en-eac3": {
      "abcdef0123456789abcdef0123456789": "00112233445566778899aabbccddeeff"
    }
  },
  "drm_type": "widevine"
}
```

## GET /api/session/{session_id}/prompt

Polled by the client during interactive authentication (OTP, PIN, device codes). Backed by the `InputBridge` in `unshackle/core/api/input_bridge.py`; `Service.request_input()` blocks server-side until the client posts a response.

Pending input:

```json
{"status": "pending_input", "prompt": "Enter OTP code: "}
```

Other states:

```json
{"status": "authenticating"}
```

```json
{"status": "authenticated"}
```

```json
{"status": "failed", "error": "Invalid credentials"}
```

## POST /api/session/{session_id}/prompt

Unblocks the server-side `request_input()` call.

Request:

```json
{"response": "123456"}
```

Response:

```json
{"status": "accepted"}
```
