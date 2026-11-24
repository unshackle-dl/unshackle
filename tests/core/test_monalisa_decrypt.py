"""MonaLisa's external decrypt tool: a missing tool raises, and a failed run leaves the ciphertext untouched."""

import subprocess
from pathlib import Path
from uuid import UUID

import pytest

from unshackle.core.drm.monalisa import MonaLisa
from unshackle.core.utils import subprocess as tool_runner

pytestmark = pytest.mark.unit


@pytest.fixture
def track(tmp_path: Path) -> Path:
    path = tmp_path / "track.mp4"
    path.write_bytes(b"cipher")
    return path


@pytest.fixture
def make_drm(monkeypatch: pytest.MonkeyPatch):
    """Build a MonaLisa with a known key; extract_keys needs a real device file, so it is skipped."""
    monkeypatch.setattr(MonaLisa, "extract_keys", lambda self: None)

    def build(tool: Path) -> MonaLisa:
        drm = MonaLisa("ticket", Path("unused.mld"), tool)
        drm._kid = UUID(int=1)
        drm._key = "00" * 16
        return drm

    return build


@pytest.fixture
def tool(tmp_path: Path) -> Path:
    path = tmp_path / "decrypt-tool"
    path.write_bytes(b"")
    return path


def fake_tool(monkeypatch: pytest.MonkeyPatch, output: bytes, returncode: int = 0, stderr: bytes = b"") -> None:
    """Replace the tool run with one that writes `output` to the `-o` path."""

    def run(cmd, **kwargs):
        Path(cmd[cmd.index("-o") + 1]).write_bytes(output)
        return subprocess.CompletedProcess(cmd, returncode, b"", stderr)

    monkeypatch.setattr(tool_runner.subprocess, "run", run)


def test_a_missing_tool_raises_and_keeps_the_file(make_drm, track: Path, tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="tool"):
        make_drm(tmp_path / "absent").decrypt(track)
    assert track.read_bytes() == b"cipher"


@pytest.mark.parametrize(
    ("output", "returncode", "stderr"),
    [(b"partial", 1, b"bad key"), (b"", 0, b"")],
    ids=["non-zero exit", "empty output"],
)
def test_a_failed_run_keeps_the_ciphertext(
    make_drm, track: Path, tool: Path, monkeypatch: pytest.MonkeyPatch, output: bytes, returncode: int, stderr: bytes
) -> None:
    fake_tool(monkeypatch, output, returncode, stderr)
    with pytest.raises(RuntimeError, match=f"failed: {stderr.decode()}"):
        make_drm(tool).decrypt(track)
    assert track.read_bytes() == b"cipher"
    assert not track.with_name("track_decrypted.mp4").exists()


def test_a_timeout_keeps_the_ciphertext_and_hides_the_key(
    make_drm, track: Path, tool: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def hang(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(tool_runner.subprocess, "run", hang)
    drm = make_drm(tool)
    with pytest.raises(RuntimeError, match="timed out") as raised:
        drm.decrypt(track)
    assert drm.key_pair not in str(raised.value)
    assert raised.value.__suppress_context__
    assert track.read_bytes() == b"cipher"


def test_a_good_run_replaces_the_file_and_drops_the_temp_output(
    make_drm, track: Path, tool: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_tool(monkeypatch, b"plain")
    make_drm(tool).decrypt(track)
    assert track.read_bytes() == b"plain"
    assert not track.with_name("track_decrypted.mp4").exists()
