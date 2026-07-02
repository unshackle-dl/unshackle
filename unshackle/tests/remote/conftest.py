"""Shared fixtures + CLI flags for tests/remote/."""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "unit: fast, mocked tests (default)")
    config.addinivalue_line("markers", "live: end-to-end tests against a running serve (opt-in via --live)")
    config.addinivalue_line("markers", "slow: tests that hit real services and may take >10s")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="Run e2e tests against a running 'unshackle serve' instance.",
    )
    parser.addoption(
        "--server-url",
        action="store",
        default=os.environ.get("UNSHACKLE_SERVE_URL", ""),
        help="Server URL for e2e tests. If empty, the suite will spawn its own serve "
        "(via --spawn-serve, default on for --live). Override with $UNSHACKLE_SERVE_URL.",
    )
    parser.addoption(
        "--secret-key",
        action="store",
        default=os.environ.get("UNSHACKLE_SECRET_KEY", ""),
        help="X-Secret-Key for e2e tests. Empty for --no-key servers.",
    )
    parser.addoption(
        "--services",
        action="store",
        default="",
        help="Comma-separated service tags to run e2e against (default: all in fixtures.yaml).",
    )
    parser.addoption(
        "--spawn-serve",
        action="store",
        default="auto",
        choices=["auto", "always", "never"],
        help="Whether to spawn 'unshackle serve' for e2e tests. "
        "'auto' (default): spawn only when --server-url is empty. "
        "'always': always spawn (kills any existing process is your problem). "
        "'never': require an external serve.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip live tests unless --live is passed."""
    if config.getoption("--live"):
        return
    skip_live = pytest.mark.skip(reason="needs --live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(url: str, timeout: float = 60.0) -> bool:
    import requests

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = requests.get(f"{url}/api/health", timeout=2)
            if r.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.5)
    return False


@pytest.fixture(scope="session")
def secret_key(request: pytest.FixtureRequest) -> str:
    return str(request.config.getoption("--secret-key"))


@pytest.fixture(scope="session")
def server_url(request: pytest.FixtureRequest):
    """Provide a base URL for the live serve.

    - If --server-url is set: use it as-is (external server).
    - Else (default) and --live is on: spawn 'unshackle serve' on a free
      port, wait for /api/health, yield the URL, terminate at session end.
    - Spawn mode controlled by --spawn-serve {auto, always, never}.
    """
    cfg = request.config
    explicit = str(cfg.getoption("--server-url")).rstrip("/")
    mode = cfg.getoption("--spawn-serve")
    is_live = cfg.getoption("--live")

    if not is_live:
        yield explicit or "http://localhost:8786"
        return

    if mode == "never":
        if not explicit:
            pytest.fail("--spawn-serve=never requires --server-url")
        if not _wait_for_health(explicit, timeout=10):
            pytest.fail(f"External serve not reachable at {explicit}")
        yield explicit
        return

    if mode == "auto" and explicit:
        if not _wait_for_health(explicit, timeout=10):
            pytest.fail(f"External serve not reachable at {explicit}")
        yield explicit
        return

    # Spawn our own serve.
    port = _free_port()
    cmd = [
        "uv",
        "run",
        "unshackle",
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--no-key",
        "--remote-only",
    ]
    proc = subprocess.Popen(  # nosec B603
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=Path(__file__).resolve().parents[2],
    )
    url = f"http://127.0.0.1:{port}"
    try:
        if not _wait_for_health(url, timeout=60):
            proc.terminate()
            pytest.fail(f"Spawned serve at {url} did not become healthy within 60s")
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="session")
def selected_services(request: pytest.FixtureRequest) -> list[str]:
    raw = str(request.config.getoption("--services")).strip()
    return [s.strip().upper() for s in raw.split(",") if s.strip()] if raw else []


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return Path(__file__).parent / "e2e" / "fixtures"


@pytest.fixture(scope="session")
def e2e_scenarios(fixtures_dir: Path, selected_services: list[str]) -> dict:
    """Load e2e service scenarios from fixtures.yaml.

    See fixtures-example.yaml for the schema. fixtures.yaml is .gitignored
    so each user keeps their own private scenario set locally.
    """
    import yaml

    path = fixtures_dir / "fixtures.yaml"
    if not path.exists():
        return {"services": {}}
    data = yaml.safe_load(path.read_text()) or {}
    services = data.get("services", {})
    if selected_services:
        services = {k: v for k, v in services.items() if k.upper() in selected_services}
    data["services"] = services
    return data


@pytest.fixture(scope="session")
def http_session(secret_key: str):
    """Plain requests.Session with X-Secret-Key wired in."""
    import requests

    s = requests.Session()
    s.headers["User-Agent"] = "unshackle-tests/1.0"
    if secret_key:
        s.headers["X-Secret-Key"] = secret_key
    return s


@pytest.fixture
def remote_client(server_url: str, secret_key: str):
    """RemoteClient pointed at the e2e serve instance."""
    from unshackle.core.remote_service import RemoteClient

    return RemoteClient(server_url=server_url, api_key=secret_key)
