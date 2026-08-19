"""SQLite vault table names resolve case-insensitively, and survive a `kv copy` round trip.

A vault table is named by the service's vault tag, whose case (and sometimes whose
spelling) differs from the installed service tag. Reads and writes have to find the real
table whatever its case, `get_services()` must yield names that can be fed straight back
in, and no lookup may create a second table differing only by case.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Iterator, Optional, Union
from uuid import UUID

import pytest

from unshackle.core.vault import Vault
from unshackle.vaults.SQLite import SQLite

LOG = logging.getLogger("kv-test")

KID = "0" * 31 + "1"
KEY = "a" * 32


def tables(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    finally:
        conn.close()


class UnlistableVault(Vault):
    """Stands in for the API vault, which supports neither listing nor bulk dumps."""

    def get_key(self, kid: Union[UUID, str], service: str) -> Optional[str]:
        return None

    def get_keys(self, service: str) -> Iterator[tuple[str, str]]:
        raise ValueError("Bulk dump is not supported.")

    def add_key(self, service: str, kid: Union[UUID, str], key: str) -> bool:
        return False

    def add_keys(self, service: str, kid_keys: dict[Union[UUID, str], str]) -> int:
        return 0

    def get_services(self) -> Iterator[str]:
        raise ValueError("Listing services is not supported.")


@pytest.fixture
def aliased_service(monkeypatch: pytest.MonkeyPatch) -> str:
    """Register an SVCD service whose vault table is named after its alias, not its tag."""
    from unshackle.core import services

    monkeypatch.setattr(services, "SERVICES", [Path("SVCD") / "__init__.py"])
    monkeypatch.setattr(services, "ALIASES", {"SVCD": ("svcdalias",)})
    return "svcdalias"


@pytest.fixture
def vault(tmp_path: Path) -> SQLite:
    """Vault whose backing DB already holds a lowercase `svca` table with one key."""
    v = SQLite("test", tmp_path / "kv.db")
    v.add_keys("svca", {KID: KEY})
    return v


@pytest.mark.parametrize("service", ["svca", "SVCA", "SvCa"])
def test_reads_find_table_in_any_case(vault: SQLite, service: str) -> None:
    assert vault.resolve_table(service) == "svca"
    assert vault.get_key(KID, service) == KEY
    assert list(vault.get_keys(service)) == [(KID, KEY)]


def test_add_keys_writes_into_existing_table(vault: SQLite) -> None:
    kid = "0" * 31 + "2"
    assert vault.add_keys("SVCA", {kid: "b" * 32}) == 1
    assert tables(vault.path) == ["sqlite_sequence", "svca"]
    assert vault.get_key(kid, "svca") == "b" * 32


def test_add_key_writes_into_existing_table(vault: SQLite) -> None:
    kid = "0" * 31 + "3"
    assert vault.add_key("SVCA", kid, "c" * 32)
    assert tables(vault.path) == ["sqlite_sequence", "svca"]
    assert vault.get_key(kid, "svca") == "c" * 32


def test_unknown_service_is_not_an_error(vault: SQLite) -> None:
    assert vault.resolve_table("SVCX") is None
    assert vault.get_key(KID, "SVCX") is None
    assert list(vault.get_keys("SVCX")) == []


def test_new_service_creates_table_with_the_given_case(tmp_path: Path) -> None:
    v = SQLite("test", tmp_path / "kv.db")
    v.add_keys("SVCB", {KID: KEY})
    assert tables(v.path) == ["SVCB", "sqlite_sequence"]


def test_synced_lowercase_table_survives_the_service_being_added_later(tmp_path: Path) -> None:
    """A `kv sync` before the service exists writes `svcc`; once SVCC is installed every
    lookup uses the canonical tag and must still land on that same table."""
    v = SQLite("test", tmp_path / "kv.db")
    v.add_keys("svcc", {KID: KEY})

    kid = "0" * 31 + "2"
    assert v.get_key(KID, "SVCC") == KEY
    assert v.add_keys("SVCC", {kid: "b" * 32}) == 1
    assert tables(v.path) == ["sqlite_sequence", "svcc"]
    assert len(list(v.get_keys("SVCC"))) == 2


def test_null_keys_are_rejected_and_filtered(tmp_path: Path) -> None:
    v = SQLite("test", tmp_path / "kv.db")
    with pytest.raises(ValueError):
        v.add_keys("SVCA", {KID: "0" * 32})

    v.create_table("svca")
    conn = sqlite3.connect(v.path)
    conn.execute("INSERT INTO svca (kid, key_) VALUES (?, ?)", (KID, "0" * 32))
    conn.commit()
    conn.close()

    assert v.get_key(KID, "SVCA") is None
    assert list(v.get_keys("SVCA")) == []


def test_get_services_yields_stored_table_names(vault: SQLite, aliased_service: str) -> None:
    """Names are yielded verbatim. Normalising the `svcdalias` table to the installed SVCD
    tag would hand back a name that addresses no table at all."""
    vault.add_keys(aliased_service, {KID: KEY})
    assert sorted(vault.get_services()) == ["svca", aliased_service]


def test_every_listed_service_can_be_copied_out_and_in(tmp_path: Path, aliased_service: str) -> None:
    """Every name `get_services()` lists must be readable by `get_keys()`."""
    from unshackle.commands.kv import copy_service_data

    src = SQLite("src", tmp_path / "src.db")
    src.add_keys(aliased_service, {KID: KEY})
    src.add_keys("svca", {KID: KEY})
    dst = SQLite("dst", tmp_path / "dst.db")

    added = [copy_service_data(dst, src, service, LOG) for service in src.get_services()]

    assert added == [1, 1]
    assert dst.get_key(KID, aliased_service) == KEY
    assert dst.get_key(KID, "svca") == KEY


def test_copy_skips_a_service_it_cannot_read(tmp_path: Path) -> None:
    """A vault that refuses bulk dumps must not abort the copy."""
    from unshackle.commands.kv import copy_service_data

    dst = SQLite("dst", tmp_path / "dst.db")
    assert copy_service_data(dst, UnlistableVault("unlistable"), "svca", LOG) == 0


def test_copy_skips_a_vault_that_cannot_list_services() -> None:
    with pytest.raises(ValueError):
        list(UnlistableVault("unlistable").get_services())


def test_lookups_work_from_another_thread(vault: SQLite) -> None:
    """`kv search` probes services in a ThreadPoolExecutor; connections are thread-local."""
    found: list[Optional[str]] = []
    thread = threading.Thread(target=lambda: found.append(vault.get_key(KID, "SVCA")))
    thread.start()
    thread.join()
    assert found == [KEY]
