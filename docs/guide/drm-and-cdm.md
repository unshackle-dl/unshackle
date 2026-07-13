# DRM & CDM Setup

Most commercial streaming content is protected by a Digital Rights Management (DRM)
system. To download and play it back, unshackle needs to obtain the **content keys**
for the encrypted tracks and then decrypt them. This page explains the DRM systems
unshackle supports, how to provide a device so it can talk to a license server, the
difference between local and remote CDMs, and how to choose a decryption backend.

!!! note "Who this page is for"
    This is a practical configuration guide for end users of the unshackle CLI.
    A few sections are marked **Developer**. They describe internal behaviour that is
    useful when writing a service, but you do not need them to download protected
    content.

## Key concepts

Before configuring anything, it helps to know the vocabulary. unshackle draws a firm
line between two things:

- A **CDM** (Content Decryption Module) is the trusted "black box" that turns a
  *license challenge* into *content keys*. It is backed by a **device**, a file
  containing the cryptographic identity of a real client (a phone, a browser, etc.).
- A **DRM system** is the wrapper around a CDM that parses the protection metadata,
  drives the challenge → license → keys handshake, and finally invokes an external
  tool to physically decrypt the downloaded file.

A handful of other terms show up throughout:

| Term | Meaning |
|------|---------|
| **PSSH** | Protection System Specific Header. The per-system init data extracted from the file's init segment or the manifest. |
| **KID** | Key ID: a 16-byte UUID identifying *which* key a track needs. |
| **CEK** | Content Encryption Key: the actual AES key, mapped from its KID. |
| **Challenge** | The license request the CDM produces, sent to the service's license server. |
| **License** | The server's response, which the CDM parses to recover the content keys. |

For a Widevine or PlayReady title, the flow is: unshackle reads the PSSH and KIDs
from the track, asks the CDM for a license challenge, sends that challenge to the
service's license server, hands the response back to the CDM to extract the keys, and
then decrypts the file with those keys.

## Supported DRM systems

unshackle implements five DRM systems. Only the first two need a device file and a
license-server handshake; the rest are special cases.

| System | Device file | License flow | Decryption |
|--------|-------------|--------------|------------|
| **Widevine** (Google) | `.wvd` | CDM challenge → license server → CDM parses → keys | shaka-packager / mp4decrypt |
| **PlayReady** (Microsoft) | `.prd` | CDM challenge (WRM header) → license server → CDM parses → keys | shaka-packager / mp4decrypt |
| **ClearKey (CENC)**, W3C EME `org.w3.clearkey` | none | JSON `{"kids":[...],"type":"temporary"}` → license server returns a JWK Set. No CDM. | shaka-packager / mp4decrypt |
| **ClearKey (HLS)**, AES-128 `EXT-X-KEY` | none | Key fetched directly from a URI or `data:` URL. No CDM, no challenge. | in-process AES-CBC |
| **MonaLisa** (proprietary, WASM) | `.mld` | Ticket from the service API; keys extracted locally via WASM. No license server. | ML-Worker binary + AES-ECB, per segment |

!!! warning "Two different ClearKeys"
    "ClearKey (HLS)" and "ClearKey (CENC)" are entirely different systems that happen
    to share a name. The HLS variant is the AES-128 key referenced by an `EXT-X-KEY`
    tag and is decrypted in-process. The CENC variant is the W3C EME
    `org.w3.clearkey` scheme, delivers keys as a JWK Set, and is decrypted through
    shaka-packager or mp4decrypt just like Widevine. Neither involves a device file.

### Widevine vs PlayReady

These are the two "real" DRMs, and in unshackle they behave almost identically: the
same PSSH/KID handling, the same decryption backends, and the same key-caching flow.
The differences that matter to you are:

- **Device file**: Widevine uses a `.wvd`; PlayReady uses a `.prd`.
- **Availability**: some services offer only one system, some offer both. When a
  manifest carries both, unshackle picks the appropriate CDM for each track (see
  [Widevine and PlayReady on the same title](#widevine-and-playready-on-the-same-title)).

To download a Widevine-protected title you need a `.wvd` device; for PlayReady you
need a `.prd` device. You can hold both, and unshackle automatically selects the
appropriate one for each track.

!!! info "Obtaining device files"
    A `.wvd` or `.prd` encodes the private keys of a real client device. unshackle
    does **not** ship with one and cannot generate the underlying secret material for
    you. You must supply your own. The `wvd` and `prd` commands below manage and
    provision device files once you have the raw key material.

## Providing a device with the `wvd` and `prd` commands

Device files live in dedicated directories, by default:

| Kind | Default directory |
|------|-------------------|
| Widevine `.wvd` | `data/WVDs` |
| PlayReady `.prd` | `data/PRDs` |

You can move these with the `directories` section of your `unshackle.yaml`:

```yaml title="unshackle.yaml"
directories:
  wvds: /home/george/unshackle-data/WVDs
  prds: /home/george/unshackle-data/PRDs
```

### Widevine devices: the `wvd` command

The `wvd` command group manages `.wvd` files.

=== "Add an existing device"

    ```console
    $ unshackle wvd add ./nexus_6p.wvd
    ```

    The file is validated and moved into the WVDs directory. You can add several at
    once by passing multiple paths.

=== "Inspect a device"

    ```console
    $ unshackle wvd parse nexus_6p
    ```

    Prints the System ID, Security Level, Type, Flags, and whether a Private Key,
    Client ID, and VMP are present. A bare name (no extension) is resolved inside the
    WVDs directory; a path with an extension is used as-is.

=== "Create from raw material"

    ```console
    $ unshackle wvd new "Nexus 6P" ./private_key.pem ./client_id.bin ./vmp.bin -t ANDROID -l 3
    ```

    Builds a `.wvd` from a PEM private key, a Client ID blob, and an optional
    FileHashes (VMP) blob. `-t/--type` is the device type (default `Android`) and
    `-l/--level` is the security level 1 to 3 (default `1`). The VMP argument is
    optional.

=== "Delete / dump"

    ```console
    $ unshackle wvd delete nexus_6p
    $ unshackle wvd dump nexus_6p ./out
    ```

    `delete` removes a device (with a confirmation prompt). `dump` extracts a
    device's contents (metadata, private key, Client ID, VMP) into a folder.

!!! warning "v1 WVD files must be migrated"
    If loading a device fails with a message about a "v1 WVD file", it was made with
    an older format. Migrate it with pywidevine before use:

    ```console
    $ pywidevine migrate --help
    ```

### PlayReady devices: the `prd` command

The `prd` command group creates and maintains `.prd` files.

=== "Create a new device"

    ```console
    $ unshackle prd new ./zgpriv.dat ./bgroupcert.dat
    ```

    Provision a new device from a group key and group certificate. You can instead
    point at a single folder that contains `zgpriv.dat` and `bgroupcert.dat`:

    ```console
    $ unshackle prd new ./device_folder/
    ```

    A fresh leaf certificate is generated and the device is written to the PRDs
    directory (or to `-o/--output`). Use `-e/--encryption_key` and `-s/--signing_key`
    to supply your own ECC keys instead of generating them.

=== "Reprovision"

    ```console
    $ unshackle prd reprovision ./data/PRDs/mydevice.prd
    ```

    Regenerates the leaf certificate and encryption/signing keys of an existing
    device in place. The device must support reprovisioning (a group key must be
    present, i.e. a version 3 or higher device).

=== "Test against Microsoft's demo server"

    ```console
    $ unshackle prd test ./data/PRDs/mydevice.prd -sl 2000 -c aesctr
    ```

    Runs a full challenge/license/keys cycle against Microsoft's public PlayReady
    test server and prints the returned keys, confirming the device works.
    `-sl/--security-level` accepts `150`, `2000`, or `3000` (default `2000`);
    `-c/--ckt` accepts `aesctr` or `aescbc`.

## Selecting which device to use: the `cdm` config

Having a device on disk is not enough; you must tell unshackle which device to use
for which service. That is the job of the `cdm` section, which maps a service tag to
a device name (the file's name without extension). A `default` entry acts as the
fallback:

```yaml title="unshackle.yaml"
cdm:
  default: nexus_6p_26830_l3
  EXAMPLE1: chromecdm_l3
  EXAMPLE2: sm_g935f_l1
```

Lookups are case-insensitive.

!!! tip "The device name is the file name"
    A device named `chromecdm_l3` in the config resolves to `chromecdm_l3.wvd` in the
    WVDs directory (or `chromecdm_l3.prd` in the PRDs directory). It may also be the
    `name` of a remote CDM defined in `remote_cdm`. See
    [Remote CDMs](#remote-cdms) below.

### Advanced selection: by quality, DRM, or profile

Instead of a plain name, a `cdm` entry can be a nested mapping, letting unshackle pick
a different device based on the track quality, the DRM system, or the active service
profile.

=== "By quality"

    Keys are matched against the video height. You can use exact heights or
    comparisons (`>=1080`, `>720`, `<=576`, `<480`). More specific matches win.

    ```yaml
    cdm:
      SOMESERVICE:
        ">=1080": sm_g935f_l1
        default: chromecdm_l3
    ```

=== "By DRM system"

    Keys `widevine` and `playready` select a device per system. unshackle applies
    these automatically, using the `widevine` device for Widevine tracks and the
    `playready` device for PlayReady tracks.

    ```yaml
    cdm:
      SOMESERVICE:
        widevine: sm_g935f_l1
        playready: mydevice_sl3
    ```

=== "By profile"

    Keys are matched against the active profile, with a `default` fallback.

    ```yaml
    cdm:
      SOMESERVICE:
        premium: sm_g935f_l1
        default: chromecdm_l3
    ```

## Local vs remote CDMs

A CDM can run in two places:

- A **local CDM** loads a `.wvd` or `.prd` device from your machine and performs the
  challenge/license/keys handshake locally. This is what the `cdm` mapping above
  resolves to by default.
- A **remote CDM** delegates the CDM operations to an HTTP API. Your device (and its
  secrets) live on the remote server; unshackle sends it PSSH/challenge data and
  receives keys back. This is useful for sharing a high-security-level device, using
  a hosted key service, or keeping device secrets off your download machine.

The device name in your `cdm` mapping resolves to a remote CDM whenever it matches the
`name` of an entry in the `remote_cdm` list. Otherwise it falls back to a local
`.prd`/`.wvd` file of that name.

## Remote CDMs

Remote CDMs are declared as a list under `remote_cdm`. Each entry has a `name` (which
you then reference from the `cdm` mapping) and a shape that depends on its `type`.
There are four kinds.

### Widevine remote CDM (pywidevine serve)

With no `type` field and a non-PlayReady device type, the entry is treated as a
standard pywidevine "serve" endpoint:

```yaml title="unshackle.yaml"
remote_cdm:
  - name: my_wv_remote
    device_type: ANDROID
    system_id: 26830
    security_level: 3
    host: https://cdm.example.com
    secret: your-api-secret
    device_name: sm_g935f_l1

cdm:
  default: my_wv_remote
```

`security_level` defaults to `3000` if omitted. Field names are read
case-insensitively, so `Device Type`/`device_type`, `System ID`/`system_id`, etc. are
all accepted.

### PlayReady remote CDM

If the device type is `PLAYREADY`, the entry is routed to the pyplayready remote CDM:

```yaml title="unshackle.yaml"
remote_cdm:
  - name: my_pr_remote
    device_type: PLAYREADY
    security_level: 3000
    host: https://cdm.example.com
    secret: your-api-secret
    device_name: mydevice_sl3
```

!!! warning "The `host` must end in `/playready`"
    For a PlayReady remote CDM the `host` URL has to include the trailing
    `/playready` path segment (e.g. `https://cdm.example.com/playready`). pyplayready's
    `RemoteCdm` treats `host` as a base and appends its own endpoint paths on top of it,
    so a host without the suffix points the requests at the wrong location. The symptom
    is `404` responses from the server rather than an obvious configuration error.

### Decrypt Labs (hosted KeyXtractor API)

Set `type: decrypt_labs` to use Decrypt Labs' hosted KeyXtractor service, which
includes an intelligent key-caching layer:

```yaml title="unshackle.yaml"
remote_cdm:
  - name: decryptlabs
    type: decrypt_labs
    device_name: L1        # ChromeCDM, L1, L2 (Widevine) or SL2, SL3 (PlayReady)
    # host: https://keyxtractor.decryptlabs.com   # optional, this is the default
    # secret: ...          # optional if decrypt_labs_api_key is set globally

decrypt_labs_api_key: your-decrypt-labs-key
```

The API key is sent as the `decrypt-labs-api-key` header. If you omit `secret` on the
entry, unshackle falls back to the global `decrypt_labs_api_key` value; if neither is
present it raises an error. The `host` defaults to
`https://keyxtractor.decryptlabs.com`. PlayReady devices (`SL2`/`SL3`) default to
security level `2000`/`3000` respectively; Widevine devices default to
`system_id: 26830`, `security_level: 3`.

### Custom API (`custom_api`)

For an arbitrary CDM HTTP API, `type: custom_api` provides a fully configurable client
that adapts to almost any request/response shape through YAML alone. A minimal example:

```yaml title="unshackle.yaml"
remote_cdm:
  - name: my_custom
    type: custom_api
    device:
      name: ChromeCDM
      type: CHROME          # CHROME, ANDROID, or PLAYREADY
      system_id: 26830
      security_level: 3
    auth:
      type: bearer          # header, bearer, or basic
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

The `custom_api` type supports much more: request/response field remapping,
transforms (base64/hex/JSON encoding, `kid:key` parsing), conditional parameters,
success/error detection, and vault-backed caching. Its full schema is aimed at
developers integrating a bespoke key server; see the developer note below.

!!! note "Developer: full `custom_api` schema"
    A `custom_api` entry accepts these sub-objects: `device` (`name`, `type`,
    `system_id`, `security_level`); `auth` (`type` of `header`/`bearer`/`basic` with
    the matching credential fields, plus `custom_headers`); `endpoints`
    (`get_request` and `decrypt_response`, each `{path, method, timeout}`);
    per-endpoint `request_mapping` (`param_names`, `static_params`,
    `conditional_params`, `transforms`, `nested_params`, `exclude_params`) and
    `response_mapping` (`fields`, `transforms`, `response_types`,
    `success_conditions`, `error_fields`, `key_fields`); `caching` (`enabled`,
    `use_vaults`, `check_cached_first`); and `legacy`/`timeout`. PlayReady mode is
    inferred when `device.type == PLAYREADY` or the device name is `SL2`/`SL3`.

## Decryption backends

Once the content keys are known, the encrypted file (for Widevine, PlayReady, and
ClearKey-CENC) is decrypted by one of two external tools:

- **shaka-packager** is the default.
- **mp4decrypt** is part of the Bento4 tools.

Both must be discoverable in unshackle's bundled `binaries/` directory or on your
`PATH`. If the selected tool's binary is missing at decrypt time, unshackle raises an
"executable not found but is required" error. See [dependencies](../getting-started/installation.md) for
installation.

You choose the backend with the `decryption` key. It can be a single value applied
globally, or a per-service mapping:

=== "Global default"

    ```yaml title="unshackle.yaml"
    decryption: shaka
    ```

=== "Per service"

    ```yaml title="unshackle.yaml"
    decryption:
      default: shaka
      SOMESERVICE: mp4decrypt
    ```

!!! info "How the value is interpreted"
    The default is `shaka`. Only the exact value `mp4decrypt` selects mp4decrypt;
    **anything else** (including `shaka`) selects shaka-packager. Per-service lookups
    are case-insensitive, with the `default`/`DEFAULT` entry used as the fallback.

The two backends produce equivalent output for standard CENC content. shaka-packager
is generally preferred; switch a specific service to `mp4decrypt` if you hit a track
that shaka-packager cannot handle.

!!! note "ClearKey (HLS) does not use these tools"
    HLS AES-128 ClearKey content is decrypted in-process using AES-CBC and ignores the
    `decryption` setting entirely. There is nothing to configure.

## ClearKey

### ClearKey over HLS (AES-128)

When an HLS playlist references a key with an `EXT-X-KEY` tag, unshackle fetches the
key directly (from the given URI or an inline `data:` URL) and decrypts each segment
in-process with AES-CBC. There is no device, no CDM, and no license challenge, so
there is nothing to set up. Such tracks are handled automatically during download.

### ClearKey over CENC (W3C EME `org.w3.clearkey`)

The CENC ClearKey scheme is a proper license flow, but the "license server" simply
returns the keys as a JWK Set rather than a DRM-wrapped blob. unshackle builds a W3C
EME request of the form:

```json
{"kids": ["<base64url-kid>", ...], "type": "temporary"}
```

It sends this to the manifest's license URL (or to whatever a service's ClearKey
license hook returns) and reads the JSON Web Key set back. No device file is needed.
The decrypted content still goes through shaka-packager or mp4decrypt like any other
CENC track, so the `decryption` setting applies.

## Widevine and PlayReady on the same title

A single manifest can advertise both Widevine and PlayReady. unshackle picks the CDM
per track: if a track needs a system that your loaded CDM does not provide, it
transparently switches to the appropriate device for that track using your `cdm`
mapping. If it cannot find a matching device, it fails with a clear message such as:

```
Title needs a Widevine CDM but SOMESERVICE is configured with PlayReady.
```

Use the `cdm` mapping's [DRM-based selection](#advanced-selection-by-quality-drm-or-profile)
to configure a device for each system; unshackle selects the right one per track
automatically.

## Keys, vaults, and caching

Fetching a license is comparatively slow and, for some services, rate-limited.
unshackle therefore resolves each key ID in this order before ever contacting a
license server:

1. Keys already known for the current DRM object.
2. An in-process license-key cache (for the running session).
3. Your configured [key vaults](vaults.md).
4. Only if still missing, a live license request.

After a successful license request, the recovered keys are written back to all
configured vaults so future downloads can skip the license server. Two flags let you
control this trade-off on the `dl` command:

| Flag | Effect |
|------|--------|
| `--vaults-only` | Never send a license request; use only cached/vault keys. Fails if a key is missing. |
| `--cdm-only` | Ignore vaults for sourcing keys and always perform a fresh license request. |

See [key vaults](vaults.md) for how to configure vault backends.

## Debugging DRM issues

A few configuration toggles help when a title will not decrypt:

| Config key | Default | Effect |
|------------|---------|--------|
| `debug` | `false` | Global debug output. |
| `debug_keys` | `false` | Log decryption keys. |
| `debug_requests` | `false` | Log HTTP requests. |

```yaml title="unshackle.yaml"
debug_keys: true
debug_requests: true
```

!!! warning "Keys and paths in logs"
    When key logging is enabled, content keys (KID:key pairs) are written to the logs.
    Treat those logs as sensitive. The unrelated `redact_paths` option (default
    `true`) masks local directory paths in log output.

## See also

- [Downloading](downloading.md): the `dl` command and its DRM-related flags.
- [Key vaults](vaults.md): caching content keys across downloads.
- [Configuration](../reference/configuration.md): the full `unshackle.yaml` reference.
- [Dependencies](../getting-started/installation.md): installing shaka-packager and mp4decrypt.
