from typing import Union

from .episode import Episode, Series
from .movie import Movie, Movies
from .music import Album, Music, Song

Title_T = Union[Movie, Episode, Song]
Titles_T = Union[Movies, Series, Music, Album]


def remap_titles(titles: Titles_T, title_map: dict) -> Titles_T:
    """
    Rewrite titles in-place using an exact-match ``title_map``.

    Some services name a title differently from how the user wants it stored, which can
    break library matching. ``title_map`` maps a source title string to the desired output
    title. Episodes are matched on their ``title`` (the show name), Movies and Songs on
    their ``name``. Returns the same collection for convenient chaining.
    """
    if not title_map or not titles:
        return titles

    def remap_one(title: Title_T) -> None:
        attr = "title" if isinstance(title, Episode) else "name"
        current = getattr(title, attr, None)
        if current and current in title_map:
            setattr(title, attr, title_map[current])

    if hasattr(titles, "__iter__"):
        for title in titles:
            remap_one(title)
    else:
        remap_one(titles)
    return titles


__all__ = (
    "Episode",
    "Series",
    "Movie",
    "Movies",
    "Music",
    "Album",
    "Song",
    "Title_T",
    "Titles_T",
    "remap_titles",
)
