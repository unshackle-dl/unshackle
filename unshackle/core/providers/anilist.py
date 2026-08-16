from __future__ import annotations

from difflib import SequenceMatcher
from typing import Optional, Union

import requests

from unshackle.core.providers._base import ExternalIds, MetadataProvider, MetadataResult, clean

GRAPHQL_URL = "https://graphql.anilist.co"

MEDIA_FIELDS = "id idMal title { romaji english native } format startDate { year } countryOfOrigin"

SEARCH_QUERY = (
    "query UnshackleAniListSearch($search: String) { Page(perPage: 10) "
    "{ media(search: $search, type: ANIME, sort: SEARCH_MATCH) { %s } } }" % MEDIA_FIELDS
)
MEDIA_QUERY = "query UnshackleAniListMedia($id: Int) { Media(id: $id, type: ANIME) { %s } }" % MEDIA_FIELDS
MAL_QUERY = "query UnshackleAniListMal($idMal: Int) { Media(idMal: $idMal, type: ANIME) { %s } }" % MEDIA_FIELDS

TITLE_LANGUAGES: tuple[str, ...] = ("english", "romaji", "native")

# AniList records where a title was produced, not what it is spoken in; these are the
# production countries whose anime output is reliably in the country's own language
COUNTRY_TO_LANGUAGE: dict[str, str] = {"JP": "ja", "KR": "ko", "CN": "zh", "TW": "zh", "HK": "zh"}


def parse_anilist_ref(value: Union[int, str]) -> Optional[tuple[str, int]]:
    """Split an AniList reference into its namespace and number.

    A bare number is an AniList ID; ``mal:12345`` is a MyAnimeList ID, which AniList
    resolves natively. Returns None for anything else.
    """
    text = str(value).strip().lower()
    namespace = "id"
    if ":" in text:
        prefix, _, text = text.partition(":")
        if prefix != "mal":
            return None
        namespace = "mal"
    # isdecimal, not isdigit: superscripts pass isdigit but crash int()
    if not text.isdecimal():
        return None
    number = int(text)
    return (namespace, number) if number > 0 else None


class AniListProvider(MetadataProvider):
    """Anime metadata provider using AniList's public GraphQL API (no API key)."""

    NAME = "anilist"
    REQUIRES_KEY = False
    ID_KIND = "anilist"

    def is_available(self) -> bool:
        return True

    def graphql(self, query: str, variables: dict) -> Optional[dict]:
        try:
            r = self.session.post(GRAPHQL_URL, json={"query": query, "variables": variables}, timeout=30)
            body = r.json()
        except (requests.RequestException, ValueError) as exc:
            self.log.debug("AniList GraphQL request failed: %s", exc)
            return None

        if body.get("errors"):
            # a miss is a 404 with an error body, not an empty result
            self.log.debug("AniList GraphQL errors: %s", [e.get("message") for e in body["errors"]][:3])
            return None
        return body.get("data")

    def title_variant(self, titles: dict) -> Optional[str]:
        from unshackle.core.config import config

        configured = (config.anilist_title_language or "english").lower()
        for variant in [configured, *(v for v in TITLE_LANGUAGES if v != configured)]:
            value = titles.get(variant)
            if value:
                return str(value)
        return None

    def to_result(self, node: dict) -> Optional[MetadataResult]:
        title = self.title_variant(node.get("title") or {})
        if not title:
            return None
        return MetadataResult(
            title=title,
            year=(node.get("startDate") or {}).get("year"),
            kind="movie" if node.get("format") == "MOVIE" else "tv",
            external_ids=ExternalIds(anilist_id=node.get("id")),
            original_language=COUNTRY_TO_LANGUAGE.get(node.get("countryOfOrigin") or ""),
            source="anilist",
            raw=node,
        )

    def search(self, title: str, year: Optional[int], kind: str) -> Optional[MetadataResult]:
        self.log.debug("Searching AniList for %r (%s, %s)", title, kind, year)

        data = self.graphql(SEARCH_QUERY, {"search": title})
        nodes = ((data or {}).get("Page") or {}).get("media") or []
        if not nodes:
            self.log.debug("AniList returned no results for %r", title)
            return None

        # soft filters with fallback: other providers search kind-scoped endpoints, here
        # one ANIME search covers both kinds, so a TV hit must not shadow a movie search
        matching = [n for n in nodes if (n.get("format") == "MOVIE") == (kind == "movie")]
        nodes = matching or nodes
        if year:
            dated = [n for n in nodes if (n.get("startDate") or {}).get("year") == year]
            nodes = dated or nodes

        best = max(nodes, key=lambda n: match_ratio(n, title))
        result = self.to_result(best)
        if result:
            self.log.debug("AniList -> %s (ID %s)", result.title, result.external_ids.anilist_id)
        return result

    def get_by_id(self, provider_id: Union[int, str], kind: str) -> Optional[MetadataResult]:
        """Fetch metadata by AniList ID, or by MyAnimeList ID when given as 'mal:12345'."""
        ref = parse_anilist_ref(provider_id)
        if not ref:
            self.log.debug("Not an AniList reference: %r", provider_id)
            return None

        namespace, number = ref
        self.log.debug("Fetching AniList media %s:%d", namespace, number)
        if namespace == "mal":
            data = self.graphql(MAL_QUERY, {"idMal": number})
        else:
            data = self.graphql(MEDIA_QUERY, {"id": number})

        node = (data or {}).get("Media")
        if not node:
            return None
        return self.to_result(node)

    def get_external_ids(self, provider_id: Union[int, str], kind: str) -> ExternalIds:
        """Return external IDs. AniList knows no western IDs, so only its own."""
        ref = parse_anilist_ref(provider_id)
        if not ref:
            return ExternalIds()
        namespace, number = ref
        if namespace == "id":
            return ExternalIds(anilist_id=number)
        result = self.get_by_id(provider_id, kind)
        return result.external_ids if result else ExternalIds()


def match_ratio(node: dict, title: str) -> float:
    names = (node.get("title") or {}).values()
    return max((SequenceMatcher(None, clean(title), clean(n)).ratio() for n in names if n), default=0.0)
