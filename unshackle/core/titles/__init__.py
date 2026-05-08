from typing import Union

from .episode import Episode, Series
from .movie import Movie, Movies
from .music import Album, Music, Song

Title_T = Union[Movie, Episode, Song]
Titles_T = Union[Movies, Series, Music, Album]


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
)
