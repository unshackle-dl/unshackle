import random
import re
import subprocess
from typing import Optional

from unshackle.core import binaries
from unshackle.core.proxies.proxy import Proxy


class Hola(Proxy):
    def __init__(self):
        """
        Proxy provider that uses Hola's direct connections through the hola-proxy binary.
        """
        self.binary = binaries.HolaProxy
        if not self.binary:
            raise EnvironmentError("hola-proxy executable not found but is required for the Hola proxy provider.")

        self.countries = self.get_countries()

    def __repr__(self) -> str:
        countries = len(self.countries)

        return f"{countries} Countr{['ies', 'y'][countries == 1]}"

    def get_proxy(self, query: str) -> Optional[str]:
        """
        Get an HTTP proxy URI for a Datacenter ('direct') or Residential ('lum') Hola server.

        Returns None for a query that is not a country Hola lists, or when Hola gives no proxy for it.

        TODO: - Add ability to select 'lum' proxies (residential proxies).
              - Return and use Proxy Authorization
        """
        query = query.lower()
        if not any(query in country for country in self.countries):
            return None

        ban = "Transaction error: temporary ban detected."
        banned = ConnectionError("Hola banned your IP temporarily from it's services. Try change your IP.")
        try:
            # hola-proxy logs to stderr from another thread, so a merged stream splits the Login/Password lines.
            result = subprocess.run(
                [self.binary, "-country", query, "-list-proxies"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except subprocess.TimeoutExpired as e:
            # While banned, hola-proxy retries forever and never exits, so the ban only shows in its log so far.
            log = e.stderr.decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            if ban in log:
                raise banned from None
            raise
        if ban in result.stdout + result.stderr:
            raise banned
        result.check_returncode()

        login = re.search(r"Login: (.*)\nPassword: (.*)\nProxy-Authorization: (.*)", result.stdout)
        if not login:
            return None
        username, password, proxy_authorization = login.groups()

        servers = re.findall(r"(zagent.*)", result.stdout)
        proxies = []
        for server in servers:
            host, ip_address, direct, peer, hola, trial, trial_peer, vendor = server.split(",")
            proxies.append(f"http://{username}:{password}@{ip_address}:{peer}")

        return random.choice(proxies) if proxies else None

    def get_countries(self) -> list[dict[str, str]]:
        """Get a list of available Countries."""
        p = subprocess.run(
            [self.binary, "-list-countries"],
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        ).stdout

        return [{code: name} for country in p.splitlines() for (code, name) in [country.split(" - ", maxsplit=1)]]
