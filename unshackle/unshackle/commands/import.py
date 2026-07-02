from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from unshackle.commands.dl import dl
from unshackle.core.constants import context_settings


class ImportCommand:
    @staticmethod
    @click.command(
        name="import",
        short_help="Reconstruct a download (download, decrypt, mux) from an --export JSON file.",
        context_settings={**context_settings, "ignore_unknown_options": True},
    )
    @click.argument("export_file", type=Path)
    @click.argument("dl_args", nargs=-1, type=click.UNPROCESSED)
    @click.pass_context
    def cli(ctx: click.Context, export_file: Path, dl_args: tuple[str, ...]) -> None:
        """
        Reconstruct an exported download without re-contacting the service.

        Re-fetches the manifest, injects the stored keys, then downloads/decrypts/muxes as a
        normal `dl` run. Any `dl` options after the file are forwarded verbatim:

            unshackle import export.json -r HDR10 --proxy US
        """
        if not export_file.is_file():
            raise click.ClickException(f"Export file not found: {export_file}")

        try:
            data: dict[str, Any] = json.loads(export_file.read_text(encoding="utf8"))
        except json.JSONDecodeError as e:
            raise click.ClickException(f"Export file is not valid JSON: {e}")

        if data.get("version") != 2:
            raise click.ClickException(
                f"Unsupported export version {data.get('version')!r}. "
                "Re-create the export with a current build of unshackle."
            )

        service_tag = data.get("service")
        if not service_tag:
            raise click.ClickException("Export file is missing the 'service' tag.")

        args = [*dl_args, "--import", str(export_file), service_tag]
        dl.cli.main(args=args, prog_name="unshackle dl", standalone_mode=False)


globals()["import"] = ImportCommand
