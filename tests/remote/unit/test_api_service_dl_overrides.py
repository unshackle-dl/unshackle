"""services.<TAG>.dl defaults reach REST API download jobs, below the values the request sends."""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import click
import pytest

from unshackle.core.api import download_manager as dm
from unshackle.core.api import handlers
from unshackle.core.api.errors import APIError, APIErrorCode

pytestmark = pytest.mark.unit


class FakeManager:
    def __init__(self):
        self.params = None

    async def start_workers(self):
        pass

    def create_job(self, service, title_id, owner_key=None, **params):
        self.params = params
        return SimpleNamespace(job_id="j", status=SimpleNamespace(value="queued"), created_time=datetime.now())


@pytest.fixture
def env(monkeypatch):
    manager = FakeManager()
    gate_input = {}
    monkeypatch.setattr(handlers, "validate_service", lambda service, request=None: "EXAMPLE1")
    monkeypatch.setattr(handlers.Services, "load", lambda service: SimpleNamespace())
    monkeypatch.setattr(dm, "get_download_manager", lambda: manager)
    monkeypatch.setattr(handlers, "enforce_download_gates", lambda params, request: gate_input.update(params))
    monkeypatch.setattr(handlers, "caller_key", lambda request: None)
    monkeypatch.setattr(handlers.config, "serve", {}, raising=False)
    monkeypatch.setattr(handlers.config, "services", {}, raising=False)

    def run(body=None, dl=None, serve=None):
        if dl is not None:
            monkeypatch.setattr(handlers.config, "services", {"EXAMPLE1": {"dl": dl}})
        if serve is not None:
            monkeypatch.setattr(handlers.config, "serve", serve)
        asyncio.run(handlers.download_handler({"service": "EXAMPLE1", "title_id": "t1", **(body or {})}, None))
        return manager.params

    run.gate = gate_input
    return run


def test_service_dl_sets_defaults(env):
    params = env(dl={"workers": 7, "range": ["HDR10"]})
    assert params["workers"] == 7
    assert params["range"] == ["HDR10"]


def test_request_value_beats_service_dl(env):
    assert env(body={"workers": 3}, dl={"workers": 7})["workers"] == 3


def test_service_dl_beats_serve(env):
    assert env(dl={"workers": 7}, serve={"workers": 5})["workers"] == 7


def test_routing_and_unknown_keys_are_ignored(env):
    baseline = env()
    params = env(dl={"remote": True, "server": "x", "speed_limit": "1M"})
    assert params.get("remote") == baseline.get("remote")
    assert params.get("server") == baseline.get("server")
    assert "speed_limit" not in params


def test_click_spelling_of_range_lands_as_range(env):
    assert env(dl={"range_": ["HLG"]})["range"] == ["HLG"]


def test_gate_sees_service_dl(env):
    env(dl={"quality": [2160]})
    assert env.gate["quality"] == [2160]


def test_client_keys_are_the_body_keys(env):
    params = env(body={"workers": 3, "client_keys": ["forged"], "quality": [1080]}, dl={"workers": 7})
    assert params["client_keys"] == ["client_keys", "quality", "workers"]


def test_relayed_job_skips_service_dl(env, monkeypatch):
    async def relay(data, request):
        return "remote-server", {"tag": "EXAMPLE1"}, data

    monkeypatch.setattr(handlers, "relay_target", relay)
    baseline_workers = handlers.DEFAULT_DOWNLOAD_PARAMS["workers"]
    params = env(dl={"workers": 7})
    assert params["workers"] == baseline_workers
    assert set(params["client_keys"]) >= set(handlers.DEFAULT_DOWNLOAD_PARAMS)


def test_client_sent_keys_uses_the_stamp():
    params = {"workers": 1, "quality": [1080], "client_keys": ["workers"]}
    assert dm.client_sent_keys(params) == {"workers"}
    assert "client_keys" not in params


def test_client_sent_keys_without_stamp_counts_all_keys():
    assert dm.client_sent_keys({"workers": 1, "quality": None}) == {"workers", "quality"}


def test_service_defaults_and_options_travel_nested(env, monkeypatch):
    @click.command()
    @click.option("--profile", type=int, default=1)
    @click.option("--device", default="chrome")
    def cli(**kwargs):
        pass

    monkeypatch.setattr(handlers.Services, "load", lambda service: SimpleNamespace(cli=cli))
    params = env(body={"profile": "work", "service_params": {"profile": 2}})
    assert params["profile"] == "work"
    assert params["service_params"] == {"profile": 2, "device": "chrome"}
    assert env(body={"device": "tv"})["service_params"] == {"profile": 1, "device": "tv"}


def use_cli(monkeypatch, *options):
    """Make the loaded service declare ``options`` on its cli."""
    cli = click.Command("svc", params=list(options))
    monkeypatch.setattr(handlers.Services, "load", lambda service: SimpleNamespace(cli=cli))


def test_a_service_option_its_type_refuses_is_a_400(env, monkeypatch):
    use_cli(monkeypatch, click.Option(["--episodes"], type=click.IntRange(1, 3), default=1))
    with pytest.raises(APIError) as exc_info:
        env(body={"service_params": {"episodes": 9}})
    assert exc_info.value.error_code == APIErrorCode.INVALID_PARAMETERS
    assert "episodes" in exc_info.value.message
    assert env(body={"service_params": {"episodes": "2"}})["service_params"]["episodes"] == "2"


@pytest.mark.parametrize(
    ("name", "value"),
    [("episodes", {"a": 1}), ("episodes", [1]), ("movie", 2), ("movie", [1]), ("device", {"a": 1}), ("device", [1])],
)
def test_a_json_shape_click_cannot_convert_is_a_400(env, monkeypatch, name, value):
    use_cli(
        monkeypatch,
        click.Option(["--episodes"], type=int),
        click.Option(["--movie"], is_flag=True),
        click.Option(["--device"]),
    )
    with pytest.raises(APIError) as exc_info:
        env(body={"service_params": {name: value}})
    assert exc_info.value.error_code == APIErrorCode.INVALID_PARAMETERS
    assert name in exc_info.value.message


def test_a_flat_dl_key_never_fills_a_service_option_of_the_same_name(env, monkeypatch):
    use_cli(monkeypatch, click.Option(["--quality"], type=click.Choice(["SD", "HD"]), default="HD"))
    assert env(body={"quality": [1080]})["service_params"] == {"quality": "HD"}
