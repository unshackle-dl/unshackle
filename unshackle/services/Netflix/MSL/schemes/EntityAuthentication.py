from .. import EntityAuthenticationSchemes
from ..MSLObject import MSLObject


# noinspection PyPep8Naming
class EntityAuthentication(MSLObject):
    def __init__(self, scheme, authdata):
        """
        Data used to identify and authenticate the entity associated with a message.
        https://github.com/Netflix/msl/wiki/Entity-Authentication-%28Configuration%29

        :param scheme: Entity Authentication Scheme identifier
        :param authdata: Entity Authentication data
        """
        self.scheme = str(scheme)
        self.authdata = authdata

    @classmethod
    def Unauthenticated(cls, identity):
        """
        The unauthenticated entity authentication scheme does not provide encryption or authentication and only
        identifies the entity. Therefore entity identities can be harvested and spoofed. The benefit of this
        authentication scheme is that the entity has control over its identity. This may be useful if the identity is
        derived from or related to other data, or if retaining the identity is desired across state resets or in the
        event of MSL errors requiring entity re-authentication.
        """
        return cls(
            scheme=EntityAuthenticationSchemes.Unauthenticated,
            authdata={"identity": identity}
        )

    @classmethod
    def Widevine(cls, devtype, keyrequest):
        """
        The Widevine entity authentication scheme is used by devices with the Widevine CDM. It does not provide
        encryption or authentication and only identifies the entity. Therefore entity identities can be harvested
        and spoofed. The entity identity is composed from the provided device type and Widevine key request data. The
        Widevine CDM properties can be extracted from the key request data.

        When coupled with the Widevine key exchange scheme, the entity identity can be cryptographically validated by
        comparing the entity authentication key request data against the key exchange key request data.

        Note that the local entity will not know its entity identity when using this scheme.

        > Devtype

        An arbitrary value identifying the device type the local entity wishes to assume. The data inside the Widevine
        key request may be optionally used to validate the claimed device type.

        :param devtype: Local entity device type
        :param keyrequest: Widevine key request
        """
        return cls(
            scheme=EntityAuthenticationSchemes.Widevine,
            authdata={
                "devtype": devtype,
                "keyrequest": keyrequest
            }
        )
