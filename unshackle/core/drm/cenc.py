"""Which CENC systems an init segment or an HLS key list carries, and which KID belongs to the track."""

from __future__ import annotations

import base64
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from uuid import UUID

import m3u8
from construct import Container
from pymp4.parser import Box
from pyplayready.system.pssh import PSSH as PR_PSSH
from pywidevine.cdm import Cdm as WidevineCdm
from pywidevine.pssh import PSSH as WV_PSSH

from unshackle.core.config import config
from unshackle.core.constants import AnyTrack
from unshackle.core.drm import key_args as drm_key_args
from unshackle.core.drm.decrypters import decrypt_cenc, mp4decrypt, shaka_decrypt
from unshackle.core.drm.verify import PIFF_TENC
from unshackle.core.session import AnySession, utf8_text
from unshackle.core.utilities import get_boxes
from unshackle.core.utils import run_proxy
from unshackle.core.utils.subprocess import ffprobe

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from unshackle.core.drm.playready import PlayReady
    from unshackle.core.drm.widevine import Widevine

WIDEVINE_ID: UUID = WV_PSSH.SystemId.Widevine
PLAYREADY_ID: UUID = PR_PSSH.SYSTEM_ID
KEYFORMATS = {
    WidevineCdm.urn: WIDEVINE_ID,
    f"urn:uuid:{PLAYREADY_ID}": PLAYREADY_ID,
    "com.microsoft.playready": PLAYREADY_ID,
}
PLACEHOLDER_KIDS = {
    UUID("00000000-0000-0000-0000-000000000000"),  # All zeros (key rotation default)
    UUID("00010203-0405-0607-0809-0a0b0c0d0e0f"),  # Sequential 0x00-0x0f
    UUID("00010203-0405-0607-0809-101112131415"),  # Shaka Packager test pattern
}


def key_system(key: Any) -> UUID | None:
    """The system ID that an EXT-X-KEY's KEYFORMAT names, or None when it names neither Widevine nor PlayReady."""
    return KEYFORMATS.get((getattr(key, "keyformat", None) or "").lower())


def cenc_key(key: Any) -> bool:
    """True when an EXT-X-KEY gives a CENC DRM: METHOD ISO-23001-7, or a Widevine or PlayReady KEYFORMAT."""
    return bool(key) and (key.method == "ISO-23001-7" or key_system(key) is not None)


def key_data(key: Any) -> str:
    """The base64 PSSH at the end of an EXT-X-KEY data URI."""
    return str(key.uri.split(",")[-1])


def init_kid(init_data: bytes) -> UUID | None:
    """The track's own KID: FFprobe's enc_key_id, then the first real tenc KID, then the first real PIFF tenc KID.

    A pssh box is no source, because it can name the KIDs of other renditions too. The PIFF box is read by
    offset, and only when its AlgorithmID is not 0 (clear): pymp4 parses it but cannot build it again, so
    ``get_boxes`` raises on it.
    """
    probe = ffprobe(init_data)
    for stream in (probe or {}).get("streams") or []:
        enc_key_id = stream.get("tags", {}).get("enc_key_id")
        if enc_key_id:
            return UUID(bytes=base64.b64decode(enc_key_id))
    for tenc in get_boxes(init_data, b"tenc"):
        if tenc.key_ID.int != 0:
            return tenc.key_ID
    marker = b"uuid" + PIFF_TENC
    at = init_data.find(marker)
    while at != -1:
        kid = init_data[at + 28 : at + 44]
        if len(kid) == 16 and any(kid) and init_data[at + 26]:
            return UUID(bytes=kid)
        at = init_data.find(marker, at + 1)
    return None


def hls_key_drm(key: Any, kid: UUID | None = None) -> Widevine | PlayReady | None:
    """The Widevine or PlayReady DRM of an EXT-X-KEY, or None for another KEYFORMAT.

    Widevine takes the tag's own KEYID first. It takes ``kid`` (from the init segment) only when the
    tag and the PSSH carry no real KID, because a v0 PSSH can carry none.
    """
    from unshackle.core.drm.playready import PlayReady
    from unshackle.core.drm.widevine import Widevine

    system = key_system(key)
    if system == PLAYREADY_ID:
        return PlayReady.from_pssh_b64(key_data(key))
    if system != WIDEVINE_ID:
        return None
    extra_params = dict(key._extra_params)
    extra_params.pop("pssh", None)
    pssh = WV_PSSH(key_data(key))
    if kid in PLACEHOLDER_KIDS or any(k not in PLACEHOLDER_KIDS for k in pssh.key_ids):
        kid = None
    keyid, kid_attr = extra_params.pop("keyid", None), extra_params.pop("kid", None)
    tag_kid = tag_key_id(keyid) or tag_key_id(kid_attr)
    return Widevine(pssh=pssh, kid=tag_kid or kid, **extra_params)


def drm_stub(drm_type: str, kid_hexes: list[str]) -> Widevine | PlayReady:
    """A PlayReady (``drm_type`` ``playready``) or else Widevine DRM that names ``kid_hexes``, to hold content keys got without its PSSH.

    The PSSH is built here, so it is for display only: the caller already has the content keys.
    """
    from unshackle.core.drm.playready import PlayReady
    from unshackle.core.drm.widevine import Widevine

    kids = [UUID(hex=k) for k in kid_hexes]
    if drm_type != "playready":
        # no kid: the stub names several KIDs, and the first is not the track's own
        return Widevine(pssh=WV_PSSH.new(system_id=WIDEVINE_ID, key_ids=[*kids]))
    wrm_header = (
        '<WRMHEADER xmlns="http://schemas.microsoft.com/DRM/2007/03/PlayReadyHeader" version="4.0.0.0">'
        "<DATA><PROTECTINFO><KEYLEN>16</KEYLEN><ALGID>AESCTR</ALGID></PROTECTINFO>"
        f"<KID>{base64.b64encode(kids[0].bytes_le).decode()}</KID></DATA></WRMHEADER>"
    )
    drm = PlayReady.from_pssh_b64(base64.b64encode(wrm_header.encode("utf-16-le")).decode())
    drm.kids.extend(kid for kid in kids if kid not in drm.kids)
    return drm


def tag_key_id(value: Any) -> UUID | None:
    """The KID of an EXT-X-KEY KEYID or KID attribute, or None when it is absent, a placeholder or not a KID."""
    try:
        kid = as_kid(value.removeprefix("0x").removeprefix("0X") if value else None)
    except ValueError:
        log.debug(f"Ignoring the EXT-X-KEY KEYID {value!r}: it is not a Key ID")
        return None
    return None if kid in PLACEHOLDER_KIDS else kid


def as_kid(kid: UUID | str | bytes | None) -> UUID | None:
    """A KID given as a UUID, a hex string or 16 bytes, as a UUID."""
    if not kid:
        return None
    if isinstance(kid, str):
        kid = UUID(hex=kid)
    elif isinstance(kid, bytes):
        kid = UUID(bytes=kid)
    if not isinstance(kid, UUID):
        raise ValueError(f"Expected kid to be a {UUID}, str, or bytes, not {kid!r}")
    return kid


class CencDrm:
    """What Widevine and PlayReady share: reading their PSSH and KID from track data, and the CENC decrypt.

    A subclass gives its ``SYSTEM_ID``, its display ``NAME``, ``from_pssh_box`` and its licence step.
    ``from_track`` and ``from_init_data`` are for when the caller has no PSSH to give. They raise
    PSSHNotFound when the data has no pssh of this system, and the constructor raises KIDNotFound.
    """

    SYSTEM_ID: UUID
    NAME: str
    Exceptions: Any
    content_keys: dict[UUID, str]
    _kid: UUID | None

    @classmethod
    def from_pssh_box(cls, box: Container, kid: UUID | None) -> Self:
        """Make the DRM from a parsed pssh box of this system and the track's own KID."""
        raise NotImplementedError

    @classmethod
    def from_boxes(cls, boxes: Iterable[Container], init_data: bytes | None) -> Self:
        """Make the DRM from the first pssh box of this system, with the KID of ``init_data``."""
        box = next((box for box in boxes if box.system_ID == cls.SYSTEM_ID), None)
        if box is None:
            raise cls.Exceptions.PSSHNotFound("PSSH was not found in track data.")
        return cls.from_pssh_box(box, init_kid(init_data) if init_data else None)

    @classmethod
    def from_track(cls, track: AnyTrack, session: AnySession | None = None) -> Self:
        """Make the DRM from a track's HLS playlist keys and its init segment."""
        if not session:
            session = run_proxy.fallback_session()
            session.headers.update(config.headers)

        boxes: list[Container] = []
        if track.descriptor == track.Descriptor.HLS:
            m3u_url = track.url
            assert isinstance(m3u_url, str)
            master = m3u8.loads(utf8_text(session.get(m3u_url)), uri=m3u_url)
            boxes.extend(
                Box.parse(base64.b64decode(key_data(key)))
                for key in (master.session_keys or master.keys)
                if key_system(key) == cls.SYSTEM_ID
            )

        init_data = track.get_init_segment(session=session)
        if init_data:
            boxes.extend(get_boxes(init_data, b"pssh"))
        return cls.from_boxes(boxes, init_data)

    @classmethod
    def from_init_data(cls, init_data: bytes) -> Self:
        """Make the DRM from the pssh and KID of an init segment."""
        if not init_data:
            raise ValueError("Init data should be provided.")
        if not isinstance(init_data, bytes):
            raise TypeError(f"Expected init data to be bytes, not {init_data!r}")
        return cls.from_boxes(get_boxes(init_data, b"pssh"), init_data)

    @property
    def kids(self) -> list[UUID]:
        """All the KIDs this DRM names."""
        raise NotImplementedError

    @property
    def kid(self) -> UUID | None:
        """The first KID, if any."""
        return next(iter(self.kids), None)

    def to_dict(self) -> dict[str, Any]:
        """This DRM in the export form; see ``wire.drm_to_dict``."""
        from unshackle.core.wire import drm_to_dict

        return drm_to_dict(self)  # type: ignore[arg-type]

    def own_kids(self) -> list[UUID]:
        """Return the KIDs that identify this track's own content key.

        The KID given to the constructor (from the manifest or the init segment) names the
        track exactly. A PSSH can name the KIDs of other renditions too.
        """
        if self._kid and self._kid in self.content_keys:
            return [self._kid]
        return self.kids

    def decrypt(self, path: Path) -> None:
        """Decrypt the file at ``path`` in place with this track's content keys."""
        decrypt_cenc(self, path, self.NAME)

    def mp4decrypt_key_args(self, track_kids: dict[int, UUID] | None = None) -> list[str]:
        """Build the mp4decrypt --key arguments for every content key, the zero-KID and the track ID fallbacks."""
        return drm_key_args.mp4decrypt_key_args(self.content_keys, self.own_kids(), track_kids)

    def decrypt_with_mp4decrypt(self, path: Path) -> None:
        """Decrypt with mp4decrypt."""
        mp4decrypt(path, self.mp4decrypt_key_args)

    def decrypt_with_shaka_packager(self, path: Path) -> None:
        """Decrypt with shaka-packager."""
        shaka_decrypt(path, drm_key_args.shaka_keys(self.content_keys, self.own_kids()), self.NAME)
