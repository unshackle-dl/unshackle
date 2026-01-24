"""CLI command for authenticating remote services."""

from typing import Optional

import click
from rich.table import Table

from unshackle.core.config import config
from unshackle.core.console import console
from unshackle.core.constants import context_settings
from unshackle.core.remote_auth import RemoteAuthenticator


@click.group(short_help="Manage remote service authentication.", context_settings=context_settings)
def remote_auth() -> None:
    """Authenticate and manage sessions for remote services."""
    pass


@remote_auth.command(name="authenticate")
@click.argument("service", type=str)
@click.option(
    "-r", "--remote", type=str, help="Remote server name or URL (from config)", required=False
)
@click.option("-p", "--profile", type=str, help="Profile to use for authentication")
def authenticate_command(service: str, remote: Optional[str], profile: Optional[str]) -> None:
    """
    Authenticate a service locally and upload session to remote server.

    This command:
    1. Authenticates the service locally (shows browser, handles 2FA, etc.)
    2. Extracts the authenticated session
    3. Uploads the session to the remote server

    The server will use this pre-authenticated session for all requests.

    Examples:
        unshackle remote-auth authenticate DSNP
        unshackle remote-auth authenticate NF --profile john
        unshackle remote-auth auth AMZN --remote my-server
    """
    # Get remote server config
    remote_config = _get_remote_config(remote)
    if not remote_config:
        return

    remote_url = remote_config["url"]
    api_key = remote_config["api_key"]
    server_name = remote_config.get("name", remote_url)

    console.print(f"\n[bold cyan]Authenticating {service} for remote server:[/bold cyan] {server_name}")
    console.print(f"[dim]Server: {remote_url}[/dim]\n")

    # Create authenticator
    authenticator = RemoteAuthenticator(remote_url, api_key)

    # Authenticate and save locally
    success = authenticator.authenticate_and_save(service, profile)

    if success:
        console.print(f"\n[bold green]✓ Success![/bold green] Session saved locally. You can now use remote_{service} service.")
    else:
        console.print(f"\n[bold red]✗ Failed to authenticate {service}[/bold red]")
        raise click.Abort()


@remote_auth.command(name="status")
@click.option(
    "-r", "--remote", type=str, help="Remote server name or URL (from config)", required=False
)
def status_command(remote: Optional[str]) -> None:
    """
    Show status of all authenticated sessions in local cache.

    Examples:
        unshackle remote-auth status
        unshackle remote-auth status --remote my-server
    """
    import datetime

    from unshackle.core.local_session_cache import get_local_session_cache


    # Get local session cache
    cache = get_local_session_cache()

    # Get remote server config (optional filter)
    remote_url = None
    if remote:
        remote_config = _get_remote_config(remote)
        if remote_config:
            remote_url = remote_config["url"]
            server_name = remote_config.get("name", remote_url)
    else:
        server_name = "All Remotes"

    # Get sessions (filtered by remote if specified)
    sessions = cache.list_sessions(remote_url)

    if not sessions:
        if remote_url:
            console.print(f"\n[yellow]No authenticated sessions for {server_name}[/yellow]")
        else:
            console.print("\n[yellow]No authenticated sessions in local cache[/yellow]")
        console.print("\nUse [cyan]unshackle remote-auth authenticate <SERVICE>[/cyan] to add sessions")
        return

    # Display sessions in table
    table = Table(title=f"Local Authenticated Sessions - {server_name}")
    table.add_column("Remote", style="magenta")
    table.add_column("Service", style="cyan")
    table.add_column("Profile", style="green")
    table.add_column("Cached", style="dim")
    table.add_column("Age", style="yellow")
    table.add_column("Status", style="bold")

    for session in sessions:
        cached_time = datetime.datetime.fromtimestamp(session["cached_at"]).strftime("%Y-%m-%d %H:%M")

        # Format age
        age_seconds = session["age_seconds"]
        if age_seconds < 3600:
            age_str = f"{age_seconds // 60}m"
        elif age_seconds < 86400:
            age_str = f"{age_seconds // 3600}h"
        else:
            age_str = f"{age_seconds // 86400}d"

        # Status
        status = "[red]Expired" if session["expired"] else "[green]Valid"

        # Short remote URL for display
        remote_display = session["remote_url"].replace("https://", "").replace("http://", "")
        if len(remote_display) > 30:
            remote_display = remote_display[:27] + "..."

        table.add_row(
            remote_display,
            session["service_tag"],
            session["profile"],
            cached_time,
            age_str,
            status
        )

    console.print()
    console.print(table)
    console.print("\n[dim]Sessions are stored locally and expire after 24 hours[/dim]")
    console.print()


@remote_auth.command(name="delete")
@click.argument("service", type=str)
@click.option(
    "-r", "--remote", type=str, help="Remote server name or URL (from config)", required=False
)
@click.option("-p", "--profile", type=str, default="default", help="Profile name")
def delete_command(service: str, remote: Optional[str], profile: str) -> None:
    """
    Delete an authenticated session from local cache.

    Examples:
        unshackle remote-auth delete DSNP
        unshackle remote-auth delete NF --profile john
    """
    from unshackle.core.local_session_cache import get_local_session_cache

    # Get remote server config
    remote_config = _get_remote_config(remote)
    if not remote_config:
        return

    remote_url = remote_config["url"]

    cache = get_local_session_cache()

    console.print(f"\n[yellow]Deleting local session for {service} (profile: {profile})...[/yellow]")

    deleted = cache.delete_session(remote_url, service, profile)

    if deleted:
        console.print("[green]✓ Session deleted from local cache[/green]")
    else:
        console.print(f"[red]✗ No session found for {service} (profile: {profile})[/red]")


def _get_remote_config(remote: Optional[str]) -> Optional[dict]:
    """
    Get remote server configuration.

    Args:
        remote: Remote server name or URL, or None for first configured remote

    Returns:
        Remote config dict or None
    """
    if not config.remote_services:
        console.print("[red]No remote services configured in unshackle.yaml[/red]")
        console.print("\nAdd a remote service to your config:")
        console.print("[dim]remote_services:")
        console.print("  - url: https://your-server.com")
        console.print("    api_key: your-api-key")
        console.print("    name: my-server[/dim]")
        return None

    # If no remote specified, use the first one
    if not remote:
        return config.remote_services[0]

    # Check if remote is a name
    for remote_config in config.remote_services:
        if remote_config.get("name") == remote:
            return remote_config

    # Check if remote is a URL
    for remote_config in config.remote_services:
        if remote_config.get("url") == remote:
            return remote_config

    console.print(f"[red]Remote server '{remote}' not found in config[/red]")
    console.print("\nAvailable remotes:")
    for remote_config in config.remote_services:
        name = remote_config.get("name", remote_config.get("url"))
        console.print(f"  - {name}")

    return None
