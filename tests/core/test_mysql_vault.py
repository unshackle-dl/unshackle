"""The MySQL vault keeps one connection for every thread, quotes its table names and dedupes KIDs without case."""

from __future__ import annotations

import re
import threading
from typing import Any

import pymysql
import pytest

from unshackle.core.vaults import Vaults
from unshackle.vaults import MySQL as mysql_module
from unshackle.vaults.MySQL import MySQL

KID = "ab" * 16
KEY = "a" * 32


class FakeServer:
    """Answers the statements the vault sends, holds its tables in memory and counts connections."""

    def __init__(self) -> None:
        self.tables: dict[str, list[tuple[str, str]]] = {"EXAMPLE": [(KID.upper(), KEY)]}
        self.connects = 0
        self.kwargs: dict[str, Any] = {}
        self.pings = 0
        self.active = 0
        self.overlaps = 0
        self.down = False
        self.guard = threading.Lock()

    def connect(self, **kwargs: Any) -> FakeConnection:
        self.connects += 1
        self.kwargs = kwargs
        return FakeConnection(self, kwargs["db"])


class FakeConnection:
    def __init__(self, server: FakeServer, db: str) -> None:
        self.server = server
        self.db = db

    def ping(self, reconnect: bool = True) -> None:
        self.server.pings += 1
        if self.server.down:
            raise pymysql.err.OperationalError(2003, "Can't connect to MySQL server")

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.server = connection.server
        self.rows: list[dict[str, Any]] = []
        with self.server.guard:
            self.server.active += 1
            self.server.overlaps += self.server.active > 1

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        with self.server.guard:
            self.server.active -= 1

    def execute(self, sql: str, args: Any = ()) -> None:
        tables = self.server.tables
        if sql == "SHOW GRANTS":
            self.rows = [{"Grants": "GRANT ALL PRIVILEGES ON *.* TO `user`@`%`"}]
        elif "UPPER(TABLE_NAME)" in sql:
            self.rows = [{"TABLE_NAME": t} for t in tables if t.upper() == args[1].upper()]
        elif "count(TABLE_NAME)" in sql:
            self.rows = [{"count": int(args[1] in tables)}]
        elif sql.strip().startswith("CREATE TABLE"):
            match = re.search(r"EXISTS `(.+?)` \(", sql)
            if not match:
                raise pymysql.err.ProgrammingError(1064, "You have an error in your SQL syntax")
            tables.setdefault(match.group(1), [])
        elif sql.startswith("SELECT `id`, `key_`"):
            self.rows = [{"id": 1, "key_": k} for kid, k in tables[table(sql)] if kid.lower() == args[0].lower()]
        elif sql.startswith("SELECT `kid`, `key_`"):
            self.rows = [{"kid": kid, "key_": key} for kid, key in tables[table(sql)]]
        elif sql.startswith("SELECT kid FROM"):
            wanted = {a.lower() for a in args}
            self.rows = [{"kid": kid} for kid, _ in tables[table(sql)] if kid.lower() in wanted]
        else:
            raise AssertionError(f"unexpected statement {sql!r}")

    def executemany(self, sql: str, rows: Any) -> None:
        stored = self.server.tables[table(sql)]
        for kid, key in rows:
            if (kid.lower(), key.lower()) in {(k.lower(), v.lower()) for k, v in stored}:
                raise pymysql.err.IntegrityError(1062, "Duplicate entry")
            stored.append((kid, key))

    def fetchone(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


def table(sql: str) -> str:
    match = re.search(r"FROM `(.+?)`|INTO `(.+?)`", sql)
    assert match
    return match.group(1) or match.group(2)


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    fake = FakeServer()
    monkeypatch.setattr(mysql_module.pymysql, "connect", fake.connect)
    return fake


def make_vault() -> MySQL:
    return MySQL("test", host="127.0.0.1", database="keys", username="user")


def test_every_thread_shares_one_connection_one_cursor_at_a_time(server: FakeServer) -> None:
    vault = make_vault()
    start = threading.Barrier(8)
    found: list[str | None] = []

    def look_up() -> None:
        start.wait()
        for _ in range(20):
            found.append(vault.get_key(KID, "EXAMPLE"))

    threads = [threading.Thread(target=look_up) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert found == [KEY] * 160
    assert server.connects == 1
    assert server.kwargs["autocommit"] is True
    assert server.kwargs["read_timeout"] and server.kwargs["write_timeout"]
    assert server.overlaps == 0


@pytest.mark.parametrize("service", ["9EXAMPLE", "EXAMPLE-OTHER"])
def test_a_tag_that_is_not_a_bare_identifier_gets_a_table(server: FakeServer, service: str) -> None:
    vault = make_vault()
    assert vault.add_keys(service, {KID: KEY}) == 1
    assert vault.get_key(KID, service) == KEY


def test_add_keys_skips_a_kid_stored_in_another_case(server: FakeServer) -> None:
    vault = make_vault()
    assert vault.add_keys("EXAMPLE", {KID: KEY, "cd" * 16: "b" * 32, "CD" * 16: "B" * 32}) == 1
    assert server.tables["EXAMPLE"] == [(KID.upper(), KEY), ("CD" * 16, "B" * 32)]


def test_an_idle_connection_is_pinged_before_use(server: FakeServer, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = make_vault()
    vault.get_key(KID, "EXAMPLE")
    assert server.pings == 0
    monkeypatch.setattr(mysql_module.ConnectionFactory, "PING_AFTER", -1.0)
    vault.get_key(KID, "EXAMPLE")
    assert server.pings == 2


def test_a_server_that_goes_down_is_skipped_for_the_rest_of_the_run(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    vaults = Vaults("EXAMPLE")
    vaults.vaults.append(make_vault())
    monkeypatch.setattr(mysql_module.ConnectionFactory, "PING_AFTER", -1.0)
    server.down = True
    assert vaults.get_key(KID) == (None, None)
    assert vaults.live() == []
    server.down = False
    assert vaults.vaults[0].get_key(KID, "EXAMPLE") == KEY


def test_get_keys_frees_the_connection_before_it_yields(server: FakeServer) -> None:
    vault = make_vault()
    rows = vault.get_keys("EXAMPLE")
    assert next(rows) == (KID.upper(), KEY)
    found: list[str | None] = []
    thread = threading.Thread(target=lambda: found.append(vault.get_key(KID, "EXAMPLE")))
    thread.start()
    thread.join(timeout=10)
    assert found == [KEY]
