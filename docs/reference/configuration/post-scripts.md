# Post-download scripts { #post-scripts }

Operate your own script when a download finishes, with unshackle's metadata passed in as
`{variable}` placeholders. Use it to hand a finished file to an uploader, send a
notification, or start a library scan.

## `post_scripts`

- **Type:** `list[dict]` &nbsp;·&nbsp; **Default:** `[]` (unshackle operates no scripts)

```yaml title="unshackle.yaml"
post_scripts:
  - command: "python /opt/upload.py {filepath} --service={service} --season={season} --episode={episode} --tmdb={tmdb} --subs={sidecars}"
    event: success # success | failure
    mode: file # file | season | run
  - command: "python /opt/notify.py --title={title} --episode={episode}"
```

Every entry that matches the event and mode runs, so one download can feed an uploader and
a notifier without a wrapper script. You can also write an entry as a plain string, which
is the same as `{command: "...", event: success, mode: file}`.

| Key | Default | Meaning |
| --- | --- | --- |
| `command` | required | The command to run, with `{variable}` placeholders |
| `event` | `success` | `success` when a file was written, `failure` when the download failed |
| `mode` | `file` | `file`, `season`, or `run`. See [Modes](#modes) |
| `wait` | `false` | `true` makes unshackle wait for the script to exit before carrying on. See [Waiting for a script](#wait) |

unshackle skips an entry whose `event` or `mode` is not one of the listed values, and logs a
warning that names the bad value, once per command. unshackle also skips an entry whose
`command` it cannot split into arguments, such as one with an unbalanced quote, and logs a
warning. Neither stops the download or the other entries.

## How the command runs

unshackle splits the command into arguments **first**, and then substitutes the variables
into those arguments. The process runs without a shell.

This is what keeps a title like `Bob"; rm -rf ~` harmless: a value can never turn into an
extra argument or a shell operator, and you never have to think about quoting. It also
means a path containing spaces always arrives as a single argument on every OS.

!!! warning "Prefix your variables"
    Titles, episode names and IDs come from the streaming service, not from you. Tokenizing
    first stops a value becoming a *new* argument, but a value on its own can still be a
    whole argument. A bare `{title}` whose value is `--upload-file=/etc/passwd` would arrive
    as an option to your script, not as text. Write `--title={title}`, not a bare `{title}`,
    so a service can never forge a flag. unshackle refuses to operate a command when a
    substituted value would become an option-like token (one that starts with `-`), and logs
    a warning instead.

!!! warning "No shell features, and name your interpreter"
    Pipes, `&&`, `>` redirection, `~` and globs do not work, because unshackle uses no shell.
    If you want them, make the shell explicit: `bash -c "..."`.

    For the same reason there is no interpreter lookup. Write `python /opt/upload.py`
    rather than `/opt/upload.py`. unshackle will not guess whether you meant the system
    Python or the one in its own virtual environment.

    On Windows, when a **fixed** part of an argument contains a space, quote the whole
    argument (`"--out=C:\My Dir\{filename}"`), not only the value. Substituted values
    never need quoting on any OS.

    On Windows, do not target a `.bat` or `.cmd` file directly. Windows runs batch files
    through `cmd.exe`, whose argument quoting differs from every other program and has a
    history of letting a crafted value break out (CVE-2024-3566). Call the interpreter and
    pass the script as an argument (`python upload.py`, `pwsh -File upload.ps1`) instead.

By default, scripts are fire and forget. unshackle starts the process, writes the command to
the debug log, and carries on. It does not wait for the script, capture its output, time it
out, or change its own exit code when a script fails. A script keeps running if unshackle
exits first, and <kbd>Ctrl</kbd>+<kbd>C</kbd> during a season pack does not kill the
scripts already started.

!!! note "One process per file"
    In `file` mode a large season pack starts one process per episode with nothing
    throttling them. If your script is heavy, set `wait: true`, queue the work inside the
    script, or use `mode: season`.

### Waiting for a script { #wait }

```yaml title="unshackle.yaml"
post_scripts:
  - command: "python /opt/upload.py --file={filepath}"
    wait: true
```

With `wait: true`, unshackle waits until the script exits, then writes the exit code to the
debug log and carries on. Because the `file` hook runs between titles, unshackle runs one
script at a time over a season pack instead of starting them all at once. unshackle only
logs the exit code: a failing script does not fail the download or change unshackle's own
exit code. `--postscript` entries never wait.

## Variables

Metadata comes from the same naming context that produced the file's name, built per output
file. So `{quality}` and `{hdr}` always give the values for the file unshackle passes in. Downloading
`-q 1080,2160 -r HDR10,SDR` produces four files, each with its own invocation and its own
values.

| Variable | Value |
| --- | --- |
| `{filepath}` | Absolute path of the output file |
| `{filename}` | Its filename, with extension |
| `{folder}` | Absolute path of its parent folder |
| `{ext}` | Its extension, including the dot |
| `{sidecars}` | Sidecar subtitle files written for this output, newline separated |
| `{title}` | Show, movie or track name, sanitized |
| `{title_raw}` | Exactly what the service returned, untouched |
| `{title_id}` | The service's ID for the title |
| `{service}` | Service tag |
| `{year}` `{season}` `{episode}` `{episode_name}` | Season and episode are plain numbers (`1`, `5`) |
| `{quality}` `{resolution}` `{hdr}` `{hfr}` `{vcodec}` `{acodec}` `{edition}` `{tag}` `{lang_tag}` | As in the filename template, for this file |
| `{tmdb}` `{imdb}` `{tvdb}` | The tagging IDs in use, otherwise empty. See [Tagging IDs](#tagging-ids) |
| `{error}` | Failure message. Empty on success |

Every variable from your `output_template` is available here as well, including
`{video}` and `{audio}`, of which `{vcodec}` and `{acodec}` are aliases.

!!! warning "Empty variables become an empty string"
    `--tmdb={tmdb}` with no ID resolved hands your script `--tmdb=`, so treat an empty
    string as absent. Write your script to accept an empty value for every variable.

    Empty is common, and not only when data is missing:

    - No metadata provider found an ID. `{tmdb}` is empty when no metadata provider found the title,
      the matching API key is missing, or the title is a movie you gave no ID for.
    - The title has no such field. Movies have no `{season}` or `{episode}`, and music has
      neither.
    - **The naming context renders it empty on purpose.** `{hdr}` is empty for an SDR
      file, because SDR is the absence of an HDR tag in a filename rather than a tag that
      reads `SDR`. `{edition}`, `{atmos}`, `{multi}`, `{dual}` and `{hfr}` behave the same
      way. If your script needs the word `SDR`, derive it from an empty `{hdr}` yourself.
    - The hook is a `failure` hook. A failed download has no output file to read metadata
      from, so everything that comes from the naming context is empty. See
      [Events](#events).

    There is no `{variable?}` conditional here, unlike `output_template`. The token is
    always passed, so the argument list your script receives keeps the same shape on every
    run and your argument parser does not have to cope with a flag appearing and
    disappearing.

    Season and episode are the plain numbers, not the `S01E05` form used in filenames. The
    padding and the separator belong to the filename template rather than to the data.

### Music variables

A music download builds its variables from the music naming context, so a hook gets the
release fields instead of the season and episode ones. `{season}`, `{episode}` and
`{episode_name}` are always empty for music.

| Variable | Value |
| --- | --- |
| `{artist}` | Track artist |
| `{album_artist}` | Album artist, falling back to the track artist |
| `{album}` | Album name |
| `{track_number}` `{disc}` | Zero padded. `{disc}` is empty on a single-disc release |
| `{track_total}` `{disc_total}` | Zero padded totals, empty when the service did not give them |
| `{isrc}` `{upc}` `{label}` `{genre}` | As the service reported them, empty when absent |
| `{release_type}` | `album`, `single`, `ep`, and so on. A service that reports nothing gives `album` |
| `{explicit}` | The word `Explicit` when the track is flagged, otherwise empty |

In a `file` hook `{title}` is the track name. See [Modes](#modes) for what an album hook
puts in `{title}`.

`{year}` is empty when the service gives the release no year. `{ext}` is the container the
track arrived in, such as `.flac`, because a music download is not muxed.

### Tagging IDs { #tagging-ids }

`{tmdb}`, `{imdb}` and `{tvdb}` carry the IDs unshackle tags the file with. unshackle reads
them at the moment it sends the hook, and not at the start of the run. The title search inside
the download loop finds an episode's TMDB ID, so a hook fires with it already filled in.

They hold an ID you passed with `--tmdb`, `--imdb` or `--tvdb`, plus a TMDB ID the episode
search resolved. No such search runs for a movie, so a movie hook gets them empty unless you
passed one.

### Sidecar files

`{sidecars}` holds the sidecar subtitle files written beside the output, which are the only
extra files a download leaves next to it. unshackle muxes attachments such as fonts into the
container and deletes temporary artwork, so neither appears. Expect `{sidecars}` to be
empty unless [`subtitle.output_mode`](download.md#subtitle) is `sidecar` or `both`, which is
not the default. A music download writes no sidecars at all.

Paths are absolute and separated by a newline, because a newline is the only character that
cannot appear in a filename on any supported OS. The whole list arrives as a single
argument, so your template needs no quoting.

When one download writes several outputs (`-q 1080,720`), each output's folder gets its own
copy of the sidecar files, and each invocation lists the copies sitting beside its own
`{filepath}`.

```python title="Reading {sidecars} in Python"
subs = [p for p in sys.argv[i].removeprefix("--subs=").split("\n") if p]
```

## Modes { #modes }

| `mode` | Runs | `{filepath}` | `{sidecars}` |
| --- | --- | --- | --- |
| `file` | Once per output file | That file | That output's sidecars |
| `season` | Once per season folder, after that season's last queued title | Empty | Empty |
| `run` | Once per folder written, at the end of the run | Empty | Empty |

!!! warning "`season` means everything this run asked for"
    A `season` hook fires when the last title **this run queued** for that season finishes.
    With `-w S01-S02` the S01 hook runs while S02 is still downloading. It says nothing
    about whether the season is complete on disk. Downloading a single episode fires the
    hook for that season as soon as the episode lands.

    A title that fails stops the whole run. unshackle dispatches the `failure` hook for
    that title and then returns. It downloads no further title, and no `season` or `run`
    hook fires after that point. This applies to every title type. If one episode of S01
    fails, the S01 hook does not fire at all.

unshackle attaches the hook to the folder, not to the season, so one season landing in several
folders (`-q 1080,720` with a quality-dependent folder template) fires it once per folder,
each with its own `{folder}`. A movie is a group of its own: a `season` hook fires for a
movie's folder as soon as the movie finishes.

In `run` mode every variable except `{folder}` is empty, because one run can cover several
shows. Only `{folder}` is meaningful, which makes it useful for starting a library scan
once.

### Music

Music uses the same three modes, with the album standing in for the season.

| `mode` | Runs |
| --- | --- |
| `file` | Once per track, after its metadata has been written |
| `season` | Once per album folder, after every queued track of that album has landed |
| `run` | Once per folder written, at the end of the run |

In a `file` hook `{title}` and `{title_raw}` are the track name. In an album hook they are
the album name, and the per-track variables `{track_number}`, `{disc}` and `{isrc}` are
empty, in the same way that `{episode}` is empty in a season hook.

An album hook obeys the same rule as a season hook. It fires when the last track **this run
queued** for that album lands. This does not mean that the album is complete on disk. A
one-track download fires the album hook as soon as that track lands. If a track fails, the
run stops there, so the album hook never fires, in the same way as an episode and its
season.

## Events { #events }

`success` runs after the file has moved to its final path. `failure` runs when the download
fails, with `{filepath}` empty and `{error}` set. Each track of an album is its own title.
A music `failure` hook therefore describes the track that failed, not the release.

!!! warning "`failure` only fires in `file` mode"
    A `failure` entry with `mode: season` or `mode: run` never runs. Leave `mode` at its
    default on a failure entry.

A `failure` hook has no output file to read metadata from, so it carries only what the
title object already knew: `{title}`, `{title_raw}`, `{title_id}`, `{year}`, `{season}`,
`{episode}`, `{episode_name}`, `{service}`, the tagging IDs and `{error}`. Everything that
comes from the naming context, such as `{quality}`, `{hdr}`, `{artist}` and `{album}`, is
empty.

A resumed `--continue-downloads` download that finishes runs `success` as usual. unshackle
sees your script's exit code only when that entry sets `wait: true`, and even then it only
writes the code to the debug log. Nothing else reports a failing script. Your script owns
its own error handling and logging.

A `success` hook always names a file this run wrote. There is no skip-if-exists path in
`dl` today, so nothing fires for a title that was not downloaded.

## `--postscript`

`dl` accepts `--postscript "<command>"`, repeatable. Every command given this way runs with
`event: success` and `mode: file`.

```console
$ unshackle dl --postscript "python /opt/upload.py {filepath}" SERVICE TITLE_ID
```

It replaces the `post_scripts` config for that run rather than adding to it, so no
configured entry runs while it is in use. That includes your `season`, `run` and `failure`
entries, which have no `--postscript` equivalent.

## The REST API

The API never accepts a post-script command. `postscript`, `post_script` and `post_scripts`
in a `POST /api/download` body get `400 INVALID_PARAMETERS`. A command that arrives from an
HTTP caller would be remote code execution.

unshackle does operate the scripts defined in `unshackle.yaml` for API jobs, music jobs
included. Only the
command itself may not cross the network.

## Logging

unshackle logs the command it operated twice: at `DEBUG` level on the console, and as a
`post_script_dispatch` entry in the
[structured JSON debug log](../../guide/troubleshooting.md#the-structured-json-debug-log)
when that log is on.

unshackle masks both the same way, and honours [`redact_paths`](misc.md), which is on by default.
Debug logs get shared in issue reports, and a hook command line is mostly paths. URL
credentials and `token=` / `api_key=` query parameters are also masked.

!!! warning "Do not hardcode secrets in the command"
    unshackle cannot recognise an arbitrary secret in the command line, such as
    `-H "Authorization: Bearer <key>"`, and logs it verbatim. Read the secret from a file or an
    environment variable inside your script rather than putting it in the `command`.

## Limitations

- No `success` hook operates under `--no-mux` or `--skip-dl`, because neither writes a muxed
  output. A `failure` hook still runs under `--no-mux` if the download itself fails.
- `failure` hooks only operate in `file` mode.
- There is no webhook sender. Call `curl` from a script instead.
- Script output is not captured. Redirect it inside your own script if you want a log.
