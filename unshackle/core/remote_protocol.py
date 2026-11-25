"""The remote session protocol: the routes, login states, error codes and message shapes of client and server.

``dl --remote`` (:mod:`unshackle.core.remote_service`) and the server (:mod:`unshackle.core.api`) both read this
record. Titles, tracks and DRM keep the form of :mod:`unshackle.core.wire`, and ``dl_params`` that of
:mod:`unshackle.core.dl_options`. Every name and value here is on the wire, so a change must stay readable by an
older peer.
"""

from __future__ import annotations

from enum import Enum, StrEnum
from typing import Any, NotRequired, Optional, TypedDict

HEALTH_ROUTE = "/api/health"
SERVICES_ROUTE = "/api/services"
SEARCH_ROUTE = "/api/search"
SESSION_CREATE_ROUTE = "/api/session/create"
SESSION_ROUTE = "/api/session/{session_id}"
TITLES_ROUTE = SESSION_ROUTE + "/titles"
TRACKS_ROUTE = SESSION_ROUTE + "/tracks"
SEGMENTS_ROUTE = SESSION_ROUTE + "/segments"
SEGMENT_FILTER_ROUTE = SESSION_ROUTE + "/segment_filter"
SAMPLES_ROUTE = SESSION_ROUTE + "/samples"
LICENSE_ROUTE = SESSION_ROUTE + "/license"
KEYS_ROUTE = SESSION_ROUTE + "/keys"
BAD_KEY_ROUTE = SESSION_ROUTE + "/keys/bad"
LOGS_ROUTE = SESSION_ROUTE + "/logs"
PROMPT_ROUTE = SESSION_ROUTE + "/prompt"
CHANNEL_ROUTE = SESSION_ROUTE + "/channel"

CHANNEL_SESSION_GONE = 4404
"""The close code of a session channel whose remote session the server removed: deleted, expired, evicted or stopped."""


class AuthStatus(StrEnum):
    """Authentication lifecycle states for a remote session, as the ``status`` of a prompt reply."""

    AUTHENTICATING = "authenticating"
    PENDING_INPUT = "pending_input"
    AUTHENTICATED = "authenticated"
    FAILED = "failed"


class APIErrorCode(str, Enum):
    """Standard API error codes for programmatic error handling."""

    INVALID_INPUT = "INVALID_INPUT"
    INVALID_SERVICE = "INVALID_SERVICE"
    INVALID_PROXY = "INVALID_PROXY"
    INVALID_PARAMETERS = "INVALID_PARAMETERS"

    AUTH_FAILED = "AUTH_FAILED"
    FORBIDDEN = "FORBIDDEN"
    SERVER_CDM_CAPPED = "SERVER_CDM_CAPPED"
    GEOFENCE = "GEOFENCE"

    NOT_FOUND = "NOT_FOUND"
    NO_CONTENT = "NO_CONTENT"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    TRACK_NOT_FOUND = "TRACK_NOT_FOUND"

    CONFLICT = "CONFLICT"

    RATE_LIMITED = "RATE_LIMITED"

    INTERNAL_ERROR = "INTERNAL_ERROR"
    SERVICE_ERROR = "SERVICE_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    DRM_ERROR = "DRM_ERROR"
    DOWNLOAD_ERROR = "DOWNLOAD_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    SERVER_DRAINING = "SERVER_DRAINING"
    WORKER_ERROR = "WORKER_ERROR"


class ClientErrorKind(StrEnum):
    """The error kinds a remote client gives a failure of its own. The server never sends them.

    They share a namespace with :class:`APIErrorCode`, because ``RemoteError.kind`` holds either one.
    """

    CONNECT = "CONNECT"
    TIMEOUT = "TIMEOUT"
    AUTH = "AUTH"
    TUNNEL_CLOSED = "TUNNEL_CLOSED"
    GRANTS_CHANGED = "GRANTS_CHANGED"
    UNKNOWN = "UNKNOWN"


class ClientLogin(TypedDict, total=False):
    """The login material a client sends with a remote session or a search."""

    credentials: dict[str, Any]
    cookies: str
    cache: dict[str, str]


class ClientInfo(TypedDict):
    """What a client tells the server about itself, for the server's logs."""

    version: str
    code_hash: Optional[str]
    platform: str
    argv: str


class SessionCreate(ClientLogin, total=False):
    """The body of a remote session creation."""

    service: str
    title_id: str
    profile: str
    service_params: dict[str, Any]
    season: Any
    episode: Any
    part: Any
    wanted: Any
    proxy: Optional[str]
    no_proxy: bool
    client_region: str
    proxy_region: str
    server_cdm: bool
    cdm_type: str
    cdm_security_level: int
    cdm_relay: bool
    cdm_system_id: int
    cdm_device_type: str
    service_config: dict[str, Any]
    range_: list[str]
    vcodec: list[str]
    quality: list[int]
    best_available: bool
    generate_dv: bool
    dl_params: dict[str, Any]
    client: ClientInfo


class SessionCreated(TypedDict, total=False):
    """The reply to a remote session creation. An older server leaves out the licensing fields."""

    session_id: str
    service: str
    status: str
    server_account: bool
    server_cdm: bool
    server_cdm_max_height: Optional[int]
    server_vault: bool
    server_device: bool


class TracksRequest(TypedDict):
    title_id: str


class TracksReply(TypedDict, total=False):
    """The remote session fields of a tracks reply, beside the title, tracks and chapters in the form of ``core.wire``."""

    session_headers: dict[str, str]
    session_cookies: dict[str, str]
    session_cookie_jar: list[dict[str, Any]]
    server_cdm: Optional[bool]
    server_cdm_type: str
    sample_decrypt_tracks: list[str]


class SegmentFilterRequest(TypedDict):
    track_id: str


class SegmentFilterReply(TypedDict):
    """The segment URIs to skip; None when the track has no segment filter."""

    unwanted: Optional[list[str]]


class LicenseRequest(TypedDict, total=False):
    """A licence relay (``challenge``), a certificate request, or a server CDM key request (``mode`` server_cdm)."""

    track_id: str
    track_ids: list[str]
    challenge: str
    drm_type: str
    mode: str
    pssh: str
    kid: str
    is_certificate: bool


class LicenseReply(TypedDict, total=False):
    """A licence, a certificate, or the content keys of the server CDM.

    ``keys`` maps KID to content key for one ``track_id``, and track id to that map for ``track_ids``. A capped
    track gets no content keys: ``capped_tracks`` holds its refusal details, ``reason`` and ``max_height``.
    """

    license: Optional[str]
    keys: dict[str, Any]
    vault_keys: list[str]
    clear_tracks: list[str]
    drm_type: str
    drm_types: dict[str, str]
    capped_tracks: dict[str, dict[str, Any]]


class BadKeyReport(TypedDict):
    kid: str
    key: str


class TrackKeys(TypedDict):
    track_id: str
    keys: dict[str, str]


class PromptReply(TypedDict, total=False):
    """The login state of a remote session, and the prompt or CDM call that waits for the client."""

    status: str
    prompt: Optional[str]
    cdm_call: dict[str, Any]
    error: str


class PromptAnswer(TypedDict):
    response: str


class LogRecord(TypedDict):
    seq: int
    level: str
    message: str
    ts: float


class LogsReply(TypedDict):
    session_id: str
    logs: list[LogRecord]
    last_seq: int


class TitlesReply(TypedDict):
    """The titles of a remote session, each in the form of ``core.wire``."""

    session_id: str
    titles: list[dict[str, Any]]


class SessionClosed(TypedDict, total=False):
    """The reply to a closed remote session, with the service cache files that changed while it ran."""

    status: str
    cache: dict[str, str]


class ChannelMessage(StrEnum):
    """The ``type`` of a remote session channel message. The server sends each type except ``answer``, which the client sends."""

    PROMPT = "prompt"
    CDM_CALL = "cdm_call"
    LOG = "log"
    STATUS = "status"
    DRAIN = "drain"
    ANSWER = "answer"


class ChannelPrompt(TypedDict):
    """A prompt for the user; the answer names its ``id``."""

    type: str
    id: str
    prompt: str


class ChannelCdmCall(TypedDict):
    """A call the service makes on the client's CDM, in the shape of the prompt route's ``cdm_call``."""

    type: str
    id: str
    call: dict[str, Any]


class ChannelLog(LogRecord):
    """One service log record, as the logs route gives it."""

    type: str


class ChannelStatus(TypedDict):
    """The login state of the remote session: on attach, and each time it changes."""

    type: str
    status: str
    error: NotRequired[str]


class ChannelDrain(TypedDict):
    """Whether the server restarts after its open sessions finish: on attach, at a drain, and when a drain stops."""

    type: str
    draining: bool


class ChannelAnswer(TypedDict):
    """The client's answer to a prompt or CDM call. A CDM call's answer is the JSON object as a string.

    ``error`` says why the client could not answer a prompt, so the service stops at once.
    """

    type: str
    id: str
    response: str
    error: NotRequired[str]


class SessionInfo(TypedDict, total=False):
    session_id: str
    service: str
    valid: bool
    expires_in: int
    draining: bool
    track_count: int
    title_count: int
