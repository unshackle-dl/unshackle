# The Configuration File

Almost everything about how unshackle behaves is controlled by a single YAML file named `unshackle.yaml`: where it saves downloads, which CDM it uses, your credentials, proxies, filename templates, and more. This page covers where that file lives, how unshackle finds it, the shape of its contents, the directory layout unshackle builds around it, and how to edit values from the command line.

!!! note "Where the exhaustive key list lives"
    This page is an overview. For the complete, key-by-key breakdown of every setting, its type, and its default, see the [Configuration Reference](../reference/configuration.md).

## The config filename

The main configuration file is always named `unshackle.yaml`. It is a plain YAML file. You can open and edit it in any text editor, or manage individual values with the [`unshackle cfg`](#editing-the-config-with-unshackle-cfg) command.

If you have never created one, that is fine: unshackle runs entirely on built-in defaults when no config file exists. The file only needs to contain the keys you want to change from their defaults.

## Where the config file lives

When unshackle starts, it searches a fixed list of locations and uses the **first one that exists**. In order of priority:

| # | Location | Typical path |
|---|----------|--------------|
| 1 | The unshackle package folder | `.../site-packages/unshackle/unshackle.yaml` |
| 2 | The parent of the package folder | `.../site-packages/unshackle.yaml` |
| 3 | Your OS user-config directory | see the table below |

The third location, your per-user config directory, is the recommended place for most installations, because it lives outside the package and survives reinstalls and upgrades. Its exact path depends on your operating system:

=== "Linux"

    ```text
    ~/.config/unshackle/unshackle.yaml
    ```

=== "Windows"

    ```text
    %LOCALAPPDATA%\unshackle\unshackle.yaml
    ```

=== "macOS"

    ```text
    ~/Library/Application Support/unshackle/unshackle.yaml
    ```

!!! tip "Not sure which file is being used?"
    Run `unshackle env info`. It prints the path the config was loaded from (or tells you none was found), along with every directory unshackle is currently using.

The config is read exactly once, when unshackle starts. If you edit the file, the change takes effect on the next command you run.

## Top-level structure

`unshackle.yaml` is a flat map of top-level keys. Each key configures one area of the program. You only include the keys you want to change; everything else falls back to its default.

Here is a small, realistic example that sets a download location, a release tag, a default CDM, and one service's credentials:

```yaml title="unshackle.yaml"
# Where finished downloads are written
directories:
  downloads: ~/Videos/unshackle

# Release group tag appended to filenames
tag: MYGRP

# Default Widevine/PlayReady device to use, with per-service overrides
cdm:
  default: my_device_l3
  AMZN: my_device_l1

# Per-service login details (service tag -> "username:password")
credentials:
  AMZN: my_email@example.com:hunter2

# Default HTTP headers merged into every request
headers:
  Accept-Language: en-US,en;q=0.9
```

The top-level keys group loosely into these areas:

| Area | Example keys |
|------|--------------|
| Downloading & DRM | `dl`, `cdm`, `remote_cdm`, `decryption` |
| Networking | `network`, `headers` |
| Credentials & cookies | `credentials`, `firefox_cookies` |
| Tracks & muxing | `subtitle`, `audio`, `muxing`, `language_tags` |
| Key vaults | `key_vaults`, `vault_timeout` |
| Proxies & remote | `proxy_providers`, `remote_services`, `serve`, `services` |
| Naming & tagging | `tag`, `output_template`, `chapter_fallback_name` |
| External API keys | `tmdb_api_key`, `simkl_client_id`, `ipinfo_api_key` |
| Behavior & logging | `update_checks`, `redact_paths`, `debug`, `unicode_filenames` |
| Paths | `directories`, `filenames` |

For the full list with types and defaults, see the [Configuration Reference](../reference/configuration.md).

!!! warning "Unknown keys are silently ignored"
    unshackle does not validate your config against a schema. If you misspell a key, it is simply skipped and the default is used. You will not get an error. Double-check key names (and their nesting) if a setting does not seem to apply.

## The directory layout

The `directories` key controls where unshackle reads and writes its various files. Each directory has a sensible default, and you can override most of them by giving a new path. Paths support `~` for your home directory.

```yaml title="unshackle.yaml"
directories:
  downloads: ~/Videos/unshackle
  temp: /mnt/fast/unshackle-temp
  cache: ~/.cache/unshackle
```

The directories unshackle uses:

| Name | Purpose | Overridable |
|------|---------|-------------|
| `downloads` | Default output folder for finished downloads | Yes |
| `temp` | Temporary working files during a download | Yes |
| `cache` | Cache store (title cache, update checks, service caches) | Yes |
| `cookies` | Per-service cookie files | Yes |
| `logs` | Log files | Yes |
| `exports` | Export JSONs | Yes |
| `wvds` | Widevine devices (`.wvd` files) | Yes |
| `prds` | PlayReady devices (`.prd` files) | Yes |
| `dcsl` | DCSL data | Yes |
| `services` | Search paths for service code (see below) | Yes |
| `commands` | CLI command modules | Yes |
| `vaults` | Vault modules | Yes |
| `fonts` | Bundled fonts | Yes |

!!! note "Some directories cannot be moved"
    A handful of internal locations are protected: the package root, the core directory, the user-config directory, and the data base. If you list them under `directories`, unshackle silently ignores the override. This is intentional; those paths are tied to where the package is installed.

### The `services` directory is special

Unlike the other entries, `services` is a **list**, and each entry can be either a local folder or a remote repository. This lets you mix your own local service code with services pulled from Git:

```yaml title="unshackle.yaml"
directories:
  services:
    - unshackle-dl/services      # a GitHub owner/repo shorthand
    - https://github.com/me/my-services.git
    - ~/code/local-services      # a local folder
```

Entries are searched in the order listed, and **the first source to define a given service tag wins**, so put local folders last if you want them to act as fallbacks rather than overrides. Remote repositories are cloned and periodically updated for you. See [Services](../dev/creating-a-service.md) for the full details on how service discovery and repositories work.

## The `filenames` key

Alongside `directories`, the `filenames` key lets you override the templates unshackle uses when naming its own working and log files (for example the log filename or the temporary chapters file). Most users never need to touch this. The available names and their default templates are listed in the [Configuration Reference](../reference/configuration.md#filenames).

## Editing the config with `unshackle cfg`

You do not have to edit `unshackle.yaml` by hand. The `unshackle cfg` command reads and writes individual values for you, creating the config file (and its parent directory) if it does not exist yet.

**Read a single value** by passing its key. Nested keys use dot notation:

```console
$ unshackle cfg tag
$ unshackle cfg cdm.default
```

**Set a value** by passing a key and a value:

```console
$ unshackle cfg tag MYGRP
$ unshackle cfg cdm.default my_device_l3
```

Values are parsed as Python literals where possible, so numbers, booleans, and lists work naturally, while bare text is treated as a string:

```console
$ unshackle cfg update_checks false
$ unshackle cfg vault_timeout 30
```

**Remove a value** with `--unset`:

```console
$ unshackle cfg cdm.default --unset
```

**List everything** currently set with `--list`:

```console
$ unshackle cfg --list
```

When it writes, `unshackle cfg` targets the config file that was loaded. If none exists yet, it creates `unshackle.yaml` in your OS user-config directory.

!!! warning "Editing with `cfg` strips comments"
    Because `unshackle cfg` rewrites the whole file when it saves, any comments in `unshackle.yaml` are removed by a write. If you keep important notes as comments, edit the file by hand instead, or keep those notes elsewhere.

## Next steps

- Browse the [Configuration Reference](../reference/configuration.md) for every key and default.
- Set up your first download in [Downloading](../guide/downloading.md).
- Learn how services are discovered and updated in [Services](../dev/creating-a-service.md).
