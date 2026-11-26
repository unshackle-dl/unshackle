"""The remote session protocol: what a remote client sends must be what the server serves."""

from __future__ import annotations

import ast
import functools
from pathlib import Path
from typing import Any

import pytest
import yaml

from unshackle.core import remote_channel
from unshackle.core import remote_protocol as protocol
from unshackle.core import remote_service
from unshackle.core.api import routes
from unshackle.core.api.handlers import SESSION_TRANSPORT_KEYS

ROUTE_NAMES = {name for name in vars(protocol) if name.endswith("_ROUTE")}
CLIENT_METHODS = {
    "get": "GET",
    "get_optional": "GET",
    "probe": "GET",
    "post": "POST",
    "post_optional": "POST",
    "delete": "DELETE",
    "ws_connect": "GET",
}
SERVED = {(method, path): handler for method, path, handler, _ in routes.ROUTES}


@functools.cache
def client_tree() -> ast.Module:
    """The client modules as one tree: the remote service and the session channel adapters."""
    modules = [ast.parse(Path(m.__file__).read_text(encoding="utf-8")) for m in (remote_service, remote_channel)]
    return ast.Module(body=[node for module in modules for node in module.body], type_ignores=[])


def client_calls() -> tuple[set[tuple[str, str]], set[str]]:
    """The (method, route) pairs the client sends, and every route name the client module reads.

    A URL a function keeps in a local variable counts at the call that sends it.
    """
    tree = client_tree()
    referenced = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in ROUTE_NAMES}
    calls: set[tuple[str, str]] = set()
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        local = {
            target.id: node.value
            for node in ast.walk(function)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for call in ast.walk(function):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.args):
                continue
            if call.func.attr in ("request", "send") and isinstance(call.args[0], ast.Constant) and len(call.args) > 1:
                method, target = str(call.args[0].value).upper(), call.args[1]
            elif call.func.attr in CLIENT_METHODS:
                method, target = CLIENT_METHODS[call.func.attr], call.args[0]
            else:
                continue
            if isinstance(target, ast.Name):
                target = local.get(target.id, target)
            calls |= {
                (method, getattr(protocol, n.id))
                for n in ast.walk(target)
                if isinstance(n, ast.Name) and n.id in ROUTE_NAMES
            }
    return calls, referenced


def test_the_client_writes_no_api_path_of_its_own():
    """A path the client writes out by hand escapes the route check below."""
    tree = client_tree()
    docstrings = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Expr)}
    assert not [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "/api/" in node.value
        and id(node) not in docstrings
    ]


def test_every_route_the_client_calls_is_served_with_that_method():
    calls, referenced = client_calls()
    assert calls, "the scan found no client calls"
    assert {path for _, path in calls} >= {getattr(protocol, name) for name in referenced}
    assert calls <= SERVED.keys()


def test_the_server_takes_its_remote_session_routes_from_the_protocol():
    protocol_paths = {getattr(protocol, name) for name in ROUTE_NAMES}
    served_paths = {path for _, path in SERVED}
    assert protocol_paths <= served_paths
    assert {path for path in served_paths if path.startswith("/api/session")} <= protocol_paths


def test_the_wire_values_stay_what_older_peers_send():
    """Both halves read one record, so only this list catches a renamed value that an older peer still sends."""
    assert {name: getattr(protocol, name) for name in ROUTE_NAMES} == {
        "HEALTH_ROUTE": "/api/health",
        "SERVICES_ROUTE": "/api/services",
        "SEARCH_ROUTE": "/api/search",
        "SESSION_CREATE_ROUTE": "/api/session/create",
        "SESSION_ROUTE": "/api/session/{session_id}",
        "TITLES_ROUTE": "/api/session/{session_id}/titles",
        "TRACKS_ROUTE": "/api/session/{session_id}/tracks",
        "SEGMENTS_ROUTE": "/api/session/{session_id}/segments",
        "SEGMENT_FILTER_ROUTE": "/api/session/{session_id}/segment_filter",
        "SAMPLES_ROUTE": "/api/session/{session_id}/samples",
        "LICENSE_ROUTE": "/api/session/{session_id}/license",
        "KEYS_ROUTE": "/api/session/{session_id}/keys",
        "BAD_KEY_ROUTE": "/api/session/{session_id}/keys/bad",
        "LOGS_ROUTE": "/api/session/{session_id}/logs",
        "PROMPT_ROUTE": "/api/session/{session_id}/prompt",
        "CHANNEL_ROUTE": "/api/session/{session_id}/channel",
    }
    assert [m.value for m in protocol.ChannelMessage] == ["prompt", "cdm_call", "log", "status", "drain", "answer"]
    assert protocol.CHANNEL_SESSION_GONE == 4404
    assert [s.value for s in protocol.AuthStatus] == ["authenticating", "pending_input", "authenticated", "failed"]
    assert {c.name for c in protocol.APIErrorCode} >= {"SESSION_NOT_FOUND", "SERVER_CDM_CAPPED", "SERVER_DRAINING"}
    assert [k.value for k in protocol.ClientErrorKind] == [
        "CONNECT",
        "TIMEOUT",
        "AUTH",
        "TUNNEL_CLOSED",
        "GRANTS_CHANGED",
        "UNKNOWN",
    ]


def test_session_transport_keys_are_the_create_body():
    """The server keeps these out of the flat service options; it reads them from the session create body."""
    assert SESSION_TRANSPORT_KEYS == {
        "service",
        "title_id",
        "profile",
        "service_params",
        "season",
        "episode",
        "part",
        "wanted",
        "proxy",
        "no_proxy",
        "credentials",
        "cookies",
        "cache",
        "client_region",
        "proxy_region",
        "server_cdm",
        "cdm_type",
        "cdm_security_level",
        "cdm_relay",
        "cdm_system_id",
        "cdm_device_type",
        "service_config",
        "range_",
        "vcodec",
        "quality",
        "best_available",
        "generate_dv",
        "dl_params",
    }


@pytest.mark.parametrize(
    "shape,fields",
    [
        (protocol.ChannelPrompt, {"type", "id", "prompt", "qr"}),
        (protocol.ChannelCdmCall, {"type", "id", "call"}),
        (protocol.ChannelLog, {"type", "seq", "level", "message", "ts", "qr"}),
        (protocol.ChannelStatus, {"type", "status", "error"}),
        (protocol.ChannelDrain, {"type", "draining"}),
        (protocol.ChannelAnswer, {"type", "id", "response", "error"}),
    ],
)
def test_the_channel_messages_keep_their_fields(shape, fields):
    """A session channel message is a JSON text frame; an older peer reads these keys."""
    assert keys(shape) == fields


def test_a_client_error_kind_is_never_a_server_error_code():
    assert not {k.value for k in protocol.ClientErrorKind} & {c.value for c in protocol.APIErrorCode}


def keys(shape: Any) -> set[str]:
    return set(shape.__required_keys__ | shape.__optional_keys__)


def properties(schema: dict | None) -> set[str]:
    return set(
        ((schema or {}).get("content", {}).get("application/json", {}).get("schema", {}) or {}).get("properties", {})
    )


@pytest.mark.parametrize(
    "method,route,request_shape,reply_shape",
    [
        ("POST", protocol.SESSION_CREATE_ROUTE, protocol.SessionCreate, protocol.SessionCreated),
        ("POST", protocol.TRACKS_ROUTE, protocol.TracksRequest, None),
        ("POST", protocol.SEGMENT_FILTER_ROUTE, protocol.SegmentFilterRequest, protocol.SegmentFilterReply),
        ("POST", protocol.LICENSE_ROUTE, protocol.LicenseRequest, protocol.LicenseReply),
        ("POST", protocol.KEYS_ROUTE, protocol.TrackKeys, None),
        ("POST", protocol.BAD_KEY_ROUTE, protocol.BadKeyReport, None),
        ("GET", protocol.LOGS_ROUTE, None, protocol.LogsReply),
        ("GET", protocol.PROMPT_ROUTE, None, protocol.PromptReply),
        ("POST", protocol.PROMPT_ROUTE, protocol.PromptAnswer, None),
        ("GET", protocol.SESSION_ROUTE, None, protocol.SessionInfo),
    ],
)
def test_the_openapi_yaml_names_only_protocol_keys(method, route, request_shape, reply_shape):
    """The Swagger docs of a route list a subset of the keys its protocol shapes declare."""
    spec = yaml.safe_load((SERVED[(method, route)].__doc__ or "").split("---", 1)[1])
    if request_shape:
        assert properties(spec.get("requestBody")) <= keys(request_shape)
    if reply_shape:
        assert properties(spec["responses"].get("200")) <= keys(reply_shape)
