# The Configuration File

A single configuration file named `unshackle.yaml` controls almost everything about how unshackle behaves. It sets where unshackle saves downloads, which CDM it uses, your credentials, proxies, filename templates, and more. This page gives where that file lives, how unshackle finds it, and the shape of its contents. It also gives the directory layout unshackle makes around it, and how to edit values from the command line.

!!! note "Where the exhaustive key list lives"
    This page is an overview. For the complete, key-by-key breakdown of every setting, its type, and its default, see the [Configuration Reference](../reference/configuration/index.md).

## The config filename

The main configuration file is always named `unshackle.yaml`. It uses plain YAML. You can open and edit it in any text editor, or manage individual values with the [`unshackle cfg`](#editing-the-config-with-unshackle-cfg) command.

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
    Operate `unshackle env info`. It prints the path unshackle loaded the config from, or tells you that it found none. It also prints every directory unshackle currently uses.

The config is read exactly once, when unshackle starts. If you edit the file, the change takes effect on the next command.

## Top-level structure

`unshackle.yaml` is a flat map of top-level config keys. Each config key configures one area of the program. You include only the config keys you want to change. Everything else falls back to its default.

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
  EXAMPLE: my_device_l1

# Per-service login details (service tag -> "username:password")
credentials:
  EXAMPLE: my_email@example.com:hunter2

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
| External API keys | `tmdb_api_key`, `tvdb_api_key`, `simkl_client_id`, `ipinfo_api_key`, `metadata_providers` |
| Behaviour & logging | `update_checks`, `redact_paths`, `debug`, `unicode_filenames` |
| Post-download scripts | `post_scripts` |
| Paths | `directories`, `filenames` |

For the full list with types and defaults, see the [Configuration Reference](../reference/configuration/index.md).

!!! warning "Unknown keys are silently ignored"
    unshackle does not validate your config against a schema. If you misspell a config key, unshackle skips it and uses the default. You will not get an error. Examine the config key names (and their nesting) if a setting does not seem to apply.

## Setting download defaults (`dl:`)

If you always pass the same `dl` flags (a language, a resolution, a codec), put them under a
`dl:` config key once and unshackle applies them to every download. Any flag from
[Downloading](../guide/downloading.md) works here. The config key is the flag's long name with
dashes turned into underscores (`--best-available` → `best_available`).

```yaml title="unshackle.yaml"
dl:
  lang: [en]          # -l en
  quality: [1080]     # -q 1080
  vcodec: [H.265]     # -v H.265
  sub_format: srt     # convert subtitles to SRT
  downloads: 2        # two tracks at once
```

You can still override any of these on the command line for a one-off. An explicit flag
always beats the config default. You can also scope defaults to a single service by nesting a
`dl:` block under it:

```yaml title="Per-service defaults"
dl:
  lang: [en]          # default for everything
services:
  EXAMPLE:
    dl:
      lang: [en, ja]  # Example downloads English + Japanese
```

!!! tip "A few keys are named after the flag's internal name"
    Most keys are obvious, but set these exact ones: `range` (`-r`), `list` (`--list`),
    `tmdb_id` (`--tmdb`), `imdb_id` (`--imdb`), `tvdb_id` (`--tvdb`), `no_atmos` (`--noatmos`), and `output_dir`
    (`-o`). The [Configuration Reference](../reference/configuration/download.md#dl) has the full list
    and every available config key.

## The directory layout

The `directories` config key controls where unshackle reads and writes its various files. Each directory has a sensible default, and you can override most of them by giving a new path. A path can use `~` for your home directory.

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
    unshackle protects five internal entries: `app_dirs`, `core_dir`, `namespace_dir`, `user_configs`, and `data`. If you write any of them under `directories`, unshackle ignores the override. This is intentional. Those paths come from the package location and your OS user directories.

### The `services` directory is special

Unlike the other entries, `services` is a **list**, and each entry can be either a local folder or a remote repository. This lets you mix your own local services with services pulled from Git:

```yaml title="unshackle.yaml"
directories:
  services:
    - you/your-services          # a GitHub owner/repo shorthand
    - https://example.com/private-services.git
    - ~/code/local-services      # a local folder
```

unshackle examines the entries in the order you wrote them, and **the first source to define a given service tag wins**. Put local folders last if you want them to act as fallbacks and not overrides. unshackle clones remote repositories on first use and refreshes them at most once a day. See [Creating a Service](../dev/creating-a-service.md) for how service discovery and repositories work.

## The `filenames` config key

Alongside `directories`, the `filenames` config key lets you override the templates unshackle uses when it names its own working and log files. Two examples are the log filename and the temporary chapters file. Most users never need to touch this. The [Configuration Reference](../reference/configuration/directories.md#filenames) gives the available names and their default templates.

## Editing the config with `unshackle cfg`

You do not have to edit `unshackle.yaml` by hand. The `unshackle cfg` command reads and writes individual values for you, creating the config file (and its parent directory) if it does not exist yet.

**Read a single value** by passing its config key. Nested config keys use dot notation:

```console
$ unshackle cfg tag
$ unshackle cfg cdm.default
```

**Set a value** by passing a config key and a value:

```console
$ unshackle cfg tag MYGRP
$ unshackle cfg cdm.default my_device_l3
```

unshackle parses the value as a Python literal, so write booleans as `True`/`False` and quote the strings inside a list (`"['en']"`). unshackle stores anything that is not valid Python literal syntax as a plain string:

```console
$ unshackle cfg update_checks False
$ unshackle cfg vault_timeout 30
```

**Remove a value** with `--unset`:

```console
$ unshackle cfg cdm.default --unset
```

**Show everything** currently set with `--list`:

```console
$ unshackle cfg --list
```

When it writes, `unshackle cfg` targets the config file that unshackle loaded. If none exists yet, it makes `unshackle.yaml` inside the `unshackle` package folder (location 1 above), not your OS user-config directory. To keep the config outside the package, make the file at the user-config path yourself first, then `unshackle cfg` writes to it.

!!! warning "Editing with `cfg` strips comments"
    Because `unshackle cfg` rewrites the whole file when it saves, a write removes any comments in `unshackle.yaml`. If you keep important notes as comments, edit the file by hand instead, or keep those notes elsewhere.

## Next steps

- Browse the [Configuration Reference](../reference/configuration/index.md) for every config key and default.
- Set up your first download in [Downloading](../guide/downloading.md).
- Learn how unshackle discovers and updates services in [Creating a Service](../dev/creating-a-service.md).
