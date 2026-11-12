from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Callable, Optional, Union
from uuid import UUID

import m3u8
from construct import Container
from pymp4.parser import Box
from pywidevine.cdm import Cdm as WidevineCdm
from pywidevine.pssh import PSSH

from unshackle.core.config import config
from unshackle.core.constants import AnyTrack
from unshackle.core.drm import key_args as drm_key_args
from unshackle.core.drm.decrypters import decrypt_cenc, mp4decrypt, shaka_decrypt
from unshackle.core.session import AnySession, utf8_text
from unshackle.core.utilities import call_licence, get_boxes, log_event
from unshackle.core.utils import run_proxy
from unshackle.core.utils.subprocess import ffprobe


class Widevine:
    """Widevine DRM System."""

    PLACEHOLDER_KIDS = {
        UUID("00000000-0000-0000-0000-000000000000"),  # All zeros (key rotation default)
        UUID("00010203-0405-0607-0809-0a0b0c0d0e0f"),  # Sequential 0x00-0x0f
        UUID("00010203-0405-0607-0809-101112131415"),  # Shaka Packager test pattern
    }

    def __init__(self, pssh: PSSH, kid: Union[UUID, str, bytes, None] = None, **kwargs: Any) -> None:
        if not pssh:
            raise ValueError("Provided PSSH is empty.")
        if not isinstance(pssh, PSSH):
            raise TypeError(f"Expected pssh to be a {PSSH}, not {pssh!r}")

        if pssh.system_id == PSSH.SystemId.PlayReady:
            pssh = self.from_playready(pssh)

        self._kid: Optional[UUID] = None
        if kid:
            if isinstance(kid, str):
                kid = UUID(hex=kid)
            elif isinstance(kid, bytes):
                kid = UUID(bytes=kid)
            if not isinstance(kid, UUID):
                raise ValueError(f"Expected kid to be a {UUID}, str, or bytes, not {kid!r}")
            self._kid = kid
            if pssh.key_ids and all(k in self.PLACEHOLDER_KIDS for k in pssh.key_ids):
                pssh.set_key_ids([kid])
            elif kid not in (pssh.key_ids or []):
                pssh.set_key_ids([*(pssh.key_ids or []), kid])

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
    def from_track(cls, track: AnyTrack, session: Optional[AnySession] = None) -> Widevine:
        """
        Get PSSH and KID from within the Initiation Segment of the Track Data.
        It also tries to get PSSH and KID from other track data like M3U8 data
        as well as through FFprobe.

        Make a Widevine DRM System object from a track's information.
        Use this method only when a PSSH cannot be given directly.
        It is *rare* to need to use this.

        You can give your own `requests.Session` to use custom headers
        and more.

        Raises:
            PSSHNotFound: If the PSSH was not found within the data.
            KIDNotFound: If the KID was not found within the data or PSSH.
        """
        if not session:
            session = run_proxy.fallback_session()
            session.headers.update(config.headers)

        kid: Optional[UUID] = None
        pssh_boxes: list[Container] = []
        tenc_boxes: list[Container] = []

        if track.descriptor == track.Descriptor.HLS:
            m3u_url = track.url
            assert isinstance(m3u_url, str)
            master = m3u8.loads(utf8_text(session.get(m3u_url)), uri=m3u_url)
            pssh_boxes.extend(
                Box.parse(base64.b64decode(x.uri.split(",")[-1]))
                for x in (master.session_keys or master.keys)
                if x and x.keyformat and x.keyformat.lower() == WidevineCdm.urn
            )

        init_data = track.get_init_segment(session=session)
        if init_data:
            # try get via ffprobe, needed for non mp4 data e.g. WEBM from Google Play
            probe = ffprobe(init_data)
            if probe:
                for stream in probe.get("streams") or []:
                    enc_key_id = stream.get("tags", {}).get("enc_key_id")
                    if enc_key_id:
                        kid = UUID(bytes=base64.b64decode(enc_key_id))
            pssh_boxes.extend(list(get_boxes(init_data, b"pssh")))
            tenc_boxes.extend(list(get_boxes(init_data, b"tenc")))

        pssh = next((b for b in pssh_boxes if b.system_ID == PSSH.SystemId.Widevine), None)
        if not pssh:
            raise Widevine.Exceptions.PSSHNotFound("PSSH was not found in track data.")

        tenc = next(iter(tenc_boxes), None)
        if not kid and tenc and tenc.key_ID.int != 0:
            kid = tenc.key_ID

        return cls(pssh=PSSH(pssh), kid=kid)

    @classmethod
    def from_init_data(cls, init_data: bytes) -> Widevine:
        """
        Get PSSH and KID from within Initialization Segment Data.

        Use this method only when a PSSH cannot be given directly.
        It is *rare* to need to use this.

        Raises:
            PSSHNotFound: If the PSSH was not found within the data.
            KIDNotFound: If the KID was not found within the data or PSSH.
        """
        if not init_data:
            raise ValueError("Init data should be provided.")
        if not isinstance(init_data, bytes):
            raise TypeError(f"Expected init data to be bytes, not {init_data!r}")

        kid: Optional[UUID] = None
        pssh_boxes: list[Container] = list(get_boxes(init_data, b"pssh"))
        tenc_boxes: list[Container] = list(get_boxes(init_data, b"tenc"))

        # try get via ffprobe, needed for non mp4 data e.g. WEBM from Google Play
        probe = ffprobe(init_data)
        if probe:
            for stream in probe.get("streams") or []:
                enc_key_id = stream.get("tags", {}).get("enc_key_id")
                if enc_key_id:
                    kid = UUID(bytes=base64.b64decode(enc_key_id))

        pssh = next((b for b in pssh_boxes if b.system_ID == PSSH.SystemId.Widevine), None)
        if not pssh:
            raise Widevine.Exceptions.PSSHNotFound("PSSH was not found in track data.")

        tenc = next(iter(tenc_boxes), None)
        if not kid and tenc and tenc.key_ID.int != 0:
            kid = tenc.key_ID

        return cls(pssh=PSSH(pssh), kid=kid)

    @property
    def pssh(self) -> PSSH:
        """Get Protection System Specific Header Box."""
        return self._pssh

    @property
    def kid(self) -> Optional[UUID]:
        """Get first Key ID, if any."""
        return next(iter(self.kids), None)

    @property
    def kids(self) -> list[UUID]:
        """Get all Key IDs from PSSH, falling back to the externally provided KID."""
        pssh_kids = self._pssh.key_ids
        if pssh_kids:
            return pssh_kids
        if self._kid:
            return [self._kid]
        return []

    def to_dict(self) -> dict[str, Any]:
        """Serialise this DRM instance for export/import (PSSH + KIDs).

        unshackle stores the content keys once at the export's track level, and does not
        duplicate them here.
        """
        return {
            "system": "Widevine",
            "pssh_b64": self.pssh.dumps(),
            "kids": [kid.hex for kid in self.kids],
        }

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

                if hasattr(cdm, "set_required_kids"):
                    try:
                        cdm.set_required_kids(self.kids, session_id=session_id)
                    except TypeError:  # CDM predating the per-session signature
                        cdm.set_required_kids(self.kids)

                challenge = cdm.get_license_challenge(session_id, self.pssh)

                if hasattr(cdm, "has_cached_keys") and cdm.has_cached_keys(session_id):
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
                    cdm.parse_license(session_id, license_res)
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

    def decrypt(self, path: Path) -> None:
        """Decrypt the file at ``path`` in place with this track's Widevine content keys."""
        decrypt_cenc(self, path, "Widevine")

    def own_kids(self) -> list[UUID]:
        """Return the KIDs that identify this track's own content key.

        The KID given to the constructor (from the manifest or the init segment) names the
        track exactly. A PSSH can name the KIDs of other renditions too.
        """
        if self._kid and self._kid in self.content_keys:
            return [self._kid]
        return self.kids

    def mp4decrypt_key_args(self, track_kids: Optional[dict[int, UUID]] = None) -> list[str]:
        """Build the mp4decrypt --key arguments for every content key, the zero-KID and the track ID fallbacks."""
        return drm_key_args.mp4decrypt_key_args(self.content_keys, self.own_kids(), track_kids)

    def decrypt_with_mp4decrypt(self, path: Path) -> None:
        """Decrypt with mp4decrypt."""
        mp4decrypt(path, self.mp4decrypt_key_args)

    def decrypt_with_shaka_packager(self, path: Path) -> None:
        """Decrypt with shaka-packager."""
        shaka_decrypt(path, drm_key_args.shaka_keys(self.content_keys, self.own_kids()), "Widevine")

    class Exceptions:
        class PSSHNotFound(Exception):
            """PSSH (Protection System Specific Header) was not found."""

        class KIDNotFound(Exception):
            """KID (Encryption Key ID) was not found."""

        class CEKNotFound(Exception):
            """CEK (Content Encryption Key) for KID was not found in License."""

        class EmptyLicense(Exception):
            """License returned no Content Encryption Keys."""


__all__ = ("Widevine",)
