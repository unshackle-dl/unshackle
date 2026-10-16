from abc import ABCMeta, abstractmethod
from typing import Iterator, Optional, Union
from uuid import UUID

from requests import ConnectionError as RequestsConnectionError
from requests import Session, Timeout
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from unshackle.core import __version__
from unshackle.core.session import POOL_MAX_SIZE


RATE_LIMIT_RETRIES = 3


class RateLimited(Exception):
    """The vault still answered 429 after the retries."""

    def __init__(self, message: str = f"still rate limited after {RATE_LIMIT_RETRIES} retries"):
        super().__init__(message)


UNREACHABLE = (RequestsConnectionError, Timeout, ConnectionError, TimeoutError)


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


class Vault(metaclass=ABCMeta):
    # a vault on this machine; Vaults.get_key tries these first, before the network ones
    local: bool = False

    def __init__(self, name: str, no_push: bool = False):
        self.name = name
        self.no_push = no_push

    def __str__(self) -> str:
        return f"{self.name} {type(self).__name__}"

    @abstractmethod
    def get_key(self, kid: Union[UUID, str], service: str) -> Optional[str]:
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
    def add_key(self, service: str, kid: Union[UUID, str], key: str) -> bool:
        """Add KID:KEY to the Vault."""

    @abstractmethod
    def add_keys(self, service: str, kid_keys: dict[Union[UUID, str], str]) -> int:
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

    def is_bad_key(self, kid: Union[UUID, str], key: str) -> bool:
        """Return True if this Vault has flagged the KID:KEY as wrong."""
        return False

    def flag_bad_key(self, service: str, kid: Union[UUID, str], key: str, source: str) -> None:
        """Remember a KID:KEY that failed decryption and where it came from."""

    def unflag_bad_key(self, kid: Union[UUID, str], key: str) -> None:
        """Forget a flagged KID:KEY, for example when a CDM licence confirmed it."""


__all__ = ("Vault",)
