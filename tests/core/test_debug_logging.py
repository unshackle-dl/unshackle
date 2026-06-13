"""Unit tests for the structured debug logger: JSONL output, secret redaction / key gating,
the log_event / timed_operation primitives, the convenience-helper message override (regression
for the hardcoded-message TypeError footgun), and tool_run logging."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unshackle.core.utilities import (close_debug_logger, get_debug_logger, init_debug_logger, log_event,
                                      timed_operation)
from unshackle.core.utils.redact import redact_all, redact_path, redact_text, redact_url, safe_display_url
from unshackle.core.utils.subprocess import log_tool_run

pytestmark = pytest.mark.unit


def read_entries(path: Path) -> list[dict]:
    """Parse a JSONL debug log into a list of dicts (proves every line is valid JSON)."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def find(entries: list[dict], operation: str) -> dict:
    matches = [e for e in entries if e.get("operation") == operation]
    assert matches, f"no entry with operation={operation!r} (have {[e.get('operation') for e in entries]})"
    return matches[0]


@pytest.fixture
def debug_log(tmp_path: Path):
    """Enabled debug logger writing to a temp JSONL file; torn down after each test."""
    path = tmp_path / "debug.jsonl"
    init_debug_logger(log_path=path, enabled=True)
    yield path
    close_debug_logger()


@pytest.fixture
def debug_log_with_keys(tmp_path: Path):
    """Enabled debug logger with log_keys=True (mirrors config.debug_keys)."""
    path = tmp_path / "debug_keys.jsonl"
    init_debug_logger(log_path=path, enabled=True, log_keys=True)
    yield path
    close_debug_logger()


# ---------- JSONL output shape ----------


def test_every_line_is_valid_json_with_required_fields(debug_log: Path):
    log_event("sample_event", message="hi", context={"a": 1})
    close_debug_logger()  # flush session_end
    entries = read_entries(debug_log)
    assert entries, "expected at least one JSONL entry"
    for e in entries:
        assert {"timestamp", "session_id", "level", "operation"} <= e.keys()
    assert {"session_start", "session_end"} <= {e["operation"] for e in entries}


def test_session_id_is_stable_across_entries(debug_log: Path):
    log_event("a")
    log_event("b")
    close_debug_logger()
    ids = {e["session_id"] for e in read_entries(debug_log)}
    assert len(ids) == 1


# ---------- log_event ----------


def test_log_event_emits_with_level_message_and_context(debug_log: Path):
    log_event("feature_done", level="INFO", message="did it", context={"count": 3})
    e = find(read_entries(debug_log), "feature_done")
    assert e["level"] == "INFO"
    assert e["message"] == "did it"
    assert e["context"] == {"count": 3}


def test_log_event_noop_when_disabled():
    close_debug_logger()
    assert get_debug_logger() is None
    log_event("should_not_explode", message="no logger")  # must not raise


# ---------- timed_operation ----------


def test_timed_operation_logs_success_and_duration(debug_log: Path):
    with timed_operation("work", level="INFO", context={"k": "v"}):
        pass
    e = find(read_entries(debug_log), "work")
    assert e["level"] == "INFO"
    assert e["success"] is True
    assert isinstance(e["duration_ms"], (int, float))
    assert e["context"] == {"k": "v"}


def test_timed_operation_logs_error_and_reraises(debug_log: Path):
    with pytest.raises(ValueError, match="boom"):
        with timed_operation("work_fail"):
            raise ValueError("boom")
    e = find(read_entries(debug_log), "work_fail")
    assert e["level"] == "ERROR"
    assert e["success"] is False
    assert "duration_ms" in e
    assert e["error"]["type"] == "ValueError"


def test_timed_operation_noop_when_disabled():
    close_debug_logger()
    with timed_operation("disabled_block"):  # must not raise, must still run body
        value = 1 + 1
    assert value == 2


# ---------- secret redaction / key gating ----------


def test_sanitize_redacts_sensitive_key_names(debug_log: Path):
    dl = get_debug_logger()
    cleaned = dl.sanitize_data(
        {
            "password": "hunter2",
            "token": "abc",
            "auth": "bearer x",
            "cookie": "sess=1",
            "secret": "s",
            "has_password": True,  # has_ prefix stays
            "quality": "1080p",  # non-secret stays
        }
    )
    assert cleaned["password"] == "[REDACTED]"
    assert cleaned["token"] == "[REDACTED]"
    assert cleaned["auth"] == "[REDACTED]"
    assert cleaned["cookie"] == "[REDACTED]"
    assert cleaned["secret"] == "[REDACTED]"
    assert cleaned["has_password"] is True
    assert cleaned["quality"] == "1080p"


def test_sanitize_gates_key_fields_unless_log_keys(debug_log: Path):
    dl = get_debug_logger()  # log_keys=False
    cleaned = dl.sanitize_data({"content_key": "deadbeef", "kid": "00112233", "key_found": True, "key_count": 2})
    assert cleaned["content_key"] == "[REDACTED]"  # redacted when log_keys False
    assert cleaned["kid"] == "00112233"  # kid whitelisted
    assert cleaned["key_found"] is True  # key_found whitelisted
    assert cleaned["key_count"] == 2  # _count whitelisted


def test_sanitize_keeps_keys_when_log_keys_enabled(debug_log_with_keys: Path):
    dl = get_debug_logger()  # log_keys=True
    cleaned = dl.sanitize_data({"content_key": "deadbeef"})
    assert cleaned["content_key"] == "deadbeef"


def test_sanitize_masks_query_param_secrets_in_nonurl_string(debug_log: Path):
    dl = get_debug_logger()
    cleaned = dl.sanitize_data({"blob": "creds token=abc password=hunter2"})
    assert "token=***" in cleaned["blob"]
    assert "password=***" in cleaned["blob"]
    assert "hunter2" not in cleaned["blob"]


def test_sanitize_collapses_urls(debug_log: Path):
    dl = get_debug_logger()
    cleaned = dl.sanitize_data(
        {
            "segment": "https://cdn.example.net/a/b/x_audio_144.mp4?token=abc",
            "manifest": "https://cdn.example.net/v/manifest.mpd",
            "proxy": "http://bob:secret@proxy.example:8080/x",
        }
    )
    assert cleaned["segment"] == "redacted.mp4"  # extension kept, host/path/query gone
    assert cleaned["manifest"] == "redacted.mpd"
    assert cleaned["proxy"] == "redacted"  # userinfo + host never reaches disk


def test_logged_content_url_is_collapsed_on_disk(debug_log: Path):
    log_event(
        "downloader_start",
        context={"first_url": "https://user:pw@cdn.example.net/seg_144.mp4?api_key=zzz"},
    )
    close_debug_logger()
    raw = debug_log.read_text(encoding="utf-8")
    assert "cdn.example.net" not in raw
    assert "user:pw" not in raw
    assert "api_key=zzz" not in raw
    assert "redacted.mp4" in raw


# ---------- redact.py helpers ----------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://u:p@h/x", "https://***@h/x"),
        ("https://h/x?token=abc&q=1", "https://h/x?token=***&q=1"),
        ("https://h/x?password=p", "https://h/x?password=***"),
        ("nothing secret here", "nothing secret here"),
    ],
)
def test_redact_text(text: str, expected: str):
    assert redact_text(text) == expected


def test_redact_text_masks_known_secrets_longest_first():
    out = redact_text("a=foobar b=foo", secrets=["foo", "foobar"])
    assert "foobar" not in out
    assert out == "a=*** b=***"


def test_safe_display_url_strips_userinfo_and_query():
    assert safe_display_url("https://u:p@host:8080/a/b?token=x&y=2") == "https://host:8080/a/b"


# ---------- redact_url ----------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://cdn.x/a/seg_144.mp4?t=1", "redacted.mp4"),
        ("https://cdn.x/v/manifest.mpd", "redacted.mpd"),
        ("https://cdn.x/v/playlist.m3u8", "redacted.m3u8"),
        ("http://u:p@cdn.x/seg.m4s", "redacted.m4s"),
        ("https://api.example/auth/token", "redacted"),  # no extension
    ],
)
def test_redact_url_collapses_to_redacted_ext(url: str, expected: str):
    assert redact_url(url) == expected


def test_redact_url_leaves_nonurl_untouched():
    assert redact_url("just some text, no url") == "just some text, no url"


def test_redact_url_handles_url_embedded_in_text():
    assert redact_url("got https://cdn.x/a.mp4 ok") == "got redacted.mp4 ok"


# ---------- redact_path ----------


def test_redact_path_strips_install_root():
    # redact.py resolves the project/install root as the <unshackle> anchor.
    from unshackle.core.utils import redact as redact_mod

    root = str(Path(redact_mod.__file__).resolve().parents[3])
    assert redact_path(f"{root}/temp/Audio.mp4") == "<unshackle>/temp/Audio.mp4"


def test_redact_path_strips_home_dir():
    home = str(Path.home())
    assert redact_path(f"{home}/elsewhere/x").startswith("~/")


def test_redact_path_leaves_relative_paths_untouched():
    assert redact_path("temp/Audio.mp4") == "temp/Audio.mp4"


# ---------- redact_all + output_dir integration ----------


def test_redact_all_composes_secrets_urls_and_paths():
    root = str(Path(__import__("unshackle.core.utils.redact", fromlist=["x"]).__file__).resolve().parents[3])
    out = redact_all(f"dl https://u:p@cdn.x/seg.mp4?token=t into {root}/temp")
    assert "cdn.x" not in out
    assert "redacted.mp4" in out
    assert "<unshackle>/temp" in out


def test_logged_output_dir_is_path_redacted(debug_log: Path):
    root = str(Path(__import__("unshackle.core.utils.redact", fromlist=["x"]).__file__).resolve().parents[3])
    log_event("downloader_start", context={"output_dir": f"{root}/temp", "filename": "Audio_3eeab4ed.mp4"})
    e = find(read_entries(debug_log), "downloader_start")
    assert e["context"]["output_dir"] == "<unshackle>/temp"
    assert e["context"]["filename"] == "Audio_3eeab4ed.mp4"  # bare filename untouched


# ---------- convenience-helper message override (footgun regression) ----------


def test_log_drm_operation_accepts_message_and_level_override(debug_log: Path):
    dl = get_debug_logger()
    # Previously raised: TypeError: log() got multiple values for keyword argument 'message'
    dl.log_drm_operation("Widevine", "license_request", level="INFO", message="custom msg", kid="abc")
    e = find(read_entries(debug_log), "drm_license_request")
    assert e["message"] == "custom msg"
    assert e["level"] == "INFO"
    assert e["drm_type"] == "Widevine"


def test_log_drm_operation_default_message(debug_log: Path):
    get_debug_logger().log_drm_operation("PlayReady", "license_response")
    e = find(read_entries(debug_log), "drm_license_response")
    assert e["message"] == "PlayReady license_response"


def test_log_vault_query_accepts_message_override(debug_log: Path):
    get_debug_logger().log_vault_query("local", "get_key", message="hit", key_found=True)
    e = find(read_entries(debug_log), "vault_get_key")
    assert e["message"] == "hit"
    assert e["vault"] == "local"


def test_log_service_call_accepts_message_override(debug_log: Path):
    get_debug_logger().log_service_call("POST", "https://api.example/x", message="license", status=200)
    e = find(read_entries(debug_log), "service_call")
    assert e["message"] == "license"
    assert e["request"]["method"] == "POST"
    assert e["request"]["status"] == 200


# ---------- tool_run ----------


def test_log_tool_run_success_is_debug(debug_log: Path):
    log_tool_run("ffmpeg concat", "ffmpeg", 0, duration_ms=12.3, segments=5)
    e = find(read_entries(debug_log), "tool_run")
    assert e["level"] == "DEBUG"
    assert e["context"]["tool"] == "ffmpeg"
    assert e["context"]["returncode"] == 0
    assert e["context"]["segments"] == 5


def test_log_tool_run_failure_is_error(debug_log: Path):
    log_tool_run("mkvpropedit tags", "mkvpropedit", 2)
    e = find(read_entries(debug_log), "tool_run")
    assert e["level"] == "ERROR"
    assert "failed" in e["message"]


def test_log_tool_run_noop_when_disabled():
    close_debug_logger()
    log_tool_run("ffmpeg", "ffmpeg", 0)  # must not raise
