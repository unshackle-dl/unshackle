from abc import ABCMeta, abstractmethod
from typing import Iterator, Optional, Union
from uuid import UUID


class Vault(metaclass=ABCMeta):
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


__all__ = ("Vault",)
