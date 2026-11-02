import functools
import time
from abc import abstractmethod
from typing import Any, Callable, Optional, TypeVar

CATALOGUE_TTL = 600.0
CATALOGUES: dict[str, tuple[float, Any]] = {}

F = TypeVar("F", bound=Callable[..., Any])


def cached_catalogue(fetch: F) -> F:
    """Keep a proxy provider's server catalogue for ``CATALOGUE_TTL`` seconds; the next call fetches an empty answer again.

    A server builds its proxy providers on every request, and each build fetches the catalogue.
    """

    @functools.wraps(fetch)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        hit = CATALOGUES.get(fetch.__qualname__)
        if hit and time.monotonic() - hit[0] < CATALOGUE_TTL:
            return hit[1]
        value = fetch(*args, **kwargs)
        if value:
            CATALOGUES[fetch.__qualname__] = (time.monotonic(), value)
        return value

    return wrapper  # type: ignore[return-value]


class Proxy:
    @abstractmethod
    def __init__(self, **kwargs: Any) -> None:
        """
        The constructor initializes the Service using passed configuration data.

        Any authorization or pre-fetching of data should be done here.
        """

    @abstractmethod
    def __repr__(self) -> str:
        """Return a string denoting a list of Countries and Servers (if possible)."""
        countries = ...
        servers = ...
        return f"{countries} Countr{['ies', 'y'][countries == 1]} ({servers} Server{['s', ''][servers == 1]})"

    @abstractmethod
    def get_proxy(self, query: str) -> Optional[str]:
        """
        Get a proxy URI from the proxy provider.

        Return None when this proxy provider has no proxy for the query, so that a bare query
        moves on to the next proxy provider. Use exceptions for errors with the call.

        The returned Proxy URI must be a string supported by Python-Requests:
        '{scheme}://[{user}:{pass}@]{host}:{port}'
        """
