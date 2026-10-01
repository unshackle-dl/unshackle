"""`unshackle setup`: asset choice, archive extraction, config writing and the user binaries folder."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterable, Iterator

import pytest
import yaml
from click.testing import CliRunner

from unshackle.commands import setup as setup_mod
from unshackle.commands.env import get_dependencies
from unshackle.core import binaries
from unshackle.core.config import Config, config

EXT = ".exe" if sys.platform == "win32" else ""


@pytest.mark.parametrize(
    ("tool", "system", "machine", "suffix"),
    [
        ("shaka-packager", "linux", "x86_64", "/v2.6.1/packager-linux-x64"),
        ("shaka-packager", "linux", "aarch64", "/v2.6.1/packager-linux-arm64"),
        ("shaka-packager", "win32", "AMD64", "/v2.6.1/packager-win-x64.exe"),
        ("FFmpeg", "linux", "x86_64", "-linux64-gpl.tar.xz"),
        ("FFmpeg", "win32", "AMD64", "-win64-gpl.zip"),
        ("MKVToolNix", "win32", "AMD64", "mkvtoolnix-64-bit-102.0.zip"),
        ("dovi_tool", "linux", "arm64", "-aarch64-unknown-linux-musl.tar.gz"),
        ("CCExtractor", "linux", "x86_64", "/v0.96.6/ccextractor-minimal-x86_64.AppImage"),
        ("CCExtractor", "win32", "AMD64", "/v0.96.6/CCExtractor.0.96.6_win_portable.zip"),
    ],
)
def test_asset_url_matches_platform(tool: str, system: str, machine: str, suffix: str) -> None:
    url = setup_mod.asset_url(tool, system, machine)
    assert url is not None and url.startswith("https://") and url.endswith(suffix)


@pytest.mark.parametrize(
    ("tool", "system", "machine"),
    [
        ("MKVToolNix", "linux", "x86_64"),
        ("CCExtractor", "linux", "aarch64"),
        ("mp4decrypt", "linux", "aarch64"),
        ("shaka-packager", "darwin", "arm64"),
        ("FFmpeg", "win32", "ARM64"),
    ],
)
def test_asset_url_is_none_without_a_portable_build(tool: str, system: str, machine: str) -> None:
    assert setup_mod.asset_url(tool, system, machine) is None


def test_every_url_and_hint_is_filled_from_the_version() -> None:
    for tool, spec in setup_mod.TOOLS.items():
        hint = setup_mod.versioned(tool, spec["hint"])
        assert hint and "{" not in hint
        for key in spec["urls"]:
            url = setup_mod.asset_url(tool, *key.split("-", 1))
            assert url is not None and url.startswith("https://") and "{" not in url
            assert spec["version"].replace(".", "-") in url or spec["version"] in url


def test_label_shows_pinned_versions_only() -> None:
    assert setup_mod.label("shaka-packager") == "shaka-packager 2.6.1"
    assert setup_mod.label("FFmpeg") == "FFmpeg"
    assert setup_mod.label("CCExtractor", "linux") == "CCExtractor 0.96.6"
    assert setup_mod.label("CCExtractor", "win32") == "CCExtractor 0.96.6, 88 MB"


def make_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def make_tar(path: Path, members: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


@pytest.mark.parametrize(("archive_name", "make"), [("tool.zip", make_zip), ("tool.tar.gz", make_tar)])
def test_extract_puts_named_members_flat(
    tmp_path: Path, archive_name: str, make: Callable[[Path, dict[str, bytes]], Path]
) -> None:
    archive = make(
        tmp_path / archive_name,
        {"pkg/bin/tool": b"tool", "pkg/bin/other": b"other", "pkg/README": b"readme"},
    )
    dest = tmp_path / "bin"

    placed = setup_mod.extract(archive, dest, ("tool",))

    assert placed == [dest / "tool"]
    assert sorted(p.name for p in dest.iterdir()) == ["tool"]
    assert (dest / "tool").read_bytes() == b"tool"
    if sys.platform != "win32":
        assert os.access(dest / "tool", os.X_OK)


@pytest.mark.parametrize(("archive_name", "make"), [("evil.zip", make_zip), ("evil.tar.gz", make_tar)])
def test_extract_never_writes_outside_dest(
    tmp_path: Path, archive_name: str, make: Callable[[Path, dict[str, bytes]], Path]
) -> None:
    archive = make(tmp_path / archive_name, {"../../escaped": b"x", "/abs/escaped": b"y"})
    dest = tmp_path / "a" / "b" / "bin"

    setup_mod.extract(archive, dest, ("escaped",))

    assert not (tmp_path / "escaped").exists()
    assert not (tmp_path / "a" / "escaped").exists()
    assert [p.name for p in dest.iterdir()] == ["escaped"]


def test_extract_moves_a_lone_binary(tmp_path: Path) -> None:
    download = tmp_path / "packager-linux-x64"
    download.write_bytes(b"elf")
    dest = tmp_path / "bin"

    assert setup_mod.extract(download, dest, ("packager",)) == [dest / "packager"]
    assert not download.exists()


def test_is_clone_follows_pyproject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = tmp_path / "unshackle"
    package.mkdir()
    monkeypatch.setattr(config.directories, "namespace_dir", package)
    assert setup_mod.is_clone() is False
    (tmp_path / "pyproject.toml").write_text("")
    assert setup_mod.is_clone() is True


def test_write_config_tool_mode_moves_data_dirs(tmp_path: Path) -> None:
    path = tmp_path / "config" / "unshackle.yaml"
    data_root = tmp_path / "data"

    assert setup_mod.write_config(path, tmp_path / "downloads", data_root) is not None

    loaded = Config.from_yaml(path)
    assert loaded.output_template
    assert loaded.directories.downloads == tmp_path / "downloads"
    assert loaded.directories.wvds == data_root / "WVDs"
    assert loaded.directories.temp == data_root / "temp"
    assert loaded.directories.services == [data_root / "services"]


def test_write_config_clone_mode_keeps_package_data_dirs(tmp_path: Path) -> None:
    path = tmp_path / "unshackle.yaml"

    setup_mod.write_config(path, tmp_path / "downloads", None)

    data = yaml.safe_load(path.read_text())
    assert data["directories"] == {"downloads": str(tmp_path / "downloads")}
    assert Config.from_yaml(path).directories.wvds == Config._Directories.wvds


def test_write_config_never_overwrites(tmp_path: Path) -> None:
    path = tmp_path / "unshackle.yaml"
    path.write_text("tag: MINE\n")

    assert setup_mod.write_config(path, tmp_path / "downloads", tmp_path / "data") is None
    assert path.read_text() == "tag: MINE\n"


def make_exe(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}{EXT}"
    path.write_bytes(b"")
    path.chmod(0o755)
    return path


def test_find_checks_the_user_binaries_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binaries, "user_binaries_dir", tmp_path / "user")
    tool = make_exe(tmp_path / "user", "setup-test-tool")

    assert binaries.find("setup-test-tool") == tool


def test_find_prefers_the_package_binaries_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binaries, "package_binaries_dir", tmp_path / "unshackle" / "binaries")
    monkeypatch.setattr(binaries, "user_binaries_dir", tmp_path / "user")
    make_exe(tmp_path / "user", "setup-test-tool")
    packaged = make_exe(tmp_path / "unshackle" / "binaries", "setup-test-tool")

    assert binaries.find("setup-test-tool") == packaged


@pytest.fixture
def asks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every Confirm prompt and answer it with no."""
    prompts: list[str] = []

    def fake_ask(prompt: str, **kwargs: object) -> bool:
        prompts.append(prompt)
        return False

    monkeypatch.setattr(setup_mod.Confirm, "ask", fake_ask)
    return prompts


@pytest.mark.parametrize(
    ("clone", "has_config", "asked", "keep"),
    [(False, False, True, False), (True, False, False, True), (False, True, False, True)],
)
def test_only_a_first_tool_mode_run_recommends_a_clone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    asks: list[str],
    clone: bool,
    has_config: bool,
    asked: bool,
    keep: bool,
) -> None:
    monkeypatch.setattr(setup_mod, "get_config_path", lambda: tmp_path / "unshackle.yaml" if has_config else None)

    assert setup_mod.keep_tool_install(clone=clone) is keep
    assert asks == (["Continue with the uv tool install?"] if asked else [])


def test_declining_the_tool_install_stops_setup(monkeypatch: pytest.MonkeyPatch, asks: list[str]) -> None:
    def fail() -> None:
        raise AssertionError("setup went past the clone prompt")

    monkeypatch.setattr(setup_mod, "is_clone", lambda: False)
    monkeypatch.setattr(setup_mod, "get_config_path", lambda: None)
    monkeypatch.setattr(setup_mod, "get_dependencies", fail)

    result = CliRunner().invoke(setup_mod.setup, [])

    assert result.exit_code == 0, result.output
    assert asks == ["Continue with the uv tool install?"]


REQUIRED_DEPS = {"FFmpeg", "FFprobe", "MKVToolNix", "mkvpropedit", "shaka-packager"}


def fake_deps(missing: Iterable[str]) -> list[dict[str, object]]:
    """Return a `get_dependencies()` result for every TOOLS dep, with the deps of `missing` tools not found."""
    return [
        {"name": dep, "binary": None if tool in missing else Path(dep), "required": dep in REQUIRED_DEPS}
        for tool, spec in setup_mod.TOOLS.items()
        for dep in spec["deps"]
    ]


def test_tools_deps_exist_in_env_check(monkeypatch: pytest.MonkeyPatch) -> None:
    # Set in the module dict: a setattr reads the old value first, and that read runs the PATH search.
    for attr in set(binaries.__all__) - {"find", "register", "get_registered_dependencies"}:
        monkeypatch.setitem(vars(binaries), attr, None)

    deps = {dep["name"]: dep["required"] for dep in get_dependencies()}

    names = {dep for spec in setup_mod.TOOLS.values() for dep in spec["deps"]}
    assert names <= deps.keys()
    assert {name for name in names if deps[name]} == REQUIRED_DEPS


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Point every path setup touches into tmp_path and record its prompts and installs.

    Every tool is missing and downloadable, and each prompt takes its default answer.
    """
    box = SimpleNamespace(
        package=tmp_path / "clone" / "unshackle",
        user_config=tmp_path / "user_config" / "unshackle.yaml",
        user_data=tmp_path / "user_data",
        prompts=[],
        installs=[],
    )
    box.package.mkdir(parents=True)
    (box.package / "unshackle-example.yaml").write_bytes(
        (config.directories.namespace_dir / "unshackle-example.yaml").read_bytes()
    )
    for key, name in setup_mod.DATA_DIRS.items():
        monkeypatch.setattr(config.directories, key, box.package / name)
    monkeypatch.setattr(config.directories, "services", [box.package / "services"])
    monkeypatch.setattr(config.directories, "namespace_dir", box.package)
    monkeypatch.setattr(setup_mod, "PACKAGE_CONFIG_PATH", box.package / "unshackle.yaml")
    monkeypatch.setattr(setup_mod, "USER_CONFIG_PATH", box.user_config)
    monkeypatch.setattr(binaries, "package_binaries_dir", box.package / "binaries")
    monkeypatch.setattr(binaries, "user_binaries_dir", box.user_data / "binaries")
    monkeypatch.setattr(setup_mod, "is_clone", lambda: True)
    monkeypatch.setattr(setup_mod, "get_config_path", lambda: None)
    monkeypatch.setattr(setup_mod, "get_dependencies", lambda: fake_deps(setup_mod.TOOLS))

    def confirm(prompt: str, default: bool = False, **kwargs: object) -> bool:
        box.prompts.append((prompt, default))
        return default

    monkeypatch.setattr(setup_mod.Confirm, "ask", confirm)
    monkeypatch.setattr(setup_mod.Prompt, "ask", lambda prompt, default="", **kwargs: default)
    monkeypatch.setattr(setup_mod, "install", lambda tool, url, dest: box.installs.append((tool, dest)) or True)
    monkeypatch.setattr(setup_mod, "asset_url", lambda tool: f"https://example.invalid/{tool}")
    return box


@pytest.mark.parametrize("clone", [True, False])
def test_setup_writes_into_the_clone_or_the_user_dirs(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, clone: bool
) -> None:
    monkeypatch.setattr(setup_mod, "is_clone", lambda: clone)

    result = CliRunner().invoke(setup_mod.setup, [])

    assert result.exit_code == 0, result.output
    if clone:
        assert {dest for _, dest in sandbox.installs} == {sandbox.package / "binaries"}
        assert (sandbox.package / "unshackle.yaml").is_file()
        assert not sandbox.user_config.exists()
        assert not sandbox.user_data.exists()
    else:
        assert {dest for _, dest in sandbox.installs} == {sandbox.user_data / "binaries"}
        assert sandbox.user_config.is_file()
        assert not (sandbox.package / "unshackle.yaml").exists()
        assert (sandbox.user_data / "WVDs").is_dir()
    assert [tool for tool, _ in sandbox.installs] == ["FFmpeg", "MKVToolNix", "shaka-packager", "mp4decrypt"]
    config_path = sandbox.package / "unshackle.yaml" if clone else sandbox.user_config
    assert f"A minimal unshackle.yaml was written to {config_path}." in result.output
    assert f"To expand it, see {setup_mod.DOCS_URL}" in result.output
    run = "uv run unshackle" if clone else "unshackle"
    assert f"Next: run `{run} env check` to confirm the tools." in result.output


@pytest.mark.parametrize(
    ("missing", "downloadable", "prompt", "default", "installed"),
    [
        (
            {"shaka-packager", "mp4decrypt"},
            {"shaka-packager", "mp4decrypt"},
            "Install the DRM tools (shaka-packager 2.6.1, mp4decrypt 1.6.0.641) (required)?",
            True,
            ["shaka-packager", "mp4decrypt"],
        ),
        ({"mp4decrypt"}, {"mp4decrypt"}, "Install the DRM tools (mp4decrypt 1.6.0.641)?", False, []),
        (
            {"shaka-packager"},
            {"shaka-packager"},
            "Install the DRM tools (shaka-packager 2.6.1) (required)?",
            True,
            ["shaka-packager"],
        ),
        (
            {"shaka-packager", "mp4decrypt"},
            {"shaka-packager"},
            "Install the DRM tools (shaka-packager 2.6.1) (required)?",
            True,
            ["shaka-packager"],
        ),
    ],
)
def test_drm_tools_share_one_prompt(
    sandbox: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    missing: set[str],
    downloadable: set[str],
    prompt: str,
    default: bool,
    installed: list[str],
) -> None:
    monkeypatch.setattr(setup_mod, "get_dependencies", lambda: fake_deps(missing))
    monkeypatch.setattr(
        setup_mod, "asset_url", lambda tool: f"https://x.invalid/{tool}" if tool in downloadable else None
    )

    result = CliRunner().invoke(setup_mod.setup, [])

    assert result.exit_code == 0, result.output
    assert sandbox.prompts == [(prompt, default)]
    assert [tool for tool, _ in sandbox.installs] == installed
    assert ("mp4decrypt: no download" in result.output) == ("mp4decrypt" in missing - downloadable)


@pytest.fixture
def fake_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    """Accept any device file except one that holds b"bad"."""

    def load(path: Path) -> None:
        if Path(path).read_bytes() == b"bad":
            raise ValueError("bad magic")

    monkeypatch.setattr("pywidevine.device.Device.load", load)
    monkeypatch.setattr("pyplayready.device.Device.load", load)


def script_cdm_prompts(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> list[tuple[str, object]]:
    """Answer the CDM prompts in order, then finish; every other prompt takes its default."""
    asked: list[tuple[str, object]] = []

    def ask(prompt: str, default: str = "", choices: object = None, **kwargs: object) -> str:
        asked.append((prompt, choices))
        if prompt.startswith("Downloads"):
            return default
        return answers.pop(0) if answers else default

    monkeypatch.setattr(setup_mod.Prompt, "ask", ask)
    return asked


def make_devices(folder: Path, files: dict[str, bytes]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (folder / name).write_bytes(data)
    return folder


def test_device_files_expands_a_folder_without_recursion(tmp_path: Path) -> None:
    folder = make_devices(tmp_path / "in", {"a.wvd": b"", "b.wvd": b"", "c.prd": b"", "notes.txt": b""})
    make_devices(folder / "sub", {"deep.wvd": b""})

    assert setup_mod.device_files(str(folder)) == [folder / "a.wvd", folder / "b.wvd", folder / "c.prd"]
    assert setup_mod.device_files(str(make_devices(tmp_path / "empty", {}))) == []


def test_device_files_keeps_quotes_and_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert setup_mod.device_files('"~/x.wvd"') == [tmp_path / "x.wvd"]


@pytest.mark.usefixtures("fake_devices")
def test_cdm_prompt_repeats_and_asks_for_the_default(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    folder = make_devices(tmp_path / "in", {"a.wvd": b"", "b.wvd": b"", "c.prd": b"", "notes.txt": b""})
    single = make_devices(tmp_path / "other", {"d.wvd": b""}) / "d.wvd"
    asked = script_cdm_prompts(monkeypatch, [str(folder), f'"{single}"', "", "c"])

    result = CliRunner().invoke(setup_mod.setup, [])

    assert result.exit_code == 0, result.output
    for name in ("a.wvd", "b.wvd", "c.prd", "d.wvd"):
        assert f"Imported: {name}" in result.output
    assert [p for p, _ in asked].count(setup_mod.CDM_PROMPT) == 3
    assert ("Default CDM", ["a", "b", "c", "d"]) in asked
    assert sorted(p.name for p in (sandbox.package / "WVDs").iterdir()) == ["a.wvd", "b.wvd", "d.wvd"]
    assert [p.name for p in (sandbox.package / "PRDs").iterdir()] == ["c.prd"]
    assert yaml.safe_load((sandbox.package / "unshackle.yaml").read_text())["cdm"] == {"default": "c"}


@pytest.mark.usefixtures("fake_devices")
def test_one_bad_device_does_not_stop_the_rest(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    folder = make_devices(tmp_path / "in", {"bad.wvd": b"bad", "good.wvd": b"", "good2.prd": b""})
    asked = script_cdm_prompts(monkeypatch, [str(folder), "", "good"])

    result = CliRunner().invoke(setup_mod.setup, [])

    assert result.exit_code == 0, result.output
    assert "Skipped bad.wvd: not a valid Widevine device (bad magic)" in result.output
    assert "Imported: good.wvd" in result.output
    assert "Imported: good2.prd" in result.output
    assert ("Default CDM", ["good", "good2"]) in asked
    assert not (sandbox.package / "WVDs" / "bad.wvd").exists()


@pytest.mark.usefixtures("fake_devices")
def test_folder_without_devices_says_so(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Longer than the 80-column console, so a wrapped line would split the path.
    folder = make_devices(tmp_path / ("long" * 20) / "in", {"notes.txt": b""})
    script_cdm_prompts(monkeypatch, [str(folder)])

    result = CliRunner().invoke(setup_mod.setup, [])

    assert result.exit_code == 0, result.output
    assert f"No .wvd or .prd files in {folder}" in result.output
    assert "cdm" not in yaml.safe_load((sandbox.package / "unshackle.yaml").read_text())


@pytest.mark.usefixtures("fake_devices")
def test_destination_folder_is_already_in_place(sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    wvds = make_devices(sandbox.package / "WVDs", {"x.wvd": b"device"})
    asked = script_cdm_prompts(monkeypatch, [str(wvds)])

    result = CliRunner().invoke(setup_mod.setup, [])

    assert result.exit_code == 0, result.output
    assert "Already in place: x.wvd" in result.output
    assert (wvds / "x.wvd").read_bytes() == b"device"
    assert all(prompt != "Default CDM" for prompt, _ in asked)
    assert yaml.safe_load((sandbox.package / "unshackle.yaml").read_text())["cdm"] == {"default": "x"}


@pytest.mark.usefixtures("fake_devices")
def test_existing_config_is_never_edited(
    sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    existing = sandbox.package / "unshackle.yaml"
    existing.write_text("tag: MINE\n")
    monkeypatch.setattr(setup_mod, "get_config_path", lambda: existing)
    folder = make_devices(tmp_path / "in", {"a.wvd": b"", "b.wvd": b""})
    asked = script_cdm_prompts(monkeypatch, [str(folder)])

    result = CliRunner().invoke(setup_mod.setup, [])

    assert result.exit_code == 0, result.output
    assert "Imported: b.wvd" in result.output
    assert existing.read_text() == "tag: MINE\n"
    assert "A minimal unshackle.yaml" not in result.output
    assert all(prompt != "Default CDM" for prompt, _ in asked)


def test_extract_puts_a_tool_and_its_dlls_in_a_subfolder(tmp_path: Path) -> None:
    spec = setup_mod.platform_spec("CCExtractor", "win32")
    archive = make_zip(
        tmp_path / "ccx.zip",
        {
            name: b"x"
            for name in (
                "ccextractorwinfull.exe",
                "avcodec-60.dll",
                "vcruntime140.dll",
                "flutter_windows.dll",
                "url_launcher_windows_plugin.dll",
                "ccxgui.exe",
                "data/app.so",
                "tessdata/eng.traineddata",
            )
        },
    )
    dest = tmp_path / "bin" / spec["folder"]

    placed = setup_mod.extract(archive, dest, ("ccextractorwinfull.exe",), spec["extra"], spec["exclude"])

    assert placed == [dest / "ccextractorwinfull.exe"]
    assert sorted(p.name for p in dest.iterdir()) == ["avcodec-60.dll", "ccextractorwinfull.exe", "vcruntime140.dll"]
    assert [p.name for p in (tmp_path / "bin").iterdir()] == ["ccextractor"]


def test_find_resolves_a_tool_in_its_subfolder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(binaries, "user_binaries_dir", tmp_path / "user")
    tool = make_exe(tmp_path / "user" / "ccextractor", "setup-test-ccx")

    assert binaries.find("setup-test-ccx") == tool


@pytest.fixture
def fake_appimage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Add a tool that downloads as an AppImage, and serve its download from tmp_path."""
    monkeypatch.setitem(
        setup_mod.TOOLS,
        "FakeImage",
        {"version": "1.0", "bins": ("fakeimage",), "urls": {}, "hint": "see the fake docs"},
    )

    def fake_requests(url: str, output_dir: Path, filename: str, **kwargs: object) -> Iterator[dict[str, Path]]:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / filename).write_bytes(b"appimage")
        yield {"file_downloaded": output_dir / filename}

    monkeypatch.setattr(setup_mod, "requests", fake_requests)
    monkeypatch.setattr(setup_mod, "session", lambda: None)
    return tmp_path / "bin"


@pytest.mark.parametrize("returncode", [0, 1])
def test_appimage_is_kept_only_when_it_runs(
    fake_appimage: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], returncode: int
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        assert kwargs["text"] and kwargs["encoding"] == "utf-8" and kwargs["errors"] == "replace"
        return subprocess.CompletedProcess(args, returncode, "", "No suitable fusermount binary found")

    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)

    ok = setup_mod.install("FakeImage", "https://x.invalid/FakeImage.AppImage", fake_appimage)

    binary = fake_appimage / f"fakeimage{EXT}"
    assert calls == [[str(binary), "--version"]]
    assert ok is (returncode == 0)
    assert binary.exists() is (returncode == 0)
    if returncode:
        output = capsys.readouterr().out
        assert "the AppImage does not run, it needs FUSE" in output
        assert "Install it manually: see the fake docs" in output


def test_group_prompt_shows_the_download_size(sandbox: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(setup_mod.TOOLS["CCExtractor"], "size", "88 MB")
    monkeypatch.setattr(setup_mod, "get_dependencies", lambda: fake_deps({"SubtitleEdit", "CCExtractor"}))

    result = CliRunner().invoke(setup_mod.setup, [])

    assert result.exit_code == 0, result.output
    assert sandbox.prompts == [("Install the Subtitle tools (SubtitleEdit 5.2.0, CCExtractor 0.96.6, 88 MB)?", False)]
