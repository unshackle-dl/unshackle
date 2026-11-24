from abc import abstractmethod
from typing import Optional


class Proxy:
    @abstractmethod
    def __init__(self, **kwargs):
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
