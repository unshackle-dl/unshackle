# DRM & CDM { #drm-cdm }

These are the config keys. For concepts, device provisioning, and the `wvd`/`prd` commands see
the [DRM & CDM guide](../../guide/drm-and-cdm.md). Device files themselves live in
[`directories.wvds`](directories.md) (`.wvd`) and `directories.prds` (`.prd`).

## `cdm`

- **Type:** `dict` &nbsp;·&nbsp; **Default:** `{}`

Maps a service tag to the CDM (Widevine/PlayReady device) to use, with a `default` fallback.
Resolution is case-insensitive: an override (`dl --cdm <name>`, or the `cdm` field of an API
job) wins, then the per-service entry, then `default`. An override pins one device for the whole
run and skips any quality or Widevine/PlayReady sub-entries.

```yaml
cdm:
  default: chromecdm_l3
  EXAMPLE2: android_l1
```

A value may also be a **nested dict** for advanced selection by quality, DRM system, or
profile:

```yaml
cdm:
  EXAMPLE1:
    ">=1080": android_l1        # by track height
    "<1080": chromecdm_l3
    widevine: android_l1        # by DRM system
    playready: sl3000
    default: chromecdm_l3
```

Quality keys can hold `1080`, `>=1080`, `>720`, `<=576`, `<480` style comparisons. DRM keys
are `widevine` / `playready`. unshackle matches the quality keys first, but only when a video
height is known. If the value is still a dict after that, unshackle uses
`widevine`/`playready` when present, or else the credential profile name, then `default`.

unshackle finds the resolved name in [`remote_cdm`](#remote_cdm) by `name` first. If no entry
matches, unshackle loads it as a local device file, in this order: `<name>.prd` in
`directories.prds`, `<name>.prd` in `directories.wvds`, then `<name>.wvd` in
`directories.wvds`.

## `remote_cdm`

- **Type:** `list[dict]` &nbsp;·&nbsp; **Default:** `[]`

A list of remote CDM definitions. unshackle matches each entry by its `name` (referenced from
[`cdm`](#cdm)), and its `type` selects the backend:

| `type` | Backend | Key fields |
|--------|---------|-----------|
| `decrypt_labs` | Decrypt Labs KeyXtractor | `host`, `device_name`, `secret` ([fields](#remote-cdm-decrypt-labs)) |
| `custom_api` | Fully YAML-configurable remote API | `host` (required), `device`, `auth`, `endpoints`, `request_mapping`, `response_mapping`, `caching`, `timeout` ([fields](#remote-cdm-custom-api)) |
| *(none)*, `Device Type: PLAYREADY` | pyplayready `RemoteCdm` | `host`, `secret`, `device_name`, `security_level` (default 3000) |
| *(none)*, otherwise | pywidevine `RemoteCdm` | `host`, `secret`, `device_name`, `device_type`, `system_id`, `security_level` (default 3000) |

```yaml
remote_cdm:
  - name: keyxtractor
    type: decrypt_labs
    device_name: L1
  - name: my_wv_server
    host: https://cdm.example.com
    secret: s3cr3t
    device_name: android_l1
    device_type: ANDROID
    system_id: 26830
    security_level: 1
```

For the two pywidevine/pyplayready backends, field names are read case-insensitively in both
styles: `Device Type`/`device_type`, `System ID`/`system_id`, `Security Level`/`security_level`,
`Host`/`host`, `Secret`/`secret`, `Device Name`/`device_name`.

!!! warning "PlayReady `host` needs a `/playready` suffix"
    pyplayready's `RemoteCdm` treats `host` as a base URL and appends its own endpoint paths,
    so a PlayReady entry's `host` must include the trailing `/playready` segment (for example
    `https://cdm.example.com/playready`). Without it the server returns `404` rather than a
    configuration error.

### `decrypt_labs` fields { #remote-cdm-decrypt-labs }

| Field | Default | Notes |
|-------|---------|-------|
| `host` | `https://keyxtractor.decryptlabs.com` | |
| `device_name` | `ChromeCDM` | `ChromeCDM`, `L1`, `L2` (Widevine) or `SL2`, `SL3` (PlayReady) |
| `secret` | from [`decrypt_labs_api_key`](misc.md#external-api-keys) | Sent as the `decrypt-labs-api-key` header. An error is raised if neither is set. |
| `system_id` | `26830` (Widevine), `0` (PlayReady) | |
| `security_level` | Widevine `3`; PlayReady `2000` for `SL2`, else `3000` | |

unshackle uses PlayReady mode when `device_type` is `PLAYREADY` or `device_name` starts with `SL`.

### `custom_api` fields { #remote-cdm-custom-api }

| Field | Type | Notes |
|-------|------|-------|
| `host` | str | Required. Base URL of the API. |
| `device` | dict | `name`, `type` (`CHROME`, `ANDROID`, `PLAYREADY`), `system_id`, `security_level` |
| `auth` | dict | `type` (default `header`): `header` uses `header_name` (default `Authorization`) and `key`; `bearer` uses `bearer_token` or `key`; `basic` uses `username` and `password`. `custom_headers` is merged in for any type. |
| `endpoints` | dict | `get_request` and `decrypt_response`, each `{path, method, timeout}` |
| `request_mapping` | dict | Per endpoint: `param_names`, `static_params`, `conditional_params`, `transforms`, `nested_params`, `exclude_params` |
| `response_mapping` | dict | Per endpoint: `fields`, `transforms`, `response_types`, `success_conditions`, `error_fields`, `key_fields` |
| `caching` | dict | `enabled`, `use_vaults`, `check_cached_first` |
| `legacy` | dict | Legacy mode options. |
| `timeout` | int | Default `30`. Request timeout in seconds. |

Transforms cover base64/hex/JSON encoding and `kid:key` parsing. unshackle uses PlayReady mode
when `device.type` is `PLAYREADY` or the device name is `SL2`/`SL3`.

```yaml
remote_cdm:
  - name: my_custom
    type: custom_api
    host: https://cdm.example.com
    device:
      name: ChromeCDM
      type: CHROME
      system_id: 26830
      security_level: 3
    auth:
      type: bearer
      key: your-token
    endpoints:
      get_request:
        path: /get-challenge
        method: POST
      decrypt_response:
        path: /get-keys
        method: POST
    timeout: 30
```

## `decryption`

- **Type:** `str` or `dict` &nbsp;·&nbsp; **Default:** `"shaka"`

Selects the tool that physically decrypts CENC-encrypted tracks. unshackle compares the value
case-insensitively. `"mp4decrypt"` selects Bento4's mp4decrypt, and **anything else**
(including `"shaka"`) selects shaka-packager. When the value is a dict, unshackle compares the
service tag case-insensitively and uses the `default` entry as the fallback.

unshackle decrypts HLS AES-128 ClearKey in-process and ignores this setting.

=== "Global"

    ```yaml
    decryption: shaka
    ```

=== "Per-service"

    ```yaml
    decryption:
      default: shaka
      EXAMPLE1: mp4decrypt
    ```

## `decrypt_segments`

- **Type:** `bool` &nbsp;·&nbsp; **Default:** `false`

Decrypts each fragmented MP4 segment as it arrives, instead of decrypting the whole track
after the download ends. The decryption runs during the download, so the wait after the last
segment goes away. The output file is the same either way.

This works with `mp4decrypt` only. Set [`decryption`](#decryption) to `mp4decrypt` for the
service (or as the default); with `shaka` the option has no effect, because shaka-packager
cannot decrypt a segment without its init segment. It applies to DASH tracks and to HLS
tracks that use one init segment, one `EXT-X-KEY` and no `EXT-X-DISCONTINUITY`. Every other
track keeps the whole-file decryption.

!!! note "No resume"

    A track that decrypts segment by segment always downloads from the start.
    [`continue_downloads`](download.md#continue_downloads) cannot reuse its segments, because a reused segment
    is already decrypted.

```yaml
decrypt_segments: true
```
