import sqlite3
import threading
from pathlib import Path
from sqlite3 import Connection
from typing import Iterator, Optional, Union
from uuid import UUID

from unshackle.core.services import Services
from unshackle.core.vault import Vault


class SQLite(Vault):
    """Key Vault using a locally-accessed sqlite DB file."""

    def __init__(self, name: str, path: Union[str, Path], no_push: bool = False):
        super().__init__(name, no_push)
        self.path = Path(path).expanduser()
        # TODO: Use a DictCursor or such to get fetches as dict?
        self.conn_factory = ConnectionFactory(self.path)

    def get_key(self, kid: Union[UUID, str], service: str) -> Optional[str]:
        if isinstance(kid, UUID):
            kid = kid.hex

        conn = self.conn_factory.get()
        cursor = conn.cursor()

        # Try both the original service name and lowercase version to handle case sensitivity issues
        service_variants = [service]
        if service != service.lower():
            service_variants.append(service.lower())
        if service != service.upper():
            service_variants.append(service.upper())

        try:
            for service_name in service_variants:
                if not self.has_table(service_name):
                    continue

                cursor.execute(
                    f"SELECT `id`, `key_` FROM `{service_name}` WHERE `kid`=? AND `key_`!=?", (kid, "0" * 32)
                )
                cek = cursor.fetchone()
                if cek:
                    return cek[1]

            return None
        finally:
            cursor.close()

    def get_keys(self, service: str) -> Iterator[tuple[str, str]]:
        if not self.has_table(service):
            # no table, no keys, simple
            return None

        conn = self.conn_factory.get()
        cursor = conn.cursor()

        try:
            cursor.execute(f"SELECT `kid`, `key_` FROM `{service}` WHERE `key_`!=?", ("0" * 32,))
            for kid, key_ in cursor.fetchall():
                yield kid, key_
        finally:
            cursor.close()

    def add_key(self, service: str, kid: Union[UUID, str], key: str) -> bool:
        if not key or key.count("0") == len(key):
            raise ValueError("You cannot add a NULL Content Key to a Vault.")

        if not self.has_table(service):
            self.create_table(service)

        if isinstance(kid, UUID):
            kid = kid.hex

        conn = self.conn_factory.get()
        cursor = conn.cursor()

        try:
            cursor.execute(
                # TODO: SQL injection risk
                f"SELECT `id` FROM `{service}` WHERE `kid`=? AND `key_`=?",
                (kid, key),
            )
            if cursor.fetchone():
                # table already has this exact KID:KEY stored
                return True
            cursor.execute(
                # TODO: SQL injection risk
                f"INSERT INTO `{service}` (kid, key_) VALUES (?, ?)",
                (kid, key),
            )
        finally:
            conn.commit()
            cursor.close()

        return True

    def add_keys(self, service: str, kid_keys: dict[Union[UUID, str], str]) -> int:
        for kid, key in kid_keys.items():
            if not key or key.count("0") == len(key):
                raise ValueError("You cannot add a NULL Content Key to a Vault.")

        if not self.has_table(service):
            self.create_table(service)

        if not isinstance(kid_keys, dict):
            raise ValueError(f"The kid_keys provided is not a dictionary, {kid_keys!r}")
        if not all(isinstance(kid, (str, UUID)) and isinstance(key_, str) for kid, key_ in kid_keys.items()):
            raise ValueError("Expecting dict with Key of str/UUID and value of str.")

        if any(isinstance(kid, UUID) for kid, key_ in kid_keys.items()):
            kid_keys = {kid.hex if isinstance(kid, UUID) else kid: key_ for kid, key_ in kid_keys.items()}

        if not kid_keys:
            return 0

        conn = self.conn_factory.get()
        cursor = conn.cursor()

        try:
            # Query existing KIDs in batches to avoid SQLite variable limit
            # Try larger batch first (newer SQLite supports 32766), fall back to 500 if needed
            existing_kids: set[str] = set()
            kid_list = list(kid_keys.keys())
            batch_size = 32000

            i = 0
            while i < len(kid_list):
                batch = kid_list[i : i + batch_size]
                placeholders = ",".join(["?"] * len(batch))
                try:
                    cursor.execute(f"SELECT kid FROM `{service}` WHERE kid IN ({placeholders})", batch)
                    existing_kids.update(row[0] for row in cursor.fetchall())
                    i += batch_size
                except sqlite3.OperationalError as e:
                    if "too many SQL variables" in str(e) and batch_size > 500:
                        batch_size = 500
                        continue
                    raise

            new_keys = {kid: key for kid, key in kid_keys.items() if kid not in existing_kids}

            if not new_keys:
                return 0

            cursor.executemany(
                f"INSERT INTO `{service}` (kid, key_) VALUES (?, ?)",
                new_keys.items(),
            )
            return len(new_keys)
        finally:
            conn.commit()
            cursor.close()

    def get_services(self) -> Iterator[str]:
        conn = self.conn_factory.get()
        cursor = conn.cursor()

        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            for (name,) in cursor.fetchall():
                if name != "sqlite_sequence":
                    yield Services.get_tag(name)
        finally:
            cursor.close()

    def has_table(self, name: str) -> bool:
        """Check if the Vault has a Table with the specified name."""
        conn = self.conn_factory.get()
        cursor = conn.cursor()

        try:
            cursor.execute("SELECT count(name) FROM sqlite_master WHERE type='table' AND name=?", (name,))
            return cursor.fetchone()[0] == 1
        finally:
            cursor.close()

    def create_table(self, name: str):
        """Create a Table with the specified name if not yet created."""
        if self.has_table(name):
            return

        conn = self.conn_factory.get()
        cursor = conn.cursor()

        try:
            cursor.execute(
                # TODO: SQL injection risk
                f"""
                CREATE TABLE IF NOT EXISTS {name} (
                  "id"        INTEGER NOT NULL UNIQUE,
                  "kid"       TEXT NOT NULL COLLATE NOCASE,
                  "key_"      TEXT NOT NULL COLLATE NOCASE,
                  PRIMARY KEY("id" AUTOINCREMENT),
                  UNIQUE("kid", "key_")
                );
                """
            )
        finally:
            conn.commit()
            cursor.close()


class ConnectionFactory:
    def __init__(self, path: Union[str, Path]):
        self._path = path
        self._store = threading.local()

    def _create_connection(self) -> Connection:
        return sqlite3.connect(self._path)

    def get(self) -> Connection:
        if not hasattr(self._store, "conn"):
            self._store.conn = self._create_connection()
        return self._store.conn
