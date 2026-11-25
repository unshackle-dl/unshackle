"""The CDM catalogue: a device name's DRM system read from config, and what each built CDM object says it is.

The config peek, the loader and the CDM classes read one rule, so a device is the same system before
and after it loads.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from unshackle.commands.dl import cdm_summary
from unshackle.core.api import handlers
from unshackle.core.cdm.client_relay import ClientDeviceCdm
from unshackle.core.cdm.detect import cdm_system, cdm_type_stub, is_playready_cdm, is_remote_cdm, is_widevine_cdm
from unshackle.core.cdm.loader import device_system, load_cdm
from unshackle.core.config import config

pytestmark = pytest.mark.unit

ENTRIES = [
    ({"type": "decrypt_labs", "secret": "s", "device_name": "SL2"}, "playready"),
    ({"type": "decrypt_labs", "secret": "s", "device_name": "ChromeCDM", "device_type": "PLAYREADY"}, "playready"),
    ({"type": "decrypt_labs", "secret": "s", "device_name": "L1"}, "widevine"),
    ({"type": "custom_api", "host": "http://h.invalid", "device": {"type": "PLAYREADY", "name": "pr"}}, "playready"),
    ({"type": "custom_api", "host": "http://h.invalid", "device": {"name": "SL3000"}}, "playready"),
    ({"type": "custom_api", "host": "http://h.invalid", "device": {"name": "ChromeCDM"}}, "widevine"),
    ({"Device Type": "PLAYREADY", "host": "http://h.invalid", "secret": "s", "device_name": "d"}, "playready"),
    (
        {
            "device_type": "ANDROID",
            "system_id": 1,
            "security_level": 3,
            "host": "http://h.invalid",
            "secret": "s",
            "device_name": "SLOT",
        },
        "widevine",
    ),
]


def app_config(tmp_path, entries=(), cdm=None):
    """A config view with ``entries`` as its remote_cdm and ``tmp_path`` as both device folders."""
    folders = SimpleNamespace(prds=tmp_path / "prd", wvds=tmp_path / "wvd")
    folders.prds.mkdir(exist_ok=True)
    folders.wvds.mkdir(exist_ok=True)
    return SimpleNamespace(remote_cdm=list(entries), directories=folders, cdm=cdm or {})


@pytest.mark.parametrize(("entry", "system"), ENTRIES)
def test_config_and_the_built_cdm_agree(entry, system, tmp_path, monkeypatch):
    entry = {"name": "x", **entry}
    assert device_system("x", app_config(tmp_path, [entry])) == system

    monkeypatch.setattr(config, "remote_cdm", [entry])
    cdm = load_cdm("x", service_name="EXAMPLE")
    assert cdm_system(cdm) == system
    assert is_remote_cdm(cdm)


def test_the_server_settles_a_service_on_its_remote_device_system(tmp_path):
    entry = {"name": "x", "type": "decrypt_labs", "secret": "s", "device_name": "SL3000"}
    assert (
        handlers.detect_cdm_type_for_service("EXAMPLE", app_config(tmp_path, [entry], {"EXAMPLE": "x"})) == "playready"
    )


@pytest.mark.parametrize(
    ("folder", "suffix", "system"),
    [("prd", "prd", "playready"), ("wvd", "prd", "playready"), ("wvd", "wvd", "widevine")],
)
def test_a_local_file_names_its_system(folder, suffix, system, tmp_path):
    view = app_config(tmp_path)
    (tmp_path / folder / f"x.{suffix}").write_bytes(b"")
    assert device_system("x", view) == system


def test_an_unknown_name_is_no_device(tmp_path, monkeypatch):
    view = app_config(tmp_path)
    assert device_system("x", view) is None
    monkeypatch.setattr(config, "remote_cdm", [])
    monkeypatch.setattr(config, "directories", view.directories)
    with pytest.raises(ValueError, match="does not exist"):
        load_cdm("x")


def test_stand_ins_carry_their_system():
    assert cdm_system(cdm_type_stub("playready")) == "playready"
    assert cdm_system(cdm_type_stub("None")) == "widevine"
    assert cdm_system(ClientDeviceCdm(SimpleNamespace(), is_playready=True)) == "playready"
    assert not is_remote_cdm(cdm_type_stub("widevine"))


def test_library_cdms_are_known_by_class():
    from pyplayready.cdm import Cdm as PlayReadyCdm
    from pyplayready.remote.remotecdm import RemoteCdm as PlayReadyRemoteCdm
    from pywidevine.cdm import Cdm as WidevineCdm
    from pywidevine.remotecdm import RemoteCdm as WidevineRemoteCdm

    local_pr, remote_wv = object.__new__(PlayReadyCdm), object.__new__(WidevineRemoteCdm)
    assert is_playready_cdm(local_pr) and not is_remote_cdm(local_pr)
    assert is_widevine_cdm(object.__new__(WidevineCdm))
    assert is_widevine_cdm(remote_wv) and is_remote_cdm(remote_wv)
    assert is_remote_cdm(object.__new__(PlayReadyRemoteCdm))


@pytest.mark.parametrize("cdm", [None, object()])
def test_no_cdm_is_neither_system(cdm):
    assert cdm_system(cdm) is None
    assert not is_playready_cdm(cdm) and not is_widevine_cdm(cdm) and not is_remote_cdm(cdm)


@pytest.mark.parametrize(
    ("device", "drm_type"), [({"type": "PLAYREADY", "name": "pr"}, "PlayReady"), ({"name": "wv"}, "Widevine")]
)
def test_the_load_log_names_a_custom_remote_cdm_by_its_system(device, drm_type):
    from unshackle.core.cdm.custom_remote_cdm import CustomRemoteCDM

    summary = cdm_summary(CustomRemoteCDM(host="http://h.invalid", device=device))
    assert summary["drm_type"] == drm_type
    assert summary["remote"] is True
    assert summary["device"] == device["name"]
