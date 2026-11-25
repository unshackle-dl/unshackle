"""Characterisation table: the device and DRM system each CDM ladder gives for one ``cdm`` config entry.

Device names start with the system they are for: ``wv_`` is Widevine, ``pr_`` is PlayReady. A stub is ``stub:wv`` or
``stub:pr``. Each answer tuple runs over the named systems of its column.
"""

from collections.abc import Callable
from functools import partial
from types import SimpleNamespace
from typing import Any

import pytest

import unshackle.core.cdm as cdm_mod
from unshackle.commands.dl import dl
from unshackle.core.api import handlers
from unshackle.core.cdm.choice import choose_cdm, drm_system
from unshackle.core.cdm.detect import is_playready_cdm

pytestmark = pytest.mark.unit

LOCAL = (None, "widevine", "playready", "pr")
SERVER = ("widevine", "playready", "wv")
TIER = {"devices": ["wv_tier"], "playready_devices": ["pr_tier"]}
DUAL = {"widevine": "wv_dev", "playready": "pr_dev"}

ROWS: dict[str, tuple[dict, str | None, dict | None]] = {
    "string widevine": ({"EXAMPLE": "wv_dev"}, None, None),
    "string playready": ({"EXAMPLE": "pr_dev"}, None, None),
    "profile mapping, no profile": ({"EXAMPLE": {"alice": "pr_alice", "default": "wv_dev"}}, None, None),
    # drift, not changed: the licence and stub answers ignore the profile that the load answer follows
    "profile mapping, named profile": ({"EXAMPLE": {"alice": "pr_alice", "default": "wv_dev"}}, "alice", None),
    # drift, not changed: the stub takes the first value of the entry, the load and licence answers cdm.default
    "profile mapping, other profile, cdm.default": (
        {"EXAMPLE": {"alice": "pr_alice"}, "default": "wv_def"},
        "bob",
        None,
    ),
    # drift, not changed: a playready client gets a widevine stub here, but a playready stub for a widevine string
    "widevine only": ({"EXAMPLE": {"widevine": "wv_dev"}}, None, None),
    "playready only": ({"EXAMPLE": {"playready": "pr_dev"}}, None, None),
    "dual, widevine first": ({"EXAMPLE": DUAL}, None, None),
    "dual, playready first": ({"EXAMPLE": {"playready": "pr_dev", "widevine": "wv_dev"}}, None, None),
    "dual, keys in mixed case": ({"example": {"PlayReady": "pr_dev", "Widevine": "wv_dev"}}, None, None),
    # drift, not changed: two keys settle no system, but with one device the entry does not follow the client
    "dual, playready empty": ({"EXAMPLE": {"widevine": "wv_dev", "playready": None}}, None, None),
    "dual, playready first and empty": ({"EXAMPLE": {"playready": None, "widevine": "wv_dev"}}, None, None),
    # drift, not changed: dl has no playready device here, the licence answer takes the entry's default
    "widevine and a default device": ({"EXAMPLE": {"widevine": "wv_dev", "default": "pr_dev"}}, None, None),
    "default entry string": ({"default": "pr_def"}, None, None),
    "default entry dual": ({"default": {"widevine": "wv_def", "playready": "pr_def"}}, None, None),
    # drift, not changed: the short form "pr" reaches the stub unread and gives a widevine stub
    "default entry widevine only": ({"default": {"widevine": "wv_def"}}, None, None),
    # drift, not changed: as above, the stub reads the first tier
    "quality tiers, cdm.default": (
        {"EXAMPLE": {"<=1080": "wv_tier1", ">1080": "wv_tier2"}, "default": "pr_def"},
        None,
        None,
    ),
    "quality tiers, no default": ({"EXAMPLE": {"<=1080": "wv_tier1", ">1080": "pr_tier2"}}, None, None),
    # drift, not changed: a config key that is not a string crashes every ladder
    "quality key read as a number": ({"EXAMPLE": {1080: "wv_dev", "default": "pr_dev"}}, None, None),
    "missing entry": ({"OTHER": "pr_other"}, None, None),
    "per-key view replaces the entry": ({"EXAMPLE": DUAL, "OTHER": "wv_other"}, None, {"EXAMPLE": "pr_key"}),
    "per-key view, other service": ({"EXAMPLE": "wv_dev", "OTHER": "wv_other"}, None, {"OTHER": "pr_key"}),
    "per-key view, dual": ({"EXAMPLE": "wv_dev"}, None, {"EXAMPLE": DUAL}),
}


@pytest.fixture
def devices(monkeypatch: pytest.MonkeyPatch) -> None:
    """A device loads as its own name, and its name says its DRM system in place of a device file."""
    monkeypatch.setitem(cdm_mod.__dict__, "load_cdm", lambda name, **_: name)
    monkeypatch.setattr(handlers, "device_system", lambda name, _: {"wv": "widevine", "pr": "playready"}.get(name[:2]))


def shown(value: Any) -> Any:
    """A loaded device as its name, a stub as its system, an error as its class name."""
    if value is None or isinstance(value, (str, bool, list)):
        return value
    return "stub:pr" if is_playready_cdm(value) else "stub:wv"


def attempt(answer: Callable[[], Any]) -> Any:
    """What ``answer`` gives, with an error shown as its class name."""
    try:
        return shown(answer())
    except Exception as error:
        return type(error).__name__


def each(systems: tuple, answer: Callable[[Any], Any]) -> tuple:
    """``answer`` for each named system."""
    return tuple(attempt(partial(answer, system)) for system in systems)


def answers(monkeypatch: pytest.MonkeyPatch, cdm: dict, profile: str | None, key_cdm: dict | None) -> dict:
    """What every ladder answers for one config row."""
    monkeypatch.setattr(handlers.config, "cdm", cdm, raising=False)
    user = {**TIER, **({"cdm": key_cdm} if key_cdm else {})}
    view = handlers.key_cdm_config(user)
    cmd = dl.__new__(dl)
    cmd.vaults = None
    track = SimpleNamespace(drm_preference=None)
    return {
        "dl": each(LOCAL, lambda s: cmd.get_cdm("EXAMPLE", profile, drm=s)),
        "load": each(LOCAL, lambda s: handlers.load_full_cdm("EXAMPLE", profile, s, view)),
        "stub": each(LOCAL, lambda s: handlers.resolve_server_cdm("EXAMPLE", profile, s, view)),
        "device": each(SERVER, lambda s: handlers.resolve_device_name(user, s, "EXAMPLE")),
        "settled": attempt(lambda: handlers.detect_cdm_type_for_service("EXAMPLE", view)),
        "both": attempt(lambda: choose_cdm(view.cdm, "EXAMPLE").both),
        "candidates": each(SERVER, lambda s: handlers.server_drm_candidates("EXAMPLE", user, s, track)),
    }


EXPECTED: dict[str, dict[str, Any]] = {
    "string widevine": {
        "dl": ("wv_dev", "wv_dev", "wv_dev", "wv_dev"),
        "load": ("wv_dev", "wv_dev", "stub:pr", "stub:pr"),
        "stub": ("stub:wv", "stub:wv", "stub:wv", "stub:wv"),
        "device": ("wv_dev", "wv_dev", "wv_dev"),
        "settled": "widevine",
        "both": False,
        "candidates": (["widevine"], ["widevine"], ["widevine"]),
    },
    "string playready": {
        "dl": ("pr_dev", "pr_dev", "pr_dev", "pr_dev"),
        "load": ("pr_dev", "stub:wv", "pr_dev", "pr_dev"),
        "stub": ("stub:pr", "stub:pr", "stub:pr", "stub:pr"),
        "device": ("pr_dev", "pr_dev", "pr_dev"),
        "settled": "playready",
        "both": False,
        "candidates": (["playready"], ["playready"], ["playready"]),
    },
    "profile mapping, no profile": {
        "dl": ("wv_dev", "wv_dev", "wv_dev", "wv_dev"),
        "load": ("wv_dev", "wv_dev", "stub:pr", "stub:pr"),
        "stub": ("stub:wv", "stub:wv", "stub:wv", "stub:wv"),
        "device": ("wv_dev", "wv_dev", "wv_dev"),
        "settled": "widevine",
        "both": False,
        "candidates": (["widevine"], ["widevine"], ["widevine"]),
    },
    "profile mapping, named profile": {
        "dl": ("pr_alice", "pr_alice", "pr_alice", "pr_alice"),
        "load": ("pr_alice", "stub:wv", "pr_alice", "pr_alice"),
        "stub": ("stub:wv", "stub:wv", "stub:wv", "stub:wv"),
        "device": ("wv_dev", "wv_dev", "wv_dev"),
        "settled": "widevine",
        "both": False,
        "candidates": (["widevine"], ["widevine"], ["widevine"]),
    },
    "profile mapping, other profile, cdm.default": {
        "dl": ("wv_def", "wv_def", "wv_def", "wv_def"),
        "load": ("wv_def", "wv_def", "stub:pr", "stub:pr"),
        "stub": ("stub:pr", "stub:pr", "stub:pr", "stub:pr"),
        "device": ("wv_def", "wv_def", "wv_def"),
        "settled": "widevine",
        "both": False,
        "candidates": (["widevine"], ["widevine"], ["widevine"]),
    },
    "widevine only": {
        "dl": ("wv_dev", "wv_dev", None, None),
        "load": ("wv_dev", "wv_dev", "stub:wv", "stub:wv"),
        "stub": ("stub:wv", "stub:wv", "stub:wv", "stub:wv"),
        "device": ("wv_dev", "pr_tier", "wv_tier"),
        "settled": "widevine",
        "both": False,
        "candidates": (["widevine", "playready"], ["widevine", "playready"], ["widevine", "playready"]),
    },
    "playready only": {
        "dl": ("pr_dev", "pr_dev", "pr_dev", "pr_dev"),
        "load": ("pr_dev", "stub:wv", "pr_dev", "pr_dev"),
        "stub": ("stub:pr", "stub:pr", "stub:pr", "stub:pr"),
        "device": ("wv_tier", "pr_dev", "wv_tier"),
        "settled": "playready",
        "both": False,
        "candidates": (["playready", "widevine"], ["playready", "widevine"], ["playready", "widevine"]),
    },
    "dual, widevine first": {
        "dl": ("wv_dev", "wv_dev", "pr_dev", "pr_dev"),
        "load": ("wv_dev", "wv_dev", "pr_dev", "pr_dev"),
        "stub": ("stub:wv", "stub:wv", "stub:wv", "stub:wv"),
        "device": ("wv_dev", "pr_dev", "wv_tier"),
        "settled": None,
        "both": True,
        "candidates": (["widevine", "playready"], ["playready", "widevine"], ["widevine", "playready"]),
    },
    "dual, playready first": {
        "dl": ("pr_dev", "wv_dev", "pr_dev", "pr_dev"),
        "load": ("pr_dev", "wv_dev", "pr_dev", "pr_dev"),
        "stub": ("stub:pr", "stub:pr", "stub:pr", "stub:pr"),
        "device": ("wv_dev", "pr_dev", "wv_tier"),
        "settled": None,
        "both": True,
        "candidates": (["widevine", "playready"], ["playready", "widevine"], ["widevine", "playready"]),
    },
    "dual, keys in mixed case": {
        "dl": ("pr_dev", "wv_dev", "pr_dev", "pr_dev"),
        "load": ("pr_dev", "wv_dev", "pr_dev", "pr_dev"),
        "stub": ("stub:pr", "stub:pr", "stub:pr", "stub:pr"),
        "device": ("wv_dev", "pr_dev", "wv_tier"),
        "settled": None,
        "both": True,
        "candidates": (["widevine", "playready"], ["playready", "widevine"], ["widevine", "playready"]),
    },
    "dual, playready empty": {
        "dl": ("wv_dev", "wv_dev", None, None),
        "load": ("wv_dev", "wv_dev", "stub:wv", "stub:wv"),
        "stub": ("stub:wv", "stub:wv", "stub:wv", "stub:wv"),
        "device": ("wv_dev", "pr_tier", "wv_tier"),
        "settled": None,
        "both": False,
        "candidates": (["widevine", "playready"], ["playready", "widevine"], ["widevine", "playready"]),
    },
    "dual, playready first and empty": {
        "dl": (None, "wv_dev", None, None),
        "load": ("stub:wv", "wv_dev", "stub:wv", "stub:wv"),
        "stub": ("stub:wv", "stub:wv", "stub:wv", "stub:wv"),
        "device": ("wv_dev", "pr_tier", "wv_tier"),
        "settled": None,
        "both": False,
        "candidates": (["widevine", "playready"], ["playready", "widevine"], ["widevine", "playready"]),
    },
    "widevine and a default device": {
        "dl": ("wv_dev", "wv_dev", None, None),
        "load": ("wv_dev", "wv_dev", "stub:wv", "stub:wv"),
        "stub": ("stub:wv", "stub:wv", "stub:wv", "stub:wv"),
        "device": ("wv_dev", "pr_dev", "pr_dev"),
        "settled": "widevine",
        "both": False,
        "candidates": (["widevine", "playready"], ["widevine", "playready"], ["widevine", "playready"]),
    },
    "default entry string": {
        "dl": ("pr_def", "pr_def", "pr_def", "pr_def"),
        "load": ("pr_def", "stub:wv", "pr_def", "pr_def"),
        "stub": (None, "stub:wv", "stub:pr", "stub:wv"),
        "device": ("wv_tier", "pr_tier", "wv_tier"),
        "settled": None,
        "both": False,
        "candidates": (["widevine", "playready"], ["playready", "widevine"], ["widevine", "playready"]),
    },
    "default entry dual": {
        "dl": ("wv_def", "wv_def", "pr_def", "pr_def"),
        "load": ("wv_def", "wv_def", "pr_def", "pr_def"),
        "stub": (None, "stub:wv", "stub:pr", "stub:wv"),
        "device": ("wv_tier", "pr_tier", "wv_tier"),
        "settled": None,
        "both": True,
        "candidates": (["widevine", "playready"], ["playready", "widevine"], ["widevine", "playready"]),
    },
    "default entry widevine only": {
        "dl": ("wv_def", "wv_def", None, None),
        "load": ("wv_def", "wv_def", "stub:pr", "stub:wv"),
        "stub": (None, "stub:wv", "stub:pr", "stub:wv"),
        "device": ("wv_tier", "pr_tier", "wv_tier"),
        "settled": None,
        "both": False,
        "candidates": (["widevine", "playready"], ["playready", "widevine"], ["widevine", "playready"]),
    },
    "quality tiers, cdm.default": {
        "dl": ("pr_def", "pr_def", "pr_def", "pr_def"),
        "load": ("pr_def", "stub:wv", "pr_def", "pr_def"),
        "stub": ("stub:wv", "stub:wv", "stub:wv", "stub:wv"),
        "device": ("pr_def", "pr_def", "pr_def"),
        "settled": "playready",
        "both": False,
        "candidates": (["playready"], ["playready"], ["playready"]),
    },
    "quality tiers, no default": {
        "dl": (None, None, None, None),
        "load": ("stub:wv", "stub:wv", "stub:wv", "stub:wv"),
        "stub": ("stub:wv", "stub:wv", "stub:wv", "stub:wv"),
        "device": ("wv_tier", "pr_tier", "wv_tier"),
        "settled": None,
        "both": False,
        "candidates": (["widevine", "playready"], ["playready", "widevine"], ["widevine", "playready"]),
    },
    "quality key read as a number": {
        "dl": ("AttributeError", "AttributeError", "AttributeError", "AttributeError"),
        "load": ("AttributeError", "AttributeError", "AttributeError", "AttributeError"),
        "stub": ("AttributeError", "AttributeError", "AttributeError", "AttributeError"),
        "device": ("AttributeError", "AttributeError", "AttributeError"),
        "settled": "AttributeError",
        "both": "AttributeError",
        "candidates": ("AttributeError", "AttributeError", "AttributeError"),
    },
    "missing entry": {
        "dl": (None, None, None, None),
        "load": (None, "stub:wv", "stub:pr", "stub:wv"),
        "stub": (None, "stub:wv", "stub:pr", "stub:wv"),
        "device": ("wv_tier", "pr_tier", "wv_tier"),
        "settled": None,
        "both": False,
        "candidates": (["widevine", "playready"], ["playready", "widevine"], ["widevine", "playready"]),
    },
    "per-key view replaces the entry": {
        "dl": ("wv_dev", "wv_dev", "pr_dev", "pr_dev"),
        "load": ("pr_key", "stub:wv", "pr_key", "pr_key"),
        "stub": ("stub:pr", "stub:pr", "stub:pr", "stub:pr"),
        "device": ("pr_key", "pr_key", "pr_key"),
        "settled": "playready",
        "both": False,
        "candidates": (["playready"], ["playready"], ["playready"]),
    },
    "per-key view, other service": {
        "dl": ("wv_dev", "wv_dev", "wv_dev", "wv_dev"),
        "load": ("wv_dev", "wv_dev", "stub:pr", "stub:pr"),
        "stub": ("stub:wv", "stub:wv", "stub:wv", "stub:wv"),
        "device": ("wv_dev", "wv_dev", "wv_dev"),
        "settled": "widevine",
        "both": False,
        "candidates": (["widevine"], ["widevine"], ["widevine"]),
    },
    "per-key view, dual": {
        "dl": ("wv_dev", "wv_dev", "wv_dev", "wv_dev"),
        "load": ("wv_dev", "wv_dev", "pr_dev", "pr_dev"),
        "stub": ("stub:wv", "stub:wv", "stub:wv", "stub:wv"),
        "device": ("wv_dev", "pr_dev", "wv_tier"),
        "settled": None,
        "both": True,
        "candidates": (["widevine", "playready"], ["playready", "widevine"], ["widevine", "playready"]),
    },
}


def test_every_row_has_a_pinned_answer() -> None:
    assert ROWS.keys() == EXPECTED.keys()


@pytest.mark.parametrize("name", ROWS)
def test_each_ladder_gives_the_pinned_choice(monkeypatch: pytest.MonkeyPatch, devices: None, name: str) -> None:
    cdm, profile, key_cdm = ROWS[name]
    assert answers(monkeypatch, cdm, profile, key_cdm) == EXPECTED[name]


@pytest.mark.parametrize(
    ("name", "system"),
    [("wv", "widevine"), ("PlayReady", "playready"), ("PR", "playready"), ("clearkey", None), (5, None), (None, None)],
)
def test_drm_system_reads_each_name_of_a_system(name: Any, system: str | None) -> None:
    assert drm_system(name) == system


@pytest.mark.parametrize(
    ("entry", "system"),
    [({"widevine": "wv_dev"}, "widevine"), (DUAL, None), ({"default": "pr_dev"}, None), ("pr_dev", None)],
)
def test_the_entry_settles_a_system_only_when_it_names_one(entry: Any, system: str | None) -> None:
    assert choose_cdm({"EXAMPLE": entry}, "EXAMPLE").system == system
