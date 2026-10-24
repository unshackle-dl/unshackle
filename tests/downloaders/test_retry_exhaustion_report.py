"""A segment that fails every attempt reports the attempts and the cause, without the signed URL."""

import sys

import pytest

from unshackle.commands.dl import dl
from unshackle.core.downloaders.requests import download, retries_exhausted
from unshackle.core.session import MaxRetriesError, RnetSession, failure_reason

downloader = sys.modules[download.__module__]

SIGNED_URL = "https://cdn.example.invalid/dvt2=exp=1~hmac=deadbeef/seg/40_000.mp4a"
RNET_BROKEN_PIPE = (
    f'is_request error: wreq::Error {{ kind: Request, url: "{SIGNED_URL}", source: hyper::Error(Io, '
    'Custom { kind: BrokenPipe, error: "stream closed because of a broken pipe" }) }'
)


class RequestError(Exception):
    """Stands in for rnet.RequestError, which carries the URL in its text."""


def test_failure_reason_keeps_the_rnet_error_text_and_drops_the_url():
    reason = failure_reason(MaxRetriesError("m", cause=RequestError(RNET_BROKEN_PIPE)))

    assert reason == "RequestError (stream closed because of a broken pipe)"


def test_spent_retries_report_one_line_without_the_url(monkeypatch, tmp_path):
    session = RnetSession()
    calls = []

    def get(url, **kwargs):
        calls.append(kwargs.get("max_retries"))
        raise MaxRetriesError("GET cdn.example.invalid failed: x", cause=RequestError(RNET_BROKEN_PIPE))

    monkeypatch.setattr(session, "get", get)
    monkeypatch.setattr(downloader, "retry_sleep", lambda exc, attempts: 0)

    with pytest.raises(MaxRetriesError) as raised:
        for _ in download(url=SIGNED_URL, save_path=tmp_path / "seg", session=session):
            pass

    assert calls == [0] * downloader.MAX_ATTEMPTS
    assert retries_exhausted(raised.value)
    assert not retries_exhausted(ValueError("boom"))
    header, line = dl.failure_lines(raised.value)
    assert f"gave up after {downloader.MAX_ATTEMPTS} failed attempts" in line
    assert "hmac" not in line
