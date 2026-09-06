"""Pins per-service decryption-tool and CDM resolution, including key casing.

A per-service `cdm:`/`decryption:` entry must resolve regardless of the key's case vs the
service tag, falling back to the configured default otherwise. Guards the silent
"falls back to default on a case mismatch" foot-gun.
"""

from __future__ import annotations

import pytest

from unshackle.core.config import Config, resolve_cdm_name, resolve_decryption
from unshackle.core.utils.collections import ci_get

pytestmark = pytest.mark.unit


def test_ci_get_exact_and_case_insensitive():
    m = {"DEMO": "a", "Default": "b"}
    assert ci_get(m, "DEMO") == "a"
    assert ci_get(m, "demo") == "a"
    assert ci_get(m, "default") == "b"
    assert ci_get(m, "missing") is None
    assert ci_get(m, "missing", "fallback") == "fallback"
    assert ci_get({}, "DEMO", "fallback") == "fallback"


def test_decryption_map_keys_upper_cased_on_build():
    c = Config(decryption={"example": "mp4decrypt", "default": "shaka"})
    assert c.decryption_map == {"EXAMPLE": "mp4decrypt", "DEFAULT": "shaka"}
    assert c.decryption == "shaka"


def test_resolve_decryption_per_service_and_default():
    c = Config(decryption={"EXAMPLE": "mp4decrypt", "default": "shaka"})
    assert resolve_decryption(c.decryption_map, c.decryption, "EXAMPLE") == "mp4decrypt"
    # service not mapped -> default
    assert resolve_decryption(c.decryption_map, c.decryption, "DEMO") == "shaka"
    # case-insensitive both ways (lowercase yaml key, mixed-case lookup)
    c2 = Config(decryption={"example": "mp4decrypt", "default": "shaka"})
    assert resolve_decryption(c2.decryption_map, c2.decryption, "Example") == "mp4decrypt"


def test_decryption_scalar_form():
    c = Config(decryption="mp4decrypt")
    assert c.decryption_map == {}
    assert resolve_decryption(c.decryption_map, c.decryption, "ANY") == "mp4decrypt"


def test_cdm_keys_preserved_on_build():
    c = Config(cdm={"demo": "dev_a", "default": "dev_b"})
    assert c.cdm == {"demo": "dev_a", "default": "dev_b"}


def test_resolve_cdm_name_case_insensitive():
    # lowercase yaml key must resolve for an uppercase service tag (the bug being fixed)
    c = Config(cdm={"demo": "dev_a", "default": "dev_b"})
    assert resolve_cdm_name(c.cdm, "DEMO") == "dev_a"
    # unmapped service -> default
    assert resolve_cdm_name(c.cdm, "EXAMPLE") == "dev_b"
    # uppercase yaml key, lowercase lookup
    c2 = Config(cdm={"DEMO": "dev_a", "default": "dev_b"})
    assert resolve_cdm_name(c2.cdm, "demo") == "dev_a"


def test_resolve_cdm_name_override_wins():
    c = Config(cdm={"DEMO": "dev_a", "default": "dev_b"})
    assert resolve_cdm_name(c.cdm, "DEMO", override="dev_override") == "dev_override"


def test_resolve_cdm_name_no_match_no_default():
    c = Config(cdm={"DEMO": "dev_a"})
    assert resolve_cdm_name(c.cdm, "EXAMPLE") is None
