import threading
from typing import Iterator, Optional, Union
from uuid import UUID

import pymysql
from pymysql.cursors import DictCursor

from unshackle.core.services import Services
from unshackle.core.vault import Vault


class MySQL(Vault):
    """Key Vault using a remotely-accessed mysql database connection."""

    def __init__(self, name: str, host: str, database: str, username: str, no_push: bool = False, **kwargs):
        """
        All extra arguments provided via **kwargs will be sent to pymysql.connect.
        This can be used to provide more specific connection information.
        """
        super().__init__(name, no_push)
        self.slug = f"{host}:{database}:{username}"
        self.conn_factory = ConnectionFactory(
            dict(host=host, db=database, user=username, cursorclass=DictCursor, **kwargs)
        )

        self.permissions = self.get_permissions()
        if not self.has_permission("SELECT"):
            raise PermissionError(f"MySQL vault {self.slug} has no SELECT permission.")

    def get_key(self, kid: Union[UUID, str], service: str) -> Optional[str]:
        if isinstance(kid, UUID):
            kid = kid.hex

        service_variants = [service]
        if service != service.lower():
            service_variants.append(service.lower())
        if service != service.upper():
            service_variants.append(service.upper())

        conn = self.conn_factory.get()
        cursor = conn.cursor()

        try:
            for service_name in service_variants:
                if not self.has_table(service_name):
                    continue

                cursor.execute(
                    # TODO: SQL injection risk
                    f"SELECT `id`, `key_` FROM `{service_name}` WHERE `kid`=%s AND `key_`!=%s",
                    (kid, "0" * 32),
                )
                cek = cursor.fetchone()
                if cek:
                    return cek["key_"]

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
            cursor.execute(
                # TODO: SQL injection risk
                f"SELECT `kid`, `key_` FROM `{service}` WHERE `key_`!=%s",
                ("0" * 32,),
            )
            for row in cursor.fetchall():
                yield row["kid"], row["key_"]
        finally:
            cursor.close()

    def add_key(self, service: str, kid: Union[UUID, str], key: str) -> bool:
        if not key or key.count("0") == len(key):
            raise ValueError("You cannot add a NULL Content Key to a Vault.")

        if not self.has_permission("INSERT", table=service):
            raise PermissionError(f"MySQL vault {self.slug} has no INSERT permission.")

        if not self.has_table(service):
            try:
                self.create_table(service)
            except PermissionError:
                return False

        if isinstance(kid, UUID):
            kid = kid.hex

        conn = self.conn_factory.get()
        cursor = conn.cursor()

        try:
            cursor.execute(
                # TODO: SQL injection risk
                f"SELECT `id` FROM `{service}` WHERE `kid`=%s AND `key_`=%s",
                (kid, key),
            )
            if cursor.fetchone():
                # table already has this exact KID:KEY stored
                return True
            cursor.execute(
                # TODO: SQL injection risk
                f"INSERT INTO `{service}` (kid, key_) VALUES (%s, %s)",
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

        if not self.has_permission("INSERT", table=service):
            raise PermissionError(f"MySQL vault {self.slug} has no INSERT permission.")

        if not self.has_table(service):
            try:
                self.create_table(service)
            except PermissionError:
                return 0

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
            placeholders = ",".join(["%s"] * len(kid_keys))
            cursor.execute(f"SELECT kid FROM `{service}` WHERE kid IN ({placeholders})", list(kid_keys.keys()))
            existing_kids = {row["kid"] for row in cursor.fetchall()}

            new_keys = {kid: key for kid, key in kid_keys.items() if kid not in existing_kids}

            if not new_keys:
                return 0

            cursor.executemany(
                f"INSERT INTO `{service}` (kid, key_) VALUES (%s, %s)",
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
            cursor.execute("SHOW TABLES")
            for table in cursor.fetchall():
                # each entry has a key named `Tables_in_<db name>`
                yield Services.get_tag(list(table.values())[0])
        finally:
            cursor.close()

    def has_table(self, name: str) -> bool:
        """Check if the Vault has a Table with the specified name."""
        conn = self.conn_factory.get()
        cursor = conn.cursor()

        try:
            cursor.execute(
                "SELECT count(TABLE_NAME) FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                (conn.db, name),
            )
            return list(cursor.fetchone().values())[0] == 1
        finally:
            cursor.close()

    def create_table(self, name: str):
        """Create a Table with the specified name if not yet created."""
        if self.has_table(name):
            return

        if not self.has_permission("CREATE"):
            raise PermissionError(f"MySQL vault {self.slug} has no CREATE permission.")

        conn = self.conn_factory.get()
        cursor = conn.cursor()

        try:
            cursor.execute(
                # TODO: SQL injection risk
                f"""
                CREATE TABLE IF NOT EXISTS {name} (
                  id          int AUTO_INCREMENT PRIMARY KEY,
                  kid         VARCHAR(64) NOT NULL,
                  key_        VARCHAR(64) NOT NULL,
                  UNIQUE(kid, key_)
                );
                """
            )
        finally:
            conn.commit()
            cursor.close()

    def get_permissions(self) -> list:
        """Get and parse Grants to a more easily usable list tuple array."""
        conn = self.conn_factory.get()
        cursor = conn.cursor()

        try:
            cursor.execute("SHOW GRANTS")
            grants = cursor.fetchall()
            grants = [next(iter(x.values())) for x in grants]
            grants = [tuple(x[6:].split(" TO ")[0].split(" ON ")) for x in list(grants)]
            grants = [
                (
                    list(map(str.strip, perms.replace("ALL PRIVILEGES", "*").split(","))),
                    location.replace("`", "").split("."),
                )
                for perms, location in grants
            ]
            return grants
        finally:
            conn.commit()
            cursor.close()

    def has_permission(self, operation: str, database: Optional[str] = None, table: Optional[str] = None) -> bool:
        """Check if the current connection has a specific permission."""
        grants = [x for x in self.permissions if x[0] == ["*"] or operation.upper() in x[0]]
        if grants and database:
            grants = [x for x in grants if x[1][0] in (database, "*")]
        if grants and table:
            grants = [x for x in grants if x[1][1] in (table, "*")]
        return bool(grants)


class ConnectionFactory:
    def __init__(self, con: dict):
        self._con = con
        self._store = threading.local()

    def _create_connection(self) -> pymysql.Connection:
        return pymysql.connect(**self._con)

    def get(self) -> pymysql.Connection:
        if not hasattr(self._store, "conn"):
            self._store.conn = self._create_connection()
        return self._store.conn
