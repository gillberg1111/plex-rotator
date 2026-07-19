"""Plex implementation of `MediaClient`.

============================================================================
SAFETY GUARANTEE — this app NEVER deletes media files from Plex.
============================================================================
The only Plex destructive operations this app performs are:
  * Playlist.delete()      — removes the playlist (metadata only)
  * Playlist.removeItem()  — removes an item FROM a playlist (does NOT touch
                             the underlying Episode/Show or its file on disk)

Below, on import, we monkey-patch Episode/Show/Season/Movie.delete() to raise
RuntimeError. Even if a future bug accidentally tries to delete library items
or media files, it will fail loudly instead of removing anything.
============================================================================
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, date

from plexapi.exceptions import NotFound
from plexapi.playlist import Playlist
from plexapi.server import PlexServer
from plexapi.video import Episode, Movie, Season, Show

from media_client import (
    EpisodeRef,
    MediaClient,
    MovieSummary,
    PlaylistItem,
    SeasonSummary,
    ShowSummary,
)

_log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Safety guard — installed at module import
# --------------------------------------------------------------------------- #


def _refuse_delete(self, *_args, **_kwargs):  # pragma: no cover - safety guard
    cls = type(self).__name__
    name = getattr(self, "title", repr(self))
    raise RuntimeError(
        f"Refused {cls}.delete() on '{name}'. This app is configured to never "
        f"delete media files or library items from Plex."
    )


# Disable file/item destructive operations the moment this module loads.
# Playlist.delete is intentionally left intact (playlists are metadata only).
for _cls in (Episode, Show, Season, Movie):
    _cls.delete = _refuse_delete  # type: ignore[method-assign]
_log.info("Plex safety guard installed: Episode/Show/Season/Movie.delete() disabled")


# --------------------------------------------------------------------------- #
# Helpers (module-level, no I/O)
# --------------------------------------------------------------------------- #


def _air_date(ep) -> datetime | None:
    val = getattr(ep, "originallyAvailableAt", None)
    if isinstance(val, datetime):
        return val
    if isinstance(val, date):
        return datetime(val.year, val.month, val.day)
    return None


def _isoformat_air_date(val) -> str | None:
    if val is None:
        return None
    try:
        return val.date().isoformat() if hasattr(val, "date") else val.isoformat()
    except Exception:
        return None


def _title_match(movie_title: str, show_title: str) -> bool:
    """Word-boundary match for movie titles against a show name.

    Matches 'Psych: The Movie', 'Mr. Monk's Last Case: A Monk Movie',
    'Psych 3: This is Gus', etc. Doesn't match 'Psychic Detective' (no boundary).
    """
    if not movie_title or not show_title:
        return False
    pattern = r"\b" + re.escape(show_title.lower()) + r"\b"
    return bool(re.search(pattern, movie_title.lower()))


def _tvdb_id_from_guids(guids) -> str | None:
    """Extract numeric TVDB id from a list of plexapi Guid objects."""
    for g in (guids or []):
        gid = getattr(g, "id", "")
        if gid.startswith("tvdb://"):
            return gid[len("tvdb://"):]
    return None


def _imdb_id_from_guids(guids) -> str | None:
    """Extract IMDB id (e.g. 'tt0903747') from a list of plexapi Guid objects."""
    for g in (guids or []):
        gid = getattr(g, "id", "") or ""
        if gid.startswith("imdb://"):
            return gid[len("imdb://"):].split("?")[0]
    return None


def _tmdb_id_from_guids(guids) -> int | None:
    """Extract numeric TMDB id from a list of plexapi Guid objects."""
    for g in (guids or []):
        gid = getattr(g, "id", "") or ""
        if gid.startswith("tmdb://"):
            try:
                return int(gid[len("tmdb://"):].split("?")[0])
            except ValueError:
                return None
    return None


# --------------------------------------------------------------------------- #
# PlexClient
# --------------------------------------------------------------------------- #


class PlexClient(MediaClient):
    backend = "plex"

    def __init__(self) -> None:
        # Lazy connection: stash the credentials but don't talk to Plex yet.
        # That way get_client('plex') can be called even when Plex is down —
        # we only raise on the first actual API call, which the caller's
        # try/except handles uniformly with all other backend errors.
        from media_client import backend_setting
        self._url = backend_setting("plex_url") or ""
        self._token = backend_setting("plex_token") or ""
        self._server_instance: PlexServer | None = None

    @property
    def _server(self) -> PlexServer:
        if self._server_instance is None:
            self._server_instance = PlexServer(self._url, self._token)
        return self._server_instance

    # ----- internal helpers -------------------------------------------------

    def _tv_sections(self):
        libs = [s.strip() for s in os.environ.get("TV_LIBRARIES", "").split(",") if s.strip()]
        sections = [s for s in self._server.library.sections() if s.type == "show"]
        if libs:
            sections = [s for s in sections if s.title in libs]
        return sections

    def _movie_sections(self):
        return [s for s in self._server.library.sections() if s.type == "movie"]

    def _get_show(self, rating_key: str) -> Show:
        return self._server.fetchItem(int(rating_key))

    def _get_playlist(self, rating_key: str | None) -> Playlist | None:
        if not rating_key:
            return None
        try:
            item = self._server.fetchItem(int(rating_key))
            return item if isinstance(item, Playlist) else None
        except NotFound:
            return None

    def _fetch_episode_items(self, rating_keys: list[str]) -> list:
        """Resolve rating_keys back to Plex Episode/Movie objects, in input order.

        Used internally for addItems/removeItem/createPlaylist; callers above
        the MediaClient interface should never see Plex objects.
        """
        if not rating_keys:
            return []
        items = []
        for rk in rating_keys:
            try:
                items.append(self._server.fetchItem(int(rk)))
            except NotFound:
                continue
        return items

    @staticmethod
    def _ep_to_playlist_item(ep) -> PlaylistItem:
        """Convert a Plex Episode/Movie playlist entry to a generic PlaylistItem."""
        is_movie = getattr(ep, "type", None) == "movie" or not getattr(
            ep, "grandparentRatingKey", None
        )
        air = _isoformat_air_date(getattr(ep, "originallyAvailableAt", None))
        return PlaylistItem(
            rating_key=str(ep.ratingKey),
            show_rating_key=str(getattr(ep, "grandparentRatingKey", "") or ""),
            season=int(getattr(ep, "seasonNumber", 0) or 0) if not is_movie else 999,
            episode=int(getattr(ep, "index", 0) or 0) if not is_movie else 1,
            view_count=int(getattr(ep, "viewCount", 0) or 0),
            view_offset_ms=int(getattr(ep, "viewOffset", 0) or 0),
            title=getattr(ep, "title", "") or "",
            air_date=air,
            kind="movie" if is_movie else "episode",
        )

    # ----- library / show discovery ----------------------------------------

    def list_all_shows(self) -> list[ShowSummary]:
        out: list[ShowSummary] = []
        for section in self._tv_sections():
            for show in section.all(includeGuids=1):
                out.append(
                    ShowSummary(
                        rating_key=str(show.ratingKey),
                        title=show.title,
                        year=show.year,
                        library=section.title,
                        thumb=getattr(show, "thumb", None),
                        tvdb_id=_tvdb_id_from_guids(getattr(show, "guids", None)),
                        tmdb_id=_tmdb_id_from_guids(getattr(show, "guids", None)),
                        imdb_id=_imdb_id_from_guids(getattr(show, "guids", None)),
                        status=getattr(show, "status", None),
                        content_rating=getattr(show, "contentRating", None),
                        season_count=getattr(show, "childCount", None),
                        community_rating=getattr(show, "audienceRating", None),
                    )
                )
        out.sort(key=lambda s: s.title.lower())
        return out

    def list_shows_by_genres(self, genres: list[str]) -> list[ShowSummary]:
        if not genres:
            return []
        # Plex's section.search(genre=...) is case-insensitive on the genre
        # name. We OR across genres by running one search per term and
        # unioning the ratingKeys.
        seen: dict[str, ShowSummary] = {}
        for section in self._tv_sections():
            for genre in genres:
                g = genre.strip()
                if not g:
                    continue
                try:
                    results = section.search(genre=g, includeGuids=1)
                except Exception:
                    continue
                for show in results:
                    rk = str(show.ratingKey)
                    if rk in seen:
                        continue
                    seen[rk] = ShowSummary(
                        rating_key=rk,
                        title=show.title,
                        year=show.year,
                        library=section.title,
                        thumb=getattr(show, "thumb", None),
                        tvdb_id=_tvdb_id_from_guids(getattr(show, "guids", None)),
                        tmdb_id=_tmdb_id_from_guids(getattr(show, "guids", None)),
                        imdb_id=_imdb_id_from_guids(getattr(show, "guids", None)),
                        status=getattr(show, "status", None),
                        content_rating=getattr(show, "contentRating", None),
                        season_count=getattr(show, "childCount", None),
                        community_rating=getattr(show, "audienceRating", None),
                    )
        out = list(seen.values())
        out.sort(key=lambda s: s.title.lower())
        return out

    def list_all_genres(self) -> list[str]:
        out: set[str] = set()
        try:
            for section in self._tv_sections():
                for choice in section.listChoices("genre"):
                    if choice.title:
                        out.add(choice.title)
        except Exception:
            import logging
            log_ = logging.getLogger(__name__)
            log_.exception("list_all_genres failed on Plex")
        return sorted(out)

    def list_tv_sections(self) -> list[str]:
        return [s.title for s in self._tv_sections()]

    def list_movie_sections(self) -> list[str]:
        return [s.title for s in self._movie_sections()]

    def get_show_summary(self, rating_key: str) -> ShowSummary:
        show = self._get_show(rating_key)
        return ShowSummary(
            rating_key=str(show.ratingKey),
            title=show.title,
            year=show.year,
            library=show.librarySectionTitle if hasattr(show, "librarySectionTitle") else "",
            thumb=getattr(show, "thumb", None),
            tvdb_id=_tvdb_id_from_guids(getattr(show, "guids", None)),
            tmdb_id=_tmdb_id_from_guids(getattr(show, "guids", None)),
            imdb_id=_imdb_id_from_guids(getattr(show, "guids", None)),
        )

    def season_summaries(self, rating_key: str) -> list[SeasonSummary]:
        show = self._get_show(rating_key)
        out: list[SeasonSummary] = []
        for season in show.seasons():
            idx = int(getattr(season, "index", 0) or 0)
            count = int(getattr(season, "leafCount", 0) or 0)
            if count == 0:
                try:
                    count = len(season.episodes())
                except Exception:
                    count = 0
            if count == 0:
                continue
            out.append(
                SeasonSummary(
                    index=idx,
                    title=season.title or (f"Season {idx}" if idx > 0 else "Specials"),
                    episode_count=count,
                    thumb=getattr(season, "thumb", None) or getattr(show, "thumb", None),
                    year=getattr(season, "year", None),
                )
            )
        out.sort(key=lambda s: s.index)
        return out

    def episodes_for_show(
        self,
        rating_key: str,
        start_season: int = 1,
        end_season: int | None = None,
        include_specials: bool = False,
    ) -> list[EpisodeRef]:
        show = self._get_show(rating_key)

        regulars = []
        specials = []
        for ep in show.episodes():
            season = int(ep.seasonNumber or 0)
            if season == 0:
                if include_specials:
                    specials.append(ep)
                continue
            if season < start_season:
                continue
            if end_season is not None and season > end_season:
                continue
            regulars.append(ep)

        regulars.sort(key=lambda e: (int(e.seasonNumber or 0), int(e.index or 0)))

        ordered_with_keys: list[tuple[float, int, object]] = []
        for i, ep in enumerate(regulars):
            ordered_with_keys.append((float(i), 0, ep))

        if specials:
            reg_dates = [_air_date(r) for r in regulars]
            for sp in specials:
                sp_date = _air_date(sp)
                if sp_date is None or not reg_dates:
                    slot = -1.0
                else:
                    slot = -1.0
                    for i, rd in enumerate(reg_dates):
                        if rd is not None and rd <= sp_date:
                            slot = float(i)
                    slot += 0.5
                sp_secondary = int((sp_date or datetime.min).timestamp()) if sp_date else 0
                ordered_with_keys.append((slot, sp_secondary, sp))

        ordered_with_keys.sort(key=lambda t: (t[0], t[1]))

        out: list[EpisodeRef] = []
        for _, _, ep in ordered_with_keys:
            ad = _air_date(ep)
            out.append(
                EpisodeRef(
                    rating_key=str(ep.ratingKey),
                    show_rating_key=str(show.ratingKey),
                    show_title=show.title,
                    season=int(ep.seasonNumber or 0),
                    episode=int(ep.index or 0),
                    title=ep.title or "",
                    view_count=int(ep.viewCount or 0),
                    view_offset_ms=int(ep.viewOffset or 0),
                    air_date=ad.date().isoformat() if ad else None,
                )
            )
        return out

    # ----- movies -----------------------------------------------------------

    def list_all_movies(self) -> list[MovieSummary]:
        try:
            movies = []
            for section in self._server.library.sections():
                if section.type != "movie":
                    continue
                for movie in section.all(includeGuids=1):
                    tmdb_id = None
                    for guid in (movie.guids or []):
                        if guid.id.startswith("tmdb://"):
                            try:
                                tmdb_id = int(guid.id.split("//")[1])
                            except (ValueError, IndexError):
                                pass
                            break
                    movies.append(MovieSummary(
                        rating_key=str(movie.ratingKey),
                        title=movie.title,
                        year=movie.year,
                        thumb=movie.thumb,
                        air_date=(
                            str(movie.originallyAvailableAt.date())
                            if movie.originallyAvailableAt else None
                        ),
                        view_count=getattr(movie, "viewCount", 0) or 0,
                        tmdb_id=tmdb_id,
                        imdb_id=_imdb_id_from_guids(movie.guids),
                    ))
            return movies
        except Exception:
            _log.warning("list_all_movies failed", exc_info=True)
            return []

    def find_show_by_tvdb_id(self, tvdb_id: int) -> ShowSummary | None:
        try:
            for show in self.list_all_shows():
                if show.tvdb_id and int(show.tvdb_id) == tvdb_id:
                    return show
            return None
        except Exception:
            _log.warning("find_show_by_tvdb_id failed for %s", tvdb_id, exc_info=True)
            return None

    def find_associated_movies(self, show_title: str) -> list[MovieSummary]:
        out: list[MovieSummary] = []
        seen: set[str] = set()
        for section in self._movie_sections():
            try:
                results = section.search(title=show_title)
            except Exception:
                results = []
            for m in results:
                rk = str(getattr(m, "ratingKey", "") or "")
                if not rk or rk in seen:
                    continue
                if not _title_match(getattr(m, "title", "") or "", show_title):
                    continue
                out.append(
                    MovieSummary(
                        rating_key=rk,
                        title=m.title or "",
                        year=getattr(m, "year", None),
                        thumb=getattr(m, "thumb", None),
                        air_date=_isoformat_air_date(
                            getattr(m, "originallyAvailableAt", None)
                        ),
                        view_count=int(getattr(m, "viewCount", 0) or 0),
                    )
                )
                seen.add(rk)
        out.sort(key=lambda x: (x.air_date or "", x.title.lower()))
        return out

    def get_movie_summary(self, rating_key: str) -> MovieSummary | None:
        try:
            m = self._server.fetchItem(int(rating_key))
        except (NotFound, ValueError):
            return None
        if not isinstance(m, Movie):
            return None
        return MovieSummary(
            rating_key=str(m.ratingKey),
            title=m.title or "",
            year=getattr(m, "year", None),
            thumb=getattr(m, "thumb", None),
            air_date=_isoformat_air_date(getattr(m, "originallyAvailableAt", None)),
            view_count=int(getattr(m, "viewCount", 0) or 0),
        )

    # ----- playlists --------------------------------------------------------

    def playlist_exists(self, rating_key: str | None) -> bool:
        return self._get_playlist(rating_key) is not None

    def get_playlist_items(self, rating_key: str) -> list[PlaylistItem]:
        pl = self._get_playlist(rating_key)
        if pl is None:
            return []
        return [self._ep_to_playlist_item(ep) for ep in pl.items()]

    def playlist_item_count(self, rating_key: str) -> int:
        pl = self._get_playlist(rating_key)
        if pl is None:
            return 0
        try:
            return len(pl.items())
        except Exception:
            return 0

    def create_playlist(self, title: str, ordered_rating_keys: list[str]) -> str:
        if not ordered_rating_keys:
            raise ValueError("Cannot create a Plex playlist with zero items")
        eps = self._fetch_episode_items(ordered_rating_keys)
        if not eps:
            raise ValueError("None of the requested items were found in Plex")
        pl = self._server.createPlaylist(title, items=eps)
        return str(pl.ratingKey)

    def delete_playlist(self, rating_key: str) -> None:
        pl = self._get_playlist(rating_key)
        if pl is None:
            return
        # Playlist.delete is left unguarded by the safety patch; only library
        # items (Episode/Show/Season/Movie) are blocked.
        pl.delete()

    def rename_playlist(self, rating_key: str, new_title: str) -> None:
        if not rating_key or not new_title:
            return
        pl = self._get_playlist(rating_key)
        if pl is None:
            raise RuntimeError(f"Plex playlist {rating_key!r} not found")
        pl.editTitle(new_title)

    def add_items_to_playlist(
        self, rating_key: str, item_rating_keys: list[str]
    ) -> None:
        if not item_rating_keys:
            return
        pl = self._get_playlist(rating_key)
        if pl is None:
            raise RuntimeError(f"Plex playlist {rating_key!r} not found")
        eps = self._fetch_episode_items(item_rating_keys)
        if eps:
            pl.addItems(eps)

    def remove_items_from_playlist(
        self, rating_key: str, item_rating_keys: list[str]
    ) -> None:
        if not item_rating_keys:
            return
        pl = self._get_playlist(rating_key)
        if pl is None:
            return
        eps = self._fetch_episode_items(item_rating_keys)
        for ep in eps:
            try:
                pl.removeItem(ep)
            except Exception:
                continue

    def get_view_counts(self, rating_keys):
        out: dict[str, int] = {}
        if not rating_keys:
            return out
        keys = [str(k) for k in rating_keys]
        CHUNK = 100
        for i in range(0, len(keys), CHUNK):
            chunk = keys[i:i + CHUNK]
            try:
                items = self._server.fetchItems("/library/metadata/" + ",".join(chunk))
            except Exception:
                _log.warning("get_view_counts fetch failed on plex chunk", exc_info=True)
                continue
            for it in items:
                try:
                    out[str(it.ratingKey)] = int(getattr(it, "viewCount", 0) or 0)
                except Exception:
                    continue
        return out

    def set_playlist_image(self, rating_key: str, image_url: str) -> None:
        """Upload a cover poster to the playlist from an HTTP URL.
        Fire-and-forget: log and swallow on any failure."""
        if not rating_key or not image_url:
            return
        try:
            pl = self._get_playlist(rating_key)
            if pl is None:
                return
            pl.uploadPoster(url=image_url)
        except Exception:
            _log.warning("set_playlist_image failed for Plex playlist %s", rating_key, exc_info=True)

    # replace_playlist_items: default impl from MediaClient ABC works for Plex
    # (remove all then add all). Plex doesn't have a native atomic-replace
    # endpoint; the default impl is what we'd hand-roll anyway.

    # ----- images -----------------------------------------------------------

    def fetch_image(
        self,
        image_ref: str,
        width: int | None = None,
        height: int | None = None,
    ) -> tuple[bytes, str]:
        """Fetch an image by Plex thumb path (e.g. '/library/metadata/123/thumb/...').

        When width/height are given, asks Plex to transcode the image to that
        size, which is much faster to render in a poster grid.
        """
        if width and height:
            full_path = self._server.url(image_ref)
            url = self._server.transcodeImage(full_path, height, width)
        else:
            url = self._server.url(image_ref)
        resp = self._server._session.get(url, timeout=10)
        resp.raise_for_status()
        return resp.content, resp.headers.get("Content-Type", "image/jpeg")

    # ----- metadata refresh --------------------------------------------------

    def refresh_show_metadata(self, rating_key: str) -> None:
        try:
            show = self._server.fetchItem(int(rating_key))
            show.refresh()
        except Exception as e:
            _log.warning("Plex metadata refresh failed for %s: %s", rating_key, e)
            raise

    def list_playlist_episodes(self, playlist_id: str) -> list:
        try:
            pl = self._server.fetchItem(int(playlist_id))
            return pl.items()
        except Exception:
            return []


# --------------------------------------------------------------------------- #
# Module-level singleton + back-compat shims
# --------------------------------------------------------------------------- #
#
# Historical `plex_client` exposed module-level functions like
# `list_all_shows()`. New code should call `media_client.get_client("plex")`
# and use the MediaClient interface. The shims below let any straggler
# imports keep working without behavior change.

_singleton: PlexClient | None = None


def client() -> PlexClient:
    global _singleton
    if _singleton is None:
        _singleton = PlexClient()
    return _singleton


def server() -> PlexServer:
    """Direct access to the underlying PlexServer. Avoid in new code — use
    the MediaClient interface instead."""
    return client()._server


def list_all_shows() -> list[ShowSummary]:
    return client().list_all_shows()


def get_show_summary(rating_key: str) -> ShowSummary:
    return client().get_show_summary(rating_key)


def season_summaries(rating_key: str) -> list[SeasonSummary]:
    return client().season_summaries(rating_key)


def episodes_for_show(
    rating_key: str,
    start_season: int = 1,
    end_season: int | None = None,
    include_specials: bool = False,
) -> list[EpisodeRef]:
    return client().episodes_for_show(rating_key, start_season, end_season, include_specials)


def find_associated_movies(show_title: str) -> list[MovieSummary]:
    return client().find_associated_movies(show_title)


def fetch_image(
    path: str, width: int | None = None, height: int | None = None
) -> tuple[bytes, str]:
    return client().fetch_image(path, width, height)
