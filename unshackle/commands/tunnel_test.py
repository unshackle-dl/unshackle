import logging
from typing import Optional

import click

from unshackle.core.remote_service import remote_client, start_tunnel
from unshackle.core.tunnel import CHECK_ROUTE
from unshackle.core.utils.ip_info import get_ip_info
from unshackle.core.utils.redact import safe_display_url


@click.command(short_help="Check that a remote server can send its requests through this machine.")
@click.option(
    "--server", type=str, default=None, help="Name of the remote server from remote_services config (if multiple)."
)
def tunnel_test(server: Optional[str]) -> None:
    """Check --local-proxy before a real --remote run.

    The command finds the exit IP of this machine, opens a tunnel to the server, and makes the server
    look up its exit IP through the tunnel. The two must match.
    """
    log = logging.getLogger("tunnel-test")

    local = get_ip_info()
    if not local:
        raise click.ClickException("Step 1 failed: an IP lookup from this machine did not work.")
    log.info(f"Step 1: this machine exits at {local['ip']} ({str(local.get('country', '?')).upper()})")

    client = remote_client(server)
    tunnel = start_tunnel(client)
    log.info(f"Step 2: opened a tunnel to {safe_display_url(client.server_url)}")
    try:
        remote = client.get(f"{CHECK_ROUTE}?id={tunnel.id}")
    finally:
        tunnel.stop()

    country = str(remote.get("country", "?")).upper()
    # one exit can answer over IPv4 here and IPv6 there, so a matching country also passes
    if remote.get("ip") != local["ip"] and remote.get("country") != local.get("country"):
        raise click.ClickException(
            f"Step 3 failed: the server's requests exit at {remote.get('ip')} ({country}), "
            f"not where this machine exits. Report this with the IP addresses."
        )
    log.info(f"Step 3: the server's requests exit at {remote['ip']} ({country}), {remote.get('ms')} ms for one lookup")
    log.info("The tunnel works. Add --local-proxy to a --remote command.")
