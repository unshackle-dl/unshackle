from enum import Enum


class Scheme(Enum):
    def __str__(self):
        return str(self.value)


class EntityAuthenticationSchemes(Scheme):
    """https://github.com/Netflix/msl/wiki/Entity-Authentication-%28Configuration%29"""
    Unauthenticated = "NONE"
    Widevine = "WIDEVINE"


class UserAuthenticationSchemes(Scheme):
    """https://github.com/Netflix/msl/wiki/User-Authentication-%28Configuration%29"""
    EmailPassword = "EMAIL_PASSWORD"
    NetflixIDCookies = "NETFLIXID"


class KeyExchangeSchemes(Scheme):
    """https://github.com/Netflix/msl/wiki/Key-Exchange-%28Configuration%29"""
    AsymmetricWrapped = "ASYMMETRIC_WRAPPED"
    Widevine = "WIDEVINE"
    DiffieHellman = "DH"
