"""Cryptographic utilities for secure remote service authentication."""

import base64
import hashlib
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    from nacl.public import Box, PrivateKey, PublicKey
    from nacl.secret import SecretBox
    from nacl.utils import random

    NACL_AVAILABLE = True
except ImportError:
    NACL_AVAILABLE = False

log = logging.getLogger("crypto")


class CryptoError(Exception):
    """Cryptographic operation error."""

    pass


class ServerKeyPair:
    """
    Server-side key pair for secure remote authentication.

    Uses NaCl (libsodium) for public key cryptography.
    The server generates a key pair and shares the public key with clients.
    Clients encrypt sensitive data with the public key, which only the server can decrypt.
    """

    def __init__(self, private_key: Optional[PrivateKey] = None):
        """
        Initialize server key pair.

        Args:
            private_key: Existing private key, or None to generate new
        """
        if not NACL_AVAILABLE:
            raise CryptoError("PyNaCl is not installed. Install with: pip install pynacl")

        self.private_key = private_key or PrivateKey.generate()
        self.public_key = self.private_key.public_key

    def get_public_key_b64(self) -> str:
        """
        Get base64-encoded public key for sharing with clients.

        Returns:
            Base64-encoded public key
        """
        return base64.b64encode(bytes(self.public_key)).decode("utf-8")

    def decrypt_message(self, encrypted_message: str, client_public_key_b64: str) -> Dict[str, Any]:
        """
        Decrypt a message from a client.

        Args:
            encrypted_message: Base64-encoded encrypted message
            client_public_key_b64: Base64-encoded client public key

        Returns:
            Decrypted message as dictionary
        """
        try:
            # Decode keys
            client_public_key = PublicKey(base64.b64decode(client_public_key_b64))
            encrypted_data = base64.b64decode(encrypted_message)

            # Create box for decryption
            box = Box(self.private_key, client_public_key)

            # Decrypt
            decrypted = box.decrypt(encrypted_data)
            return json.loads(decrypted.decode("utf-8"))

        except Exception as e:
            log.error(f"Decryption failed: {e}")
            raise CryptoError(f"Failed to decrypt message: {e}")

    def save_to_file(self, path: Path) -> None:
        """
        Save private key to file.

        Args:
            path: Path to save the key
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        key_data = {
            "private_key": base64.b64encode(bytes(self.private_key)).decode("utf-8"),
            "public_key": self.get_public_key_b64(),
        }
        path.write_text(json.dumps(key_data, indent=2), encoding="utf-8")
        log.info(f"Server key pair saved to {path}")

    @classmethod
    def load_from_file(cls, path: Path) -> "ServerKeyPair":
        """
        Load private key from file.

        Args:
            path: Path to load the key from

        Returns:
            ServerKeyPair instance
        """
        if not path.exists():
            raise CryptoError(f"Key file not found: {path}")

        try:
            key_data = json.loads(path.read_text(encoding="utf-8"))
            private_key_bytes = base64.b64decode(key_data["private_key"])
            private_key = PrivateKey(private_key_bytes)
            log.info(f"Server key pair loaded from {path}")
            return cls(private_key)
        except Exception as e:
            raise CryptoError(f"Failed to load key from {path}: {e}")


class ClientCrypto:
    """
    Client-side cryptography for secure remote authentication.

    Generates ephemeral key pairs and encrypts sensitive data for the server.
    """

    def __init__(self):
        """Initialize client crypto with ephemeral key pair."""
        if not NACL_AVAILABLE:
            raise CryptoError("PyNaCl is not installed. Install with: pip install pynacl")

        # Generate ephemeral key pair for this session
        self.private_key = PrivateKey.generate()
        self.public_key = self.private_key.public_key

    def get_public_key_b64(self) -> str:
        """
        Get base64-encoded public key for sending to server.

        Returns:
            Base64-encoded public key
        """
        return base64.b64encode(bytes(self.public_key)).decode("utf-8")

    def encrypt_credentials(
        self, credentials: Dict[str, Any], server_public_key_b64: str
    ) -> Tuple[str, str]:
        """
        Encrypt credentials for the server.

        Args:
            credentials: Dictionary containing sensitive data (username, password, cookies, etc.)
            server_public_key_b64: Base64-encoded server public key

        Returns:
            Tuple of (encrypted_message_b64, client_public_key_b64)
        """
        try:
            # Decode server public key
            server_public_key = PublicKey(base64.b64decode(server_public_key_b64))

            # Create box for encryption
            box = Box(self.private_key, server_public_key)

            # Encrypt
            message = json.dumps(credentials).encode("utf-8")
            encrypted = box.encrypt(message)

            # Return base64-encoded encrypted message and client public key
            encrypted_b64 = base64.b64encode(encrypted).decode("utf-8")
            client_public_key_b64 = self.get_public_key_b64()

            return encrypted_b64, client_public_key_b64

        except Exception as e:
            log.error(f"Encryption failed: {e}")
            raise CryptoError(f"Failed to encrypt credentials: {e}")


def encrypt_credential_data(
    username: Optional[str], password: Optional[str], cookies: Optional[str], server_public_key_b64: str
) -> Tuple[str, str]:
    """
    Helper function to encrypt credential data.

    Args:
        username: Username or None
        password: Password or None
        cookies: Cookie file content or None
        server_public_key_b64: Server's public key

    Returns:
        Tuple of (encrypted_data_b64, client_public_key_b64)
    """
    client_crypto = ClientCrypto()

    credentials = {}
    if username and password:
        credentials["username"] = username
        credentials["password"] = password
    if cookies:
        credentials["cookies"] = cookies

    return client_crypto.encrypt_credentials(credentials, server_public_key_b64)


def decrypt_credential_data(encrypted_data_b64: str, client_public_key_b64: str, server_keypair: ServerKeyPair) -> Dict[str, Any]:
    """
    Helper function to decrypt credential data.

    Args:
        encrypted_data_b64: Base64-encoded encrypted data
        client_public_key_b64: Client's public key
        server_keypair: Server's key pair

    Returns:
        Decrypted credentials dictionary
    """
    return server_keypair.decrypt_message(encrypted_data_b64, client_public_key_b64)


# Session-only authentication helpers


def serialize_authenticated_session(service_instance) -> Dict[str, Any]:
    """
    Serialize an authenticated service session for remote use.

    This extracts session cookies and headers WITHOUT including credentials.

    Args:
        service_instance: Authenticated service instance

    Returns:
        Dictionary with session data (cookies, headers) but NO credentials
    """
    from unshackle.core.api.session_serializer import serialize_session

    session_data = serialize_session(service_instance.session)

    # Add additional metadata
    session_data["authenticated"] = True
    session_data["service_tag"] = service_instance.__class__.__name__

    return session_data


def is_session_valid(session_data: Dict[str, Any]) -> bool:
    """
    Check if session data appears valid.

    Args:
        session_data: Session data dictionary

    Returns:
        True if session has cookies or auth headers
    """
    if not session_data:
        return False

    # Check for cookies or authorization headers
    has_cookies = bool(session_data.get("cookies"))
    has_auth = "Authorization" in session_data.get("headers", {})

    return has_cookies or has_auth


__all__ = [
    "ServerKeyPair",
    "ClientCrypto",
    "CryptoError",
    "encrypt_credential_data",
    "decrypt_credential_data",
    "serialize_authenticated_session",
    "is_session_valid",
    "NACL_AVAILABLE",
]
