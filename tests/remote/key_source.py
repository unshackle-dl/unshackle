import logging
from types import SimpleNamespace
from typing import Any, Iterable, Optional
from uuid import UUID

from unshackle.core.remote_service import RemoteService, ServerVault


def remote_keys(
    server_cdm: Optional[bool] = True,
    server_cdm_type: str = "widevine",
    client_licensed: Iterable[str] = (),
    local_cdm: Any = None,
    server_vault_keys: Optional[dict[UUID, str]] = None,
) -> RemoteService:
    """A RemoteService with only the state dl reads through ServerKeySource, and no server."""
    svc = RemoteService.__new__(RemoteService)
    svc._server_cdm = server_cdm
    svc.server_cdm_type = server_cdm_type
    svc.client_licensed = set(client_licensed)
    svc.deferred_refusals = {}
    svc.server_vault_keys = dict(server_vault_keys or {})
    svc.server_vault = ServerVault(svc)
    svc.ctx = SimpleNamespace(obj=SimpleNamespace(cdm=local_cdm, cdm_for=None))
    return svc


CAPPED = {"keys": {}, "capped_tracks": {"vid": {"reason": "server_cdm_max_height", "max_height": 1080}}}
PR_DEVICE = SimpleNamespace(is_playready=True, security_level=3000)


def remote_session(local_cdm: Any, answer: dict) -> RemoteService:
    """A server-CDM RemoteService whose every POST records its payload in ``posts`` and returns ``answer``."""
    svc = remote_keys(server_cdm_type="playready", local_cdm=local_cdm)
    svc._track_alias = {}
    svc._title_alias = {}
    svc._session_id = "sess"
    svc.log = logging.getLogger("test-remote-session")
    posts: list = []
    svc.client = SimpleNamespace(post=lambda endpoint, payload, expect=(): posts.append(payload) or answer)
    svc.drain_server_logs = lambda: None
    svc.posts = posts
    return svc


class OneTrack(list):
    @property
    def videos(self) -> list:
        return list(self)

    @property
    def audio(self) -> list:
        return []


def one_title(track: Any) -> SimpleNamespace:
    """A title holding only ``track``, as a video."""
    return SimpleNamespace(tracks=OneTrack([track]))
