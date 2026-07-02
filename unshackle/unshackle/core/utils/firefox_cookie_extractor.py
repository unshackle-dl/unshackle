"""
Firefox Cookie and LocalStorage Extractor Utility for unshackle.
Provides a secure, read-only mechanism to extract authentication tokens from Firefox.
"""

import os
import platform
import shutil
import sqlite3
import tempfile
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Dict, List, Optional


def get_firefox_root() -> Path:
    """Locates the operating system specific root directory for Firefox profiles."""
    system = platform.system()
    home = Path.home()
    if system == 'Windows':
        return Path(os.getenv('APPDATA', '')) / 'Mozilla' / 'Firefox'
    elif system == 'Darwin':
        return home / 'Library' / 'Application Support' / 'Firefox'
    elif system == 'Linux':
        paths = [
            home / '.mozilla' / 'firefox',
            home / 'snap' / 'firefox' / 'common' / '.mozilla' / 'firefox',
            home / '.var' / 'app' / 'org.mozilla.firefox' / '.mozilla' / 'firefox'
        ]
        for p in paths:
            if p.exists():
                return p
        return paths[0]
    raise RuntimeError(f"Unsupported operating system: {system}")


def get_latest_profile_path(ff_root: Path) -> Path:
    """Finds the most recently modified valid Firefox profile subdirectory."""
    search_dirs = [ff_root / 'Profiles', ff_root]
    valid_profiles = []
    for base in search_dirs:
        if base.exists():
            valid_profiles.extend([p for p in base.iterdir() if p.is_dir() and (p / 'cookies.sqlite').exists()])

    if not valid_profiles:
        raise FileNotFoundError("No active Firefox profiles detected.")

    return max(valid_profiles, key=lambda p: (p / 'cookies.sqlite').stat().st_mtime)


def get_local_storage_data(profile_path: Path, hosts: List[str], tmp_dir_path: Path) -> Dict[str, str]:
    """
    Extracts LocalStorage tokens from webappsstore.sqlite with strict origin matching.
    Operates within a secure temporary directory.
    """
    ls_db = profile_path / 'webappsstore.sqlite'
    if not ls_db.exists():
        return {}

    temp_ls = tmp_dir_path / "webappsstore.sqlite"
    extracted_storage = {}

    try:
        shutil.copy2(ls_db, temp_ls)
        conn = sqlite3.connect(temp_ls)
        cursor = conn.cursor()

        for host in hosts:
            # Firefox stores LocalStorage as reversed host (e.g., moc.viki.:https:443)
            # Use strict prefix matching to prevent over-broad harvesting
            reversed_host = host[::-1]
            query = "SELECT key, value FROM webappsstore2 WHERE originKey LIKE ?"
            cursor.execute(query, (f"{reversed_host}.%",))

            for key, value in cursor.fetchall():
                extracted_storage[key] = value

        conn.close()
    except Exception:
        pass

    return extracted_storage


def get_firefox_cookies(service_settings: dict) -> Optional[CookieJar]:
    """
    Extracts cookies and optionally localStorage from Firefox based on provided host patterns.
    Implements security hardening and data consistency (WAL support) as per PR review.
    """
    raw_hosts = service_settings.get('hosts', [])
    # Filter empty or dangerously short hosts to prevent full store dumps (Finding #3)
    priority_hosts = [h.strip().lower() for h in raw_hosts if h and len(h.strip()) >= 3]
    use_local_storage = service_settings.get('local_storage', False)

    if not priority_hosts:
        return None

    try:
        profile_path = get_latest_profile_path(get_firefox_root())
    except Exception:
        return None

    cookie_jar = CookieJar()

    # Use a secure temporary directory with restricted permissions (Finding #5)
    with tempfile.TemporaryDirectory(prefix="unshackle_ff_") as tmp_dir:
        tmp_dir_path = Path(tmp_dir)
        os.chmod(tmp_dir, 0o700)

        temp_db = tmp_dir_path / "cookies.sqlite"
        temp_wal = tmp_dir_path / "cookies.sqlite-wal"

        try:
            # Copy main DB and its WAL log to ensure fresh data capture (Finding #5)
            shutil.copy2(profile_path / 'cookies.sqlite', temp_db)
            wal_path = profile_path / 'cookies.sqlite-wal'
            if wal_path.exists():
                shutil.copy2(wal_path, temp_wal)

            conn = sqlite3.connect(temp_db)
            cursor = conn.cursor()

            for host in priority_hosts:
                # Precise matching: exact host or dot-prefixed domain suffix (Finding #2)
                query = """
                    SELECT host, name, value, path, expiry, isSecure, isHttpOnly
                    FROM moz_cookies
                    WHERE host = ? OR host LIKE ?
                """
                cursor.execute(query, (host, f"%.{host}"))
                rows = cursor.fetchall()

                for h, n, v, p, e, secure, httponly in rows:
                    # Final validation to ensure proper domain suffix
                    if h == host or h.endswith(f".{host}"):
                        c = Cookie(
                            version=0, name=n, value=v, port=None, port_specified=False,
                            domain=h, domain_specified=True, domain_initial_dot=h.startswith('.'),
                            path=p, path_specified=True, secure=bool(secure), expires=e,
                            discard=False, comment=None, comment_url=None,
                            rest={'HttpOnly': str(bool(httponly))}, rfc2109=False
                        )
                        cookie_jar.set_cookie(c)
            conn.close()

            # Optional LocalStorage harvesting (Finding #4)
            if use_local_storage:
                storage_data = get_local_storage_data(profile_path, priority_hosts, tmp_dir_path)
                for key, val in storage_data.items():
                    c = Cookie(
                        version=0, name=key, value=val, port=None, port_specified=False,
                        domain=".localstorage", domain_specified=True, domain_initial_dot=False,
                        path='/', path_specified=True, secure=False, expires=None,
                        discard=True, comment=None, comment_url=None, rest={}, rfc2109=False
                    )
                    cookie_jar.set_cookie(c)

        except Exception:
            return None

    return cookie_jar if len(list(cookie_jar)) > 0 else None
