from __future__ import annotations

import base64
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any, Callable, Optional, Union
from uuid import UUID

import m3u8
from construct import Container
from pymp4.parser import Box
from pyplayready.cdm import Cdm as PlayReadyCdm
from pyplayready.system.pssh import PSSH
from rich.text import Text

from unshackle.core import binaries
from unshackle.core.clients.base import BaseHttpClient
from unshackle.core.clients.factory import http_unshackle
from unshackle.core.config import config
from unshackle.core.console import console
from unshackle.core.constants import AnyTrack
from unshackle.core.utilities import get_boxes
from unshackle.core.utils.subprocess import ffprobe


class PlayReady:
    """PlayReady DRM System."""

    def __init__(
        self,
        pssh: PSSH,
        kid: Union[UUID, str, bytes, None] = None,
        pssh_b64: Optional[str] = None,
        **kwargs: Any,
    ):
        if not pssh:
            raise ValueError("Provided PSSH is empty.")
        if not isinstance(pssh, PSSH):
            raise TypeError(f"Expected pssh to be a {PSSH}, not {pssh!r}")

        if pssh_b64:
            kids = self._extract_kids_from_pssh_b64(pssh_b64)
        else:
            kids = []

        # Extract KIDs using pyplayready's method (may miss some KIDs)
        if not kids:
            for header in pssh.wrm_headers:
                try:
                    signed_ids, _, _, _ = header.read_attributes()
                except Exception:
                    continue
                for signed_id in signed_ids:
                    try:
                        kids.append(UUID(bytes_le=base64.b64decode(signed_id.value)))
                    except Exception:
                        continue

        if kid:
            if isinstance(kid, str):
                kid = UUID(hex=kid)
            elif isinstance(kid, bytes):
                kid = UUID(bytes=kid)
            if not isinstance(kid, UUID):
                raise ValueError(f"Expected kid to be a {UUID}, str, or bytes, not {kid!r}")
            if kid not in kids:
                kids.append(kid)

        self._pssh = pssh
        self._kids = kids

        if not self.kids:
            raise PlayReady.Exceptions.KIDNotFound("No Key ID was found within PSSH and none were provided.")

        self.content_keys: dict[UUID, str] = {}
        self.data: dict = kwargs or {}
        if pssh_b64:
            self.data.setdefault("pssh_b64", pssh_b64)

    def _extract_kids_from_pssh_b64(self, pssh_b64: str) -> list[UUID]:
        """Extract all KIDs from base64-encoded PSSH data."""
        try:
            import xml.etree.ElementTree as ET

            # Decode the PSSH
            pssh_bytes = base64.b64decode(pssh_b64)

            # Try to find XML in the PSSH data
            # PlayReady PSSH usually has XML embedded in it
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

                # Extract from CUSTOMATTRIBUTES/KIDS
                kid_elements = root.findall(".//pr:CUSTOMATTRIBUTES/pr:KIDS/pr:KID", ns)
                for kid_elem in kid_elements:
                    value = kid_elem.get("VALUE")
                    if value:
                        try:
                            kid_bytes = base64.b64decode(value + "==")
                            kid_uuid = UUID(bytes_le=kid_bytes)
                            kids.append(kid_uuid)
                        except Exception:
                            pass

                # Also get individual KID
                individual_kids = root.findall(".//pr:DATA/pr:KID", ns)
                for kid_elem in individual_kids:
                    if kid_elem.text:
                        try:
                            kid_bytes = base64.b64decode(kid_elem.text.strip() + "==")
                            kid_uuid = UUID(bytes_le=kid_bytes)
                            if kid_uuid not in kids:
                                kids.append(kid_uuid)
                        except Exception:
                            pass

                return kids

        except Exception:
            pass

        return []

    @classmethod
    def from_track(cls, track: AnyTrack, session: Optional[BaseHttpClient] = None) -> PlayReady:
        if not session:
            session = http_unshackle.new('playready')

        kid: Optional[UUID] = None
        pssh_boxes: list[Container] = []
        tenc_boxes: list[Container] = []

        if track.descriptor == track.Descriptor.HLS:
            m3u_url = track.url
            master = m3u8.loads(session.get(m3u_url).text, uri=m3u_url)
            pssh_boxes.extend(
                Box.parse(base64.b64decode(x.uri.split(",")[-1]))
                for x in (master.session_keys or master.keys)
                if x and x.keyformat and x.keyformat.lower() in {
                    f"urn:uuid:{PSSH.SYSTEM_ID}", "com.microsoft.playready"
                }
            )

        init_data = track.get_init_segment(session=session)
        if init_data:
            probe = ffprobe(init_data)
            if probe:
                for stream in probe.get("streams") or []:
                    enc_key_id = stream.get("tags", {}).get("enc_key_id")
                    if enc_key_id:
                        kid = UUID(bytes=base64.b64decode(enc_key_id))
            pssh_boxes.extend(list(get_boxes(init_data, b"pssh")))
            tenc_boxes.extend(list(get_boxes(init_data, b"tenc")))

        pssh = next((b for b in pssh_boxes if b.system_ID == PSSH.SYSTEM_ID), None)
        if not pssh:
            raise PlayReady.Exceptions.PSSHNotFound("PSSH was not found in track data.")

        tenc = next(iter(tenc_boxes), None)
        if not kid and tenc and tenc.key_ID.int != 0:
            kid = tenc.key_ID

        pssh_bytes = Box.build(pssh)
        return cls(pssh=PSSH(pssh_bytes), kid=kid, pssh_b64=base64.b64encode(pssh_bytes).decode())

    @classmethod
    def from_init_data(cls, init_data: bytes) -> PlayReady:
        if not init_data:
            raise ValueError("Init data should be provided.")
        if not isinstance(init_data, bytes):
            raise TypeError(f"Expected init data to be bytes, not {init_data!r}")

        kid: Optional[UUID] = None
        pssh_boxes: list[Container] = list(get_boxes(init_data, b"pssh"))
        tenc_boxes: list[Container] = list(get_boxes(init_data, b"tenc"))

        probe = ffprobe(init_data)
        if probe:
            for stream in probe.get("streams") or []:
                enc_key_id = stream.get("tags", {}).get("enc_key_id")
                if enc_key_id:
                    kid = UUID(bytes=base64.b64decode(enc_key_id))

        pssh = next((b for b in pssh_boxes if b.system_ID == PSSH.SYSTEM_ID), None)
        if not pssh:
            raise PlayReady.Exceptions.PSSHNotFound("PSSH was not found in track data.")

        tenc = next(iter(tenc_boxes), None)
        if not kid and tenc and tenc.key_ID.int != 0:
            kid = tenc.key_ID

        pssh_bytes = Box.build(pssh)
        return cls(pssh=PSSH(pssh_bytes), kid=kid, pssh_b64=base64.b64encode(pssh_bytes).decode())

    @property
    def pssh(self) -> PSSH:
        return self._pssh

    @property
    def pssh_b64(self) -> Optional[str]:
        return self.data.get("pssh_b64")

    @property
    def kid(self) -> Optional[UUID]:
        return next(iter(self.kids), None)

    @property
    def kids(self) -> list[UUID]:
        return self._kids

    def _extract_keys_from_cdm(self, cdm: PlayReadyCdm, session_id: bytes) -> dict:
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
        session_id = cdm.open()
        try:
            if hasattr(cdm, "set_pssh_b64") and self.pssh_b64:
                cdm.set_pssh_b64(self.pssh_b64)

            if hasattr(cdm, "set_required_kids"):
                cdm.set_required_kids(self.kids)

            challenge = cdm.get_license_challenge(session_id, self.pssh.wrm_headers[0])

            if challenge:
                try:
                    license_res = licence(challenge=challenge)
                    if isinstance(license_res, bytes):
                        license_str = license_res.decode(errors="ignore")
                    else:
                        license_str = str(license_res)

                    if "<License>" not in license_str:
                        try:
                            license_str = base64.b64decode(license_str + "===").decode()
                        except Exception:
                            pass

                    cdm.parse_license(session_id, license_str)
                except Exception:
                    raise

            keys = self._extract_keys_from_cdm(cdm, session_id)
            self.content_keys.update(keys)
        finally:
            cdm.close(session_id)

        if not self.content_keys:
            raise PlayReady.Exceptions.EmptyLicense("No Content Keys were within the License")

    def decrypt(self, path: Path) -> None:
        """
        Decrypt a Track with PlayReady DRM.
        Args:
            path: Path to the encrypted file to decrypt
        Raises:
            EnvironmentError if the required decryption executable could not be found.
            ValueError if the track has not yet been downloaded.
            SubprocessError if the decryption process returned a non-zero exit code.
        """
        if not self.content_keys:
            raise ValueError("Cannot decrypt a Track without any Content Keys...")

        if not path or not path.exists():
            raise ValueError("Tried to decrypt a file that does not exist.")

        decrypter = str(getattr(config, "decryption", "")).lower()

        if decrypter == "mp4decrypt":
            return self._decrypt_with_mp4decrypt(path)
        else:
            return self._decrypt_with_shaka_packager(path)

    def _decrypt_with_mp4decrypt(self, path: Path) -> None:
        """Decrypt using mp4decrypt"""
        if not binaries.Mp4decrypt:
            raise EnvironmentError("mp4decrypt executable not found but is required.")

        output_path = path.with_stem(f"{path.stem}_decrypted")

        # Build key arguments
        key_args = []
        for kid, key in self.content_keys.items():
            kid_hex = kid.hex if hasattr(kid, "hex") else str(kid).replace("-", "")
            key_hex = key if isinstance(key, str) else key.hex()
            key_args.extend(["--key", f"{kid_hex}:{key_hex}"])

        cmd = [
            str(binaries.Mp4decrypt),
            "--show-progress",
            *key_args,
            str(path),
            str(output_path),
        ]

        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
        except subprocess.CalledProcessError as e:
            error_msg = e.stderr if e.stderr else f"mp4decrypt failed with exit code {e.returncode}"
            raise subprocess.CalledProcessError(e.returncode, cmd, output=e.stdout, stderr=error_msg)

        if not output_path.exists():
            raise RuntimeError(f"mp4decrypt failed: output file {output_path} was not created")
        if output_path.stat().st_size == 0:
            raise RuntimeError(f"mp4decrypt failed: output file {output_path} is empty")

        path.unlink()
        shutil.move(output_path, path)

    def _decrypt_with_shaka_packager(self, path: Path) -> None:
        """Decrypt using Shaka Packager (original method)"""
        if not binaries.ShakaPackager:
            raise EnvironmentError("Shaka Packager executable not found but is required.")

        output_path = path.with_stem(f"{path.stem}_decrypted")
        config.directories.temp.mkdir(parents=True, exist_ok=True)

        try:
            arguments = [
                f"input={path},stream=0,output={output_path},output_format=MP4",
                "--enable_raw_key_decryption",
                "--keys",
                ",".join(
                    [
                        *[
                            f"label={i}:key_id={kid.hex}:key={key.lower()}"
                            for i, (kid, key) in enumerate(self.content_keys.items())
                        ],
                        *[
                            f"label={i}:key_id={'00' * 16}:key={key.lower()}"
                            for i, (kid, key) in enumerate(self.content_keys.items(), len(self.content_keys))
                        ],
                    ]
                ),
                "--temp_dir",
                config.directories.temp,
            ]

            p = subprocess.Popen(
                [binaries.ShakaPackager, *arguments],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )

            stream_skipped = False
            had_error = False
            shaka_log_buffer = ""
            for line in iter(p.stderr.readline, ""):
                line = line.strip()
                if not line:
                    continue
                if "Skip stream" in line:
                    stream_skipped = True
                if ":INFO:" in line:
                    continue
                if "I0" in line or "W0" in line:
                    continue
                if ":ERROR:" in line:
                    had_error = True
                if "Insufficient bits in bitstream for given AVC profile" in line:
                    continue
                shaka_log_buffer += f"{line.strip()}\n"

            if shaka_log_buffer:
                shaka_log_buffer = "\n            ".join(
                    textwrap.wrap(shaka_log_buffer.rstrip(), width=console.width - 22, initial_indent="")
                )
                console.log(Text.from_ansi("\n[PlayReady]: " + shaka_log_buffer))

            p.wait()

            if p.returncode != 0 or had_error:
                raise subprocess.CalledProcessError(p.returncode, [binaries.ShakaPackager, *arguments])

            path.unlink()
            if not stream_skipped:
                shutil.move(output_path, path)
        except subprocess.CalledProcessError as e:
            if e.returncode == 0xC000013A:
                raise KeyboardInterrupt()
            raise

    class Exceptions:
        class PSSHNotFound(Exception):
            pass

        class KIDNotFound(Exception):
            pass

        class CEKNotFound(Exception):
            pass

        class EmptyLicense(Exception):
            pass


__all__ = ("PlayReady",)
