"""YAML reads a bare `no` or `yes` as a boolean, which would drop those languages."""

from pathlib import Path

import pytest
from langcodes import Language

from unshackle.core.config import Config
from unshackle.core.utils.language_tags import evaluate_language_tag

CONFIG = """
dl:
  lang: [no, en]
  s_lang: no
  a_lang: [yes]
  require_subs: no
  require_audio: [no]
  AMZN:
    lang: no
muxing:
  default_language:
    audio: no
audio:
  language_priority: [no, da]
language_tags:
  rules:
    - audio: no
      tag: NORDIC
update_checks: no
"""


def _load(tmp_path: Path) -> Config:
    path = tmp_path / "unshackle.yaml"
    path.write_text(CONFIG, encoding="utf8")
    return Config.from_yaml(path)


def test_bool_shaped_tags_survive_every_language_key(tmp_path: Path) -> None:
    config = _load(tmp_path)
    assert config.dl["lang"] == ["no", "en"]
    assert config.dl["s_lang"] == "no"
    assert config.dl["a_lang"] == ["yes"]
    assert config.dl["require_subs"] == "no"
    assert config.dl["require_audio"] == ["no"]
    assert config.dl["AMZN"]["lang"] == "no"
    assert config.muxing["default_language"]["audio"] == "no"
    assert config.audio["language_priority"] == ["no", "da"]


def test_a_real_boolean_option_stays_false(tmp_path: Path) -> None:
    assert _load(tmp_path).update_checks is False


def test_a_no_rule_tags_a_norwegian_track(tmp_path: Path) -> None:
    rules = _load(tmp_path).language_tags["rules"]
    assert evaluate_language_tag(rules, [Language.get("no")], []) == "NORDIC"


def test_tag_rules_keep_their_own_booleans(tmp_path: Path) -> None:
    """tag_rules matches text and warns about a bare bool itself."""
    path = tmp_path / "unshackle.yaml"
    path.write_text("tag_rules:\n  - when:\n      video: no\n    tag: T\n", encoding="utf8")
    assert Config.from_yaml(path).tag_rules[0]["when"]["video"] is False


def test_a_cross_type_template_variable_warns_at_load(tmp_path: Path) -> None:
    path = tmp_path / "unshackle.yaml"
    path.write_text('output_template:\n  movies: "{title}.{artist}"\n', encoding="utf8")
    with pytest.warns(UserWarning, match="not available in the movies template"):
        Config.from_yaml(path)
