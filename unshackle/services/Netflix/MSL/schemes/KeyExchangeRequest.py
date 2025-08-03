import base64

from .. import KeyExchangeSchemes
from ..MSLObject import MSLObject


# noinspection PyPep8Naming
class KeyExchangeRequest(MSLObject):
    def __init__(self, scheme, keydata):
        """
        Session key exchange data from a requesting entity.
        https://github.com/Netflix/msl/wiki/Key-Exchange-%28Configuration%29

        :param scheme: Key Exchange Scheme identifier
        :param keydata: Key Request data
        """
        self.scheme = str(scheme)
        self.keydata = keydata

    @classmethod
    def AsymmetricWrapped(cls, keypairid, mechanism, publickey):
        """
        Asymmetric wrapped key exchange uses a generated ephemeral asymmetric key pair for key exchange. It will
        typically be used when there is no other data or keys from which to base secure key exchange.

        This mechanism provides perfect forward secrecy but does not guarantee that session keys will only be available
        to the requesting entity if the requesting MSL stack has been modified to perform the operation on behalf of a
        third party.

        > Key Pair ID

        The key pair ID is included as a sanity check.

        > Mechanism & Public Key

        The following mechanisms are associated public key formats are currently supported.

            Field 	    Public  Key Format 	Description
            RSA 	    SPKI 	RSA-OAEP    encrypt/decrypt
            ECC 	    SPKI 	ECIES       encrypt/decrypt
            JWEJS_RSA 	SPKI 	RSA-OAEP    JSON Web Encryption JSON Serialization
            JWE_RSA 	SPKI 	RSA-OAEP    JSON Web Encryption Compact Serialization
            JWK_RSA 	SPKI 	RSA-OAEP    JSON Web Key
            JWK_RSAES 	SPKI 	RSA PKCS#1  JSON Web Key

        :param keypairid: key pair ID
        :param mechanism: asymmetric key type
        :param publickey: public key
        """
        return cls(
            scheme=KeyExchangeSchemes.AsymmetricWrapped,
            keydata={
                "keypairid": keypairid,
                "mechanism": mechanism,
                "publickey": base64.b64encode(publickey).decode("utf-8")
            }
        )

    @classmethod
    def Widevine(cls, keyrequest):
        """
        Google Widevine provides a secure key exchange mechanism. When requested the Widevine component will issue a
        one-time use key request. The Widevine server library can be used to authenticate the request and return
        randomly generated symmetric keys in a protected key response bound to the request and Widevine client library.
        The key response also specifies the key identities, types and their permitted usage.

        The Widevine key request also contains a model identifier and a unique device identifier with an expectation of
        long-term persistence. These values are available from the Widevine client library and can be retrieved from
        the key request by the Widevine server library.

        The Widevine client library will protect the returned keys from inspection or misuse.

        :param keyrequest: Base64-encoded Widevine CDM license challenge (PSSH: b'\x0A\x7A\x00\x6C\x38\x2B')
        """
        if not isinstance(keyrequest, str):
            keyrequest = base64.b64encode(keyrequest).decode()
        return cls(
            scheme=KeyExchangeSchemes.Widevine,
            keydata={"keyrequest": keyrequest}
        )
