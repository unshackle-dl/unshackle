import json
from collections.abc import Iterator
from uuid import UUID

from requests import RequestException

from unshackle.core.vault import RateLimited, Vault, is_null_key, json_body, vault_session


class HTTP(Vault):
    """
    Key Vault using HTTP API that can operate in several API modes.

    Supported modes:
    - query: Uses GET requests with query parameters
    - json: Uses POST requests with JSON payloads
    - decrypt_labs: Uses DecryptLabs API format (read-only)
    """

    def __init__(
        self,
        name: str,
        host: str,
        password: str | None = None,
        api_key: str | None = None,
        username: str | None = None,
        api_mode: str = "query",
        no_push: bool = False,
        timeout: float = 10.0,
    ) -> None:
        """
        Initialise HTTP Vault.

        Args:
            name: Vault name
            host: Host URL
            password: Password for query mode or API token for json mode
            api_key: API key (alternative to password, used for decrypt_labs mode)
            username: Username (required for query mode, ignored for json/decrypt_labs mode)
            api_mode: "query" for query parameters, "json" for JSON API, or "decrypt_labs" for DecryptLabs API
            no_push: If True, this vault will not receive pushed keys
        """
        super().__init__(name, no_push)
        self.url = host
        self.timeout = timeout
        self.password = api_key or password
        if not self.password:
            raise ValueError("Either password or api_key is required")

        self.username = username
        self.api_mode = api_mode.lower()
        self.batch_insert = True
        self.current_title: str | None = None
        self.session = vault_session(timeout)
        self.api_session_id = None

        if self.api_mode == "decrypt_labs":
            self.session.headers.update({"decrypt-labs-api-key": self.password})
            self.no_push = True

        # Validate configuration based on mode
        if self.api_mode == "query" and not self.username:
            raise ValueError("Username is required for query mode")
        elif self.api_mode not in ["query", "json", "decrypt_labs"]:
            raise ValueError("api_mode must be either 'query', 'json', or 'decrypt_labs'")

    def request(self, method: str, params: dict | None = None) -> dict:
        """Make a request to the JSON API vault."""
        if self.api_mode != "json":
            raise ValueError("request method is only available in json mode")

        request_payload = {
            "method": method,
            "params": {
                **(params or {}),
                "session_id": self.api_session_id,
            },
            "token": self.password,
        }

        r = self.session.post(self.url, json=request_payload, timeout=self.timeout)

        if r.status_code == 404:
            return {"status": "not_found"}

        if r.status_code == 429:
            raise RateLimited()

        if not r.ok:
            detail = json_body(r).get("message") or r.reason.title()
            raise ValueError(f"API returned HTTP Error {r.status_code}: {detail}")

        res = json_body(r)

        if res.get("status_code") != 200:
            raise ValueError(f"API returned an error: {res['status_code']} - {res['message']}")

        if session_id := res.get("message", {}).get("session_id"):
            self.api_session_id = session_id

        return res.get("message", res)

    def get_key(self, kid: UUID | str, service: str) -> str | None:
        if isinstance(kid, UUID):
            kid = kid.hex

        if self.api_mode == "decrypt_labs":
            response = self.session.post(self.url, json={"service": service.lower(), "kid": kid}, timeout=self.timeout)
            if not response.ok:
                return None

            data = json_body(response)

            if data.get("message") != "success":
                return None

            cached_keys = data.get("cached_keys")
            if not cached_keys:
                return None

            if isinstance(cached_keys, str):
                try:
                    cached_keys = json.loads(cached_keys)
                except json.JSONDecodeError:
                    return cached_keys

            if isinstance(cached_keys, dict):
                if cached_keys.get("kid") == kid:
                    return cached_keys.get("key")
                if kid in cached_keys:
                    return cached_keys[kid]
            elif isinstance(cached_keys, list):
                for entry in cached_keys:
                    if isinstance(entry, dict):
                        if entry.get("kid") == kid:
                            return entry.get("key")
                    elif isinstance(entry, str) and ":" in entry:
                        entry_kid, entry_key = entry.split(":", 1)
                        if entry_kid == kid:
                            return entry_key
            return None

        elif self.api_mode == "json":
            params = {
                "kid": kid,
                "service": service.lower(),
            }

            res = self.request("GetKey", params)
            if res.get("status") == "not_found":
                return None
            keys = res.get("keys", [])
            for key_entry in keys:
                if isinstance(key_entry, str) and ":" in key_entry:
                    entry_kid, entry_key = key_entry.split(":", 1)
                    if entry_kid == kid:
                        return entry_key
                elif isinstance(key_entry, dict):
                    if key_entry.get("kid") == kid:
                        return key_entry.get("key")
            return None
        else:  # query mode
            response = self.session.get(
                self.url,
                params={"service": service.lower(), "username": self.username, "password": self.password, "kid": kid},
                timeout=self.timeout,
            )

            data = json_body(response)

            if not data.get("keys"):
                return None

            return data["keys"][0]["key"]

    def get_keys(self, service: str) -> Iterator[tuple[str, str]]:
        if self.api_mode == "decrypt_labs":
            return
        elif self.api_mode == "json":
            # JSON API doesn't support getting all keys, so return empty iterator
            # This will cause the copy command to rely on the API's internal duplicate handling
            return
        else:
            response = self.session.get(
                self.url,
                params={"service": service.lower(), "username": self.username, "password": self.password},
                timeout=self.timeout,
            )

            data = json_body(response)

            if data.get("status_code") != 200 or not data.get("keys"):
                return

            for key_entry in data["keys"]:
                yield key_entry["kid"], key_entry["key"]

    def add_key(self, service: str, kid: UUID | str, key: str) -> bool:
        if is_null_key(key):
            raise ValueError("You cannot add a NULL Content Key to a Vault.")

        if self.api_mode == "decrypt_labs":
            return False

        if isinstance(kid, UUID):
            kid = kid.hex

        title = getattr(self, "current_title", None)

        if self.api_mode == "json":
            try:
                res = self.request(
                    "InsertKey",
                    {
                        "kid": kid,
                        "key": key,
                        "service": service.lower(),
                        "title": title,
                    },
                )
                if res.get("status") == "not_found":
                    return False
                return res.get("inserted", False)
            except RequestException:  # MissingSchema and InvalidURL are ValueErrors too
                raise
            except ValueError:  # an error answer from the vault
                return False
        else:
            response = self.session.get(
                self.url,
                params={
                    "service": service.lower(),
                    "username": self.username,
                    "password": self.password,
                    "kid": kid,
                    "key": key,
                    "title": title,
                },
                timeout=self.timeout,
            )

            data = json_body(response)

            return data.get("status_code") == 200

    def add_keys(self, service: str, kid_keys: dict[UUID | str, str]) -> int:
        if self.api_mode == "decrypt_labs":
            return 0

        for key in kid_keys.values():
            if is_null_key(key):
                raise ValueError("You cannot add a NULL Content Key to a Vault.")

        processed_kid_keys = {
            str(kid).replace("-", "") if isinstance(kid, UUID) else kid: key for kid, key in kid_keys.items()
        }

        inserted_count = 0
        title = getattr(self, "current_title", None)
        failures: list[Exception] = []

        if self.api_mode == "json":
            if self.batch_insert:
                try:
                    res = self.request(
                        "InsertKeys", {"keys": processed_kid_keys, "service": service.lower(), "title": title}
                    )
                    if res.get("status") != "not_found":
                        return int(res.get("inserted", 0))
                    self.batch_insert = False
                except ValueError as e:
                    if "unknown" in str(e).lower() and "method" in str(e).lower():
                        self.batch_insert = False
            for kid, key in processed_kid_keys.items():
                try:
                    res = self.request(
                        "InsertKey",
                        {
                            "kid": kid,
                            "key": key,
                            "service": service.lower(),
                            "title": title,
                        },
                    )
                    if res.get("status") == "not_found":
                        continue
                    if res.get("inserted", False):
                        inserted_count += 1
                except ValueError as e:
                    failures.append(e)
            if failures and len(failures) == len(processed_kid_keys):
                raise failures[-1]
        else:
            for kid, key in processed_kid_keys.items():
                response = self.session.get(
                    self.url,
                    params={
                        "service": service.lower(),
                        "username": self.username,
                        "password": self.password,
                        "kid": kid,
                        "key": key,
                        "title": title,
                    },
                    timeout=self.timeout,
                )

                data = json_body(response)

                if data.get("status_code") == 200 and data.get("inserted", True):
                    inserted_count += 1

        return inserted_count

    def get_services(self) -> Iterator[str]:
        if self.api_mode == "decrypt_labs":
            return
        elif self.api_mode == "json":
            yield from self.request("GetServices").get("services", [])
        else:
            response = self.session.get(
                self.url,
                params={"username": self.username, "password": self.password, "list_services": True},
                timeout=self.timeout,
            )

            data = json_body(response)

            if data.get("status_code") != 200:
                return

            yield from data.get("services", [])

    def set_title(self, title: str) -> None:
        """
        Set a title for the next content key insertions.
        This is optional and will be sent with add_key requests if available.
        """
        self.current_title = title
