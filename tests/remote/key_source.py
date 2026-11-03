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
    svc.server_vault_keys = dict(server_vault_keys or {})
    svc.server_vault = ServerVault(svc)
    svc.ctx = SimpleNamespace(obj=SimpleNamespace(cdm=local_cdm))
    return svc
