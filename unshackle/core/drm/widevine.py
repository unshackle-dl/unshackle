from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any
from uuid import UUID

import click
from construct import Container
from pymp4.parser import Box
from pywidevine.cdm import Cdm as WidevineCdm
from pywidevine.pssh import PSSH

from unshackle.core.cdm.http_cdm import HttpCdm
from unshackle.core.drm.cenc import PLACEHOLDER_KIDS, WIDEVINE_ID, CencDrm, as_kid
from unshackle.core.remote_protocol import APIErrorCode
from unshackle.core.utilities import call_licence, log_event


class Widevine(CencDrm):
    """Widevine DRM System."""

    SYSTEM_ID = WIDEVINE_ID
    NAME = "Widevine"
    PLACEHOLDER_KIDS = PLACEHOLDER_KIDS

    def __init__(self, pssh: PSSH, kid: UUID | str | bytes | None = None, **kwargs: Any) -> None:
        if not pssh:
            raise ValueError("Provided PSSH is empty.")
        if not isinstance(pssh, PSSH):
            raise TypeError(f"Expected pssh to be a {PSSH}, not {pssh!r}")

        if pssh.system_id == PSSH.SystemId.PlayReady:
            pssh = self.from_playready(pssh)

        self._kid = as_kid(kid)
        if self._kid:
            if pssh.key_ids and all(k in self.PLACEHOLDER_KIDS for k in pssh.key_ids):
                pssh.set_key_ids([self._kid])
            elif self._kid not in (pssh.key_ids or []):
                pssh.set_key_ids([*(pssh.key_ids or []), self._kid])

        self._pssh = pssh

        if not self.kids:
            raise Widevine.Exceptions.KIDNotFound("No Key ID was found within PSSH and none were provided.")

        self.content_keys: dict[UUID, str] = {}
        self.data: dict = kwargs or {}

    @staticmethod
    def from_playready(pssh: PSSH) -> PSSH:
        """A Widevine PSSH naming a v1 box's own KIDs, then the KIDs the PlayReady class reads from it.

        pywidevine's own conversion fails on a bare WRMHEADER and byte-swaps PlayReady KIDs.
        """
        from pyplayready.misc.exceptions import InvalidPssh

        from unshackle.core.drm.playready import PlayReady

        box = pssh.dump()
        kids: list[UUID] = list(Box.parse(box).key_IDs or [])
        try:
            kids += PlayReady.from_pssh_b64(base64.b64encode(box).decode()).kids
        except PlayReady.Exceptions.KIDNotFound:
            pass
        except InvalidPssh:
            if not kids:
                raise
        return PSSH.new(system_id=PSSH.SystemId.Widevine, key_ids=list(dict.fromkeys(kids)))

    @classmethod
    def from_pssh_box(cls, box: Container, kid: UUID | None) -> Widevine:
        """Make the DRM from a parsed Widevine pssh box and the track's own KID."""
        return cls(pssh=PSSH(box), kid=kid)

    @property
    def pssh(self) -> PSSH:
        """Get Protection System Specific Header Box."""
        return self._pssh

    @property
    def kids(self) -> list[UUID]:
        """Get all Key IDs from PSSH, falling back to the externally provided KID."""
        pssh_kids = self._pssh.key_ids
        if pssh_kids:
            return pssh_kids
        if self._kid:
            return [self._kid]
        return []

    def get_content_keys(self, cdm: WidevineCdm, certificate: Callable, licence: Callable) -> None:
        """
        Make a CDM Session and get Content Keys for this DRM Instance.
        The certificate and license params are functions. unshackle gives them
        the challenge and the session ID.
        """
        for kid in self.kids:
            if kid in self.content_keys:
                continue

            session_id = cdm.open()

            try:
                cert = certificate(challenge=cdm.service_certificate_challenge)
                if cert and hasattr(cdm, "set_service_certificate"):
                    cdm.set_service_certificate(session_id, cert)

                if isinstance(cdm, HttpCdm):
                    cdm.set_required_kids(self.kids, session_id=session_id)

                challenge = cdm.get_license_challenge(session_id, self.pssh)

                if isinstance(cdm, HttpCdm) and cdm.has_cached_keys(session_id):
                    pass
                elif not challenge:
                    raise Widevine.Exceptions.EmptyLicense(
                        "The CDM returned an empty licence challenge and has no cached keys"
                    )
                else:
                    log_event(
                        "drm_license_request",
                        level="DEBUG",
                        message=f"Requesting Widevine license for KID {kid.hex}",
                        drm_type="Widevine",
                        kid=kid.hex,
                        challenge_size=len(challenge),
                        kid_count=len(self.kids),
                    )
                    license_res = call_licence(licence, challenge, pssh=self.pssh, session_id=session_id)
                    try:
                        cdm.parse_license(session_id, license_res)
                    except Exception as e:
                        raise Widevine.Exceptions.EmptyLicense(
                            f"No usable Widevine licence for KID {kid.hex}: {e}"
                        ) from e
                    log_event(
                        "drm_license_response",
                        level="DEBUG",
                        message=f"Parsed Widevine license for KID {kid.hex}",
                        drm_type="Widevine",
                        kid=kid.hex,
                        response_size=len(license_res) if license_res else 0,
                    )

                self.content_keys = {key.kid: key.key.hex() for key in cdm.get_keys(session_id, "CONTENT")}

                if self.content_keys:
                    log_event(
                        "drm_content_keys",
                        level="INFO",
                        message=f"Recovered {len(self.content_keys)} Widevine content key(s)",
                        drm_type="Widevine",
                        kid=kid.hex,
                        key_count=len(self.content_keys),
                        keys=[{"kid": k.hex, "key": v} for k, v in self.content_keys.items()],
                    )

                if not self.content_keys:
                    raise Widevine.Exceptions.EmptyLicense("No Content Keys were within the License")

                if kid not in self.content_keys:
                    raise Widevine.Exceptions.CEKNotFound(f"No Content Key for KID {kid.hex} within the License")
            finally:
                cdm.close(session_id)

    class Exceptions:
        class PSSHNotFound(Exception):
            """PSSH (Protection System Specific Header) was not found."""

            error_code = APIErrorCode.DRM_ERROR

        class KIDNotFound(Exception):
            """KID (Encryption Key ID) was not found."""

            error_code = APIErrorCode.DRM_ERROR

        class CEKNotFound(click.ClickException):
            """CEK (Content Encryption Key) for KID was not found in License."""

            error_code = APIErrorCode.DRM_ERROR

        class EmptyLicense(click.ClickException):
            """License returned no Content Encryption Keys."""

            error_code = APIErrorCode.DRM_ERROR


__all__ = ("Widevine",)
