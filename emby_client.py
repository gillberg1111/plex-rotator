"""Emby implementation of `MediaClient`.

============================================================================
SAFETY GUARANTEE — this app NEVER deletes media files from Emby.
============================================================================
Emby's only "delete playlist" endpoint is `DELETE /Items?ids=X`, the same
endpoint that permanently deletes library items + their files on disk. We
defend against this two ways:

  1. EVERY outbound DELETE goes through `_check_delete_safety()`, which
     deny-by-default refuses unless the path matches a narrow allow-list.
     The only allow-listed DELETE is `/Playlists/{id}/Items` (removing items
     from a playlist; does NOT delete the items themselves).

  2. The intentional `delete_playlist()` code path bypasses the safety check
     for ONE call after verifying the target really is a playlist via
     `GET /Playlists/{id}`. This is the single audited bypass; anywhere else
     that tries to call `DELETE /Items` will hit the safety guard.

Even a future bug that constructs an arbitrary DELETE will be refused.
============================================================================

Auth notes:
  * API key auth via `X-Emby-Token` header — EMBY_URL and EMBY_API_KEY are
    required env vars.
  * EMBY_USERNAME is optional. If set, that user's Id is used for playlist
    ownership. Otherwise we pick the first admin from GET /Users (falling
    back to the first user).
  * User Id is resolved lazily once and cached. No re-auth on 401 — a 401
    means a bad API key, not an expired token.
  * DeviceId is a stable UUID persisted to `<DB_DIR>/emby_device_id` for
    the server's device tracking.
  * `X-Emby-Authorization` header carries client identity metadata.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import uuid

import requests

from media_client import (
    EpisodeRef,
    MediaClient,
    MovieSummary,
    PlaylistItem,
    SeasonSummary,
    ShowSummary,
)

_log = logging.getLogger(__name__)

# Client identity sent in the X-Emby-Authorization header. The version string
# is decorative on the server side but appears in the Emby Devices list.
_CLIENT_NAME = "Linearr"
_CLIENT_VERSION = "3.0.0"

# Episode/movie metadata fields we always request from /Items and friends so
# the response carries the data we need to build PlaylistItem / EpisodeRef.
_EPISODE_FIELDS = "PremiereDate,Overview,ProviderIds"
_MOVIE_FIELDS = "PremiereDate,ProductionYear,ProviderIds"

# Per-call timeout as (connect, read). The connect timeout is short so an
# unreachable Emby fails fast (~5s) instead of hanging the whole request for
# 30s; the read timeout stays generous because Emby can be slow on first scan.
_CONNECT_TIMEOUT = 5
_HTTP_TIMEOUT = (_CONNECT_TIMEOUT, 30)

# Playlist add/remove pass item ids in the query string. A large genre playlist
# can carry thousands of 32-char GUIDs, which overruns server/proxy URI limits
# (HTTP 414). Chunk every such call. 100 ids ≈ 3.3 KB of query string — safely
# under the common 4 KB reverse-proxy cap.
_MAX_IDS_PER_REQUEST = 100


def _chunked(seq, n):
    """Yield successive n-sized lists from seq (n >= 1)."""
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# --------------------------------------------------------------------------- #
# Safety guard (module-level, testable without a connected client)
# --------------------------------------------------------------------------- #


class EmbySafetyError(RuntimeError):
    """Raised when a DELETE call would touch library items or other state we
    promised never to mutate. Defense-in-depth — should never fire in
    well-behaved code."""


class EmbyAPIError(RuntimeError):
    """Raised when Emby returns an unexpected non-2xx response."""


# The only DELETE we allow through the normal request path. Removing items
# from a playlist does NOT delete the underlying library items.
_ALLOWED_DELETE_PATTERNS = (
    re.compile(r"^/Playlists/[^/]+/Items$"),
)


def _check_delete_safety(path: str) -> None:
    """Raise unless `path` matches an explicit allow-list entry.

    Intentional deletion of a playlist itself (DELETE /Items?ids=X) goes
    through `EmbyClient.delete_playlist`, which calls the HTTP layer
    via a dedicated bypass after verifying the target is a playlist.
    """
    for pat in _ALLOWED_DELETE_PATTERNS:
        if pat.match(path):
            return
    raise EmbySafetyError(
        f"Refused DELETE {path!r}. Linearr's safety guard only permits "
        f"DELETE /Playlists/{{id}}/Items via the standard request path. "
        f"Deleting a playlist itself goes through delete_playlist()."
    )


# --------------------------------------------------------------------------- #
# Helpers (pure)
# --------------------------------------------------------------------------- #


def _premiere_date_iso(value: str | None) -> str | None:
    """Trim Emby's ISO 8601 PremiereDate ('2008-04-15T00:00:00.0000000Z')
    to a plain YYYY-MM-DD string, matching the shape used everywhere else."""
    if not value:
        return None
    return value[:10] if len(value) >= 10 else None


def _title_match(movie_title: str, show_title: str) -> bool:
    """Word-boundary match for movie titles against a show name.
    Identical contract to plex_client._title_match — kept here too so each
    backend module is self-contained."""
    if not movie_title or not show_title:
        return False
    pattern = r"\b" + re.escape(show_title.lower()) + r"\b"
    return bool(re.search(pattern, movie_title.lower()))


def _parse_tmdb_id(prov: dict) -> int | None:
    """Extract TMDB numeric id from an Emby ProviderIds dict."""
    raw = prov.get("Tmdb") or prov.get("tmdb")
    if raw and str(raw).isdigit():
        return int(raw)
    return None


def _device_id_path() -> str:
    """Where to persist our stable DeviceId. Lives next to the SQLite DB so it
    rides along on the same persistent volume."""
    db_path = os.environ.get("DB_PATH", "rotator.db")
    d = os.path.dirname(db_path) or "."
    return os.path.join(d, "emby_device_id")


def _load_or_create_device_id() -> str:
    path = _device_id_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            v = f.read().strip()
            if v:
                return v
    except FileNotFoundError:
        pass
    new_id = str(uuid.uuid4())
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_id)
    except OSError:
        _log.warning("Could not persist device_id to %s; will regenerate next run", path)
    return new_id


# --------------------------------------------------------------------------- #
# EmbyClient
# --------------------------------------------------------------------------- #


class EmbyClient(MediaClient):
    backend = "emby"

    def __init__(self) -> None:
        from media_client import backend_setting
        self._url = (backend_setting("emby_url") or "").rstrip("/")
        self._api_key = backend_setting("emby_api_key") or ""
        self._username = backend_setting("emby_username")
        self._device_id = _load_or_create_device_id()
        self._session = requests.Session()
        self._user_id_cache: str | None = None
        self._user_lock = threading.Lock()

    # ----- auth -------------------------------------------------------------

    @property
    def _user_id(self) -> str | None:
        """User Id, resolved lazily on first access.

        Exposed as a property (not a plain attribute) so that ANY call site
        interpolating user_id into a request path or params dict triggers
        resolution first — request paths/params are evaluated before
        `_request` runs, so resolving inside `_request` would be too late.
        """
        if self._user_id_cache is None:
            self._resolve_user_id()
        return self._user_id_cache

    def _emby_auth_header(self) -> str:
        parts = [
            f'Client="{_CLIENT_NAME}"',
            f'Device="{_CLIENT_NAME}-host"',
            f'DeviceId="{self._device_id}"',
            f'Version="{_CLIENT_VERSION}"',
        ]
        return "MediaBrowser " + ", ".join(parts)

    def _resolve_user_id(self) -> None:
        """Resolve the Emby user Id once. Called lazily by _ensure_authenticated.

        If EMBY_USERNAME is set, find that user by name. Otherwise pick the
        first administrator, falling back to the first user.
        """
        with self._user_lock:
            if self._user_id_cache is not None:
                return
            resp = self._session.get(
                self._url + "/Users",
                headers={
                    "X-Emby-Token": self._api_key,
                    "X-Emby-Authorization": self._emby_auth_header(),
                },
                timeout=_HTTP_TIMEOUT,
            )
            if not resp.ok:
                raise EmbyAPIError(
                    f"GET /Users failed: {resp.status_code} {resp.text[:200]}"
                )
            users = resp.json()
            if not users:
                raise EmbyAPIError(
                    "No users found on Emby server. Create at least one user."
                )
            if self._username:
                for u in users:
                    if u.get("Name") == self._username:
                        self._user_id_cache = u["Id"]
                        _log.info("Emby user_id=%s resolved by EMBY_USERNAME", self._user_id_cache)
                        return
                raise EmbyAPIError(
                    f"User {self._username!r} not found on Emby server. "
                    f"Check EMBY_USERNAME."
                )
            # No EMBY_USERNAME specified — prefer admin, else first user.
            for u in users:
                policy = u.get("Policy") or {}
                if policy.get("IsAdministrator"):
                    self._user_id_cache = u["Id"]
                    _log.info("Emby user_id=%s resolved as first admin", self._user_id_cache)
                    return
            # Fall back to first user.
            self._user_id_cache = users[0]["Id"]
            _log.info("Emby user_id=%s resolved as first user", self._user_id_cache)

    def _ensure_authenticated(self) -> None:
        if self._user_id_cache is None:
            self._resolve_user_id()

    def _headers(self) -> dict[str, str]:
        return {
            "X-Emby-Token": self._api_key,
            "X-Emby-Authorization": self._emby_auth_header(),
        }

    # ----- HTTP wrapper (the safety boundary) -------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        stream: bool = False,
        _bypass_delete_check: bool = False,
    ) -> requests.Response:
        method_u = method.upper()
        if method_u == "DELETE" and not _bypass_delete_check:
            _check_delete_safety(path)

        self._ensure_authenticated()
        url = self._url + path
        resp = self._session.request(
            method_u, url,
            params=params, json=json, stream=stream,
            headers=self._headers(),
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code == 401:
            raise EmbyAPIError(
                f"{method_u} {path} -> 401 Unauthorized. Check EMBY_API_KEY."
            )
        if not resp.ok and resp.status_code != 404:
            raise EmbyAPIError(
                f"{method_u} {path} -> {resp.status_code}: {resp.text[:200]}"
            )
        return resp

    # ----- internal: library / item helpers ---------------------------------

    def _tv_library_ids(self) -> list[tuple[str, str]]:
        """Returns [(libraryId, libraryName)] for every TV library, optionally
        filtered by the TV_LIBRARIES env var (same convention as Plex)."""
        # Emby exposes user views at /Users/{id}/Views (NOT Jellyfin's /UserViews,
        # which 404s on Emby).
        resp = self._request("GET", f"/Users/{self._user_id}/Views")
        items = resp.json().get("Items") or []
        tv = [
            (it["Id"], it.get("Name", ""))
            for it in items
            if it.get("CollectionType") == "tvshows"
        ]
        allow = [s.strip() for s in os.environ.get("TV_LIBRARIES", "").split(",") if s.strip()]
        if allow:
            tv = [(lid, name) for lid, name in tv if name in allow]
        return tv

    def _movie_library_ids(self) -> list[str]:
        resp = self._request("GET", f"/Users/{self._user_id}/Views")
        items = resp.json().get("Items") or []
        return [it["Id"] for it in items if it.get("CollectionType") == "movies"]

    def _fetch_item(self, item_id: str) -> dict | None:
        """GET a single item by Id via the list form (/Items?Ids=…), which is
        stable across server versions. Returns the BaseItemDto dict or None when
        the id resolves to nothing."""
        resp = self._request("GET", "/Items", params={
            "Ids": str(item_id),
            "userId": self._user_id,
            "Fields": _MOVIE_FIELDS,
        })
        if not resp.ok:
            return None
        items = (resp.json() or {}).get("Items") or []
        return items[0] if items else None

    @staticmethod
    def _item_to_playlist_item(item: dict) -> PlaylistItem:
        """Convert an Emby BaseItemDto (Episode or Movie) to PlaylistItem."""
        kind = "movie" if item.get("Type") == "Movie" else "episode"
        user = item.get("UserData") or {}
        season = (
            999 if kind == "movie"
            else int(item.get("ParentIndexNumber") or 0)
        )
        episode = (
            1 if kind == "movie"
            else int(item.get("IndexNumber") or 0)
        )
        return PlaylistItem(
            rating_key=str(item["Id"]),
            show_rating_key=str(item.get("SeriesId") or ""),
            season=season,
            episode=episode,
            view_count=int(user.get("PlayCount") or 0),
            view_offset_ms=int((user.get("PlaybackPositionTicks") or 0) // 10000),
            title=item.get("Name") or "",
            air_date=_premiere_date_iso(item.get("PremiereDate")),
            kind=kind,
        )

    # ----- library / show discovery ----------------------------------------

    def list_all_shows(self) -> list[ShowSummary]:
        return self._list_series_via_items(extra_params={})

    def list_shows_by_genres(self, genres: list[str]) -> list[ShowSummary]:
        cleaned = [g.strip() for g in (genres or []) if g and g.strip()]
        if not cleaned:
            return []
        return self._list_series_via_items(extra_params={"genres": "|".join(cleaned)})

    def list_all_genres(self) -> list[str]:
        try:
            resp = self._request(
                "GET",
                "/Genres",
                params={
                    "UserId": self._user_id,
                    "IncludeItemTypes": "Series",
                    "Limit": 2000,
                },
            )
            return sorted(
                g["Name"]
                for g in resp.json().get("Items", [])
                if g.get("Name")
            )
        except Exception:
            _log.exception("list_all_genres failed on Emby")
            return []

    def list_tv_sections(self) -> list[str]:
        try:
            resp = self._request("GET", "/Library/VirtualFolders")
            data = resp.json() if resp.ok else []
            return [f["Name"] for f in data if f.get("CollectionType") == "tvshows"]
        except Exception:
            _log.exception("list_tv_sections failed on Emby")
            return []

    def list_movie_sections(self) -> list[str]:
        try:
            resp = self._request("GET", "/Library/VirtualFolders")
            data = resp.json() if resp.ok else []
            return [f["Name"] for f in data if f.get("CollectionType") == "movies"]
        except Exception:
            _log.exception("list_movie_sections failed on Emby")
            return []

    def _list_series_via_items(self, extra_params: dict) -> list[ShowSummary]:
        """Shared core for list_all_shows / list_shows_by_genres. Iterates
        TV libraries and unions matching series, deduplicated by Id."""
        seen: dict[str, ShowSummary] = {}
        for lib_id, lib_name in self._tv_library_ids():
            params = {
                "userId": self._user_id,
                "parentId": lib_id,
                "recursive": "true",
                "includeItemTypes": "Series",
                "fields": "ProductionYear,ProviderIds,ChildCount,CommunityRating,OfficialRating,Status",
                "enableImages": "true",
                "imageTypeLimit": 1,
                "enableImageTypes": "Primary",
            }
            params.update(extra_params)
            resp = self._request("GET", "/Items", params=params)
            for item in resp.json().get("Items") or []:
                rk = str(item["Id"])
                if rk in seen:
                    continue
                prov = item.get("ProviderIds") or {}
                seen[rk] = ShowSummary(
                    rating_key=rk,
                    title=item.get("Name") or "",
                    year=item.get("ProductionYear"),
                    library=lib_name,
                    thumb=rk if item.get("ImageTags", {}).get("Primary") else None,
                    tvdb_id=prov.get("Tvdb") or None,
                    tmdb_id=_parse_tmdb_id(prov),
                    imdb_id=prov.get("Imdb") or None,
                    status=item.get("Status"),
                    content_rating=item.get("OfficialRating"),
                    season_count=item.get("ChildCount"),
                    community_rating=item.get("CommunityRating"),
                )
        out = list(seen.values())
        out.sort(key=lambda s: s.title.lower())
        return out

    def get_show_summary(self, rating_key: str) -> ShowSummary:
        item = self._fetch_item(rating_key)
        if item is None:
            raise EmbyAPIError(f"Emby show {rating_key!r} not found")
        prov = item.get("ProviderIds") or {}
        return ShowSummary(
            rating_key=str(item["Id"]),
            title=item.get("Name") or "",
            year=item.get("ProductionYear"),
            library="",  # not exposed on the item dto
            thumb=str(item["Id"]) if item.get("ImageTags", {}).get("Primary") else None,
            tvdb_id=prov.get("Tvdb") or None,
            tmdb_id=_parse_tmdb_id(prov),
            imdb_id=prov.get("Imdb") or None,
        )

    def season_summaries(self, rating_key: str) -> list[SeasonSummary]:
        resp = self._request("GET", f"/Shows/{rating_key}/Seasons", params={
            "userId": self._user_id,
            "fields": "ProductionYear",
            "enableImages": "true",
            "imageTypeLimit": 1,
        })
        out: list[SeasonSummary] = []
        for s in resp.json().get("Items") or []:
            idx = int(s.get("IndexNumber") or 0)
            count = int(s.get("ChildCount") or 0)
            if count == 0:
                # ChildCount is unreliable on some servers; fall back to a
                # cheap episode query rather than skipping the season.
                ep_resp = self._request("GET", f"/Shows/{rating_key}/Episodes", params={
                    "userId": self._user_id,
                    "seasonId": s["Id"],
                    "limit": 1,
                })
                count = int(ep_resp.json().get("TotalRecordCount") or 0)
            if count == 0:
                continue
            out.append(SeasonSummary(
                index=idx,
                title=s.get("Name") or (f"Season {idx}" if idx > 0 else "Specials"),
                episode_count=count,
                thumb=str(s["Id"]) if s.get("ImageTags", {}).get("Primary") else None,
                year=s.get("ProductionYear"),
            ))
        out.sort(key=lambda x: x.index)
        return out

    def episodes_for_show(
        self,
        rating_key: str,
        start_season: int = 1,
        end_season: int | None = None,
        include_specials: bool = False,
    ) -> list[EpisodeRef]:
        # Ensure _user_id is populated before constructing params.
        self._ensure_authenticated()
        # One call gets every episode for the show; we filter and order in Python.
        resp = self._request("GET", f"/Shows/{rating_key}/Episodes", params={
            "userId": self._user_id,
            "fields": _EPISODE_FIELDS,
            "enableUserData": "true",
        })
        if not resp.ok:
            return []
        try:
            raw = resp.json().get("Items") or []
        except Exception:
            return []

        # Show name comes from any episode (or we'd have to GET /Items/{id}).
        show_title = (raw[0].get("SeriesName") if raw else None) or ""

        regulars: list[dict] = []
        specials: list[dict] = []
        for ep in raw:
            season = int(ep.get("ParentIndexNumber") or 0)
            if season == 0:
                if include_specials:
                    specials.append(ep)
                continue
            if season < start_season:
                continue
            if end_season is not None and season > end_season:
                continue
            regulars.append(ep)

        regulars.sort(key=lambda e: (
            int(e.get("ParentIndexNumber") or 0),
            int(e.get("IndexNumber") or 0),
        ))

        # Slot specials by air date the same way Plex does — keeps behavior
        # identical across backends so users get the same playlist shape.
        def air(ep: dict) -> str | None:
            return _premiere_date_iso(ep.get("PremiereDate"))

        ordered_with_keys: list[tuple[float, str, dict]] = []
        for i, ep in enumerate(regulars):
            ordered_with_keys.append((float(i), "", ep))
        if specials:
            reg_dates = [air(r) for r in regulars]
            for sp in specials:
                sp_date = air(sp)
                if sp_date is None or not reg_dates:
                    slot = -1.0
                else:
                    slot = -1.0
                    for i, rd in enumerate(reg_dates):
                        if rd is not None and rd <= sp_date:
                            slot = float(i)
                    slot += 0.5
                ordered_with_keys.append((slot, sp_date or "", sp))
        ordered_with_keys.sort(key=lambda t: (t[0], t[1]))

        out: list[EpisodeRef] = []
        for _, _, ep in ordered_with_keys:
            user = ep.get("UserData") or {}
            out.append(EpisodeRef(
                rating_key=str(ep["Id"]),
                show_rating_key=str(rating_key),
                show_title=show_title,
                season=int(ep.get("ParentIndexNumber") or 0),
                episode=int(ep.get("IndexNumber") or 0),
                title=ep.get("Name") or "",
                view_count=int(user.get("PlayCount") or 0),
                view_offset_ms=int((user.get("PlaybackPositionTicks") or 0) // 10000),
                air_date=air(ep),
            ))
        return out

    # ----- movies -----------------------------------------------------------

    def list_all_movies(self) -> list[MovieSummary]:
        try:
            self._ensure_authenticated()
            resp = self._request("GET", "/Items", params={
                "IncludeItemTypes": "Movie",
                "Recursive": "true",
                "Fields": "ProviderIds,PremiereDate,UserData",
                "UserId": self._user_id,
            })
            if not resp.ok:
                return []
            items = resp.json().get("Items", [])
            movies = []
            for item in items:
                provider_ids = item.get("ProviderIds", {})
                tmdb_id = None
                raw_tmdb = provider_ids.get("Tmdb") or provider_ids.get("tmdb")
                if raw_tmdb:
                    try:
                        tmdb_id = int(raw_tmdb)
                    except ValueError:
                        pass
                premiere = item.get("PremiereDate", "")
                air_date = premiere[:10] if premiere else None
                user_data = item.get("UserData", {})
                movies.append(MovieSummary(
                    rating_key=item["Id"],
                    title=item.get("Name", ""),
                    year=item.get("ProductionYear"),
                    thumb=item["Id"] if item.get("HasPrimaryImage") else None,
                    air_date=air_date,
                    view_count=user_data.get("PlayCount", 0) or 0,
                    tmdb_id=tmdb_id,
                    imdb_id=provider_ids.get("Imdb") or provider_ids.get("imdb") or None,
                ))
            return movies
        except Exception:
            _log.warning("list_all_movies failed (Emby)", exc_info=True)
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
        if not show_title:
            return []
        out: list[MovieSummary] = []
        seen: set[str] = set()
        for lib_id in self._movie_library_ids():
            resp = self._request("GET", "/Items", params={
                "userId": self._user_id,
                "parentId": lib_id,
                "recursive": "true",
                "includeItemTypes": "Movie",
                "searchTerm": show_title,
                "fields": _MOVIE_FIELDS,
                "enableUserData": "true",
                "enableImages": "true",
                "imageTypeLimit": 1,
            })
            for m in resp.json().get("Items") or []:
                mid = str(m.get("Id") or "")
                if not mid or mid in seen:
                    continue
                if not _title_match(m.get("Name") or "", show_title):
                    continue
                user = m.get("UserData") or {}
                out.append(MovieSummary(
                    rating_key=mid,
                    title=m.get("Name") or "",
                    year=m.get("ProductionYear"),
                    thumb=mid if m.get("ImageTags", {}).get("Primary") else None,
                    air_date=_premiere_date_iso(m.get("PremiereDate")),
                    view_count=int(user.get("PlayCount") or 0),
                ))
                seen.add(mid)
        out.sort(key=lambda x: (x.air_date or "", x.title.lower()))
        return out

    def get_movie_summary(self, rating_key: str) -> MovieSummary | None:
        item = self._fetch_item(rating_key)
        if item is None or item.get("Type") != "Movie":
            return None
        user = item.get("UserData") or {}
        return MovieSummary(
            rating_key=str(item["Id"]),
            title=item.get("Name") or "",
            year=item.get("ProductionYear"),
            thumb=str(item["Id"]) if item.get("ImageTags", {}).get("Primary") else None,
            air_date=_premiere_date_iso(item.get("PremiereDate")),
            view_count=int(user.get("PlayCount") or 0),
        )

    # ----- playlist read ----------------------------------------------------

    def playlist_exists(self, rating_key: str | None) -> bool:
        # Emby has no GET /Playlists/{id} (it 404s); look the item up via
        # /Items?Ids= and confirm it's still a Playlist. NO userId filter — the
        # query must be owner-agnostic, otherwise a playlist owned by a user
        # other than the resolved one looks "gone" (see delete_playlist).
        if not rating_key:
            return False
        resp = self._request("GET", "/Items", params={"Ids": rating_key})
        if not resp.ok:
            return False
        return any(it.get("Type") == "Playlist" for it in (resp.json().get("Items") or []))

    def get_playlist_items(self, rating_key: str) -> list[PlaylistItem]:
        if not rating_key:
            return []
        resp = self._request("GET", f"/Playlists/{rating_key}/Items", params={
            "userId": self._user_id,
            "fields": _EPISODE_FIELDS,
            "enableUserData": "true",
        })
        if resp.status_code == 404:
            return []
        items = resp.json().get("Items") or []
        return [self._item_to_playlist_item(it) for it in items]

    def playlist_item_count(self, rating_key: str) -> int:
        if not rating_key:
            return 0
        # Ask for one item but enable the total count so we don't pay for the
        # whole list just to count it.
        resp = self._request("GET", f"/Playlists/{rating_key}/Items", params={
            "userId": self._user_id,
            "limit": 1,
            "enableTotalRecordCount": "true",
        })
        if resp.status_code == 404:
            return 0
        return int(resp.json().get("TotalRecordCount") or 0)

    # ----- playlist write ---------------------------------------------------

    def create_playlist(self, title: str, ordered_rating_keys: list[str]) -> str:
        if not ordered_rating_keys:
            raise ValueError("Cannot create an Emby playlist with zero items")
        # Emby reads these from the query string, NOT a JSON body (a JSON body
        # yields "Unrecognized Guid format"). Ids is comma-delimited.  Split
        # into chunks so we never blow past URI length limits.
        ordered = list(ordered_rating_keys)
        first, rest = ordered[:_MAX_IDS_PER_REQUEST], ordered[_MAX_IDS_PER_REQUEST:]
        resp = self._request("POST", "/Playlists", params={
            "Name": title,
            "Ids": ",".join(str(k) for k in first),
            "UserId": self._user_id,
            "MediaType": "Video",
        })
        body = resp.json()
        new_id = body.get("Id")
        if not new_id:
            raise EmbyAPIError(f"Create playlist response missing Id: {body!r}")
        if rest:
            self.add_items_to_playlist(str(new_id), rest)
        return str(new_id)

    def delete_playlist(self, rating_key: str) -> None:
        """Delete the playlist itself.

        This is the ONE place where we intentionally hit `DELETE /Items?Ids=X`
        (the same endpoint that deletes library content). Emby has no
        `GET /Playlists/{id}`, so we verify via an `/Items?Ids=` lookup that the
        target both exists AND is a `Playlist` before bypassing the DELETE
        safety check for this one call. The param is `Ids` (PascalCase) — Emby
        is case-sensitive and silently ignores a lowercase `ids`.

        The lookup has NO `userId` filter on purpose: a playlist owned by a user
        other than the lazily-resolved one would otherwise look "gone" and we'd
        silently skip the delete (the reported bug — "Playlist deleted" yet it
        stays on Emby).
        """
        if not rating_key:
            return
        check = self._request("GET", "/Items", params={"Ids": rating_key})
        items = (check.json().get("Items") or []) if check.ok else []
        if not items:
            _log.info("Emby delete_playlist: %s not found (already gone)", rating_key)
            return  # already gone, no-op
        if items[0].get("Type") != "Playlist":
            raise EmbySafetyError(
                f"Refusing to DELETE item {rating_key!r}: it is a "
                f"{items[0].get('Type')!r}, not a Playlist."
            )
        # Verified target is a playlist; safe to bypass for this one call.
        resp = self._request(
            "DELETE", "/Items",
            params={"Ids": rating_key},
            _bypass_delete_check=True,
        )
        _log.info("Emby delete_playlist: DELETE /Items?Ids=%s -> %s", rating_key, resp.status_code)

    def rename_playlist(self, rating_key: str, new_title: str) -> None:
        """Rename via the metadata-editor path: fetch the full item dto from
        the user-scoped `/Users/{id}/Items/{id}` (the reliable read path on
        Emby), change Name, POST the dto back to `/Items/{id}`. Emby has no
        playlist-update endpoint (POST /Playlists/{id} is Jellyfin-only), and
        `/Items/{id}` expects the full dto — a partial body wipes fields.
        Confirms the target is a Playlist first."""
        if not rating_key or not new_title:
            return
        check = self._request("GET", "/Items", params={"Ids": rating_key})
        items = (check.json().get("Items") or []) if check.ok else []
        if not items or items[0].get("Type") != "Playlist":
            raise EmbyAPIError(
                f"Refusing to rename item {rating_key!r}: not a Playlist"
            )
        resp = self._request("GET", f"/Users/{self._user_id}/Items/{rating_key}")
        if not resp.ok:
            raise EmbyAPIError(
                f"Rename: fetch item {rating_key} returned {resp.status_code}"
            )
        dto = resp.json()
        dto["Name"] = new_title
        upd = self._request("POST", f"/Items/{rating_key}", json=dto)
        if not upd.ok:
            raise EmbyAPIError(
                f"Rename playlist {rating_key} returned {upd.status_code}"
            )

    def add_items_to_playlist(
        self, rating_key: str, item_rating_keys: list[str]
    ) -> None:
        if not rating_key or not item_rating_keys:
            return
        for chunk in _chunked(list(item_rating_keys), _MAX_IDS_PER_REQUEST):
            self._request("POST", f"/Playlists/{rating_key}/Items", params={
                "ids": ",".join(chunk),
                "userId": self._user_id,
            })

    def remove_items_from_playlist(
        self, rating_key: str, item_rating_keys: list[str]
    ) -> None:
        """Remove by underlying media-item id. We have to map id → PlaylistItemId
        first because Emby's DELETE /Playlists/{id}/Items takes entry ids.

        This is used by the ABC's default replace_playlist_items() fallback
        (remove-all + add-all).
        """
        if not rating_key or not item_rating_keys:
            return
        # Fetch playlist contents to build id → PlaylistItemId map. One row per
        # entry, so duplicates each get their own PlaylistItemId.
        resp = self._request("GET", f"/Playlists/{rating_key}/Items", params={
            "userId": self._user_id,
            "fields": "",
        })
        if resp.status_code == 404:
            return
        entries: list[str] = []
        wanted = set(item_rating_keys)
        for it in resp.json().get("Items") or []:
            if str(it.get("Id")) in wanted:
                pid = it.get("PlaylistItemId")
                if pid:
                    entries.append(str(pid))
        if not entries:
            return
        for chunk in _chunked(entries, _MAX_IDS_PER_REQUEST):
            self._request("DELETE", f"/Playlists/{rating_key}/Items", params={
                "entryIds": ",".join(chunk),
            })

    def get_view_counts(self, rating_keys):
        out: dict[str, int] = {}
        if not rating_keys:
            return out
        # Use the USER-SCOPED path /Users/{id}/Items — Emby reliably attaches
        # UserData (PlayCount) there, the same way the working watch-state reads
        # do (get_playlist_items → /Playlists/{id}/Items, episodes_for_show →
        # /Shows/{id}/Episodes). The plain /Items?userId= endpoint can return
        # empty UserData on Emby, so every count read 0 → franchise pruning never
        # removed watched items and the next sync re-added them (v3.3.4).
        uid = self._user_id  # resolve the lazy property once
        for chunk in _chunked([str(k) for k in rating_keys], _MAX_IDS_PER_REQUEST):
            try:
                resp = self._request("GET", f"/Users/{uid}/Items", params={
                    "Ids": ",".join(chunk),
                    "enableUserData": "true",
                })
            except Exception:
                _log.warning("get_view_counts request failed on emby chunk", exc_info=True)
                continue
            if not getattr(resp, "ok", False):
                continue
            for it in (resp.json() or {}).get("Items", []) or []:
                ud = it.get("UserData") or {}
                out[str(it.get("Id"))] = int(ud.get("PlayCount") or 0)
        return out

    def set_playlist_image(self, rating_key: str, image_url: str) -> None:
        """Set the playlist cover via POST /Items/{id}/Images/Primary.

        Emby (like Jellyfin) expects the body to be the base64-encoded image
        bytes with a Content-Type matching the image format. Fire-and-forget:
        never raises.
        """
        if not rating_key or not image_url:
            return
        try:
            import base64
            img = requests.get(image_url, timeout=_HTTP_TIMEOUT)
            if not img.ok or not img.content:
                return
            ctype = img.headers.get("Content-Type", "image/jpeg")
            resp = self._session.post(
                self._url + f"/Items/{rating_key}/Images/Primary",
                data=base64.b64encode(img.content),
                headers={**self._headers(), "Content-Type": ctype},
                timeout=_HTTP_TIMEOUT,
            )
            if not resp.ok:
                _log.warning("set_playlist_image: Emby returned %s for %s", resp.status_code, rating_key)
        except Exception:
            _log.warning("set_playlist_image failed for Emby playlist %s", rating_key, exc_info=True)

    # ----- images -----------------------------------------------------------

    def fetch_image(
        self,
        image_ref: str,
        width: int | None = None,
        height: int | None = None,
    ) -> tuple[bytes, str]:
        """Fetch a poster/thumb. `image_ref` is the underlying item id
        (Emby's image URL is always derivable from the id)."""
        params: dict = {}
        if width:
            params["maxWidth"] = width
        if height:
            params["maxHeight"] = height
        # Force a JPEG so the browser caches it consistently.
        params["format"] = "Jpg"
        resp = self._request(
            "GET", f"/Items/{image_ref}/Images/Primary",
            params=params, stream=True,
        )
        if resp.status_code == 404:
            raise EmbyAPIError(f"Emby image not found for item {image_ref!r}")
        content = resp.content
        ctype = resp.headers.get("Content-Type", "image/jpeg")
        return content, ctype

    # ----- metadata refresh --------------------------------------------------

    def refresh_show_metadata(self, rating_key: str) -> None:
        """POST /Items/{id}/Refresh — fire-and-forget metadata refresh."""
        resp = self._request(
            "POST",
            f"/Items/{rating_key}/Refresh",
            params={
                "MetadataRefreshMode": "FullRefresh",
                "ImageRefreshMode":    "FullRefresh",
                "ReplaceAllMetadata":  "false",
            },
        )
        if resp.status_code not in (200, 204):
            raise EmbyAPIError(
                f"Refresh failed for {rating_key}: {resp.status_code}"
            )

    def list_playlist_episodes(self, playlist_id: str) -> list:
        # Ensure _user_id is populated before constructing params (same
        # pattern as episodes_for_show — params are evaluated before
        # _request() calls _ensure_authenticated() internally).
        self._ensure_authenticated()
        resp = self._request(
            "GET", f"/Playlists/{playlist_id}/Items",
            params={"UserId": self._user_id, "Fields": "UserData"},
        )
        if not resp.ok:
            return []
        items = resp.json().get("Items", [])
        class _Item:
            def __init__(self, d):
                self.view_count = (d.get("UserData") or {}).get("PlayCount", 0) or 0
        return [_Item(item) for item in items]
