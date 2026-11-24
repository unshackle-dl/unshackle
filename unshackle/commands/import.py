from __future__ import annotations

from pathlib import Path

import click
import mediaexport

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

        Re-fetches the manifest, injects the stored keys, then downloads/decrypts/muxes exactly
        as the `dl` command does. unshackle forwards any `dl` options after the file verbatim:

            unshackle import export.json -r HDR10 --proxy US
        """
        if not export_file.is_file():
            raise click.ClickException(f"Export file not found: {export_file}")

        try:
            doc = mediaexport.read(export_file)
        except mediaexport.ExportError as e:
            raise click.ClickException(f"{export_file} is not a usable export file: {e}")

        service_tag = doc.service_tag
        if not service_tag:
            raise click.ClickException("Export file does not name the service it came from.")

        args = [*dl_args, "--import", str(export_file), service_tag]
        dl.cli.main(args=args, prog_name="unshackle dl", standalone_mode=False)


globals()["import"] = ImportCommand
