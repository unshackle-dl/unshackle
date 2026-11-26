"""Download job admission: one checked, merged and allowed job spec for a create and for a retry."""

import asyncio
from types import SimpleNamespace

import pytest

from unshackle.core.api import handlers, job_admission
from unshackle.core.api.errors import APIError
from unshackle.core.remote_protocol import APIErrorCode

pytestmark = pytest.mark.unit


@pytest.fixture
def server(monkeypatch, tmp_path):
    """A server with the EXAMPLE service installed and no serve or service config."""
    monkeypatch.setattr(handlers.config, "serve", {}, raising=False)
    monkeypatch.setattr(handlers.config, "services", {}, raising=False)
    monkeypatch.setattr(handlers.config.directories, "downloads", tmp_path)
    monkeypatch.setattr(handlers, "validate_service", lambda tag, request=None: "EXAMPLE")
    monkeypatch.setattr(handlers.Services, "load", lambda tag: SimpleNamespace())
    return tmp_path


def admit(body):
    return asyncio.run(job_admission.admit_job({"service": "example", "title_id": "t1", **body}))


def test_a_retry_admits_the_same_job_as_its_create(server):
    spec = admit({"workers": 4, "output_dir": "shows", "lang": ["en"]})
    again = admit(job_admission.resubmission(spec.service, spec.title_id, spec.params))
    assert again == spec
    assert spec.params["client_keys"] == ["lang", "output_dir", "workers"]
    assert spec.params["output_dir"] == str((server / "shows").resolve())


def test_a_retry_takes_the_server_defaults_of_today(server, monkeypatch):
    spec = admit({"lang": ["en"]})
    monkeypatch.setattr(handlers.config, "serve", {"workers": 9})
    again = admit(job_admission.resubmission(spec.service, spec.title_id, spec.params))
    assert again.params["workers"] == 9
    assert again.params["lang"] == ["en"]


def test_a_retry_whose_parameters_no_longer_pass_gets_the_refusal_of_a_create(server, monkeypatch, tmp_path_factory):
    spec = admit({"output_dir": "shows"})
    monkeypatch.setattr(handlers.config.directories, "downloads", tmp_path_factory.mktemp("moved"))
    with pytest.raises(APIError) as retried:
        admit(job_admission.resubmission(spec.service, spec.title_id, spec.params))
    with pytest.raises(APIError) as created:
        admit({"output_dir": spec.params["output_dir"]})
    assert retried.value.error_code is created.value.error_code is APIErrorCode.INVALID_PARAMETERS
    assert retried.value.message == created.value.message


def test_a_job_without_a_client_keys_stamp_resubmits_every_parameter_but_the_server_stamps():
    body = job_admission.resubmission("EXAMPLE", "t1", {"workers": 2, "server_proxy": True})
    assert body == {"workers": 2, "service": "EXAMPLE", "title_id": "t1"}


def test_the_server_proxy_stamp_comes_from_the_key_policy(server):
    assert admit({"server_proxy": True}).params["server_proxy"] is False


def test_the_parameter_check_leaves_the_request_body_as_it_is(server):
    body = {"output_dir": "shows"}
    assert job_admission.check_parameters(body)["output_dir"] == str((server / "shows").resolve())
    assert body == {"output_dir": "shows"}


def test_a_profile_that_names_a_path_is_invalid_parameters(server):
    with pytest.raises(APIError) as ei:
        admit({"profile": "../x"})
    assert ei.value.error_code is APIErrorCode.INVALID_PARAMETERS
    assert ei.value.details == {"service": "EXAMPLE", "title_id": "t1"}


def test_a_serve_default_that_dl_refuses_with_the_request_is_refused_at_create(server, monkeypatch):
    monkeypatch.setattr(handlers.config, "serve", {"sample": "120-180"})
    with pytest.raises(APIError) as ei:
        admit({"include_sample": True})
    assert ei.value.error_code is APIErrorCode.INVALID_PARAMETERS
    assert "--include-sample" in ei.value.message


def test_a_relayed_retry_resubmits_only_what_the_client_sent(server, monkeypatch):
    async def relay(data, request):
        return "box", {"tag": "EXAMPLE"}, {**data, "service": "EXAMPLE"}

    monkeypatch.setattr(handlers, "relay_target", relay)
    monkeypatch.setattr(handlers.config, "serve", {"lang": "en", "workers": 8})
    spec = admit({"remote": "box", "quality": [1080]})
    assert spec.params["client_keys"] == ["quality", "remote"]
    monkeypatch.setattr(handlers.config, "serve", {"lang": "en", "workers": 2})
    again = admit(job_admission.resubmission(spec.service, spec.title_id, spec.params))
    assert again.params["workers"] == 2


def test_a_retry_refused_by_admission_queues_no_job(server, monkeypatch):
    from datetime import datetime

    from unshackle.core.api import download_manager as dm

    spec = admit({"output_dir": "shows"})
    job = SimpleNamespace(
        service=spec.service, title_id=spec.title_id, parameters=spec.params, status=dm.JobStatus.FAILED
    )
    created = []

    async def start_workers():
        return None

    manager = SimpleNamespace(
        get_job=lambda job_id: job,
        start_workers=start_workers,
        create_job=lambda *a, **k: (
            created.append(k) or SimpleNamespace(job_id="j", status=dm.JobStatus.QUEUED, created_time=datetime.now())
        ),
    )
    monkeypatch.setattr(dm, "get_download_manager", lambda: manager)
    monkeypatch.setattr(handlers, "owns_job", lambda job, request: True)
    monkeypatch.setattr(handlers.config.directories, "downloads", server / "moved")
    with pytest.raises(APIError) as ei:
        asyncio.run(handlers.retry_download_job_handler("j0"))
    assert ei.value.error_code is APIErrorCode.INVALID_PARAMETERS
    assert created == []
