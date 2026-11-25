"""A real remote session entry for the tests that call the session and licence handlers directly."""

from types import SimpleNamespace
from typing import Any

from unshackle.core.api.session_store import SessionEntry


def session_entry(**fields: Any) -> SessionEntry:
    """A remote session that the server CDM licenses, unless *fields* set another :class:`SessionEntry` value."""
    return SessionEntry(
        **{
            "session_id": "sess",
            "service_tag": "EXAMPLE",
            "service_instance": SimpleNamespace(),
            "server_cdm": True,
            **fields,
        }
    )
