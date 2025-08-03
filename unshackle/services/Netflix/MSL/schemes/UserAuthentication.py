from ..MSLObject import MSLObject
from . import UserAuthenticationSchemes


# noinspection PyPep8Naming
class UserAuthentication(MSLObject):
    def __init__(self, scheme, authdata):
        """
        Data used to identify and authenticate the user associated with a message.
        https://github.com/Netflix/msl/wiki/User-Authentication-%28Configuration%29

        :param scheme: User Authentication Scheme identifier
        :param authdata: User Authentication data
        """
        self.scheme = str(scheme)
        self.authdata = authdata

    @classmethod
    def EmailPassword(cls, email, password):
        """
        Email and password is a standard user authentication scheme in wide use.

        :param email: user email address
        :param password: user password
        """
        return cls(
            scheme=UserAuthenticationSchemes.EmailPassword,
            authdata={
                "email": email,
                "password": password
            }
        )

    @classmethod
    def NetflixIDCookies(cls, netflixid, securenetflixid):
        """
        Netflix ID HTTP cookies are used when the user has previously logged in to a web site. Possession of the
        cookies serves as proof of user identity, in the same manner as they do when communicating with the web site.

        The Netflix ID cookie and Secure Netflix ID cookie are HTTP cookies issued by the Netflix web site after
        subscriber login. The Netflix ID cookie is encrypted and identifies the subscriber and analogous to a
        subscriber’s username. The Secure Netflix ID cookie is tied to a Netflix ID cookie and only sent over HTTPS
        and analogous to a subscriber’s password.

        In some cases the Netflix ID and Secure Netflix ID cookies will be unavailable to the MSL stack or application.
        If either or both of the Netflix ID or Secure Netflix ID cookies are absent in the above data structure the
        HTTP cookie headers will be queried for it; this is only acceptable when HTTPS is used as the underlying
        transport protocol.

        :param netflixid: Netflix ID cookie
        :param securenetflixid: Secure Netflix ID cookie
        """
        return cls(
            scheme=UserAuthenticationSchemes.NetflixIDCookies,
            authdata={
                "netflixid": netflixid,
                "securenetflixid": securenetflixid
            }
        )
