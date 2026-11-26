from __future__ import annotations

import base64
import re
from collections.abc import Callable
from threading import Lock
from typing import Any
from uuid import UUID

import click
from construct import Container
from pymp4.parser import Box
from pyplayready.cdm import Cdm as PlayReadyCdm
from pyplayready.misc.exceptions import InvalidPssh
from pyplayready.system.pssh import PSSH
from pyplayready.system.wrmheader import WRMHeader

from unshackle.core.cdm.http_cdm import HttpCdm
from unshackle.core.drm.cenc import PLAYREADY_ID, CencDrm, as_kid
from unshackle.core.remote_protocol import APIErrorCode
from unshackle.core.utilities import call_licence, log_event


class PlayReady(CencDrm):
    """PlayReady DRM System."""

    SYSTEM_ID = PLAYREADY_ID
    NAME = "PlayReady"

    _ABSORB_LOCK = Lock()

    def __init__(
        self,
        pssh: PSSH,
        kid: UUID | str | bytes | None = None,
        pssh_b64: str | None = None,
        **kwargs: Any,
    ) -> None:
        if not pssh:
            raise ValueError("Provided PSSH is empty.")
        if not isinstance(pssh, PSSH):
            raise TypeError(f"Expected pssh to be a {PSSH}, not {pssh!r}")

        kids: list[UUID] = []
        for header in pssh.wrm_headers:
            for header_kid in self.header_kids(header):
                if header_kid not in kids:
                    kids.append(header_kid)
        if pssh_b64:
            for extra_kid in self.extract_kids_from_pssh_b64(pssh_b64):
                if extra_kid not in kids:
                    kids.append(extra_kid)

        self._kid = as_kid(kid)
        if self._kid and self._kid not in kids:
            kids.append(self._kid)

        self._pssh = pssh
        self._kids = kids
        self._extra_headers: list[WRMHeader] = []
        self._refused_headers: set[str] = set()

        if not self.kids:
            raise PlayReady.Exceptions.KIDNotFound("No Key ID was found within PSSH and none were provided.")

        self.content_keys: dict[UUID, str] = {}
        self.data: dict = kwargs or {}
        if pssh_b64:
            self.data.setdefault("pssh_b64", pssh_b64)

    def extract_kids_from_pssh_b64(self, pssh_b64: str) -> list[UUID]:
        """Extract all KIDs from base64-encoded PSSH data."""
        try:
            # PSSH XML comes from third-party manifests; defusedxml guards against entity expansion
            import defusedxml.ElementTree as ET

            # Decode the PSSH
            pssh_bytes = base64.b64decode(pssh_b64)

            # Try to find XML in the PSSH data
            pssh_str = pssh_bytes.decode("utf-16le", errors="ignore")

            # Find WRMHEADER
            xml_start = pssh_str.find("<WRMHEADER")
            if xml_start == -1:
                # Try UTF-8
                pssh_str = pssh_bytes.decode("utf-8", errors="ignore")
                xml_start = pssh_str.find("<WRMHEADER")

            if xml_start != -1:
                clean_xml = pssh_str[xml_start:]
                xml_end = clean_xml.find("</WRMHEADER>") + len("</WRMHEADER>")
                clean_xml = clean_xml[:xml_end]

                root = ET.fromstring(clean_xml)
                ns = {"pr": "http://schemas.microsoft.com/DRM/2007/03/PlayReadyHeader"}

                kids = []

                kid_elements = root.findall(".//pr:CUSTOMATTRIBUTES/pr:KIDS/pr:KID", ns)
                for kid_elem in kid_elements:
                    value = kid_elem.get("VALUE")
                    if value:
                        try:
                            kid_bytes = base64.b64decode(value + "==")
                            kid_uuid = UUID(bytes_le=kid_bytes)
                            kids.append(kid_uuid)
                        except ValueError:
                            pass

                # v4.2/v4.3: DATA/PROTECTINFO/KIDS/KID
                protectinfo_kids = root.findall(".//pr:DATA/pr:PROTECTINFO/pr:KIDS/pr:KID", ns)
                for kid_elem in protectinfo_kids:
                    value = kid_elem.get("VALUE")
                    if value:
                        try:
                            kid_bytes = base64.b64decode(value + "==")
                            kid_uuid = UUID(bytes_le=kid_bytes)
                            if kid_uuid not in kids:
                                kids.append(kid_uuid)
                        except ValueError:
                            pass

                # v4.1: DATA/PROTECTINFO/KID
                protectinfo_kid = root.findall(".//pr:DATA/pr:PROTECTINFO/pr:KID", ns)
                for kid_elem in protectinfo_kid:
                    value = kid_elem.get("VALUE")
                    if value:
                        try:
                            kid_bytes = base64.b64decode(value + "==")
                            kid_uuid = UUID(bytes_le=kid_bytes)
                            if kid_uuid not in kids:
                                kids.append(kid_uuid)
                        except ValueError:
                            pass

                # v4.0: DATA/KID
                individual_kids = root.findall(".//pr:DATA/pr:KID", ns)
                for kid_elem in individual_kids:
                    if kid_elem.text:
                        try:
                            kid_bytes = base64.b64decode(kid_elem.text.strip() + "==")
                            kid_uuid = UUID(bytes_le=kid_bytes)
                            if kid_uuid not in kids:
                                kids.append(kid_uuid)
                        except ValueError:
                            pass

                return kids

        # covers bad base64 (binascii.Error), defusedxml rejections (both ValueError) and XML
        # ParseError (SyntaxError) from arbitrary third-party PSSH data
        except (ValueError, SyntaxError):
            pass

        return []

    @classmethod
    def from_pssh_b64(cls, pssh_b64: str, kid: UUID | None = None) -> PlayReady:
        """Load a base64 PSSH box or PlayReady Object, or a bare UTF-16LE WRMHEADER that some packagers emit.

        It wraps a bare WRMHEADER in a PlayReady Object and keeps that as `pssh_b64`, so remote CDMs and
        the REST API can parse it again.
        """
        try:
            pssh = PSSH(pssh_b64)
        except InvalidPssh:
            try:
                raw = base64.b64decode(pssh_b64)
            except ValueError:
                raw = b""
            if not raw.startswith(b"<\x00"):
                raise
            pssh_b64 = cls.pro_b64_for(WRMHeader(raw))
            pssh = PSSH(pssh_b64)
        return cls(pssh=pssh, kid=kid, pssh_b64=pssh_b64)

    @classmethod
    def from_pssh_box(cls, box: Container, kid: UUID | None) -> PlayReady:
        """Make the DRM from a parsed PlayReady pssh box and the track's own KID."""
        pssh_bytes = Box.build(box)
        return cls(pssh=PSSH(pssh_bytes), kid=kid, pssh_b64=base64.b64encode(pssh_bytes).decode())

    @property
    def pssh(self) -> PSSH:
        return self._pssh

    @property
    def pssh_b64(self) -> str | None:
        return self.data.get("pssh_b64")

    @property
    def kids(self) -> list[UUID]:
        return self._kids

    @staticmethod
    def header_kids(header: WRMHeader) -> list[UUID]:
        """Return the KIDs one WRM header names, in header order."""
        kids: list[UUID] = []
        for signed_key_id in getattr(header, "key_ids", []):
            try:
                if isinstance(signed_key_id.value, UUID):
                    kids.append(signed_key_id.value)
                else:
                    kids.append(UUID(bytes_le=base64.b64decode(signed_key_id.value)))
            except (ValueError, TypeError):
                continue
        return kids

    def absorb(self, *others: PlayReady) -> None:
        """Fold sibling PlayReady objects into this one so their headers and KIDs get licensed too.

        A manifest can attach one PlayReady object per WRM header to the same track
        (ISM shares its Protection list across tracks; HLS emits one per EXT-X-KEY). The
        caller keeps this object's identity, which the decrypt path and drm_lock depend on.
        """
        with PlayReady._ABSORB_LOCK:
            known = {header.dumps() for header in [*self.pssh.wrm_headers, *self._extra_headers]}
            for other in others:
                if other is self:
                    continue
                for header in other.distinct_headers():
                    if header.dumps() not in known:
                        known.add(header.dumps())
                        self._extra_headers.append(header)
                for kid in other.kids:
                    if kid not in self._kids:
                        self._kids.append(kid)
                for kid, key in dict(other.content_keys).items():
                    self.content_keys.setdefault(kid, key)

    def distinct_headers(self) -> list[WRMHeader]:
        """Return the WRM headers to license, deduplicated by the KID set each one names.

        Two headers naming the same KIDs (a v4.0 header beside its v4.3 twin, or one PRH per
        rendition for one content key) cost one challenge. A header with no readable KID is kept: it
        may still be the only route to a content key nothing else names.
        """
        headers: list[WRMHeader] = []
        seen: set[frozenset[UUID]] = set()
        for header in [*self.pssh.wrm_headers, *self._extra_headers]:
            kid_set = frozenset(self.header_kids(header))
            if kid_set and kid_set in seen:
                continue
            seen.add(kid_set)
            headers.append(header)
        return headers

    @staticmethod
    def pro_b64_for(header: WRMHeader) -> str:
        """Build a base64 one-record PlayReady Object holding only this header.

        Remote CDMs send the value given to set_pssh_b64 rather than the header passed
        to get_license_challenge, so a per-header challenge needs a per-header PRO.
        """
        record = header.dumps().encode("utf-16-le")
        pro = PSSH.PlayreadyHeader.build(
            {"length": 10 + len(record), "records": [{"type": 1, "length": len(record), "data": record}]}
        )
        return base64.b64encode(pro).decode()

    def extract_keys_from_cdm(self, cdm: PlayReadyCdm, session_id: bytes) -> dict:
        """Extract keys from CDM session with cross-library compatibility.

        Args:
            cdm: CDM instance
            session_id: Session identifier

        Returns:
            Dictionary mapping KID UUIDs to hex keys
        """
        keys = {}
        for key in cdm.get_keys(session_id):
            if hasattr(key, "key_id"):
                kid = key.key_id
            elif hasattr(key, "kid"):
                kid = key.kid
            else:
                continue

            if hasattr(key, "key") and hasattr(key.key, "hex"):
                key_hex = key.key.hex()
            elif hasattr(key, "key") and isinstance(key.key, bytes):
                key_hex = key.key.hex()
            elif hasattr(key, "key") and isinstance(key.key, str):
                key_hex = key.key
            else:
                continue

            keys[kid] = key_hex
        return keys

    def get_content_keys(self, cdm: PlayReadyCdm, certificate: Callable, licence: Callable) -> None:
        """License every distinct WRM header whose KIDs still lack a content key.

        A v4.2+ header may list several KIDs and one response may stack several licences,
        so one challenge often covers everything. Content that ships several headers (ISM
        with one ProtectionHeader per track type, HLS with one PRH per playlist) gets one
        challenge per header that is not yet covered. Headers are sent as-is: Microsoft lets
        the server ignore the header, so a merged header could ask for keys it never grants.

        A header that a round refused, or that yielded nothing, is not sent again by this
        object: every track of a title runs this method, and a repeat would only repeat the
        refusal.
        """
        headers = self.distinct_headers()
        wanted = set(self.kids)
        for index, header in enumerate(headers):
            header_kids = self.header_kids(header)
            if header_kids and set(header_kids) <= self.content_keys.keys():
                continue
            if len(headers) > 1 and header.dumps() in self._refused_headers:
                continue
            if len(headers) == 1:
                pssh_b64, required = self.pssh_b64, self.kids
            else:
                pssh_b64, required = self.pro_b64_for(header), header_kids or self.kids
            try:
                keys = self._license_header(cdm, header, pssh_b64, required, licence)
            except PlayReady.Exceptions.DeviceRevoked:
                raise
            except Exception as e:
                self._refused_headers.add(header.dumps())
                if not self.content_keys and index == len(headers) - 1:
                    raise
                log_event(
                    "drm_license_error",
                    level="WARNING",
                    message=f"PlayReady header {index + 1}/{len(headers)} was refused: {e}",
                    drm_type="PlayReady",
                    header_kids=[kid.hex for kid in header_kids],
                )
                continue
            self.content_keys.update(keys)
            if keys:
                log_event(
                    "drm_content_keys",
                    level="INFO",
                    message=f"Recovered {len(keys)} PlayReady content key(s) from CDM",
                    drm_type="PlayReady",
                    key_count=len(keys),
                    keys=[{"kid": k.hex if hasattr(k, "hex") else str(k), "key": v} for k, v in keys.items()],
                )
            else:
                self._refused_headers.add(header.dumps())

        if not self.content_keys:
            raise PlayReady.Exceptions.EmptyLicense("No Content Keys were within the License")

        missing = wanted - self.content_keys.keys()
        if missing:
            log_event(
                "drm_missing_keys",
                level="WARNING",
                message=f"No PlayReady key for KID(s) {', '.join(sorted(kid.hex for kid in missing))}",
                drm_type="PlayReady",
                missing_kids=[kid.hex for kid in missing],
            )

    def _license_header(
        self,
        cdm: PlayReadyCdm,
        header: WRMHeader,
        pssh_b64: str | None,
        required_kids: list[UUID],
        licence: Callable,
    ) -> dict[UUID, str]:
        """Run one challenge/response round for a single header and return the keys it yielded."""
        session_id = cdm.open()
        try:
            if isinstance(cdm, HttpCdm):
                if pssh_b64:
                    cdm.set_pssh_b64(pssh_b64, session_id=session_id)
                cdm.set_required_kids(required_kids, session_id=session_id)

            challenge = cdm.get_license_challenge(session_id, header)

            if challenge:
                license_str = ""
                try:
                    log_event(
                        "drm_license_request",
                        level="DEBUG",
                        message="Requesting PlayReady license",
                        drm_type="PlayReady",
                        challenge_size=len(challenge),
                        kid_count=len(required_kids),
                    )
                    license_res = call_licence(licence, challenge, pssh_b64=pssh_b64, session_id=session_id)
                    if isinstance(license_res, bytes):
                        license_str = license_res.decode(errors="ignore")
                    else:
                        license_str = str(license_res)

                    if "<License>" not in license_str:
                        try:
                            license_str = base64.b64decode(license_str + "===").decode()
                        except ValueError:
                            pass

                    try:
                        cdm.parse_license(session_id, license_str)
                    except Exception as e:
                        if self.detect_revocation(license_str) or self.detect_revocation(str(e)):
                            raise
                        kids = ", ".join(kid.hex for kid in required_kids)
                        raise PlayReady.Exceptions.EmptyLicense(
                            f"No usable PlayReady licence for KID {kids}: {e}"
                        ) from e
                    log_event(
                        "drm_license_response",
                        level="DEBUG",
                        message="Parsed PlayReady license",
                        drm_type="PlayReady",
                        response_size=len(license_str),
                    )
                except PlayReady.Exceptions.DeviceRevoked:
                    raise
                except Exception as e:
                    revoked = self.detect_revocation(license_str) or self.detect_revocation(str(e))
                    if revoked:
                        raise PlayReady.Exceptions.DeviceRevoked(revoked) from e
                    raise

            return self.extract_keys_from_cdm(cdm, session_id)
        finally:
            cdm.close(session_id)

    @staticmethod
    def detect_revocation(text: str) -> str | None:
        """Return the decoded error string if a revocation HRESULT is present, else None.

        Reads the code from the raw SOAP body (<StatusCode>0x8004C065</StatusCode>)
        or from an exception message that carries it, so this method covers any service.
        """
        from unshackle.core.drm.playready_errors import describe, is_revocation

        if not text:
            return None
        for token in re.findall(r"0x[0-9A-Fa-f]{8}|-?\d{7,}", text):
            code = int(token, 16) if token.lower().startswith("0x") else int(token)
            if is_revocation(code):
                return describe(code) or f"0x{code & 0xFFFFFFFF:08X}"
        return None

    class Exceptions:
        class PSSHNotFound(Exception):
            error_code = APIErrorCode.DRM_ERROR

        class KIDNotFound(Exception):
            error_code = APIErrorCode.DRM_ERROR

        class CEKNotFound(click.ClickException):
            error_code = APIErrorCode.DRM_ERROR

        class EmptyLicense(click.ClickException):
            error_code = APIErrorCode.DRM_ERROR

        class DeviceRevoked(Exception):
            error_code = APIErrorCode.DRM_ERROR


__all__ = ("PlayReady",)
