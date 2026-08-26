"""Operate user-defined scripts after a download completes.

unshackle tokenizes a command one time, then substitutes the variables into the
resulting argv tokens, then spawns the process with ``shell=False``. Substituting after tokenizing
is what makes a title like ``Bob"; rm -rf ~`` harmless: a value can never become a new
token or a shell operator. The cost is that the command has no shell features and no
interpreter lookup, so users name the interpreter themselves (``python upload.py``).

Scripts are fire-and-forget by default: unshackle spawns them, logs the command, and moves
on. With ``wait: true`` unshackle waits for the script to exit, which runs heavy scripts one
at a time over a season pack. It logs the exit code, but a script never changes unshackle's
own exit code, and unshackle never captures output or times a script out.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

from unshackle.core.config import config
from unshackle.core.utilities import log_event
from unshackle.core.utils.redact import redact_path, redact_text

log = logging.getLogger("post-script")

VARIABLE_RE = re.compile(r"\{([a-z_]+)\}")

EVENTS = ("success", "failure")
MODES = ("file", "season", "run")

SIDECAR_SEPARATOR = "\n"

_warned_entries: set[str] = set()


def tokenize(command: str) -> list[str]:
    """Split a command string into argv tokens, before unshackle substitutes any variable."""
    if os.name == "nt":

        def unquote(token: str) -> str:
            if len(token) > 1 and token[0] == token[-1] and token[0] in "\"'":
                return token[1:-1]
            match = re.match(r"^([^\"']*=)([\"'])(.*)\2$", token)
            return f"{match.group(1)}{match.group(3)}" if match else token

        return [unquote(token) for token in shlex.split(command, posix=False)]
    return shlex.split(command)


def substitute(tokens: Sequence[str], context: dict[str, str]) -> list[str]:
    """Replace every ``{variable}`` inside each token. Unknown or empty ones become ''."""
    unknown: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in context:
            unknown.add(name)
        return context.get(name, "")

    argv = [VARIABLE_RE.sub(replace, token) for token in tokens]
    if unknown:
        log.warning("Unknown post-script variables treated as empty: %s", ", ".join(sorted(unknown)))
    return argv


def _data_injected_flag(tokens: Sequence[str], argv: Sequence[str]) -> Optional[str]:
    """A token that only became a ``-flag`` after substitution.

    Tokenizing before substituting stops a value becoming a *new* token, but a bare
    ``{title}`` token whose value is service-controlled (``--upload-file=x``) still becomes a
    whole option token to the user's script. Flag it when the leading ``-`` came from the
    value, not the template. A literal flag like ``--opt={var}`` starts with ``-`` in the
    template itself and is the user's own, so it is never flagged.
    """
    for template, token in zip(tokens, argv):
        if token.startswith("-") and not template.startswith("-"):
            return token
    return None


def build_context(
    title: Any,
    media_info: Optional[Any] = None,
    *,
    filepath: Optional[Path] = None,
    folder: Optional[Path] = None,
    sidecars: Sequence[Path] = (),
    service: str = "",
    ids: Optional[dict[str, Any]] = None,
    error: str = "",
) -> dict[str, str]:
    """Assemble the variable set for one output file.

    Metadata comes from the same naming context that produced the file's name, so
    ``{quality}`` and ``{hdr}`` always give the values of *this* file. That is what lets
    ``-q 1080,2160 -r HDR10,SDR`` label its four outputs correctly. unshackle gives the
    season and the episode as plain numbers rather than in the filename's ``S01E05``
    form: the spacer and padding belong to the filename template, not to the data.
    """
    context: dict[str, str] = {}

    if media_info is not None:
        try:
            for key, value in title.build_template_context(media_info, show_service=True).items():
                context[key] = "" if value is None else str(value)
        except Exception as e:  # noqa: BLE001 - a naming quirk must never stop the download
            log.debug("Could not build post-script metadata context: %s", e)

    context["vcodec"] = context.get("video", "")
    context["acodec"] = context.get("audio", "")

    season = getattr(title, "season", None)
    number = getattr(title, "number", None)
    context["season"] = "" if season is None else str(season)
    context["episode"] = "" if number is None else str(number)
    context["episode_name"] = str(getattr(title, "name", "") or "") if season is not None else ""
    context["title_raw"] = str(getattr(title, "title", None) or getattr(title, "name", "") or "")
    context.setdefault("title", context["title_raw"].replace("$", "S"))
    context.setdefault("year", str(getattr(title, "year", "") or ""))
    context["title_id"] = str(getattr(title, "id", "") or "")
    context["service"] = service

    context["filepath"] = str(filepath) if filepath else ""
    context["filename"] = filepath.name if filepath else ""
    context["ext"] = filepath.suffix if filepath else ""
    context["folder"] = str(folder or (filepath.parent if filepath else ""))
    context["sidecars"] = SIDECAR_SEPARATOR.join(str(path) for path in sidecars)

    ids = ids or {}
    for name in ("tmdb", "imdb", "tvdb"):
        value = ids.get(name)
        context[name] = "" if value is None else str(value)

    context["error"] = error
    return context


def season_context(context: dict[str, str], folder: Path) -> dict[str, str]:
    """Blank the file-level variables so a season/album hook describes the folder."""
    out = dict(context)
    for key in ("filepath", "filename", "ext", "sidecars", "episode", "episode_name"):
        out[key] = ""
    if out.get("album"):  # music: the album stands in for the season
        for key in ("track_number", "disc", "isrc"):
            if key in out:
                out[key] = ""
        out["title"] = out["title_raw"] = out["album"]
    return out | {"folder": str(folder)}


def _entries(event: str, mode: str, overrides: Sequence[str] = ()) -> Iterator[tuple[str, bool]]:
    """``(command, wait)`` pairs for this event and mode. --postscript replaces the config list."""
    if overrides:
        if event == "success" and mode == "file":
            yield from ((command, False) for command in overrides)
        return

    raw = config.post_scripts
    if isinstance(raw, (str, dict)):
        raw = [raw]
    elif not isinstance(raw, list):
        if raw:
            log.warning("Ignoring malformed post_scripts config: %r", raw)
        raw = []
    for entry in raw:
        if isinstance(entry, str):
            entry = {"command": entry}
        if not isinstance(entry, dict):
            log.warning("Ignoring malformed post_scripts entry: %r", entry)
            continue
        command = str(entry.get("command") or "").strip()
        if not command:
            continue
        entry_event = str(entry.get("event") or "success")
        entry_mode = str(entry.get("mode") or "file")
        if entry_event not in EVENTS or entry_mode not in MODES:
            if command not in _warned_entries:
                _warned_entries.add(command)
                log.warning(
                    "Ignoring post_scripts entry with unknown event/mode %r/%r (valid: %s / %s)",
                    entry_event,
                    entry_mode,
                    "|".join(EVENTS),
                    "|".join(MODES),
                )
            continue
        if entry_event != event or entry_mode != mode:
            continue
        yield command, bool(entry.get("wait", False))


def dispatch(event: str, mode: str, context: dict[str, str], overrides: Sequence[str] = ()) -> None:
    """Spawn every post-script configured for `event` and `mode`. Never raises."""
    for command, wait in _entries(event, mode, overrides):
        try:
            tokens = tokenize(command)
        except ValueError as e:  # shlex: unbalanced quote in the command template
            log.warning("Skipping malformed post-script command (%s): %s", e, redact_path(command))
            continue
        argv = substitute(tokens, context)
        if not argv:
            continue
        injected = _data_injected_flag(tokens, argv)
        if injected is not None:
            log.warning(
                "Skipping post-script: a substituted value expands to the option-like token %r; "
                "prefix the variable (--opt={var}) so service metadata cannot forge a flag",
                redact_text(redact_path(injected)),
            )
            continue
        safe_command = redact_text(redact_path(shlex.join(argv)))
        log.debug("Running post-script: %s", safe_command)
        log_event(
            "post_script_dispatch",
            message=f"Running post-script for {event}/{mode}",
            context={"event": event, "mode": mode, "command": safe_command, "argc": len(argv)},
        )
        detach: dict[str, Any] = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}  # type: ignore[attr-defined]
            if os.name == "nt"
            else {"start_new_session": True}
        )
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, **detach)
        except (OSError, ValueError) as e:
            log.warning("Post-script failed to start (%s): %s", e, redact_text(redact_path(argv[0])))
            continue
        if wait:
            rc = proc.wait()
            log.debug("Post-script exited %s: %s", rc, safe_command)
            log_event(
                "post_script_exit",
                message=f"Post-script for {event}/{mode} exited {rc}",
                context={"event": event, "mode": mode, "command": safe_command, "returncode": rc},
            )
