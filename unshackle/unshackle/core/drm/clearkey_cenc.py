from __future__ import annotations

import base64
import json
import shutil
import subprocess
import textwrap
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Union
from uuid import UUID

from requests import Session
from rich.text import Text

from unshackle.core import binaries
from unshackle.core.config import config
from unshackle.core.console import console
from unshackle.core.utilities import log_event


def b64url_encode_nopad(data: bytes) -> str:
    # W3C EME uses base64url without padding for key IDs and key values
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class ClearKeyCENC:
    """W3C EME ClearKey (org.w3.clearkey) DRM System over MPEG-CENC content.

    Distinct from the HLS AES-128 `ClearKey` class: keys here are delivered by a
    license server as a JWK Set keyed by KID, and content is standard CENC
    (decrypted with shaka-packager/mp4decrypt KID:KEY pairs, same as Widevine).
    """

    urn = "urn:uuid:e2719d58-a985-b3c9-781a-b030af78d30e"

    def __init__(
        self,
        kids: Iterable[Union[UUID, str, bytes]],
        laurl: Optional[str] = None,
        content_keys: Optional[dict[UUID, str]] = None,
        **kwargs: Any,
    ):
        kid_list: list[UUID] = []
        for kid in kids or []:
            if isinstance(kid, str):
                kid = UUID(hex=kid)
            elif isinstance(kid, bytes):
                kid = UUID(bytes=kid)
            if not isinstance(kid, UUID):
                raise ValueError(f"Expected kid to be a {UUID}, str, or bytes, not {kid!r}")
            kid_list.append(kid)
        if not kid_list:
            raise ClearKeyCENC.Exceptions.KIDNotFound("No Key ID was provided.")

        self.kids: list[UUID] = kid_list
        self.laurl: Optional[str] = laurl
        self.content_keys: dict[UUID, str] = dict(content_keys or {})
        self.data: dict = kwargs or {}

    @property
    def kid(self) -> Optional[UUID]:
        """Get first Key ID, if any."""
        return next(iter(self.kids), None)

    def to_dict(self) -> dict[str, Any]:
        """Serialise this DRM instance for export/import (KIDs + license URL).

        Content keys are stored once at the export's track level, not duplicated here.
        """
        data: dict[str, Any] = {
            "system": "ClearKeyCENC",
            "kids": [kid.hex for kid in self.kids],
        }
        if self.laurl:
            data["laurl"] = self.laurl
        return data

    def get_license_challenge(self) -> bytes:
        """Build the W3C EME ClearKey JSON license request for the unkeyed KIDs."""
        kids = [kid for kid in self.kids if kid not in self.content_keys] or self.kids
        request = {"kids": [b64url_encode_nopad(kid.bytes) for kid in kids], "type": "temporary"}
        return json.dumps(request).encode("utf8")

    def get_content_keys(self, *, licence: Callable, session: Optional[Session] = None) -> None:
        """
        Obtain Content Keys for this DRM Instance from a ClearKey license server.

        The licence param is expected to be a function and will be provided with the
        W3C JSON license request as `challenge`. It may return the JWK Set license as
        a dict, JSON str, or bytes. If it returns None and the manifest provided a
        Laurl, the challenge is POSTed there directly instead.
        """
        if all(kid in self.content_keys for kid in self.kids):
            return

        challenge = self.get_license_challenge()

        log_event(
            "drm_license_request",
            level="DEBUG",
            message=f"Requesting ClearKey license for {len(self.kids)} KID(s)",
            drm_type="ClearKeyCENC",
            kids=[kid.hex for kid in self.kids],
            challenge_size=len(challenge),
        )

        response = licence(challenge=challenge)

        if response is None and self.laurl:
            if not session:
                session = Session()
                session.headers.update(config.headers)
            r = session.post(self.laurl, data=challenge, headers={"Content-Type": "application/json"})
            r.raise_for_status()
            response = r.content

        if not response:
            raise ClearKeyCENC.Exceptions.EmptyLicense("No ClearKey license was returned and no Laurl is available.")

        if isinstance(response, (bytes, bytearray)):
            document = json.loads(bytes(response).decode("utf8"))
        elif isinstance(response, str):
            document = json.loads(response)
        elif isinstance(response, dict):
            document = response
        else:
            raise ValueError(f"Expected the ClearKey license to be bytes, str, or dict, not {response!r}")

        for jwk in document.get("keys") or []:
            if jwk.get("kty") not in (None, "oct"):
                continue
            kid_b64 = jwk.get("kid")
            key_b64 = jwk.get("k")
            if not kid_b64 or not key_b64:
                continue
            kid = UUID(bytes=b64url_decode(kid_b64))
            self.content_keys[kid] = b64url_decode(key_b64).hex()

        if not self.content_keys:
            raise ClearKeyCENC.Exceptions.EmptyLicense("No Content Keys were within the License")

        for kid in self.kids:
            if kid not in self.content_keys:
                raise ClearKeyCENC.Exceptions.CEKNotFound(f"No Content Key for KID {kid.hex} within the License")

        log_event(
            "drm_content_keys",
            level="INFO",
            message=f"Recovered {len(self.content_keys)} ClearKey content key(s)",
            drm_type="ClearKeyCENC",
            key_count=len(self.content_keys),
            keys=[{"kid": k.hex, "key": v} for k, v in self.content_keys.items()],
        )

    def decrypt(self, path: Path) -> None:
        """
        Decrypt a Track with ClearKey DRM (standard CENC).
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
        tool = "mp4decrypt" if decrypter == "mp4decrypt" else "shaka-packager"

        log_event(
            "drm_decrypt",
            level="DEBUG",
            message=f"Decrypting {path.name} with {tool}",
            drm_type="ClearKeyCENC",
            tool=tool,
            file=path.name,
            key_count=len(self.content_keys),
        )

        decrypt_start = time.monotonic()
        if decrypter == "mp4decrypt":
            self.decrypt_with_mp4decrypt(path)
        else:
            self.decrypt_with_shaka_packager(path)

        log_event(
            "drm_decrypt_complete",
            level="DEBUG",
            message=f"Decrypted {path.name} with {tool}",
            drm_type="ClearKeyCENC",
            tool=tool,
            file=path.name,
            duration_ms=round((time.monotonic() - decrypt_start) * 1000, 1),
            output_size=path.stat().st_size if path.exists() else 0,
        )

    def decrypt_with_mp4decrypt(self, path: Path) -> None:
        """Decrypt using mp4decrypt"""
        if not binaries.Mp4decrypt:
            raise EnvironmentError("mp4decrypt executable not found but is required.")

        output_path = path.with_stem(f"{path.stem}_decrypted")

        key_args = []
        for kid, key in self.content_keys.items():
            key_args.extend(["--key", f"{kid.hex}:{key}"])

        cmd = [
            str(binaries.Mp4decrypt),
            "--show-progress",
            *key_args,
            str(path),
            str(output_path),
        ]

        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        except subprocess.CalledProcessError as e:
            error_msg = e.stderr if e.stderr else f"mp4decrypt failed with exit code {e.returncode}"
            raise subprocess.CalledProcessError(e.returncode, cmd, output=e.stdout, stderr=error_msg)

        if not output_path.exists():
            raise RuntimeError(f"mp4decrypt failed: output file {output_path} was not created")
        if output_path.stat().st_size == 0:
            raise RuntimeError(f"mp4decrypt failed: output file {output_path} is empty")

        path.unlink()
        shutil.move(output_path, path)

    def decrypt_with_shaka_packager(self, path: Path) -> None:
        """Decrypt using Shaka Packager"""
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
                    "label={}:key_id={}:key={}".format(i, kid.hex, key.lower())
                    for i, (kid, key) in enumerate(self.content_keys.items())
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
                    # file/segment was so small that it didn't have any actual data, ignore
                    stream_skipped = True
                if ":INFO:" in line:
                    continue
                if "I0" in line or "W0" in line:
                    continue
                if ":ERROR:" in line:
                    had_error = True
                if "Insufficient bits in bitstream for given AVC profile" in line:
                    # this is a warning and is something we don't have to worry about
                    continue
                shaka_log_buffer += f"{line.strip()}\n"

            if shaka_log_buffer:
                # wrap to console width - padding - '[ClearKey]: '
                shaka_log_buffer = "\n            ".join(
                    textwrap.wrap(shaka_log_buffer.rstrip(), width=console.width - 22, initial_indent="")
                )
                console.log(Text.from_ansi("\n[ClearKey]: " + shaka_log_buffer))

            p.wait()

            if p.returncode != 0 or had_error:
                raise subprocess.CalledProcessError(p.returncode, [binaries.ShakaPackager, *arguments])

            path.unlink()
            if not stream_skipped:
                shutil.move(output_path, path)
        except subprocess.CalledProcessError as e:
            if e.returncode == 0xC000013A:  # STATUS_CONTROL_C_EXIT
                raise KeyboardInterrupt()
            raise

    class Exceptions:
        class KIDNotFound(Exception):
            """KID (Encryption Key ID) was not found."""

        class CEKNotFound(Exception):
            """CEK (Content Encryption Key) for KID was not found in License."""

        class EmptyLicense(Exception):
            """License returned no Content Encryption Keys."""


__all__ = ("ClearKeyCENC",)
