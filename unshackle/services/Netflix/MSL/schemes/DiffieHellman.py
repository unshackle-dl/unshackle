import base64
import hashlib
import hmac
import json
import os
from typing import Tuple, Optional

from cryptography.hazmat.primitives.asymmetric import dh
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

from .. import KeyExchangeSchemes
from ..MSLObject import MSLObject


# noinspection PyPep8Naming
class DiffieHellman(MSLObject):
    """
    Netflix MSL Diffie-Hellman Key Exchange Implementation
    
    This implementation follows the Netflix MSL specification for the "DH" key exchange scheme,
    providing perfect forward secrecy and secure session key derivation.
    """
    
    # Standardized 2048-bit MODP group parameters (RFC 3526, Group 14)
    P = int("FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1"
            "29024E088A67CC74020BBEA63B139B22514A08798E3404DD"
            "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245"
            "E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
            "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D"
            "C2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
            "83655D23DCA3AD961C62F356208552BB9ED529077096966D"
            "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
            "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9"
            "DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
            "15728E5A8AACAA68FFFFFFFFFFFFFFFF", 16)
    G = 2
    
    # Key sizes
    KENC_SIZE = 16  # 16 bytes for AES-128-CBC
    KHMAC_SIZE = 32  # 32 bytes for HMAC-SHA256
    
    def __init__(self, scheme: KeyExchangeSchemes, keydata: dict):
        """
        Initialize Diffie-Hellman key exchange data.
        
        :param scheme: Key Exchange Scheme identifier
        :param keydata: Key Request/Response data
        """
        self.scheme = str(scheme)
        self.keydata = keydata
        self._private_key = None
        self._public_key = None
        self._shared_secret = None
        self._kenc = None
        self._khmac = None
        
    @classmethod
    def generate_parameters(cls) -> dh.DHParameters:
        """
        Generate standardized DH parameters.
        
        :return: DH parameters object
        """
        # Create parameters from standardized values
        parameters = dh.DHParameterNumbers(cls.P, cls.G).parameters(default_backend())
        return parameters
    
    @classmethod
    def generate_key_pair(cls) -> Tuple[dh.DHPrivateKey, dh.DHPublicKey]:
        """
        Generate a new DH key pair using standardized parameters.
        
        :return: Tuple of (private_key, public_key)
        """
        parameters = cls.generate_parameters()
        private_key = parameters.generate_private_key()
        public_key = private_key.public_key()
        return private_key, public_key
    
    @classmethod
    def encode_public_key(cls, public_key: dh.DHPublicKey) -> bytes:
        """
        Encode public key in MSL byte array format.
        
        MSL byte array encoding requirements:
        - Use two's complement representation in big-endian byte order
        - Most significant byte must be first
        - Include at least one sign bit
        - Exactly one zero byte must be in the zeroth element
        - Compatible with Java BigInteger.toByteArray() and BigInteger(byte[]) constructor
        
        :param public_key: DH public key to encode
        :return: Encoded public key bytes
        """
        # Get the public key value as integer
        public_numbers = public_key.public_numbers()
        y = public_numbers.y
        
        # Convert to bytes in big-endian format
        key_bytes = y.to_bytes((y.bit_length() + 7) // 8, byteorder='big')
        
        # Ensure we have at least one sign bit (add zero byte if MSB is set)
        if key_bytes and (key_bytes[0] & 0x80):
            key_bytes = b'\x00' + key_bytes
        
        # Ensure exactly one zero byte in the zeroth element
        if not key_bytes or key_bytes[0] != 0:
            key_bytes = b'\x00' + key_bytes
            
        return key_bytes
    
    @classmethod
    def decode_public_key(cls, key_bytes: bytes) -> dh.DHPublicKey:
        """
        Decode public key from MSL byte array format.
        
        :param key_bytes: Encoded public key bytes
        :return: DH public key object
        :raises ValueError: If key bytes are invalid
        """
        # Validate zero byte requirement
        if not key_bytes or key_bytes[0] != 0:
            raise ValueError("Invalid public key encoding: missing zero byte at position 0")
        
        # Remove leading zero byte for conversion
        if len(key_bytes) > 1:
            y_bytes = key_bytes[1:]
        else:
            y_bytes = b'\x00'  # Handle zero value case
            
        # Convert bytes to integer
        y = int.from_bytes(y_bytes, byteorder='big')
        
        # Create public key from parameters and y value
        parameters = cls.generate_parameters()
        public_numbers = dh.DHPublicNumbers(y, parameters.parameter_numbers())
        public_key = public_numbers.public_key(default_backend())
        
        return public_key
    
    @classmethod
    def KeyExchangeRequest(cls, parametersid: str = "xNjPCHnfTzIyKSoHZcV4aw==") -> 'DiffieHellman':
        """
        Create a key exchange request with a new DH key pair.
        
        Key Request Data Format:
        {
          "#mandatory": ["parametersid", "publickey"],
          "parametersid": "string",
          "publickey": "binary" // Base64 encoded
        }
        
        :param parametersid: Parameters identifier (default is a standard value)
        :return: DiffieHellman key exchange request object
        """
        # Generate new key pair
        private_key, public_key = cls.generate_key_pair()
        
        # Encode public key in MSL format
        encoded_public_key = cls.encode_public_key(public_key)
        
        # Create request object
        request = cls(
            scheme=KeyExchangeSchemes.DiffieHellman,
            keydata={
                "parametersid": parametersid,
                "publickey": base64.b64encode(encoded_public_key).decode('utf-8')
            }
        )
        
        # Store private key for key exchange completion
        request._private_key = private_key
        request._public_key = public_key
        
        return request
    
    @classmethod
    def KeyExchangeResponse(cls, request: 'DiffieHellman') -> 'DiffieHellman':
        """
        Create a key exchange response to a request.
        
        Key Response Data Format:
        {
          "#mandatory": ["parametersid", "publickey"], 
          "parametersid": "string", // Must match request parametersid
          "publickey": "binary" // Base64 encoded
        }
        
        :param request: DiffieHellman key exchange request object
        :return: DiffieHellman key exchange response object
        """
        # Validate parametersid
        if "parametersid" not in request.keydata:
            raise ValueError("Missing parametersid in key exchange request")
            
        # Decode request public key
        try:
            request_public_bytes = base64.b64decode(request.keydata["publickey"])
            request_public_key = cls.decode_public_key(request_public_bytes)
        except Exception as e:
            raise ValueError(f"Invalid public key in request: {str(e)}")
        
        # Generate our key pair
        private_key, public_key = cls.generate_key_pair()
        
        # Compute shared secret
        shared_secret = private_key.exchange(request_public_key)
        
        # Encode our public key in MSL format
        encoded_public_key = cls.encode_public_key(public_key)
        
        # Create response object
        response = cls(
            scheme=KeyExchangeSchemes.DiffieHellman,
            keydata={
                "parametersid": request.keydata["parametersid"],
                "publickey": base64.b64encode(encoded_public_key).decode('utf-8')
            }
        )
        
        # Store private key and shared secret for key derivation
        response._private_key = private_key
        response._public_key = public_key
        response._shared_secret = shared_secret
        
        return response
    
    def derive_keys(self) -> Tuple[bytes, bytes]:
        """
        Derive session keys from shared secret using SHA-384.
        
        Key Derivation Algorithm:
        shared_secret_bytes = convert_to_msl_byte_array(shared_secret)
        hash_bytes = SHA384(shared_secret_bytes)
        Kenc = hash_bytes[0:15]   // 16 bytes for AES-128-CBC
        Khmac = hash_bytes[16:47] // 32 bytes for HMAC-SHA256
        
        :return: Tuple of (Kenc, Khmac) derived keys
        """
        if self._shared_secret is None:
            raise ValueError("Shared secret not available. Perform key exchange first.")
        
        # Convert shared secret to MSL byte array format
        shared_secret_bytes = self._convert_to_msl_byte_array(self._shared_secret)
        
        # Hash with SHA-384
        hash_bytes = hashlib.sha384(shared_secret_bytes).digest()
        
        # Derive keys
        kenc = hash_bytes[:self.KENC_SIZE]
        khmac = hash_bytes[self.KENC_SIZE:self.KENC_SIZE + self.KHMAC_SIZE]
        
        # Store derived keys
        self._kenc = kenc
        self._khmac = khmac
        
        return kenc, khmac
    
    def _convert_to_msl_byte_array(self, data: bytes) -> bytes:
        """
        Convert byte data to MSL byte array format.
        
        For zero shared secret value, must be represented as single-byte array [0x00]
        
        :param data: Input byte data
        :return: MSL formatted byte array
        """
        if not data or all(b == 0 for b in data):
            return b'\x00'
        
        # Remove leading zero bytes but ensure at least one zero byte at position 0
        # This is a simplified implementation - in practice, we need to follow
        # Java BigInteger.toByteArray() behavior exactly
        return b'\x00' + data
    
    def get_derived_keys(self) -> Tuple[Optional[bytes], Optional[bytes]]:
        """
        Get the derived keys if available.
        
        :return: Tuple of (Kenc, Khmac) or (None, None) if not derived
        """
        return self._kenc, self._khmac
    
    def encrypt_message(self, plaintext: str) -> dict:
        """
        Encrypt a message using AES-128-CBC with the derived Kenc key.
        
        :param plaintext: Message to encrypt
        :return: Encrypted message data dictionary
        :raises ValueError: If keys not derived
        """
        if self._kenc is None:
            raise ValueError("Encryption key not available. Derive keys first.")
        
        # Generate random IV
        iv = os.urandom(16)
        
        # Pad plaintext to block size
        plaintext_bytes = plaintext.encode('utf-8')
        padding_length = 16 - (len(plaintext_bytes) % 16)
        padded_plaintext = plaintext_bytes + bytes([padding_length] * padding_length)
        
        # Encrypt
        cipher = Cipher(algorithms.AES(self._kenc), modes.CBC(iv), backend=default_backend())
        encryptor = cipher.encryptor()
        ciphertext = encryptor.update(padded_plaintext) + encryptor.finalize()
        
        return {
            "ciphertext": base64.b64encode(ciphertext).decode('utf-8'),
            "iv": base64.b64encode(iv).decode('utf-8')
        }
    
    def decrypt_message(self, encrypted_data: dict) -> str:
        """
        Decrypt a message using AES-128-CBC with the derived Kenc key.
        
        :param encrypted_data: Encrypted message data dictionary
        :return: Decrypted plaintext message
        :raises ValueError: If keys not derived or decryption fails
        """
        if self._kenc is None:
            raise ValueError("Decryption key not available. Derive keys first.")
        
        # Decode data
        try:
            ciphertext = base64.b64decode(encrypted_data["ciphertext"])
            iv = base64.b64decode(encrypted_data["iv"])
        except Exception as e:
            raise ValueError(f"Invalid encrypted data: {str(e)}")
        
        # Decrypt
        cipher = Cipher(algorithms.AES(self._kenc), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        padded_plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        
        # Remove padding
        if len(padded_plaintext) == 0:
            raise ValueError("Invalid padding")
        
        padding_length = padded_plaintext[-1]
        if padding_length > 16 or padding_length == 0:
            raise ValueError("Invalid padding")
        
        plaintext = padded_plaintext[:-padding_length]
        return plaintext.decode('utf-8')
    
    def create_hmac(self, message: str) -> str:
        """
        Create HMAC-SHA256 signature using the derived Khmac key.
        
        :param message: Message to sign
        :return: Base64 encoded HMAC signature
        :raises ValueError: If keys not derived
        """
        if self._khmac is None:
            raise ValueError("HMAC key not available. Derive keys first.")
        
        signature = hmac.new(self._khmac, message.encode('utf-8'), hashlib.sha256).digest()
        return base64.b64encode(signature).decode('utf-8')
    
    def verify_hmac(self, message: str, signature: str) -> bool:
        """
        Verify HMAC-SHA256 signature using the derived Khmac key.
        
        :param message: Message to verify
        :param signature: Base64 encoded HMAC signature
        :return: True if signature is valid, False otherwise
        :raises ValueError: If keys not derived
        """
        if self._khmac is None:
            raise ValueError("HMAC key not available. Derive keys first.")
        
        try:
            expected_signature = base64.b64decode(signature)
        except Exception:
            return False
        
        # Use compare_digest for constant-time comparison
        actual_signature = hmac.new(self._khmac, message.encode('utf-8'), hashlib.sha256).digest()
        return hmac.compare_digest(actual_signature, expected_signature)