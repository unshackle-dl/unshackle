from types import SimpleNamespace

from langcodes import Language

from unshackle.commands.dl import apply_enrich, load_tracks
from unshackle.core.remote_service import apply_title_fields
from unshackle.core.titles import Episode, Movie, Song
from unshackle.core.tracks import Chapters, Tracks

SERVICE = type("SVC", (), {})


def refreshing_service(info):
    """A service that sets its title again in get_tracks, as a remote session does."""
    return SimpleNamespace(
        get_tracks=lambda title: apply_title_fields(title, info) or Tracks(),
        get_chapters=lambda title: Chapters(),
    )


def test_enrich_wins_over_a_movie_title_set_in_get_tracks():
    movie = Movie(id_="movie-0001", service=SERVICE, name="The Gold Seekers", year=2017)
    service = refreshing_service({"name": "Los Buscadores", "year": 2016, "language": "pt"})
    load_tracks(service, movie, name="The Gold Seekers", year=2017, language=Language.get("es"))
    assert (movie.name, movie.year, str(movie.language)) == ("The Gold Seekers", 2017, "es")


def test_enrich_wins_over_a_series_title_set_in_get_tracks():
    episode = Episode(id_="episode-0001", service=SERVICE, title="English", season=1, number=1, name="Pilot")
    service = refreshing_service({"series_title": "Local", "name": "Pilot", "year": 2016})
    load_tracks(service, episode, name="English", year=2017)
    assert (episode.title, episode.name, episode.year) == ("English", "Pilot", 2017)


def test_no_enrich_keeps_the_title_from_get_tracks():
    movie = Movie(id_="movie-0001", service=SERVICE, name="Old", year=2016)
    load_tracks(refreshing_service({"name": "New", "year": 2017}), movie)
    assert (movie.name, movie.year) == ("New", 2017)


def test_a_song_takes_only_the_language():
    song = Song(id_="song-0001", service=SERVICE, name="Track 1", artist="A", album="B", track=1, disc=1, year=1999)
    apply_enrich(song, "The Gold Seekers", 2017, Language.get("es"))
    assert (song.name, song.year, str(song.language)) == ("Track 1", 1999, "es")
