from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from typing import Optional

from unshackle.core.cacher import Cacher
from unshackle.core.config import config
from unshackle.core.titles import Titles_T


class TitleCacher:
    """
    Handles caching of Title objects to reduce redundant API calls.

    This wrapper provides:
    - Region-aware caching to handle geo-restricted content
    - Automatic fallback to cached data when API calls fail
    - Cache lifetime extension during failures
    - Cache hit/miss statistics for debugging
    """

    def __init__(self, service_name: str):
        self.service_name = service_name
        self.log = logging.getLogger(f"{service_name}.TitleCache")
        self.cacher = Cacher(service_name)
        self.stats = {"hits": 0, "misses": 0, "fallbacks": 0}
        self.no_cache = False

    def _generate_cache_key(
        self, title_id: str, region: Optional[str] = None, account_hash: Optional[str] = None
    ) -> str:
        """
        Generate a unique cache key for title data.

        Args:
            title_id: The title identifier
            region: The region/proxy identifier
            account_hash: Hash of account credentials (if applicable)

        Returns:
            A unique cache key string
        """
        # Hash the title_id to handle complex IDs (URLs, dots, special chars)
        # This ensures consistent length and filesystem-safe keys
        title_hash = hashlib.sha256(title_id.encode()).hexdigest()[:16]

        # Start with base key using hash
        key_parts = ["titles", title_hash]

        # Add region if available
        if region:
            key_parts.append(region.lower())

        # Add account hash if available
        if account_hash:
            key_parts.append(account_hash[:8])  # Use first 8 chars of hash

        # Join with underscores
        cache_key = "_".join(key_parts)

        return cache_key

    def get_cached_titles(
        self,
        title_id: str,
        fetch_function,
        region: Optional[str] = None,
        account_hash: Optional[str] = None,
        no_cache: bool = False,
        reset_cache: bool = False,
    ) -> Optional[Titles_T]:
        """
        Get titles from cache or fetch from API with fallback support.

        Args:
            title_id: The title identifier
            fetch_function: Function to call to fetch fresh titles
            region: The region/proxy identifier
            account_hash: Hash of account credentials
            no_cache: Bypass cache completely
            reset_cache: Clear cache before fetching

        Returns:
            Titles object (Movies, Series, or Album)
        """
        # If caching is globally disabled or no_cache flag is set
        if not config.title_cache_enabled or no_cache:
            self.no_cache = True
            self.log.debug("Cache bypassed, fetching fresh titles")
            return fetch_function()

        # Generate cache key
        cache_key = self._generate_cache_key(title_id, region, account_hash)

        # If reset_cache flag is set, clear the cache entry
        if reset_cache:
            self.log.info(f"Clearing cache for {cache_key}")
            cache_path = (config.directories.cache / self.service_name / cache_key).with_suffix(".json")
            if cache_path.exists():
                cache_path.unlink()

        # Try to get from cache
        cache = self.cacher.get(cache_key, version=1)

        # Check if we have valid cached data
        if cache and not cache.expired:
            self.stats["hits"] += 1
            self.log.debug(f"Cache hit for {title_id} (hits: {self.stats['hits']}, misses: {self.stats['misses']})")
            return cache.data

        # Cache miss or expired, try to fetch fresh data
        self.stats["misses"] += 1
        self.log.debug(f"Cache miss for {title_id} fetching fresh data")

        try:
            # Attempt to fetch fresh titles
            titles = fetch_function()

            if titles:
                # Successfully fetched, update cache
                self.log.debug(f"Successfully fetched titles for {title_id}, updating cache")
                cache = self.cacher.get(cache_key, version=1)
                cache.set(titles, expiration=datetime.now() + timedelta(seconds=config.title_cache_time))

            return titles

        except Exception as e:
            # API call failed, check if we have fallback cached data
            if cache and cache.data:
                # We have expired cached data, use it as fallback
                current_time = datetime.now()
                max_retention_time = cache.expiration + timedelta(
                    seconds=config.title_cache_max_retention - config.title_cache_time
                )

                if current_time < max_retention_time:
                    self.stats["fallbacks"] += 1
                    self.log.warning(
                        f"API call failed for {title_id}, using cached data as fallback "
                        f"(fallbacks: {self.stats['fallbacks']})"
                    )
                    self.log.debug(f"Error was: {e}")

                    # Extend cache lifetime
                    extended_expiration = current_time + timedelta(minutes=5)
                    if extended_expiration < max_retention_time:
                        cache.expiration = extended_expiration
                        cache.set(cache.data, expiration=extended_expiration)

                    return cache.data
                else:
                    self.log.error(f"API call failed and cached data for {title_id} exceeded maximum retention time")

            # Re-raise the exception if no fallback available
            raise

    def clear_all_title_cache(self):
        """Clear all title caches for this service."""
        cache_dir = config.directories.cache / self.service_name
        if cache_dir.exists():
            for cache_file in cache_dir.glob("titles_*.json"):
                cache_file.unlink()
                self.log.info(f"Cleared cache file: {cache_file.name}")

    def get_cache_stats(self) -> dict:
        """Get cache statistics."""
        total = sum(self.stats.values())
        if total > 0:
            hit_rate = (self.stats["hits"] / total) * 100
        else:
            hit_rate = 0

        return {
            "hits": self.stats["hits"],
            "misses": self.stats["misses"],
            "fallbacks": self.stats["fallbacks"],
            "hit_rate": f"{hit_rate:.1f}%",
        }

    # -- Generic provider cache methods --

    def get_cached_provider(
        self,
        provider_name: str,
        title_id: str,
        kind: Optional[str] = None,
        region: Optional[str] = None,
        account_hash: Optional[str] = None,
    ) -> Optional[dict]:
        """Get cached metadata for any provider."""
        if not config.title_cache_enabled or self.no_cache:
            return None

        cache_key = self._generate_cache_key(title_id, region, account_hash)
        cache = self.cacher.get(cache_key, version=1)

        if not cache or not cache.data:
            return None

        provider_data = getattr(cache.data, f"{provider_name}_data", None)
        if not provider_data:
            return None

        expiration = provider_data.get("expires_at")
        if not expiration or datetime.now() >= expiration:
            self.log.debug(f"{provider_name} cache expired for {title_id}")
            return None

        if kind and provider_data.get("kind") != kind:
            self.log.debug(
                f"{provider_name} cache kind mismatch for {title_id}: "
                f"cached {provider_data.get('kind')}, requested {kind}"
            )
            return None

        self.log.debug(f"{provider_name} cache hit for {title_id}")

        # Return the inner data (provider-specific format)
        response = provider_data.get("response")
        if response is not None:
            return response

        # For TMDB-style caches that store detail + external_ids at top level
        result: dict = {}
        if "detail" in provider_data:
            result["detail"] = provider_data["detail"]
        if "external_ids" in provider_data:
            result["external_ids"] = provider_data["external_ids"]
        if "fetched_at" in provider_data:
            result["fetched_at"] = provider_data["fetched_at"]
        return result if result else provider_data

    def cache_provider(
        self,
        provider_name: str,
        title_id: str,
        data: dict,
        kind: Optional[str] = None,
        region: Optional[str] = None,
        account_hash: Optional[str] = None,
        ttl_days: int = 7,
    ) -> None:
        """Cache metadata from any provider."""
        if not config.title_cache_enabled or self.no_cache:
            return

        cache_key = self._generate_cache_key(title_id, region, account_hash)
        cache = self.cacher.get(cache_key, version=1)

        if not cache or not cache.data:
            self.log.debug(f"Cannot cache {provider_name} data: no title cache exists for {title_id}")
            return

        now = datetime.now()

        # Build cache entry in a format compatible with legacy methods
        if provider_name == "tmdb" and "detail" in data:
            # TMDB stores detail + external_ids at top level
            cache_entry = {
                **data,
                "kind": kind,
                "fetched_at": now,
                "expires_at": now + timedelta(days=ttl_days),
            }
        elif provider_name == "simkl":
            # SIMKL wraps in a "response" key
            cache_entry = {
                "response": data,
                "fetched_at": now,
                "expires_at": now + timedelta(days=ttl_days),
            }
        else:
            # Generic format: store data directly with metadata
            cache_entry = {
                "response": data,
                "kind": kind,
                "fetched_at": now,
                "expires_at": now + timedelta(days=ttl_days),
            }

        setattr(cache.data, f"{provider_name}_data", cache_entry)
        cache.set(cache.data, expiration=cache.expiration)
        self.log.debug(f"Cached {provider_name} data for {title_id}")


def get_region_from_proxy(proxy_url: Optional[str]) -> Optional[str]:
    """
    Extract region identifier from proxy URL.

    Args:
        proxy_url: The proxy URL string

    Returns:
        Region identifier or None
    """
    if not proxy_url:
        return None

    # Try to extract region from common proxy patterns
    # e.g., "us123.nordvpn.com", "gb-proxy.example.com"
    import re

    # Pattern for NordVPN style
    nord_match = re.search(r"([a-z]{2})\d+\.nordvpn", proxy_url.lower())
    if nord_match:
        return nord_match.group(1)

    # Pattern for country code at start
    cc_match = re.search(r"([a-z]{2})[-_]", proxy_url.lower())
    if cc_match:
        return cc_match.group(1)

    # Pattern for country code subdomain
    subdomain_match = re.search(r"://([a-z]{2})\.", proxy_url.lower())
    if subdomain_match:
        return subdomain_match.group(1)

    return None


def get_account_hash(credential) -> Optional[str]:
    """
    Generate a hash for account identification.

    Args:
        credential: Credential object

    Returns:
        SHA1 hash of the credential or None
    """
    if not credential:
        return None

    # Use existing sha1 property if available
    if hasattr(credential, "sha1"):
        return credential.sha1

    # Otherwise generate hash from username
    if hasattr(credential, "username"):
        return hashlib.sha1(credential.username.encode()).hexdigest()

    return None
