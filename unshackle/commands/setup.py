import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from fnmatch import fnmatch
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any, Optional

import click
import yaml
from rich.prompt import Confirm, Prompt

from unshackle.commands.env import get_dependencies
from unshackle.core import binaries
from unshackle.core.config import PACKAGE_CONFIG_PATH, USER_CONFIG_PATH, Config, config, get_config_path
from unshackle.core.console import console
from unshackle.core.constants import context_settings
from unshackle.core.downloaders import requests
from unshackle.core.session import session

GITHUB = "https://github.com"
BTBN = GITHUB + "/BtbN/FFmpeg-Builds/releases/download/{v}/ffmpeg-master-latest"
SHAKA = GITHUB + "/shaka-project/shaka-packager/releases/download/v{v}/packager"
DOVI = GITHUB + "/quietvoid/dovi_tool/releases/download/{v}/dovi_tool-{v}"
HDR10PLUS = GITHUB + "/quietvoid/hdr10plus_tool/releases/download/{v}/hdr10plus_tool-{v}"
BENTO4 = "https://www.bok.net/Bento4/binaries/Bento4-SDK-{dashed}"
SECONV = GITHUB + "/SubtitleEdit/subtitleedit/releases/download/v{v}/SeConv"
CCX = GITHUB + "/CCExtractor/ccextractor/releases/download/v{v}"

TOOLS: dict[str, dict[str, Any]] = {
    "FFmpeg": {
        "version": "latest",
        "deps": ("FFmpeg", "FFprobe"),
        "bins": ("ffmpeg", "ffprobe"),
        "urls": {
            "linux-x86_64": BTBN + "-linux64-gpl.tar.xz",
            "linux-aarch64": BTBN + "-linuxarm64-gpl.tar.xz",
            "win32-x86_64": BTBN + "-win64-gpl.zip",
        },
        "hint": "https://ffmpeg.org/download.html",
    },
    "MKVToolNix": {
        "version": "102.0",
        "deps": ("MKVToolNix", "mkvpropedit"),
        "bins": ("mkvmerge", "mkvpropedit"),
        "urls": {
            "win32-x86_64": "https://mkvtoolnix.download/windows/releases/{v}/mkvtoolnix-64-bit-{v}.zip",
        },
        "hint": "install the mkvtoolnix package (apt, dnf or pacman), or see https://mkvtoolnix.download/downloads.html",
    },
    "shaka-packager": {
        "group": "DRM",
        "version": "2.6.1",
        "deps": ("shaka-packager",),
        "bins": ("packager",),
        "urls": {
            "linux-x86_64": SHAKA + "-linux-x64",
            "linux-aarch64": SHAKA + "-linux-arm64",
            "win32-x86_64": SHAKA + "-win-x64.exe",
        },
        "hint": GITHUB + "/shaka-project/shaka-packager/releases/tag/v{v}",
    },
    "dovi_tool": {
        "group": "HDR",
        "version": "2.3.4",
        "deps": ("dovi_tool",),
        "bins": ("dovi_tool",),
        "urls": {
            "linux-x86_64": DOVI + "-x86_64-unknown-linux-musl.tar.gz",
            "linux-aarch64": DOVI + "-aarch64-unknown-linux-musl.tar.gz",
            "win32-x86_64": DOVI + "-x86_64-pc-windows-msvc.zip",
        },
        "hint": GITHUB + "/quietvoid/dovi_tool/releases",
    },
    "HDR10Plus_tool": {
        "group": "HDR",
        "version": "1.7.2",
        "deps": ("HDR10Plus_tool",),
        "bins": ("hdr10plus_tool",),
        "urls": {
            "linux-x86_64": HDR10PLUS + "-x86_64-unknown-linux-musl.tar.gz",
            "linux-aarch64": HDR10PLUS + "-aarch64-unknown-linux-musl.tar.gz",
            "win32-x86_64": HDR10PLUS + "-x86_64-pc-windows-msvc.zip",
        },
        "hint": GITHUB + "/quietvoid/hdr10plus_tool/releases",
    },
    "mp4decrypt": {
        "group": "DRM",
        "version": "1.6.0.641",
        "deps": ("mp4decrypt",),
        "bins": ("mp4decrypt",),
        "urls": {
            "linux-x86_64": BENTO4 + ".x86_64-unknown-linux.zip",
            "win32-x86_64": BENTO4 + ".x86_64-microsoft-win32.zip",
        },
        "hint": "https://www.bento4.com/downloads/",
    },
    "SubtitleEdit": {
        "group": "Subtitle",
        "version": "5.2.0",
        "deps": ("SubtitleEdit",),
        "bins": ("seconv",),
        "urls": {
            "linux-x86_64": SECONV + "-Linux-x64.tar.gz",
            "linux-aarch64": SECONV + "-Linux-ARM64.tar.gz",
            "win32-x86_64": SECONV + "-Windows-x64.zip",
        },
        "hint": GITHUB + "/SubtitleEdit/subtitleedit/releases",
    },
    "CCExtractor": {
        "group": "Subtitle",
        "version": "0.96.6",
        "deps": ("CCExtractor",),
        "bins": ("ccextractor",),
        "urls": {
            "linux-x86_64": CCX + "/ccextractor-minimal-x86_64.AppImage",
            "win32-x86_64": CCX + "/CCExtractor.{v}_win_portable.zip",
        },
        "win32": {
            "bins": ("ccextractorwinfull",),
            "folder": "ccextractor",
            "extra": ("*.dll",),
            "exclude": ("flutter_windows.dll", "*_plugin.dll"),
            "size": "88 MB",
        },
        "hint": "install the ccextractor package, or see https://ccextractor.org/public/general/downloads/",
    },
}

DATA_DIRS = {
    key: getattr(Config._Directories, key).name
    for key in ("cookies", "wvds", "prds", "cache", "logs", "exports", "dcsl", "temp")
}
DATA_DIRS["services"] = Config._Directories.services[0].name

say = partial(console.print, soft_wrap=True)


def versioned(tool: str, text: str) -> str:
    """Fill the `{v}` and `{dashed}` fields of a tool's URL or hint with its version."""
    version = TOOLS[tool]["version"]
    return text.format(v=version, dashed=version.replace(".", "-"))


def platform_spec(tool: str, system: Optional[str] = None) -> dict[str, Any]:
    """Return a tool's entry with the overrides for this platform applied."""
    entry = TOOLS[tool]
    return {**entry, **entry.get(system or sys.platform, {})}


def label(tool: str, system: Optional[str] = None) -> str:
    """Return the tool name with its pinned version and download size, for prompts."""
    spec = platform_spec(tool, system)
    text = tool if spec["version"] == "latest" else f"{tool} {spec['version']}"
    return f"{text}, {spec['size']}" if spec.get("size") else text


def asset_url(tool: str, system: Optional[str] = None, machine: Optional[str] = None) -> Optional[str]:
    """Return the download URL of a tool for a platform, or None when it has no portable build."""
    machine = (machine or platform.machine()).lower()
    machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    url = TOOLS[tool]["urls"].get(f"{system or sys.platform}-{machine}")
    return versioned(tool, url) if url else None


def extract(
    download: Path, dest: Path, files: tuple[str, ...], extra: tuple[str, ...] = (), exclude: tuple[str, ...] = ()
) -> list[Path]:
    """Put the named files of an archive flat into dest, or move a lone binary there as files[0].

    It keeps only the base name of each member, so a member path cannot write outside dest.
    """

    def wanted(name: str) -> bool:
        return name in files or (any(fnmatch(name, p) for p in extra) and not any(fnmatch(name, p) for p in exclude))

    dest.mkdir(parents=True, exist_ok=True)
    if download.name.endswith(".zip"):
        with zipfile.ZipFile(download) as zf:
            for info in zf.infolist():
                name = PurePosixPath(info.filename).name
                if wanted(name) and not info.is_dir():
                    with zf.open(info) as src, (dest / name).open("wb") as out:
                        shutil.copyfileobj(src, out)
    elif download.name.endswith((".tar.gz", ".tar.xz")):
        with tarfile.open(download, "r|*") as tf:
            for member in tf:
                name = PurePosixPath(member.name).name
                data = tf.extractfile(member) if member.isfile() and wanted(name) else None
                if data:
                    with data, (dest / name).open("wb") as out:
                        shutil.copyfileobj(data, out)
    else:
        shutil.move(download, dest / files[0])

    placed = [dest / name for name in files if (dest / name).is_file()]
    if sys.platform != "win32":
        for path in placed:
            path.chmod(path.stat().st_mode | 0o755)
    return placed


def runs(binary: Path) -> bool:
    """Return True when `binary --version` exits 0."""
    try:
        return (
            subprocess.run(
                [str(binary), "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def install(tool: str, url: str, dest: Path) -> bool:
    """Download one tool into dest. Print one line, and the manual source when it fails."""
    spec = platform_spec(tool)
    ext = ".exe" if sys.platform == "win32" else ""
    files = tuple(f"{name}{ext}" for name in spec["bins"])
    dest = dest / spec["folder"] if spec.get("folder") else dest
    say(f"Downloading {tool} ({spec['version']}) ...", end=" ")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            downloaded: Optional[Path] = None
            for event in requests(url, Path(tmp), url.rsplit("/", 1)[-1], session=session()):
                downloaded = event.get("file_downloaded") or downloaded
            if downloaded is None:
                raise RuntimeError("the download did not finish")
            placed = extract(downloaded, dest, files, spec.get("extra", ()), spec.get("exclude", ()))
        if len(placed) != len(files):
            raise RuntimeError(f"the archive does not contain {', '.join(files)}")
        if url.endswith(".AppImage") and not runs(placed[0]):
            placed[0].unlink()
            raise RuntimeError("the AppImage does not run, it needs FUSE (fusermount)")
    except Exception as e:
        say(f"[red]failed[/] ({e})")
        say(f"  Install it manually: {versioned(tool, TOOLS[tool]['hint'])}")
        return False
    say("[green]done[/]")
    return True


def is_clone() -> bool:
    """Return True when unshackle runs from a git clone, False for a `uv tool` install."""
    return (config.directories.namespace_dir.parent / "pyproject.toml").exists()


def destinations(clone: bool) -> tuple[Path, Path]:
    """Return the tools folder and the path for a new config: in the clone, or in the user dirs."""
    if clone:
        return binaries.package_binaries_dir, PACKAGE_CONFIG_PATH
    return binaries.user_binaries_dir, USER_CONFIG_PATH


CLONE_STEPS = (
    "  git clone https://github.com/unshackle-dl/unshackle.git\n"
    "  then run install.sh (Linux) or install.bat (Windows) in the new unshackle folder"
)


def keep_tool_install(clone: bool) -> bool:
    """On a first run from a uv tool install, recommend a git clone and ask whether to continue."""
    if clone or get_config_path():
        return True
    say("A git clone is the easiest setup: its tools, config, data and services stay in one folder you can see.")
    say(CLONE_STEPS)
    if Confirm.ask("Continue with the uv tool install?", default=True, console=console):
        return True
    say(f"To set up from a git clone:\n{CLONE_STEPS}")
    return False


def data_dirs(data_root: Path) -> dict[str, Any]:
    """Return the `directories` entries that put the data folders under data_root."""
    dirs: dict[str, Any] = {key: str(data_root / name) for key, name in DATA_DIRS.items()}
    dirs["services"] = [dirs["services"]]
    return dirs


def write_config(path: Path, downloads: Path, data_root: Optional[Path]) -> Optional[dict[str, Any]]:
    """Write a minimal config to path and return its data, or None when path exists.

    With a data_root, the data folders move there, out of the package folder.
    """
    if path.exists():
        return None
    example = yaml.safe_load((config.directories.namespace_dir / "unshackle-example.yaml").read_text("utf-8"))
    directories = {"downloads": str(downloads), **(data_dirs(data_root) if data_root else {})}
    data = {"tag": example["tag"], "output_template": example["output_template"], "directories": directories}
    dump_config(path, data)
    return data


def dump_config(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


DOCS_URL = "https://docs.unshackle.dev/reference/configuration/"
CDM_PROMPT = "Path to a .wvd/.prd file or a folder of them (empty to finish)"


def device_files(answer: str) -> list[Path]:
    """Expand one answer to the CDM prompt: a folder gives the .wvd and .prd files directly in it."""
    path = Path(answer.strip().strip('"')).expanduser()
    if path.is_dir():
        return sorted(file for file in path.iterdir() if file.is_file() and file.suffix.lower() in (".wvd", ".prd"))
    return [path]


def import_cdm(device: Path, wvds: Path, prds: Path) -> Path:
    """Validate a .wvd or .prd file, copy it into its folder and return the copy's path.

    A file that is already its own destination stays where it is.
    """
    if device.suffix.lower() == ".wvd":
        from pywidevine.device import Device as WidevineDevice

        load, kind, folder = WidevineDevice.load, "Widevine", wvds
    elif device.suffix.lower() == ".prd":
        from pyplayready.device import Device as PlayReadyDevice

        load, kind, folder = PlayReadyDevice.load, "PlayReady", prds
    else:
        raise ValueError("not a .wvd or .prd file")
    if not device.is_file():
        raise FileNotFoundError("no such file")
    try:
        load(device)
    except Exception as e:
        raise ValueError(f"not a valid {kind} device ({str(e).splitlines()[0]})") from e
    target = folder / device.name
    if target.exists() and target.resolve() == device.resolve():
        return target
    if target.exists():
        raise FileExistsError(f"{target} already exists")
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(device, target)
    return target


def install_tools(bin_dir: Path) -> tuple[list[str], set[str]]:
    """Find the missing tools, offer the ones with a download and install them into bin_dir.

    Returns the missing tools and the ones this run installed.
    """
    deps = {dep["name"]: dep for dep in get_dependencies()}
    missing = [tool for tool, spec in TOOLS.items() if not all(deps[dep]["binary"] for dep in spec["deps"])]
    say(f"Missing tools: {', '.join(missing) or 'none'}")

    urls: dict[str, str] = {}
    for tool in missing:
        url = asset_url(tool)
        if url:
            urls[tool] = url
        else:
            say(f"{tool}: no download for this platform, {versioned(tool, TOOLS[tool]['hint'])}")

    installed: set[str] = set()
    for group in dict.fromkeys(spec.get("group", tool) for tool, spec in TOOLS.items()):
        tools = [tool for tool in urls if TOOLS[tool].get("group", tool) == group]
        if not tools:
            continue
        required = any(deps[dep]["required"] for tool in tools for dep in TOOLS[tool]["deps"])
        names = ", ".join(label(tool) for tool in tools)
        what = names if group in TOOLS else f"the {group} tools ({names})"
        if Confirm.ask(f"Install {what}{' (required)' if required else ''}?", default=required, console=console):
            installed.update(tool for tool in tools if install(tool, urls[tool], bin_dir))
    return missing, installed


def import_devices(wvds: Path, prds: Path) -> list[str]:
    """Ask for device files or folders until an empty answer, import each one and return their CDM names."""
    imported: list[str] = []
    while answer := Prompt.ask(CDM_PROMPT, default="", console=console):
        files = device_files(answer)
        if not files:
            say(f"No .wvd or .prd files in {answer}")
        for device in files:
            try:
                copy = import_cdm(device, wvds, prds)
            except Exception as e:
                say(f"[red]Skipped[/] {device.name}: {e}")
                continue
            say(f"{'Already in place' if copy.resolve() == device.resolve() else 'Imported'}: {device.name}")
            imported.append(copy.stem)
    return list(dict.fromkeys(imported))


@click.command(short_help="Install the external tools and make a first config.", context_settings=context_settings)
def setup() -> None:
    """
    Install the external tools and make a first config.

    You can run it again: it skips the tools it finds and never overwrites a config.
    """
    clone = is_clone()
    if not keep_tool_install(clone):
        return
    data_root = binaries.user_binaries_dir.parent
    bin_dir, target = destinations(clone)
    say(f"Mode: {'git clone' if clone else 'uv tool install'}")
    missing, installed = install_tools(bin_dir)

    written = None
    existing = get_config_path()
    if existing:
        say(f"Config: {existing} (kept)")
        package = config.directories.namespace_dir.parent
        paths = [getattr(config.directories, key) for key in DATA_DIRS if key != "services"]
        paths += config.directories.services
        if not clone and any(isinstance(path, Path) and path.is_relative_to(package) for path in paths):
            say("[yellow]Warning:[/] some data folders are inside the package, and a tool upgrade deletes them.")
            say(f"Add this to {existing}:\n{yaml.safe_dump({'directories': data_dirs(data_root)}, sort_keys=False)}")
    else:
        default = Path.home() / "Downloads" / "unshackle"
        downloads = Path(Prompt.ask("Downloads folder", default=str(default), console=console)).expanduser()
        written = write_config(target, downloads, None if clone else data_root)

    if written and not clone:
        folders = {key: data_root / DATA_DIRS[key] for key in ("wvds", "prds", "cookies", "services")}
    else:
        folders = {key: getattr(config.directories, key) for key in ("wvds", "prds", "cookies")}
        local_services = [path for path in config.directories.services if isinstance(path, Path)]
        if local_services:
            folders["services"] = local_services[0]
    for key, path in folders.items():
        path.mkdir(parents=True, exist_ok=True)
        say(f"{key}: {path}")

    cdms = import_devices(folders["wvds"], folders["prds"])
    if written and cdms:
        cdm = cdms[0]
        if len(cdms) > 1:
            cdm = Prompt.ask("Default CDM", choices=cdms, default=cdm, console=console)
        written["cdm"] = {"default": cdm}
        dump_config(target, written)
        say(f"Default CDM: {cdm}")

    for tool in TOOLS:
        say(f"{'[green]✓[/]' if tool not in missing or tool in installed else '[red]✗[/]'} {tool}")
    if written:
        say(f"A minimal unshackle.yaml was written to {target}.")
        say(f"To expand it, see {DOCS_URL}")
    run = "uv run unshackle" if clone else "unshackle"
    say(f"Next: run `{run} env check` to confirm the tools.")
    say(f"Then put a service in a services folder and run `{run} dl <SERVICE> <URL>`.")
