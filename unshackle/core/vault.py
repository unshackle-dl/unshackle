import re
from abc import ABCMeta, abstractmethod
from collections.abc import Iterator
from urllib.parse import urlsplit
from uuid import UUID

from requests import ConnectionError as RequestsConnectionError
from requests import RequestException, Response, Session, Timeout
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from unshackle.core import __version__
from unshackle.core.session import POOL_MAX_SIZE
from unshackle.core.utils.redact import redact_text

RATE_LIMIT_RETRIES = 3


class RateLimited(Exception):
    """The vault still answered 429 after the retries."""

    def __init__(self, message: str = f"still rate limited after {RATE_LIMIT_RETRIES} retries") -> None:
        super().__init__(message)


UNREACHABLE = (RequestsConnectionError, Timeout, ConnectionError, TimeoutError)

OS_REASON_RE = re.compile(r"\[(?:Errno -?\d+|WinError -?\d+|SSL: \w+)\] ([^'\"()\[\]]+)")


def brief(error: BaseException) -> str:
    """One line for a vault failure, without the request URL, whose query can hold the vault password."""
    if not isinstance(error, RequestException) or (error.request is None and error.response is None):
        text = (redact_text(str(error).strip().split("\n", 1)[0]) or "")[:200]
        return text or type(error).__name__
    url = getattr(error.request, "url", None)
    host = urlsplit(url).netloc.rpartition("@")[2] if url else "vault"
    if error.response is not None:
        return f"HTTP {error.response.status_code} from {host}"
    text = str(error)
    reasons = OS_REASON_RE.findall(text)
    reason = reasons[-1].strip() if reasons else "timed out" if "timed out" in text else ""
    return f"{type(error).__name__} to {host}" + (f" ({reason})" if reason else "")


def json_body(r: Response) -> dict:
    """The response body as a JSON object. Any other body means the host is not a vault, so it is unreachable."""
    try:
        data = r.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        mime = re.match(r"[\w.+-]{1,64}/[\w.+-]{1,64}", r.headers.get("Content-Type", ""))
        content_type = mime.group() if mime else "unknown"
        raise ConnectionError(f"HTTP {r.status_code} {content_type} response is not a JSON object")
    return data


def is_null_key(key: str | None) -> bool:
    """True for a blank or all-zero content key, the null key that no vault stores."""
    return not key or key.count("0") == len(key)


def vault_session(timeout: float) -> Session:
    """An HTTP session for a remote vault that retries a 429 and nothing else.

    A 429 means the request was not processed, so a POST is safe to send again. A connect or read
    failure is not retried: a lookup waits on every remote vault, so a retry multiplies the timeout.
    """
    retry = Retry(
        total=RATE_LIMIT_RETRIES,
        connect=0,
        read=0,
        other=0,
        backoff_factor=1,
        status_forcelist=[429],
        allowed_methods=None,
        retry_after_max=int(timeout),
        raise_on_status=False,
    )
    session = Session()
    adapter = HTTPAdapter(pool_connections=POOL_MAX_SIZE, pool_maxsize=POOL_MAX_SIZE, max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": f"unshackle v{__version__}"})
    return session


class Refused(Exception):
    """The vault answered and refused the query, so it is up."""


class Vault(metaclass=ABCMeta):
    """A key vault of KID:KEY pairs.

    Its class flags tell kv what it can do: bulk_writes (add_keys takes a whole service in one call),
    batch_insert (add_keys takes more than one content key; HTTP clears it when the server refuses
    batches) and has_tables (one table per service, with resolve_table and create_table).
    """

    # a vault on this machine; Vaults.get_key tries these first, before the network ones
    local: bool = False
    report_now: bool = False
    stores_flags: bool = False
    bulk_writes: bool = False
    batch_insert: bool = True
    has_tables: bool = False

    def __init__(self, name: str, no_push: bool = False) -> None:
        self.name = name
        self.no_push = no_push

    def __str__(self) -> str:
        return f"{self.name} {type(self).__name__}"

    @abstractmethod
    def get_key(self, kid: UUID | str, service: str) -> str | None:
        """
        Get the content key from the Vault by KID (Key ID) and Service.

        It does not get the content key by PSSH, as the PSSH can be different depending on its
        implementation, or even on how it was made. Some PSSH values can also be a CENC
        Header rather than a PSSH MP4 Box, which makes the value even more confusingly different.

        However, the KID never changes unless the video file itself has changed too, meaning the
        content key for the presumed-matching KID would not work, further proving matching by KID
        is superior.
        """

    @abstractmethod
    def get_keys(self, service: str) -> Iterator[tuple[str, str]]:
        """Get All Keys from Vault by Service."""

    @abstractmethod
    def add_key(self, service: str, kid: UUID | str, key: str) -> bool:
        """Add KID:KEY to the Vault."""

    @abstractmethod
    def add_keys(self, service: str, kid_keys: dict[UUID | str, str]) -> int:
        """
        Add Multiple Content Keys with Key IDs for Service to the Vault.
        The Vault ignores pre-existing Content Keys.
        Raises PermissionError if the user has no permission to make the table.
        """

    @abstractmethod
    def get_services(self) -> Iterator[str]:
        """
        Get a list of Service Tags from Vault.

        Tags come back exactly as the Vault stores them, so passing one to
        get_key/get_keys/add_key/add_keys reaches the namespace it came from.
        """

    def is_bad_key(self, kid: UUID | str, key: str) -> bool:
        """Return True if this Vault has flagged the KID:KEY as wrong."""
        return False

    def flag_bad_key(self, service: str, kid: UUID | str, key: str, source: str) -> None:
        """Remember a KID:KEY that failed decryption and where it came from."""

    def unflag_bad_key(self, kid: UUID | str, key: str) -> None:
        """Forget a flagged KID:KEY, for example when a CDM licence confirmed it."""

    def resolve_table(self, name: str) -> str | None:
        """The stored name of the table of a service, or None. Only a vault with has_tables has tables."""
        return None

    def create_table(self, name: str) -> None:
        """Make the table of a service. Only a vault with has_tables has tables."""
        raise NotImplementedError


__all__ = ("Refused", "Vault", "brief", "json_body")
