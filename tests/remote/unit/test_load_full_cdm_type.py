"""load_full_cdm must let a proxy client's cdm_type decide the DRM system.

A proxy-mode client holds its own CDM and decrypts locally, so the server never licenses.
When the client's cdm_type differs from the server's configured device, the resolved CDM
must be a type-only stub of the client's type, not the server's device, so the service
requests a matching playout and stores the matching licence URL.
"""

import pytest

from unshackle.core.api import handlers
from unshackle.core.cdm.detect import is_playready_cdm, is_widevine_cdm


@pytest.fixture
def widevine_configured(monkeypatch):
    """config.cdm[service] is a quality-tiered dict of Widevine devices (the real deploy shape)."""
    from unshackle.core.config import config as app_config

    monkeypatch.setattr(
        app_config, "cdm", {"SVC": {"<=1080": "wv_l3", ">1080": "wv_l1", "default": "wv_l3"}}, raising=False
    )
    # every resolved device name is a Widevine device
    monkeypatch.setattr(handlers, "detect_cdm_type", lambda name, cfg: "widevine")

    def fail_load(*args, **kwargs):
        raise AssertionError("load_cdm must not run for a mismatched type")

    monkeypatch.setattr("unshackle.core.cdm.load_cdm", fail_load)


def test_playready_client_gets_a_playready_stub(widevine_configured):
    cdm = handlers.load_full_cdm("SVC", None, "playready")
    assert is_playready_cdm(cdm) and not is_widevine_cdm(cdm)


def test_widevine_client_loads_the_configured_device(monkeypatch):
    """A matching type must still load the real device (regression guard, not a stub)."""
    from unshackle.core.config import config as app_config

    monkeypatch.setattr(app_config, "cdm", {"SVC": {"default": "wv_l3"}}, raising=False)
    monkeypatch.setattr(handlers, "detect_cdm_type", lambda name, cfg: "widevine")
    loaded = {}
    monkeypatch.setattr("unshackle.core.cdm.load_cdm", lambda name, service_name=None: loaded.setdefault("n", name))
    handlers.load_full_cdm("SVC", None, "widevine")
    assert loaded["n"] == "wv_l3"
