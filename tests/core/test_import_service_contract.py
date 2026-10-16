"""ImportService and RemoteService stand in for a Service in dl, so they have every method dl calls on one."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from unshackle.commands import dl as dl_module
from unshackle.core.import_service import ImportService
from unshackle.core.remote_service import RemoteService


@pytest.mark.parametrize("cls", [ImportService, RemoteService])
def test_stand_in_service_has_every_method_dl_calls(cls: type) -> None:
    called = set(re.findall(r"\bservice\.(\w+)\(", Path(dl_module.__file__).read_text(encoding="utf-8")))
    assert called
    assert sorted(name for name in called if not hasattr(cls, name)) == []
