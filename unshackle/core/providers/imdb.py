from __future__ import annotations

import hashlib
import json
from difflib import SequenceMatcher
from typing import Optional, Union

import requests

from unshackle.core.providers._base import ExternalIds, MetadataProvider, MetadataResult, clean, fuzzy_match

GRAPHQL_URL = "https://caching.graphql.imdb.com/"

GRAPHQL_HEADERS = {
    "Accept": "application/graphql+json, application/json",
    "Content-Type": "application/json",
    "Origin": "https://www.imdb.com",
    "X-Imdb-Client-Name": "imdb-web-next-localized",
    "X-Imdb-User-Country": "US",
}

KIND_TO_TYPES: dict[str, list[str]] = {
    "movie": ["movie", "tvMovie"],
    "tv": ["tvSeries", "tvMiniSeries"],
}

TITLE_FIELDS = (
    "id titleText { text } originalTitleText { text } releaseYear { year } "
    "spokenLanguages { spokenLanguages { id text } }"
)


def escape_graphql(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def primary_language(data: dict) -> Optional[str]:
    """IMDb orders spokenLanguages most prominent first. Either level can come back null."""
    spoken = data.get("spokenLanguages") or {}
    langs = spoken.get("spokenLanguages") or []
    return langs[0].get("id") if langs else None


def title_to_result(node: dict, kind: str) -> Optional[MetadataResult]:
    title = (node.get("titleText") or {}).get("text") or (node.get("originalTitleText") or {}).get("text")
    if not title:
        return None
    return MetadataResult(
        title=title,
        year=(node.get("releaseYear") or {}).get("year"),
        kind=kind,
        external_ids=ExternalIds(imdb_id=node.get("id")),
        original_language=primary_language(node),
        source="imdb",
        raw=node,
    )


class IMDBProvider(MetadataProvider):
    """IMDb metadata provider using IMDb's own GraphQL edge cache (no API key)."""

    NAME = "imdb"
    REQUIRES_KEY = False
    ID_KIND = "imdb"

    def is_available(self) -> bool:
        return True

    def graphql(self, operation: str, query: str) -> Optional[dict]:
        """Run a persisted query, registering it with one POST the first time it is seen."""
        extensions = {"persistedQuery": {"version": 1, "sha256Hash": hashlib.sha256(query.encode()).hexdigest()}}
        # IMDb's GraphQL gateway decodes "+" literally, so these JSON params must carry no spaces
        params = {
            "operationName": operation,
            "variables": "{}",
            "extensions": json.dumps(extensions, separators=(",", ":")),
        }

        try:
            r = self.session.get(GRAPHQL_URL, params=params, headers=GRAPHQL_HEADERS, timeout=30)
            body = r.json()
            if persisted_query_missing(body):
                self.log.debug("Registering persisted query %s", operation)
                payload = {"operationName": operation, "variables": {}, "query": query, "extensions": extensions}
                r = self.session.post(GRAPHQL_URL, json=payload, headers=GRAPHQL_HEADERS, timeout=30)
                body = r.json()
        except (requests.RequestException, ValueError) as exc:
            self.log.debug("IMDb GraphQL %s failed: %s", operation, exc)
            return None

        if body.get("errors"):
            self.log.debug("IMDb GraphQL %s errors: %s", operation, [e.get("message") for e in body["errors"]][:3])
            return None
        return body.get("data")

    def search(self, title: str, year: Optional[int], kind: str) -> Optional[MetadataResult]:
        self.log.debug("Searching IMDb for %r (%s, %s)", title, kind, year)

        constraints = [f'titleTextConstraint: {{searchTerm: "{escape_graphql(title)}"}}']
        if year:
            constraints.append(
                f'releaseDateConstraint: {{releaseDateRange: {{start: "{year}-01-01", end: "{year}-12-31"}}}}'
            )
        types = KIND_TO_TYPES.get(kind)
        if types:
            constraints.append(f"titleTypeConstraint: {{anyTitleTypeIds: {json.dumps(types)}}}")

        query = (
            "query UnshackleTitleSearch { advancedTitleSearch(first: 5, constraints: {%s}) "
            "{ edges { node { title { %s } } } } }" % (", ".join(constraints), TITLE_FIELDS)
        )

        data = self.graphql("UnshackleTitleSearch", query)
        edges = ((data or {}).get("advancedTitleSearch") or {}).get("edges") or []
        nodes = [edge["node"]["title"] for edge in edges if (edge.get("node") or {}).get("title")]
        if not nodes:
            self.log.debug("IMDb returned no results for %r", title)
            return None

        best = max(nodes, key=lambda n: match_ratio(n, title))
        result = title_to_result(best, kind)
        if not result or not result.title or not fuzzy_match(result.title, title):
            self.log.debug("IMDb title mismatch: searched %r, got %r", title, result.title if result else None)
            return None

        self.log.debug("IMDb -> %s (ID %s)", result.title, result.external_ids.imdb_id)
        return result

    def get_by_id(self, provider_id: Union[int, str], kind: str) -> Optional[MetadataResult]:
        """Fetch metadata by IMDB ID (e.g. 'tt1375666')."""
        imdb_id = str(provider_id)
        self.log.debug("Fetching IMDb title %s", imdb_id)

        query = 'query UnshackleTitle { title(id: "%s") { %s } }' % (escape_graphql(imdb_id), TITLE_FIELDS)
        data = self.graphql("UnshackleTitle", query)
        node = (data or {}).get("title")
        if not node:
            return None
        return title_to_result(node, kind)

    def get_external_ids(self, provider_id: Union[int, str], kind: str) -> ExternalIds:
        """Return external IDs. For IMDB, the provider_id IS the IMDB ID."""
        return ExternalIds(imdb_id=str(provider_id))


def match_ratio(node: dict, title: str) -> float:
    names = [(node.get("titleText") or {}).get("text"), (node.get("originalTitleText") or {}).get("text")]
    return max((SequenceMatcher(None, clean(title), clean(n)).ratio() for n in names if n), default=0.0)


def persisted_query_missing(body: dict) -> bool:
    for error in body.get("errors") or []:
        code = (error.get("extensions") or {}).get("code")
        if code == "PERSISTED_QUERY_NOT_FOUND" or error.get("message") == "PersistedQueryNotFound":
            return True
    return False
