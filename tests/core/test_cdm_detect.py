"""CDM detection and load-time DRM stamping for local and remote CDMs.

Covers the predicates in ``unshackle.core.cdm.detect`` (playready/widevine, local/remote,
and the remote combinations) and the loader stamping that sets a remote CDM's DRM type at
load time instead of guessing it on each call. Services such as AMZN route to a device
profile off that type, so a wrong classification here is what produces a Downgrade.Hd
license denial.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from unshackle.core.cdm import loader as loader_mod
from unshackle.core.cdm.detect import (
    cdm_location,
    is_local_cdm,
    is_playready_cdm,
    is_remote_cdm,
    is_remote_playready_cdm,
    is_remote_widevine_cdm,
    is_widevine_cdm,
)
from unshackle.core.cdm.loader import _stamp_remote

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
PRD_DIR = REPO / "unshackle" / "PRDs"


def test_stamped_remote_playready():
    cdm = SimpleNamespace(drm="playready", is_remote_cdm=True)
    assert is_playready_cdm(cdm)
    assert not is_widevine_cdm(cdm)
    assert is_remote_cdm(cdm)
    assert is_remote_playready_cdm(cdm)
    assert not is_remote_widevine_cdm(cdm)
    assert cdm_location(cdm) == "remote"


def test_stamped_remote_widevine():
    cdm = SimpleNamespace(drm="widevine", is_remote_cdm=True)
    assert is_widevine_cdm(cdm)
    assert not is_playready_cdm(cdm)
    assert is_remote_widevine_cdm(cdm)
    assert not is_remote_playready_cdm(cdm)
    assert cdm_location(cdm) == "remote"


def test_drm_stamp_beats_conflicting_is_playready_attr():
    # The stamped drm takes priority over the wrapper's own is_playready guess.
    cdm = SimpleNamespace(drm="playready", is_playready=False, is_remote_cdm=True)
    assert is_playready_cdm(cdm)
    assert not is_widevine_cdm(cdm)


def test_drm_stamp_case_insensitive():
    cdm = SimpleNamespace(drm="PlayReady", is_remote_cdm=True)
    assert is_playready_cdm(cdm)


def test_none_classifies_as_nothing():
    for fn in (
        is_playready_cdm,
        is_widevine_cdm,
        is_remote_cdm,
        is_local_cdm,
        is_remote_playready_cdm,
        is_remote_widevine_cdm,
    ):
        assert fn(None) is False
    assert cdm_location(None) == "unknown"


@pytest.mark.parametrize("device_name", ["SL2", "SL3", "SL2000", "SL3000", "sl3000"])
def test_decryptlabs_sl_names_are_playready(device_name):
    from unshackle.core.cdm.decrypt_labs_remote_cdm import DecryptLabsRemoteCDM

    cdm = DecryptLabsRemoteCDM(secret="x", device_name=device_name)
    assert cdm.is_playready
    assert is_playready_cdm(cdm)
    assert is_remote_cdm(cdm)
    assert is_remote_playready_cdm(cdm)
    assert not is_local_cdm(cdm)


@pytest.mark.parametrize("device_name", ["ChromeCDM", "L1", "L2"])
def test_decryptlabs_widevine_names(device_name):
    from unshackle.core.cdm.decrypt_labs_remote_cdm import DecryptLabsRemoteCDM

    cdm = DecryptLabsRemoteCDM(secret="x", device_name=device_name)
    assert not cdm.is_playready
    assert is_widevine_cdm(cdm)
    assert is_remote_widevine_cdm(cdm)


def test_decryptlabs_device_type_forces_playready():
    from unshackle.core.cdm.decrypt_labs_remote_cdm import DecryptLabsRemoteCDM

    cdm = DecryptLabsRemoteCDM(secret="x", device_name="ChromeCDM", device_type="PLAYREADY")
    assert cdm.is_playready


@pytest.mark.parametrize(
    ("device", "expect_pr"),
    [
        ({"name": "SL3000"}, True),
        ({"name": "SL2"}, True),
        ({"name": "ChromeCDM"}, False),
        ({"type": "PLAYREADY", "name": "ChromeCDM"}, True),
    ],
)
def test_custom_remote_cdm_classification(device, expect_pr):
    from unshackle.core.cdm.custom_remote_cdm import CustomRemoteCDM

    cdm = CustomRemoteCDM(host="http://example.invalid", device=device)
    assert cdm.is_playready is expect_pr
    assert is_playready_cdm(cdm) is expect_pr
    assert is_remote_cdm(cdm)


def test_stamp_remote_sets_attrs_and_returns_same_object():
    obj = SimpleNamespace()
    out = _stamp_remote(obj, "playready")
    assert out is obj
    assert obj.drm == "playready"
    assert obj.is_remote_cdm is True


def test_stamp_remote_is_best_effort_on_unsettable_object():
    class Slotted:
        __slots__ = ()

    obj = Slotted()
    out = _stamp_remote(obj, "widevine")  # must not raise
    assert out is obj
    assert not hasattr(obj, "drm")


def test_load_remote_decryptlabs_playready_is_stamped():
    cdm_api = {"name": "dl-pr", "type": "decrypt_labs", "secret": "x", "device_name": "SL3000"}
    cdm = loader_mod._load_remote_cdm(dict(cdm_api), "dl-pr", "AMZN", None)
    assert cdm.drm == "playready"
    assert cdm.is_remote_cdm is True
    assert is_remote_playready_cdm(cdm)


def test_load_remote_decryptlabs_widevine_is_stamped():
    cdm_api = {"name": "dl-wv", "type": "decrypt_labs", "secret": "x", "device_name": "ChromeCDM"}
    cdm = loader_mod._load_remote_cdm(dict(cdm_api), "dl-wv", "AMZN", None)
    assert cdm.drm == "widevine"
    assert is_remote_widevine_cdm(cdm)


def test_load_remote_native_playready_is_stamped(monkeypatch):
    import pyplayready.remote.remotecdm as prmod

    class FakePlayReadyRemote:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(prmod, "RemoteCdm", FakePlayReadyRemote)
    cdm_api = {"name": "pr", "Device Type": "PLAYREADY", "host": "h", "secret": "s", "device_name": "d"}
    cdm = loader_mod._load_remote_cdm(dict(cdm_api), "pr", "AMZN", None)
    assert isinstance(cdm, FakePlayReadyRemote)
    assert cdm.drm == "playready"
    assert cdm.is_remote_cdm is True
    assert is_remote_playready_cdm(cdm)


def test_load_remote_native_widevine_is_stamped(monkeypatch):
    import pywidevine.remotecdm as wvmod

    class FakeWidevineRemote:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(wvmod, "RemoteCdm", FakeWidevineRemote)
    cdm_api = {"name": "wv", "Device Type": "ANDROID", "host": "h", "secret": "s", "device_name": "d"}
    cdm = loader_mod._load_remote_cdm(dict(cdm_api), "wv", "AMZN", None)
    assert isinstance(cdm, FakeWidevineRemote)
    assert cdm.drm == "widevine"
    assert is_remote_widevine_cdm(cdm)


@pytest.fixture
def local_playready_cdm():
    prds = sorted(PRD_DIR.glob("*.prd"))
    if not prds:
        pytest.skip(f"no local .prd present in {PRD_DIR}")
    from pyplayready.cdm import Cdm
    from pyplayready.device import Device

    return Cdm.from_device(Device.load(str(prds[0])))


def test_local_playready_is_playready_and_local(local_playready_cdm):
    cdm = local_playready_cdm
    assert is_playready_cdm(cdm)
    assert not is_widevine_cdm(cdm)
    assert is_local_cdm(cdm)
    assert not is_remote_cdm(cdm)
    assert not is_remote_playready_cdm(cdm)
    assert cdm_location(cdm) == "local"
