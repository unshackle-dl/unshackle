# Directories { #directories }

The `directories` key is a **dict** mapping directory names to filesystem paths. Only the
names listed below are honoured; a handful are **protected** and cannot be moved (attempts
to override them are silently skipped). All defaults are computed relative to the installed
package, and every user-settable path is passed through `Path(...).expanduser()`, so `~`
works.

```yaml title="unshackle.yaml"
directories:
  downloads: ~/Media/unshackle
  temp: /mnt/fast-scratch/unshackle-temp
  wvds: ~/.unshackle/WVDs
  prds: ~/.unshackle/PRDs
```

| Key | Type | Default | Overridable | Purpose |
|-----|------|---------|:-----------:|---------|
| `downloads` | path | `<repo>/downloads` | Yes | Default output directory for finished files. |
| `temp` | path | `<repo>/temp` | Yes | Temporary working files during download/decrypt/mux. |
| `cache` | path | `<data>/cache` | Yes | Generic cache, title cache, and the update-check store. |
| `cookies` | path | `<data>/cookies` | Yes | Per-service cookie files (and VPN cookie files). |
| `logs` | path | `<data>/logs` | Yes | Log files. |
| `exports` | path | `<data>/exports` | Yes | Export JSON files. |
| `wvds` | path | `<data>/WVDs` | Yes | Widevine device files (`.wvd`). |
| `prds` | path | `<data>/PRDs` | Yes | PlayReady device files (`.prd`). |
| `dcsl` | path | `<data>/DCSL` | Yes | DCSL data. |
| `commands` | path | `unshackle/commands` | Yes | CLI command modules. |
| `services` | list \| path | `[unshackle/services]` | Yes | Service search paths and/or remote repo specs (see below). |
| `vaults` | path | `unshackle/vaults` | Yes | Vault backend modules. |
| `fonts` | path | `unshackle/fonts` | Yes | Bundled fonts. |
| `user_configs` | path | `unshackle/` | No protected | Where `unshackle.yaml` lives. |
| `data` | path | `unshackle/` | No protected | Base for the data subdirectories above. |
| `core_dir` | path | `unshackle/core` | No protected | Package core. |
| `namespace_dir` | path | `unshackle/` | No protected | Package root. |
| `app_dirs` | - | `AppDirs("unshackle", False)` | No protected | Internal AppDirs instance. |

!!! note "The `services` directory is special"
    `services` may be a **list**, and each entry can be either a local directory or a
    **repository spec**: a git URL (`https://...`, `ssh://...`, `git@...`, or anything ending in
    `.git`) or `owner/repo` shorthand. Repo specs are cloned and updated automatically; plain
    paths are used as-is. **List order is priority**: the first source to define a service
    tag wins.

    ```yaml
    directories:
      services:
        - you/your-services                 # cloned from GitHub
        - https://example.com/private.git@stable
        - ~/my-services                      # local, lowest priority
    ```

!!! info "How git-backed service repos are handled"
    unshackle's use of git is **read-only on the remote**: it only ever performs a shallow
    `clone`, `fetch`, `pull`, and a local `reset`; **nothing is ever pushed**. Private repos
    are authenticated through your existing git credential helper, and unshackle stores no
    tokens of its own. Clones live under `<first-local-services-dir>/_repos/<host>__<owner>__<repo>/`
    (or the bundled `unshackle/services`), and **nothing is written to the cache directory**. After
    the first clone unshackle re-pulls **at most once every 24 h**, so it does not touch the
    network on every run.

!!! warning "Automatic and manual refresh behave differently"
    The **automatic** 24 h-TTL refresh that happens during a normal `dl`/`search` run
    **refuses to refresh and exits** (naming the offending clone) if that clone has
    uncommitted changes to tracked files or unpushed local commits. Untracked files (new
    service folders, `__pycache__`) never block it. The **manual**
    `unshackle util refresh-services` command instead **hard-resets** the clone to upstream,
    discarding any local edits.

!!! warning "Read-only installs and reinstalls"
    If the services directory lives inside the installed package, a reinstall can delete the
    `_repos` clones. They are re-cloned on next use. On **read-only installs** you must
    point `services` at a writable path, or cloning will fail.

## Filenames { #filenames }

The `filenames` key is a **dict** of templated file/name patterns. Each value is used
verbatim (no path processing). Braced fields like `{time}` and `{service}` are filled in at
runtime.

| Key | Type | Default | Notes |
|-----|------|---------|-------|
| `log` | str | `"unshackle_{name}_{time}.log"` | Written under `directories.logs`. |
| `debug_log` | str | `"unshackle_debug_{service}_{time}.jsonl"` | Structured debug log, under `directories.logs`. |
| `config` | str | `"config.yaml"` | Per-service config file, under that service's directory. |
| `root_config` | str | `"unshackle.yaml"` | The main config filename itself. |
| `chapters` | str | `"Chapters_{title}_{random}.txt"` | Under `directories.temp`. |
| `subtitle` | str | `"Subtitle_{id}_{language}.srt"` | Under `directories.temp`. |
