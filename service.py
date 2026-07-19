"""High-level operations: create / edit / prune managed rotating playlists.

Bridges db (managed state), the `MediaClient` interface (server I/O), and
`rotation` (pure logic). Every server call goes through MediaClient; this
module never imports a backend-specific module directly.

Dual-backend (v1.1.0):
  * A `managed_playlists.backend` of 'plex', 'jellyfin', or 'both' decides
    which backends each operation iterates over.
  * `_clients_for_playlist(row)` is the single dispatch point.
  * Each show row carries both `plex_show_item_id` and `jellyfin_show_item_id`
    (each nullable). Per-backend operations filter to shows that have an ID
    on that backend; missing-side shows are silently skipped on that side.
  * For 'both' playlists, sync runs heal-on-sync: shows missing an ID on one
    side get a fresh title+year match attempt against that backend's library.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

import db
import rotation
import webhooks as _webhooks
from media_client import (
    EpisodeRef,
    MediaClient,
    MovieSummary,
    ShowSummary,
    get_client,
    normalize_title,
    titles_match,
)
from media_client import parse_backend_set, ALL_BACKENDS, primary_backend
from rotation import PlaylistItem
from trakt_client import get_trakt_client

log = logging.getLogger(__name__)


def _compute_playlist_stats(
    playlist_id: int,
    backend: str,
    client: MediaClient,
    pl_id: str,
    configs: list[ShowConfig],
) -> dict:
    import datetime as _dt
    try:
        episodes = client.list_playlist_episodes(pl_id)
    except Exception:
        return {}
    total = len(episodes)
    watched = sum(1 for ep in episodes if getattr(ep, "view_count", 0) or 0 > 0)
    return {
        "synced_at": _dt.datetime.utcnow().isoformat(timespec="seconds"),
        "backend": backend,
        "total_episodes": total,
        "watched_episodes": watched,
    }


# --------------------------------------------------------------------------- #
# Config struct
# --------------------------------------------------------------------------- #


@dataclass
class ShowConfig:
    """The per-show configuration carried through preview/create/sync.

    `rating_key` is the row's primary identifier. For legacy Plex-only rows
    it equals plex_rating_key. For Jellyfin-originated rows (without a Plex
    match) it equals jellyfin_rating_key. The dedicated *_rating_key fields
    below are what we actually use for API calls per backend.
    """

    rating_key: str
    title: str = ""
    thumb: str | None = None
    start_season: int = 1
    end_season: int | None = None
    include_specials: bool = False
    include_movies: bool = False
    movie_rating_keys: list[str] = field(default_factory=list)  # Plex movie ratingKeys
    # v1.3.0 — per-show weight for rotation_weighted (clamped to >=1).
    weight: int = 1
    # v1.4.0 — soft-delete flag for genre playlists. Excluded shows are
    # kept in the DB but skipped by tail-rebuild and the auto-add path
    # of sync. UI offers a re-include button.
    is_excluded: bool = False
    # v1.1.0 dual-backend identifiers
    plex_rating_key: str | None = None
    jellyfin_rating_key: str | None = None
    jellyfin_movie_rating_keys: list[str] = field(default_factory=list)
    emby_rating_key: str | None = None
    emby_movie_rating_keys: list[str] = field(default_factory=list)
    # v1.2.0 per-episode exclusions: set of (season, episode) pairs.
    # Backend-agnostic — works on both Plex and Jellyfin since episodes use
    # the same (season, episode) shape on both sides.
    excluded_episodes: set[tuple[int, int]] = field(default_factory=set)
    # v3.2.4 — transient (not persisted): which backend `thumb` came from, so
    # the genre/smart preview builds /thumb?b=<that backend> instead of guessing
    # primary_backend(). Genre membership differs per backend, so a show's thumb
    # may originate on a non-primary backend even when it also exists on Plex.
    thumb_backend: str | None = None

    def __post_init__(self) -> None:
        # Legacy callers (Phase 1 / single-backend Plex) pass only rating_key
        # for a Plex show; mirror it into plex_rating_key so backend dispatch
        # below works uniformly. Guard on the OTHER backends being unset so an
        # Emby/Jellyfin server that happens to use NUMERIC item ids doesn't get
        # a phantom plex_rating_key (issue #5: numeric Emby id 66186 → wrong
        # primary backend → /thumb?b=plex 404).
        if (
            self.plex_rating_key is None
            and self.jellyfin_rating_key is None
            and self.emby_rating_key is None
            and self.rating_key and self.rating_key.isdigit()
        ):
            self.plex_rating_key = self.rating_key

    def id_for(self, backend: str) -> str | None:
        return {"plex": self.plex_rating_key, "jellyfin": self.jellyfin_rating_key,
                "emby": self.emby_rating_key}.get(backend)

    def movie_ids_for(self, backend: str) -> list[str]:
        return {"plex": self.movie_rating_keys, "jellyfin": self.jellyfin_movie_rating_keys,
                "emby": self.emby_movie_rating_keys}.get(backend, [])

    @property
    def excluded_csv(self) -> str:
        """Serialize excluded_episodes as 'S:E,S:E,...' for the configure form."""
        return ",".join(f"{s}:{e}" for s, e in sorted(self.excluded_episodes))


# --------------------------------------------------------------------------- #
# Dispatch helpers
# --------------------------------------------------------------------------- #


def _watched_keep() -> int:
    try:
        return max(0, int(os.environ.get("WATCHED_KEEP", "2")))
    except ValueError:
        return 2


def _backends_for(row) -> list[str]:
    """Backend names this playlist row targets."""
    backend = row["backend"] if "backend" in row.keys() else "plex"
    return parse_backend_set(backend)


def _client_for(backend: str) -> MediaClient:
    return get_client(backend)


COLUMN_MAP = {"plex": "plex_rating_key", "jellyfin": "jellyfin_playlist_id", "emby": "emby_playlist_id"}


def _playlist_id_on(row, backend: str) -> str | None:
    """The backend-specific playlist id stored on this row (None if not created)."""
    col = COLUMN_MAP.get(backend)
    if not col:
        return None
    return row[col] if col in row.keys() else None


def _clients_for_playlist(row) -> list[tuple[str, MediaClient, str | None]]:
    """Return [(backend, client, backend_playlist_id), ...] for each backend
    this playlist targets. backend_playlist_id may be None if the playlist
    hasn't been created on that backend yet (e.g. mid-create failure)."""
    out: list[tuple[str, MediaClient, str | None]] = []
    for backend in _backends_for(row):
        out.append((backend, _client_for(backend), _playlist_id_on(row, backend)))
    return out


# --------------------------------------------------------------------------- #
# Per-backend config hydration + episode fetch
# --------------------------------------------------------------------------- #


def _hydrate_configs(configs: list[ShowConfig], backend: str) -> None:
    """Fill in missing title/thumb from the backend, using whichever id is
    populated for that backend. Configs already populated stay untouched."""
    client = _client_for(backend)
    for cfg in configs:
        target_id = cfg.id_for(backend)
        if not target_id:
            continue
        if cfg.title and cfg.thumb is not None:
            continue
        try:
            summary = client.get_show_summary(target_id)
        except Exception:
            log.debug("hydrate skipped for %s on %s (lookup failed)", target_id, backend)
            continue
        cfg.title = cfg.title or summary.title
        if cfg.thumb is None:
            cfg.thumb = summary.thumb


def _episodes_for_config(
    cfg: ShowConfig, backend: str, unwatched_only: bool = False
) -> list[EpisodeRef]:
    """Episodes for one show on one backend. Returns [] if the show has no
    id on this backend (caller should treat that as "skip on this side")."""
    target_id = cfg.id_for(backend)
    if not target_id:
        return []
    client = _client_for(backend)
    eps = client.episodes_for_show(
        target_id,
        start_season=cfg.start_season,
        end_season=cfg.end_season,
        include_specials=cfg.include_specials,
    )
    if unwatched_only:
        eps = [e for e in eps if e.view_count == 0]

    # v1.2.0: drop any explicitly-excluded (season, episode) pairs. Works
    # uniformly across backends since EpisodeRef uses the same shape on both.
    if cfg.excluded_episodes:
        eps = [e for e in eps if (e.season, e.episode) not in cfg.excluded_episodes]

    if cfg.include_movies:
        movie_refs: list[EpisodeRef] = []
        for mrk in cfg.movie_ids_for(backend):
            ms = client.get_movie_summary(mrk)
            if ms is None:
                continue
            if unwatched_only and ms.view_count > 0:
                continue
            movie_refs.append(client.movie_as_episode_ref(ms, target_id, cfg.title or ""))
        movie_refs.sort(key=lambda m: (m.air_date or "9999-99-99", m.title.lower()))
        eps = eps + movie_refs

    # rebuild_tail keys items by (season, episode) per show. Both backends use
    # the same show_rating_key field on EpisodeRef, but that's the backend's
    # own show id. For dual-backend rotation each side computes independently
    # so this is fine — the kept set is read from THIS side's playlist.
    return eps


# --------------------------------------------------------------------------- #
# DB row → ShowConfig
# --------------------------------------------------------------------------- #


def _parse_excluded_episodes(raw: str | None) -> set[tuple[int, int]]:
    """Parse a 'S:E,S:E,...' string into a set of (season, episode) tuples."""
    out: set[tuple[int, int]] = set()
    if not raw:
        return out
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            s_str, e_str = token.split(":", 1)
            out.add((int(s_str), int(e_str)))
        except (ValueError, AttributeError):
            log.warning("Skipping malformed excluded_episode entry: %r", token)
    return out


def _config_from_row(row) -> ShowConfig:
    raw_plex_keys = row["movie_rating_keys"] if "movie_rating_keys" in row.keys() else ""
    plex_movies = [k for k in (raw_plex_keys or "").split(",") if k]
    raw_jf_keys = row["jellyfin_movie_item_ids"] if "jellyfin_movie_item_ids" in row.keys() else ""
    jf_movies = [k for k in (raw_jf_keys or "").split(",") if k]
    emby_id = row["emby_show_item_id"] if "emby_show_item_id" in row.keys() else None
    emby_movie_raw = row["emby_movie_item_ids"] if "emby_movie_item_ids" in row.keys() else ""
    emby_movies = [k for k in (emby_movie_raw or "").split(",") if k]
    plex_id = row["plex_show_item_id"] if "plex_show_item_id" in row.keys() else None
    jf_id = row["jellyfin_show_item_id"] if "jellyfin_show_item_id" in row.keys() else None
    # Legacy rows pre-migration don't have plex_show_item_id; fall back to the PK,
    # but ONLY when there's no Jellyfin/Emby id (a numeric Emby/Jellyfin id must
    # not be misread as a Plex ratingKey — issue #5).
    if (plex_id is None and jf_id is None and emby_id is None
            and str(row["show_rating_key"]).isdigit()):
        plex_id = str(row["show_rating_key"])
    excluded_raw = row["excluded_episode_keys"] if "excluded_episode_keys" in row.keys() else ""
    weight_val = max(1, int(_row_get(row, "weight", 1) or 1))
    is_excl = bool(_row_get(row, "is_excluded", 0) or 0)
    return ShowConfig(
        rating_key=row["show_rating_key"],
        title=row["show_title"],
        thumb=row["show_thumb"],
        start_season=int(row["start_season"] or 1),
        end_season=row["end_season"],
        include_specials=bool(row["include_specials"]),
        include_movies=bool(row["include_movies"]) if "include_movies" in row.keys() else False,
        movie_rating_keys=plex_movies,
        plex_rating_key=plex_id,
        jellyfin_rating_key=jf_id,
        jellyfin_movie_rating_keys=jf_movies,
        emby_rating_key=emby_id,
        emby_movie_rating_keys=emby_movies,
        excluded_episodes=_parse_excluded_episodes(excluded_raw),
        weight=weight_val,
        is_excluded=is_excl,
    )


# --------------------------------------------------------------------------- #
# Cross-backend show matching (used at add-time + heal-on-sync)
# --------------------------------------------------------------------------- #


def _candidates_for(backend: str) -> list:
    """List of every show on a backend. Network call — caller caches."""
    try:
        return _client_for(backend).list_all_shows()
    except Exception:
        log.exception("show matching: couldn't list shows on %s", backend)
        return []


def _find_match(candidates: list, title: str, year: int | None) -> str | None:
    for s in candidates:
        if titles_match(s.title, title, s.year, year):
            return s.rating_key
    return None


def _find_match_by_tvdb(candidates: list[ShowSummary], tvdb_id: str) -> str | None:
    """Find a show in candidates by TVDB ID."""
    for s in candidates:
        if s.tvdb_id == tvdb_id:
            return s.rating_key
    return None


def _tvdb_id_for_cfg(cfg: ShowConfig) -> str | None:
    """Look up the TVDB id for a config by asking whichever backend already
    has an id for this show. Returns None if the show can't be found or has
    no TVDB id."""
    for backend in ("plex", "jellyfin", "emby"):
        target_id = cfg.id_for(backend)
        if not target_id:
            continue
        try:
            summary = _client_for(backend).get_show_summary(target_id)
            if summary.tvdb_id:
                return summary.tvdb_id
        except Exception:
            continue
    return None


def _find_match_by_tvdb_for_cfg(
    cfg: ShowConfig, candidates: list[ShowSummary], source_backend: str
) -> str | None:
    """If the config already has an id on `source_backend`, look up its TVDB id
    and search `candidates` for a show with the same TVDB id."""
    tvdb_id = _tvdb_id_for_cfg(cfg)
    if not tvdb_id:
        return None
    return _find_match_by_tvdb(candidates, tvdb_id)


def _show_id_set(summary) -> set:
    """Provider-id set for a ShowSummary, e.g. {('tvdb','75682'),
    ('tmdb','1668'), ('imdb','tt0386676')}. Two shows that share ANY of these
    are the same show — this is what bridges backends scraped with different
    metadata agents (TVDB-only vs TMDB-only vs IMDB)."""
    ids: set = set()
    if summary is None:
        return ids
    if summary.tvdb_id:
        ids.add(("tvdb", str(summary.tvdb_id)))
    if summary.tmdb_id:
        ids.add(("tmdb", str(summary.tmdb_id)))
    if getattr(summary, "imdb_id", None):
        ids.add(("imdb", str(summary.imdb_id)))
    return ids


def _summary_for_cfg(cfg: ShowConfig):
    """The ShowSummary from whichever backend already has this show (one network
    call). Source of the config's provider ids + year for cross-backend match."""
    for backend in ("plex", "jellyfin", "emby"):
        target_id = cfg.id_for(backend)
        if not target_id:
            continue
        try:
            return _client_for(backend).get_show_summary(target_id)
        except Exception:
            continue
    return None


def _find_match_by_ids(candidates: list, want_ids: set) -> str | None:
    """rating_key of the candidate that shares ANY provider id (TVDB/TMDB/IMDB)
    with `want_ids`, else None."""
    if not want_ids:
        return None
    for s in candidates:
        if _show_id_set(s) & want_ids:
            return s.rating_key
    return None


def _enrich_configs_with_matches(configs: list[ShowConfig], target_backends: list[str]) -> None:
    """For each config missing an id on a target backend, attempt to find it
    by TVDB ID first, then by title+year. Populates the appropriate *_rating_key
    field in-place.

    Caches the candidate list per backend so we make at most ONE
    list_all_shows call per backend, regardless of how many configs need
    matching.
    """
    cache: dict[str, list] = {}
    def cands(b: str) -> list:
        if b not in cache:
            cache[b] = _candidates_for(b)
        return cache[b]

    _fields = {
        "plex": "plex_rating_key",
        "jellyfin": "jellyfin_rating_key",
        "emby": "emby_rating_key",
    }
    for cfg in configs:
        # Read the source show's provider ids once and match every other backend
        # by any shared TVDB/TMDB/IMDB id, then title+year as a last resort.
        summary = _summary_for_cfg(cfg)
        want_ids = _show_id_set(summary)
        year = (summary.year if summary else None) or _year_hint(cfg)
        for be, field in _fields.items():
            if be not in target_backends or getattr(cfg, field) is not None:
                continue
            mid = (_find_match_by_ids(cands(be), want_ids)
                   or _find_match(cands(be), cfg.title or "", year))
            if mid:
                setattr(cfg, field, mid)


def _year_hint(cfg: ShowConfig) -> int | None:
    """Best-effort year for the ShowConfig — used to disambiguate matches.
    Tries whichever backend already has an id for this show; cheap, tolerant."""
    for backend in ("plex", "jellyfin", "emby"):
        target_id = cfg.id_for(backend)
        if not target_id:
            continue
        try:
            summary = _client_for(backend).get_show_summary(target_id)
            if summary.year:
                return summary.year
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------- #
# The single tail-rebuild primitive (replaces 6 near-identical blocks)
# --------------------------------------------------------------------------- #


def _build_crossover_map(playlist_id: int) -> dict[tuple[str, int, int], tuple[int, int]]:
    """Build a lookup map for crossover group membership.
    Returns dict[(show_rating_key, season, episode), (group_id, sort_index)].
    """
    cmap: dict[tuple[str, int, int], tuple[int, int]] = {}
    for g in db.list_crossover_groups(playlist_id):
        for link in g["links"]:
            cmap[(link["show_rating_key"], link["season"], link["episode"])] = (
                g["id"], link["sort_index"],
            )
    return cmap


def _rebuild_tail_on(
    backend: str,
    client: MediaClient,
    playlist_id_on_backend: str,
    configs: list[ShowConfig],
    sort_mode: str,
    unwatched_only: bool,
    *,
    block_size: int = 1,
    shuffle_seed: int | None = None,
    crossover_map: dict[tuple[str, int, int], tuple[int, int]] | None = None,
    pruning_enabled: bool = False,
    keep_last_n: int | None = None,
    reorder_flag: list | None = None,
) -> tuple[int, int]:
    """Recompute the future portion of a playlist on a single backend.

    Returns (added_count, removed_count). Configs without an id on this
    backend are silently filtered out (they don't contribute on this side).

    `block_size` and `shuffle_seed` are only consulted when `sort_mode` is
    'rotation_blocks' or 'shuffle_chronological' respectively. Per-show
    weights for 'rotation_weighted' come from each ShowConfig.weight.
    `crossover_map` is only used in 'air_date' mode.

    When `pruning_enabled` is True, the watched head buffer is trimmed to the
    last `keep_last_n` watched items and the future tail is rebuilt unwatched,
    making pruning idempotent.
    """
    relevant_configs = [c for c in configs if c.id_for(backend)]
    if not relevant_configs:
        return (0, 0)
    if keep_last_n is None:
        keep_last_n = _watched_keep()

    items = client.get_playlist_items(playlist_id_on_backend)
    splice = rotation.splice_index(items)
    head = items[:splice]

    head_removed_keys: list[str] = []
    if pruning_enabled:
        drop_head = set(rotation.prune_indices(head, keep_last_n))
        head_removed_keys = [head[i].rating_key for i in drop_head]
        kept = [it for i, it in enumerate(head) if i not in drop_head]
    else:
        kept = head

    tail_unwatched = unwatched_only or pruning_enabled
    shows_episodes = [
        _episodes_for_config(c, backend, unwatched_only=tail_unwatched)
        for c in relevant_configs
    ]
    show_order = [c.id_for(backend) for c in relevant_configs]
    weights = [c.weight for c in relevant_configs]
    new_tail = rotation.rebuild_tail(
        kept, shows_episodes, mode=sort_mode, show_order=show_order,
        weights=weights, block_size=block_size, shuffle_seed=shuffle_seed,
        crossover_map=crossover_map,
    )

    current_tail = items[splice:]
    current_tail_keys = [it.rating_key for it in current_tail]
    new_tail_keys = [e.rating_key for e in new_tail]

    if current_tail_keys == new_tail_keys and not head_removed_keys:
        return (0, 0)

    kept_keys = {it.rating_key for it in kept}
    current_set = set(current_tail_keys)
    new_set = set(new_tail_keys)
    leavers = list((current_set - new_set) | set(head_removed_keys))
    newcomers = [k for k in new_tail_keys if k not in current_set and k not in kept_keys]

    # Cheap path: the surviving items keep their relative order and every
    # newcomer belongs at the very end — we can patch with remove+append and
    # never touch existing rows.
    surviving_current = [k for k in current_tail_keys if k in new_set]
    surviving_new = [k for k in new_tail_keys if k in current_set]
    newcomers_are_suffix = new_tail_keys[len(new_tail_keys) - len(newcomers):] == newcomers
    if surviving_current == surviving_new and newcomers_are_suffix:
        if leavers:
            client.remove_items_from_playlist(playlist_id_on_backend, leavers)
        if newcomers:
            client.add_items_to_playlist(playlist_id_on_backend, newcomers)
        return (len(newcomers), len(leavers))

    # Order changed (or a newcomer belongs mid-tail): rebuild the playlist in
    # the exact computed order, preserving the kept head. Atomic on Jellyfin;
    # the MediaClient default (remove-all + add-all) covers Plex/Emby.
    kept_ordered = [it.rating_key for it in kept]
    client.replace_playlist_items(
        playlist_id_on_backend, kept_ordered + new_tail_keys
    )
    log.info(
        "Reordered playlist tail on %s: %d kept + %d tail items "
        "(+%d new, -%d removed)",
        backend, len(kept_ordered), len(new_tail_keys),
        len(newcomers), len(leavers),
    )
    if reorder_flag is not None:
        reorder_flag.append(backend)
    return (len(newcomers), len(leavers))


def _row_get(row, key, default=None):
    """Safe accessor for sqlite3.Row that returns default if column absent."""
    return row[key] if key in row.keys() else default


def _rebuild_playlist_tails(
    row,
    full_configs: list[ShowConfig],
    *,
    sort_mode: str | None = None,
    unwatched_only: bool | None = None,
    block_size: int | None = None,
    shuffle_seed: int | None = ...,  # sentinel — None is a valid value
    op_label: str = "tail rebuild",
) -> tuple[int, int, list[str]]:
    """Iterate every enabled backend for a playlist and rebuild its tail.

    Defaults all params from the row; callers that have JUST written a new
    value to the DB (but haven't refetched the row) pass the new value via
    the corresponding kwarg.
    Returns (total_added, total_removed) across backends. Individual backend
    failures are logged but don't block the other backend.

    Soft-deleted shows (is_excluded=True) are filtered out — they remain in
    the DB but don't contribute to playlist contents.
    """
    sm = sort_mode if sort_mode is not None else row["sort_mode"]
    uw = unwatched_only if unwatched_only is not None else bool(row["unwatched_only"])
    bs = block_size if block_size is not None else int(_row_get(row, "block_size", 1) or 1)
    ss = _row_get(row, "shuffle_seed", None) if shuffle_seed is ... else shuffle_seed
    pe = bool(_row_get(row, "pruning_enabled", 1))
    kln = _watched_keep()
    active_configs = [c for c in full_configs if not c.is_excluded]
    crossover_map = _build_crossover_map(row["id"]) if sm == "air_date" else None
    total_added = total_removed = 0
    reordered: list[str] = []
    for tb, client, pl_id in _clients_for_playlist(row):
        if not pl_id:
            continue
        try:
            added, removed = _rebuild_tail_on(
                tb, client, pl_id, active_configs, sm, uw,
                block_size=bs, shuffle_seed=ss, crossover_map=crossover_map,
                pruning_enabled=pe, keep_last_n=kln,
                reorder_flag=reordered,
            )
            total_added += added
            total_removed += removed
        except Exception:
            log.exception("%s failed on %s for '%s'", op_label, tb, row["name"])
    return (total_added, total_removed, reordered)


# --------------------------------------------------------------------------- #
# Preview (no backend writes)
# --------------------------------------------------------------------------- #


def preview_playlist(
    configs: list[ShowConfig],
    limit: int = 2000,
    sort_mode: str = "rotation",
    unwatched_only: bool = False,
    backend: str = "plex",
    *,
    block_size: int = 1,
    shuffle_seed: int | None = None,
) -> list[dict]:
    """Compute the first `limit` episodes of the resulting playlist on a
    single backend without touching its write APIs. For 'both' playlists,
    the UI typically previews on whichever backend the user picked shows
    from. Default 'plex' for back-compat with existing UI."""
    _hydrate_configs(configs, backend)
    relevant = [c for c in configs if c.id_for(backend)]
    if not relevant:
        relevant = configs
    shows_eps = [_episodes_for_config(c, backend, unwatched_only=unwatched_only) for c in relevant]
    show_order = [c.id_for(backend) for c in relevant if c.id_for(backend)]
    weights = [c.weight for c in relevant]
    composed = rotation.compose(
        shows_eps, mode=sort_mode, show_order=show_order,
        weights=weights, block_size=block_size, shuffle_seed=shuffle_seed,
    )
    out: list[dict] = []
    for ep in composed[:limit]:
        out.append({
            "show": ep.show_title,
            "season": ep.season,
            "episode": ep.episode,
            "title": ep.title,
            "air_date": ep.air_date,
            "is_special": ep.season == 0,
        })
    return out


# --------------------------------------------------------------------------- #
# View models
# --------------------------------------------------------------------------- #


@dataclass
class PlaylistView:
    id: int
    name: str
    plex_rating_key: str | None
    jellyfin_playlist_id: str | None
    backend: str  # CSV backend set, e.g. 'plex,jellyfin' or 'emby'
    shows: list[dict]  # active (non-excluded) show rows
    item_count: int  # max across enabled backends
    sort_mode: str = "rotation"
    unwatched_only: bool = False
    auto_sync: bool = True
    # v1.3.0 — advanced sequencing
    block_size: int = 1
    shuffle_seed: int | None = None
    # v1.4.0 — dynamic genre playlists
    playlist_type: str = "manual"  # 'manual' | 'genre'
    genre_filter: str | None = None
    excluded_shows: list[dict] = field(default_factory=list)  # soft-deleted rows
    # v1.5.0 — manual crossover groups
    crossover_groups: list[dict] = field(default_factory=list)
    # v1.8.0 — smart playlist rules
    rule_mode: str = "genre"
    # v2.0.0 — analytics
    last_stats: dict | None = None
    # v2.2.0 — per-playlist pruning toggle
    pruning_enabled: int = 1
    # v2.3.0 — counts for index card stats
    movies_count: int = 0
    episodes_count: int = 0
    # v3.0.0 — Emby playlist id
    emby_playlist_id: str | None = None
    # v3.0.0 — backend badge info for UI
    badge_label: str = "P"
    badge_css: str = "is-plex"
    # v3.0.0 — franchise card cover (TMDB); franchise playlists store no
    # playlist_shows rows, so the index card has no per-show posters to collage.
    poster: str | None = None
    # v3.2.3 — up to 5 franchise posters for the home-page card strip (1→full …
    # 5→fifths). Empty for show/genre (those collage from per-show thumbs).
    posters: list[str] = field(default_factory=list)
    # v3.6.0 — per-playlist card artwork control
    card_poster_mode: str = "auto"
    card_poster_keys: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #


def create_managed_playlist(
    name: str,
    configs: list[ShowConfig],
    sort_mode: str = "rotation",
    unwatched_only: bool = False,
    auto_sync: bool = True,
    backend: str = "plex",
    *,
    block_size: int = 1,
    shuffle_seed: int | None = None,
    pruning_enabled: int = 1,
) -> int:
    if not configs:
        raise ValueError("Need at least one show to create a playlist")
    if sort_mode not in rotation.VALID_SORT_MODES:
        raise ValueError(f"Invalid sort_mode: {sort_mode!r}")
    db._validate_backend(backend)

    # Auto-generate a seed if creating in shuffle mode without one — gives
    # this playlist a stable shuffle that persists across syncs.
    if sort_mode == "shuffle_chronological" and shuffle_seed is None:
        import random as _random
        shuffle_seed = _random.randint(1, 2**31 - 1)

    block_size = max(1, int(block_size))

    target_backends = parse_backend_set(backend)

    # Hydrate against the source backend(s) so titles/thumbs are filled in.
    # For multi-backend mode also attempt cross-backend matching.
    for tb in target_backends:
        _hydrate_configs(configs, tb)
    if len(target_backends) > 1:
        _enrich_configs_with_matches(configs, target_backends)

    # Per-backend ordered key list (skip configs without an id on that backend).
    created_ids: dict[str, str] = {}  # backend -> new playlist id
    try:
        for tb in target_backends:
            relevant = [c for c in configs if c.id_for(tb)]
            if not relevant:
                # No shows match this side — for 'both' mode that's an edge
                # case (none of the picked shows existed here at all). Skip
                # creation on this side; the row will have NULL for it.
                continue
            shows_eps = [_episodes_for_config(c, tb, unwatched_only=unwatched_only) for c in relevant]
            show_order = [c.id_for(tb) for c in relevant]
            weights = [c.weight for c in relevant]
            composed = rotation.compose(
                shows_eps, mode=sort_mode, show_order=show_order,
                weights=weights, block_size=block_size, shuffle_seed=shuffle_seed,
            )
            if not composed:
                continue
            new_id = _client_for(tb).create_playlist(name, [e.rating_key for e in composed])
            created_ids[tb] = new_id

        if not created_ids:
            raise ValueError("Could not create the playlist on any enabled backend "
                             "(no selected show has episodes on the targeted backend(s))")
    except Exception:
        # Roll back any side that succeeded so we don't leak orphan playlists.
        for tb, pid in created_ids.items():
            try:
                _client_for(tb).delete_playlist(pid)
                log.warning("Rolled back %s playlist after create failure", tb)
            except Exception:
                log.exception("Rollback failed for %s playlist %s", tb, pid)
        raise

    playlist_id = db.create_playlist(
        name,
        sort_mode=sort_mode,
        unwatched_only=unwatched_only,
        auto_sync=auto_sync,
        backend=backend,
    )
    if "plex" in created_ids:
        db.set_plex_rating_key(playlist_id, created_ids["plex"])
    if "jellyfin" in created_ids:
        db.set_jellyfin_playlist_id(playlist_id, created_ids["jellyfin"])
    if "emby" in created_ids:
        db.set_emby_playlist_id(playlist_id, created_ids["emby"])
    if block_size != 1:
        db.set_block_size(playlist_id, block_size)
    if shuffle_seed is not None:
        db.set_shuffle_seed(playlist_id, shuffle_seed)

    db.add_shows(playlist_id, [_config_to_db_dict(c) for c in configs])
    if not pruning_enabled:
        with db.connection() as conn:
            conn.execute(
                "UPDATE managed_playlists SET pruning_enabled=0 WHERE id=?",
                (playlist_id,),
            )
    log.info(
        "Created playlist '%s' (backend=%s, sides created: %s)",
        name, backend, ",".join(sorted(created_ids.keys())) or "none",
    )
    try:
        _row = db.get_playlist(playlist_id)
        if _row:
            _webhooks.fire("playlist.created", playlist=_webhooks._playlist_info(dict(_row)))
    except Exception:
        pass
    return playlist_id


def _config_to_db_dict(c: ShowConfig) -> dict:
    return {
        "rating_key": c.rating_key,
        "plex_show_item_id": c.plex_rating_key,
        "jellyfin_show_item_id": c.jellyfin_rating_key,
        "title": c.title,
        "thumb": c.thumb,
        "start_season": c.start_season,
        "end_season": c.end_season,
        "include_specials": c.include_specials,
        "include_movies": c.include_movies,
        "movie_rating_keys": c.movie_rating_keys,
        "jellyfin_movie_item_ids": c.jellyfin_movie_rating_keys,
        "emby_show_item_id": c.emby_rating_key,
        "emby_movie_item_ids": c.emby_movie_rating_keys,
        "excluded_episodes": c.excluded_episodes,
        "weight": c.weight,
    }


# --------------------------------------------------------------------------- #
# Add shows mid-rotation
# --------------------------------------------------------------------------- #


def add_shows_to_playlist(playlist_id: int, new_configs: list[ShowConfig]) -> None:
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError(f"No managed playlist with id={playlist_id}")

    existing_rows = db.list_shows(playlist_id)
    existing_keys = {s["show_rating_key"] for s in existing_rows}
    new_configs = [c for c in new_configs if c.rating_key not in existing_keys]
    if not new_configs:
        return

    target_backends = _backends_for(row)
    for tb in target_backends:
        _hydrate_configs(new_configs, tb)
    if len(target_backends) > 1:
        _enrich_configs_with_matches(new_configs, target_backends)

    db.add_shows(playlist_id, [_config_to_db_dict(c) for c in new_configs])

    full_configs: list[ShowConfig] = [_config_from_row(r) for r in db.list_shows(playlist_id)]
    added, removed, _ = _rebuild_playlist_tails(row, full_configs, op_label="add_shows tail rebuild")
    log.info("Added shows to '%s': +%d -%d tail items", row["name"], added, removed)


# --------------------------------------------------------------------------- #
# Remove a show entirely
# --------------------------------------------------------------------------- #


def remove_show_from_playlist(playlist_id: int, show_rating_key: str) -> None:
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError(f"No managed playlist with id={playlist_id}")

    target_row = db.get_show(playlist_id, show_rating_key)
    if not target_row:
        return
    target_cfg = _config_from_row(target_row)

    for tb, client, pl_id in _clients_for_playlist(row):
        if not pl_id:
            continue
        show_id_on_backend = target_cfg.id_for(tb)
        if not show_id_on_backend:
            continue  # show wasn't on this backend in the first place
        try:
            items = client.get_playlist_items(pl_id)
            to_remove_keys = [
                it.rating_key for it in items
                if it.show_rating_key == show_id_on_backend
            ]
            client.remove_items_from_playlist(pl_id, to_remove_keys)
            log.info("Removed %d %s items for show %s from '%s'",
                     len(to_remove_keys), tb, show_id_on_backend, row["name"])
        except Exception:
            log.exception("remove_show failed on %s for '%s'", tb, row["name"])

    db.remove_show(playlist_id, show_rating_key)


# --------------------------------------------------------------------------- #
# Reorder rotation
# --------------------------------------------------------------------------- #


def reorder_shows(playlist_id: int, ordered_keys: list[str]) -> None:
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError(f"No managed playlist with id={playlist_id}")

    existing = {
        s["show_rating_key"]: s
        for s in db.list_shows(playlist_id)
        if not s["is_excluded"]
    }
    if set(ordered_keys) != set(existing.keys()):
        raise ValueError("Reorder keys don't match the current show set")

    db.set_positions(playlist_id, ordered_keys)

    full_configs = [_config_from_row(r) for r in db.list_shows(playlist_id)]
    _rebuild_playlist_tails(row, full_configs, op_label="reorder")
    log.info("Reordered '%s'", row["name"])


# --------------------------------------------------------------------------- #
# Change sort mode
# --------------------------------------------------------------------------- #


def set_playlist_sort_mode(playlist_id: int, sort_mode: str) -> None:
    if sort_mode not in rotation.VALID_SORT_MODES:
        raise ValueError(f"Invalid sort_mode: {sort_mode!r}")
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError(f"No managed playlist with id={playlist_id}")
    if row["sort_mode"] == sort_mode:
        return

    db.set_sort_mode(playlist_id, sort_mode)

    full_configs = [_config_from_row(r) for r in db.list_shows(playlist_id)]
    _rebuild_playlist_tails(
        row, full_configs, sort_mode=sort_mode, op_label="sort_mode change"
    )
    log.info("Switched '%s' to sort_mode=%s", row["name"], sort_mode)


# --------------------------------------------------------------------------- #
# Toggle unwatched-only filter
# --------------------------------------------------------------------------- #


def set_playlist_unwatched_only(playlist_id: int, unwatched_only: bool) -> None:
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError(f"No managed playlist with id={playlist_id}")
    if bool(row["unwatched_only"]) == unwatched_only:
        return

    db.set_unwatched_only(playlist_id, unwatched_only)

    full_configs = [_config_from_row(r) for r in db.list_shows(playlist_id)]
    _rebuild_playlist_tails(
        row, full_configs, unwatched_only=unwatched_only, op_label="unwatched-toggle"
    )
    log.info("Switched '%s' unwatched_only=%s", row["name"], unwatched_only)


# --------------------------------------------------------------------------- #
# Per-playlist auto-sync toggle
# --------------------------------------------------------------------------- #


def set_playlist_auto_sync(playlist_id: int, enabled: bool) -> None:
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError(f"No managed playlist with id={playlist_id}")
    db.set_auto_sync(playlist_id, enabled)
    log.info("Playlist '%s' auto_sync=%s", row["name"], enabled)


# --------------------------------------------------------------------------- #
# v1.3.0 — block size, shuffle seed, per-show weight
# --------------------------------------------------------------------------- #


def set_playlist_block_size(playlist_id: int, block_size: int) -> None:
    """Update block_size and rebuild tails on every enabled backend."""
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError(f"No managed playlist with id={playlist_id}")
    new_size = max(1, int(block_size))
    db.set_block_size(playlist_id, new_size)
    full_configs = [_config_from_row(r) for r in db.list_shows(playlist_id)]
    _rebuild_playlist_tails(
        row, full_configs, block_size=new_size, op_label="block_size change"
    )
    log.info("Playlist '%s' block_size=%d", row["name"], new_size)


def reshuffle_playlist(playlist_id: int) -> None:
    """Regenerate the shuffle seed and rebuild tails. Only meaningful for
    playlists in shuffle_chronological mode, but callable in any mode."""
    import random as _random
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError(f"No managed playlist with id={playlist_id}")
    new_seed = _random.randint(1, 2**31 - 1)
    db.set_shuffle_seed(playlist_id, new_seed)
    full_configs = [_config_from_row(r) for r in db.list_shows(playlist_id)]
    _rebuild_playlist_tails(
        row, full_configs, shuffle_seed=new_seed, op_label="reshuffle"
    )
    log.info("Playlist '%s' reshuffled (seed=%d)", row["name"], new_seed)


def set_show_weight(playlist_id: int, show_rating_key: str, weight: int) -> None:
    """Update a single show's weight and rebuild tails."""
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError(f"No managed playlist with id={playlist_id}")
    db.set_show_weight(playlist_id, show_rating_key, max(1, int(weight)))
    full_configs = [_config_from_row(r) for r in db.list_shows(playlist_id)]
    _rebuild_playlist_tails(row, full_configs, op_label="weight change")
    log.info("Playlist '%s' show %s weight=%d", row["name"], show_rating_key, weight)


# --------------------------------------------------------------------------- #
# v1.4.0 — Dynamic genre playlists
# --------------------------------------------------------------------------- #


def _parse_genre_csv(s: str | None) -> list[str]:
    return [g.strip() for g in (s or "").split(",") if g and g.strip()]


def _dedup_show_summaries_to_configs(
    per_backend: dict[str, list[ShowSummary]],
    target_backends: list[str],
) -> list[ShowConfig]:
    """Deduplicate ShowSummary lists from multiple backends into a single
    list of ShowConfigs. Matches via normalized title+year first, then
    TVDB ID fallback. Cross-backend IDs are enriched at the end for 'both'
    setups."""
    out: list[ShowConfig] = []
    seen_keys: dict[str, list[int]] = {}
    id_seen: dict[tuple, int] = {}   # (idtype, idval) -> index in out
    _years: dict[int, int | None] = {}
    link_map = db.get_manual_show_link_map()  # (backend, id) -> group_key
    group_seen: dict[str, int] = {}            # group_key -> index in out

    for tb in target_backends:
        for s in per_backend.get(tb, []):
            nk = normalize_title(s.title)
            grp = link_map.get((tb, str(s.rating_key)))

            match_idx: int | None = None
            # 0. User-asserted manual same-show link wins (explicit override).
            if grp is not None and grp in group_seen:
                match_idx = group_seen[grp]

            if match_idx is None:
                for idx in seen_keys.get(nk, []):
                    existing_year = _years.get(idx)
                    if (
                        existing_year is not None
                        and s.year is not None
                        and existing_year != s.year
                    ):
                        continue
                    match_idx = idx
                    break

            if match_idx is None:
                # Any shared provider id (TVDB/TMDB/IMDB) = same show.
                for pid in _show_id_set(s):
                    if pid in id_seen:
                        match_idx = id_seen[pid]
                        break

            if match_idx is not None:
                if tb == "plex" and out[match_idx].plex_rating_key is None:
                    out[match_idx].plex_rating_key = s.rating_key
                if tb == "jellyfin" and out[match_idx].jellyfin_rating_key is None:
                    out[match_idx].jellyfin_rating_key = s.rating_key
                if tb == "emby" and out[match_idx].emby_rating_key is None:
                    out[match_idx].emby_rating_key = s.rating_key
                if _years.get(match_idx) is None and s.year is not None:
                    _years[match_idx] = s.year
                if not out[match_idx].thumb and s.thumb:
                    out[match_idx].thumb = s.thumb
                    out[match_idx].thumb_backend = tb
                if nk not in seen_keys or match_idx not in seen_keys[nk]:
                    seen_keys.setdefault(nk, []).append(match_idx)
                for pid in _show_id_set(s):
                    id_seen.setdefault(pid, match_idx)
                if grp is not None:
                    group_seen.setdefault(grp, match_idx)
                continue

            cfg = ShowConfig(
                rating_key=s.rating_key,
                title=s.title,
                thumb=s.thumb,
                plex_rating_key=s.rating_key if tb == "plex" else None,
                jellyfin_rating_key=s.rating_key if tb == "jellyfin" else None,
                emby_rating_key=s.rating_key if tb == "emby" else None,
                thumb_backend=tb if s.thumb else None,
            )
            new_idx = len(out)
            seen_keys.setdefault(nk, []).append(new_idx)
            _years[new_idx] = s.year
            for pid in _show_id_set(s):
                id_seen.setdefault(pid, new_idx)
            if grp is not None:
                group_seen.setdefault(grp, new_idx)
            out.append(cfg)

    if len(target_backends) > 1:
        _enrich_configs_with_matches(out, target_backends)
    out.sort(key=lambda c: (c.title or "").lower())
    return out


def _resolve_genre_shows(
    genres: list[str],
    target_backends: list[str],
) -> list[ShowConfig]:
    if not genres:
        return []
    per_backend: dict[str, list] = {}
    for tb in target_backends:
        try:
            per_backend[tb] = _client_for(tb).list_shows_by_genres(genres)
        except Exception:
            log.exception("genre resolve failed on %s", tb)
            per_backend[tb] = []
    return _dedup_show_summaries_to_configs(per_backend, target_backends)


def _apply_rules(shows: list[ShowSummary], rules: list[dict]) -> list[ShowSummary]:
    result = list(shows)
    for rule in rules:
        rt = rule["rule_type"]
        op = rule["operator"]
        val = rule["value"]

        if rt == "genre":
            continue
        elif rt == "year_min":
            try:
                y = int(val)
                if op == "include":
                    result = [s for s in result if s.year is None or s.year >= y]
            except ValueError:
                pass
        elif rt == "year_max":
            try:
                y = int(val)
                if op == "include":
                    result = [s for s in result if s.year is None or s.year <= y]
            except ValueError:
                pass
        elif rt == "status":
            v_lower = val.lower()
            if op == "include":
                result = [s for s in result
                          if s.status is None or s.status.lower() == v_lower]
            elif op == "exclude":
                result = [s for s in result
                          if s.status is None or s.status.lower() != v_lower]
        elif rt == "content_rating":
            v_lower = val.lower()
            if op == "include":
                result = [s for s in result
                          if s.content_rating is None or s.content_rating.lower() == v_lower]
            elif op == "exclude":
                result = [s for s in result
                          if s.content_rating is None or s.content_rating.lower() != v_lower]
        elif rt == "season_max":
            try:
                n = int(val)
                if op == "include":
                    result = [s for s in result
                              if s.season_count is None or s.season_count <= n]
            except ValueError:
                pass
        elif rt == "season_min":
            try:
                n = int(val)
                if op == "include":
                    result = [s for s in result
                              if s.season_count is None or s.season_count >= n]
            except ValueError:
                pass
        elif rt == "rating_min":
            try:
                r = float(val)
                if op == "include":
                    result = [s for s in result
                              if s.community_rating is None or s.community_rating >= r]
            except ValueError:
                pass

    return result


def _resolve_smart_shows(
    rules: list[dict],
    target_backends: list[str],
) -> list[ShowConfig]:
    genre_includes = [r["value"] for r in rules
                      if r["rule_type"] == "genre" and r["operator"] == "include"]
    non_genre_rules = [r for r in rules if r["rule_type"] != "genre"]

    per_backend: dict[str, list] = {}
    for tb in target_backends:
        try:
            client = _client_for(tb)
            if genre_includes:
                shows = client.list_shows_by_genres(genre_includes)
            else:
                shows = client.list_all_shows()
            shows = _apply_rules(shows, non_genre_rules)
            per_backend[tb] = shows
        except Exception:
            log.exception("smart rule query failed on %s", tb)
            per_backend[tb] = []

    return _dedup_show_summaries_to_configs(per_backend, target_backends)


def create_genre_playlist(
    name: str,
    genres: list[str],
    *,
    sort_mode: str = "rotation",
    unwatched_only: bool = False,
    auto_sync: bool = True,
    backend: str = "plex",
    block_size: int = 1,
    shuffle_seed: int | None = None,
    weights: dict[str, int] | None = None,
    pruning_enabled: int = 1,
) -> int:
    """Create a playlist whose member shows are determined by a genre query
    rather than hand-picked. Future syncs re-query the backend and auto-add
    new shows matching the genre."""
    cleaned_genres = [g.strip() for g in genres if g and g.strip()]
    if not cleaned_genres:
        raise ValueError("Need at least one genre to create a genre playlist")
    db._validate_backend(backend)

    target_backends = parse_backend_set(backend)
    configs = _resolve_genre_shows(cleaned_genres, target_backends)
    # Apply per-show weights supplied from the creation form.
    if weights:
        for cfg in configs:
            if cfg.rating_key in weights:
                cfg.weight = max(1, weights[cfg.rating_key])
    if not configs:
        raise ValueError(
            f"No shows on the configured backend(s) match genres: {', '.join(cleaned_genres)}"
        )

    # Delegate to the manual creator. It already handles per-backend playlist
    # creation, rollback on partial failure, DB insert.
    playlist_id = create_managed_playlist(
        name, configs,
        sort_mode=sort_mode,
        unwatched_only=unwatched_only,
        auto_sync=auto_sync,
        backend=backend,
        block_size=block_size,
        shuffle_seed=shuffle_seed,
        pruning_enabled=pruning_enabled,
    )
    # Mark the playlist as genre type + persist the filter.
    with db.connection() as conn:
        conn.execute(
            "UPDATE managed_playlists SET playlist_type = 'genre', genre_filter = ? WHERE id = ?",
            (",".join(cleaned_genres), playlist_id),
        )
    log.info(
        "Created genre playlist '%s' (backend=%s, genres=%s, %d shows)",
        name, backend, cleaned_genres, len(configs),
    )
    try:
        _row = db.get_playlist(playlist_id)
        if _row:
            _webhooks.fire("playlist.created", playlist=_webhooks._playlist_info(dict(_row)))
    except Exception:
        pass
    return playlist_id


def set_show_excluded(playlist_id: int, show_rating_key: str, excluded: bool) -> None:
    """Soft-delete or re-include a single show. Rebuilds tails on every
    enabled backend after the change.

    Excluded shows stay in the DB so the next genre sync doesn't re-add
    them — the user explicitly removed them once."""
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError(f"No managed playlist with id={playlist_id}")
    db.set_show_excluded(playlist_id, show_rating_key, excluded)
    full_configs = [_config_from_row(r) for r in db.list_shows(playlist_id)]
    _rebuild_playlist_tails(row, full_configs, op_label="exclude/re-include")
    log.info(
        "Playlist '%s' show %s excluded=%s",
        row["name"], show_rating_key, excluded,
    )


# --------------------------------------------------------------------------- #
# Sync (with heal-on-sync for missing IDs in 'both' mode)
# --------------------------------------------------------------------------- #


def _heal_missing_ids(playlist_id: int, row, full_configs: list[ShowConfig]) -> None:
    """For 'both' playlists, re-attempt cross-backend matching for any show
    missing an id on one side. Persists newly-discovered ids so subsequent
    syncs include the show on that backend too.

    Caches the candidate list per backend (one list_all_shows call max per
    backend per sync run, regardless of how many shows need healing).
    """
    if row["backend"] != "both":
        backends_list = parse_backend_set(row["backend"])
        if len(backends_list) <= 1:
            return
    needs_plex = [c for c in full_configs if c.plex_rating_key is None]
    needs_jf = [c for c in full_configs if c.jellyfin_rating_key is None]
    needs_emby = [c for c in full_configs if c.emby_rating_key is None]
    if not needs_plex and not needs_jf and not needs_emby:
        return

    plex_cands = _candidates_for("plex") if needs_plex else []
    jf_cands = _candidates_for("jellyfin") if needs_jf else []
    emby_cands = _candidates_for("emby") if needs_emby else []

    # One ShowSummary lookup per config, reused across the three backends.
    _summ: dict[int, object] = {}
    def ids_year(cfg):
        if id(cfg) not in _summ:
            _summ[id(cfg)] = _summary_for_cfg(cfg)
        s = _summ[id(cfg)]
        return _show_id_set(s), (s.year if s else None)

    for cfg in needs_plex:
        want_ids, year = ids_year(cfg)
        matched = (_find_match_by_ids(plex_cands, want_ids)
                   or _find_match(plex_cands, cfg.title or "", year))
        if matched:
            cfg.plex_rating_key = matched
            db.set_plex_show_item_id(playlist_id, cfg.rating_key, matched)
            log.info("Healed Plex id for '%s' on playlist %s: %s",
                     cfg.title, playlist_id, matched)
    for cfg in needs_jf:
        want_ids, year = ids_year(cfg)
        matched = (_find_match_by_ids(jf_cands, want_ids)
                   or _find_match(jf_cands, cfg.title or "", year))
        if matched:
            cfg.jellyfin_rating_key = matched
            db.set_jellyfin_show_item_id(playlist_id, cfg.rating_key, matched)
            log.info("Healed Jellyfin id for '%s' on playlist %s: %s",
                     cfg.title, playlist_id, matched)
    for cfg in needs_emby:
        want_ids, year = ids_year(cfg)
        matched = (_find_match_by_ids(emby_cands, want_ids)
                   or _find_match(emby_cands, cfg.title or "", year))
        if matched:
            cfg.emby_rating_key = matched
            db.set_emby_show_item_id(playlist_id, cfg.rating_key, matched)
            log.info("Healed Emby id for '%s' on playlist %s: %s",
                     cfg.title, playlist_id, matched)


def sync_playlist(playlist_id: int, force: bool = False) -> tuple[int, int]:
    """Re-evaluate canonical episode lists against current backend metadata
    on every enabled backend. Returns (added, removed) — totals across
    backends.

    `force=True` overrides the per-playlist auto_sync opt-out. Use it for
    user-initiated "Sync Now" actions; the scheduler always uses force=False.
    """
    row = db.get_playlist(playlist_id)
    if not row:
        return (0, 0)
    if not force and not bool(row["auto_sync"]):
        return (0, 0)

    show_rows = db.list_shows(playlist_id)
    full_configs = [_config_from_row(r) for r in show_rows]

    # v1.4.0: genre playlists discover new shows on every sync.
    if _row_get(row, "playlist_type", "manual") == "genre":
        _genre_sync_discover(playlist_id, row, full_configs)
        # Reload after potential adds.
        show_rows = db.list_shows(playlist_id)
        full_configs = [_config_from_row(r) for r in show_rows]

    # v2.2.0: franchise playlists have their own sync logic.
    if _row_get(row, "playlist_type", "manual") == "franchise":
        return sync_franchise_playlist(playlist_id, force=force)

    if not full_configs:
        return (0, 0)

    _heal_missing_ids(playlist_id, row, full_configs)

    added, removed, reordered = _rebuild_playlist_tails(row, full_configs, op_label="sync")
    if reordered and not added and not removed:
        removed = -1  # sentinel: pure reorder, no membership change
    if added or removed:
        log.info("Synced '%s': +%d, -%d", row["name"], added, max(removed, 0))
        try:
            _webhooks.fire(
                "playlist.synced",
                playlist=_webhooks._playlist_info(dict(row)),
                data={"added": added, "removed": max(removed, 0)},
            )
        except Exception:
            pass

    # v2.0.0: collect stats from the first available backend side.
    for _be, _cl, _pl_id in _clients_for_playlist(row):
        if not _pl_id:
            continue
        stats = _compute_playlist_stats(playlist_id, _be, _cl, _pl_id, full_configs)
        if stats:
            db.update_playlist_stats(playlist_id, stats)
        break

    return (added, removed)


def refresh_playlist_metadata(playlist_id: int) -> dict[str, int]:
    """Trigger metadata refresh on every backend for every non-excluded show
    in the playlist. Returns {"ok": N, "errors": M} — always returns, never
    raises."""
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError(f"No playlist with id={playlist_id}")
    ok = 0
    errors = 0
    for backend, client, _pl_id in _clients_for_playlist(row):
        for show_row in db.list_shows(playlist_id):
            if show_row["is_excluded"]:
                continue
            cfg = _config_from_row(show_row)
            key = cfg.id_for(backend)
            if not key:
                continue
            try:
                client.refresh_show_metadata(key)
                ok += 1
            except Exception:
                log.warning("refresh failed for show %s on %s", key, backend)
                errors += 1
    return {"ok": ok, "errors": errors}


_SHOW_ID_COL = {
    "plex": "plex_show_item_id",
    "jellyfin": "jellyfin_show_item_id",
    "emby": "emby_show_item_id",
}


def link_show_backend(
    playlist_id: int,
    show_rating_key: str,
    backend: str,
    target_key: str,
) -> None:
    """Manually link a show's ID on one backend to the existing playlist row.

    Also MERGES duplicates: a manual link usually resolves a cross-backend
    title mismatch where genre/manual discovery created two rows for the SAME
    show (no shared provider id, different titles — e.g. a show Emby names
    differently). If another row in this playlist already carries `target_key`
    on `backend`, it's that same show, so we fold its remaining backend ids onto
    the linked row and drop the now-redundant duplicate row — leaving one entry.
    Triggers a tail rebuild on every enabled backend."""
    setters = {
        "plex": db.set_plex_show_item_id,
        "jellyfin": db.set_jellyfin_show_item_id,
        "emby": db.set_emby_show_item_id,
    }
    if backend not in setters:
        raise ValueError(f"Unknown backend: {backend!r}")
    setters[backend](playlist_id, show_rating_key, target_key)

    # Merge any other row that already owns this backend id (same show).
    survivor = dict(db.get_show(playlist_id, show_rating_key) or {})
    for r in db.list_shows(playlist_id):
        rd = dict(r)
        if rd.get("show_rating_key") == show_rating_key:
            continue
        if rd.get(_SHOW_ID_COL[backend]) != target_key:
            continue
        # Union the duplicate's other-backend ids onto the surviving row so no
        # backend linkage is lost, then delete the duplicate DB row (DB-only —
        # the rebuild below keeps its episodes, now owned by the survivor).
        for be2, col2 in _SHOW_ID_COL.items():
            if not survivor.get(col2) and rd.get(col2):
                setters[be2](playlist_id, show_rating_key, rd[col2])
                survivor[col2] = rd[col2]
        db.remove_show(playlist_id, rd["show_rating_key"])
        log.info(
            "Merged duplicate show '%s' into '%s' on manual %s link (id %s)",
            rd.get("show_title"), survivor.get("show_title"), backend, target_key,
        )

    # Persist a GLOBAL "same show" link from the survivor's backend ids, so the
    # genre/picker/sync matching layer treats them as one everywhere — not just
    # in this playlist. Needs ≥2 backend ids to be meaningful.
    link_pairs = [
        (be2, survivor[col2])
        for be2, col2 in _SHOW_ID_COL.items()
        if survivor.get(col2)
    ]
    if len(link_pairs) >= 2:
        try:
            db.link_shows_same(link_pairs, label=survivor.get("show_title"))
        except Exception:
            log.warning("failed to persist manual show link", exc_info=True)

    row = db.get_playlist(playlist_id)
    full_configs = [_config_from_row(r) for r in db.list_shows(playlist_id)]
    _rebuild_playlist_tails(row, full_configs, op_label="manual link")


def _genre_sync_discover(playlist_id: int, row, current_configs: list[ShowConfig]) -> None:
    rule_mode = _row_get(row, "rule_mode", "genre")
    target_backends = _backends_for(row)

    if rule_mode == "rules":
        rules = [dict(r) for r in db.list_rules(playlist_id)]
        if not rules:
            return
        try:
            candidates = _resolve_smart_shows(rules, target_backends)
        except Exception:
            log.exception("smart rule discovery failed for playlist %s", row["name"])
            return
    else:
        genres = _parse_genre_csv(_row_get(row, "genre_filter", None))
        if not genres:
            return
        try:
            candidates = _resolve_genre_shows(genres, target_backends)
        except Exception:
            log.exception("genre discovery failed for playlist %s", row["name"])
            return

    existing_plex = {c.plex_rating_key for c in current_configs if c.plex_rating_key}
    existing_jf = {c.jellyfin_rating_key for c in current_configs if c.jellyfin_rating_key}
    existing_emby = {c.emby_rating_key for c in current_configs if c.emby_rating_key}
    existing_pks = {c.rating_key for c in current_configs}

    new_configs: list[ShowConfig] = []
    for cand in candidates:
        if cand.rating_key in existing_pks:
            continue
        if cand.plex_rating_key and cand.plex_rating_key in existing_plex:
            continue
        if cand.jellyfin_rating_key and cand.jellyfin_rating_key in existing_jf:
            continue
        if cand.emby_rating_key and cand.emby_rating_key in existing_emby:
            continue
        new_configs.append(cand)

    if not new_configs:
        return
    db.add_shows(playlist_id, [_config_to_db_dict(c) for c in new_configs])
    log.info(
        "Sync added %d new show(s) to '%s'",
        len(new_configs), row["name"],
    )


# ── v2.2.0 — Franchise playlists ──────────────────────────────────────────


def _load_prebaked_franchises() -> list[dict]:
    path = os.path.join(os.path.dirname(__file__), "defaults", "franchises.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _normalize_for_match(title: str) -> str:
    import re as _re
    t = title.lower()
    t = _re.sub(r"[^\w\s]", " ", t)
    t = _re.sub(r"\s+", " ", t).strip()
    return t


def _normalize_cl_key(cl_id: str) -> str:
    return cl_id.replace('-', '_')


def _int_or_none(value) -> int | None:
    """int(value), or None when it isn't cleanly numeric. Provider ids can
    carry junk (e.g. an IMDb 'tt…' id in the TVDB slot — issue #16); a bad id
    on ONE show must degrade that show to title+year matching, never abort the
    whole library cache build."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _build_backend_cache(backend: str, client: MediaClient) -> dict:
    all_movies = client.list_all_movies()
    all_shows = client.list_all_shows()
    # Diagnostic: franchise lists are TMDB-keyed, so a library scraped with a
    # TVDB-only agent (no TMDB ids) will fail TMDB matching and fall back to
    # fuzzy title+year. These counts make the cause obvious in the logs:
    #   "0 movies"            -> the resolved user can't see the movie libraries
    #   "N movies (0 w/ tmdb)"-> movies present but no TMDB ids (TVDB-scraped)
    log.info(
        "Franchise lib cache [%s]: %d movies (%d tmdb, %d imdb), "
        "%d shows (%d tvdb, %d tmdb, %d imdb)",
        backend,
        len(all_movies), sum(1 for m in all_movies if m.tmdb_id),
        sum(1 for m in all_movies if m.imdb_id),
        len(all_shows),
        sum(1 for s in all_shows if s.tvdb_id),
        sum(1 for s in all_shows if s.tmdb_id),
        sum(1 for s in all_shows if s.imdb_id),
    )
    return {
        "client": client,
        "movie_by_tmdb": {m.tmdb_id: m for m in all_movies if m.tmdb_id},
        "movie_by_imdb": {m.imdb_id: m for m in all_movies if m.imdb_id},
        "movie_by_title_year": {
            (_normalize_for_match(m.title), m.year): m for m in all_movies
        },
        "show_by_tvdb": {
            k: s for s in all_shows
            if s.tvdb_id and (k := _int_or_none(s.tvdb_id)) is not None
        },
        "show_by_tmdb": {
            k: s for s in all_shows
            if s.tmdb_id and (k := _int_or_none(s.tmdb_id)) is not None
        },
        "show_by_imdb": {s.imdb_id: s for s in all_shows if s.imdb_id},
        "show_by_title_year": {
            (_normalize_for_match(s.title), s.year): s for s in all_shows
        },
        "episode_cache": {},
    }


# TMDB external-id resolution, cached per process. Franchise data is TMDB-keyed;
# this bridges it to libraries scraped with a TVDB/IMDB-only agent (no TMDB ids).
_EXTERNAL_ID_CACHE: dict[tuple[str, int], dict] = {}


def _franchise_external_ids(kind: str, tmdb_id) -> dict:
    """{'imdb_id', 'tvdb_id'} for a franchise item's TMDB id, via TMDB
    external_ids (cached). Returns {} if TMDB is unconfigured or the lookup
    fails — callers then fall through to title+year matching."""
    try:
        tmdb_id = int(tmdb_id)
    except (TypeError, ValueError):
        return {}
    key = (kind, tmdb_id)
    if key in _EXTERNAL_ID_CACHE:
        return _EXTERNAL_ID_CACHE[key]
    out: dict = {}
    try:
        import tmdb_client
        out = tmdb_client.external_ids(kind, tmdb_id) or {}
    except Exception:
        out = {}
    _EXTERNAL_ID_CACHE[key] = out
    return out


def _franchise_poster_urls(franchise_items: list[dict], limit: int = 5) -> list[str]:
    """Up to `limit` distinct TMDB poster URLs (w500) for a franchise, taken
    from its first resolvable items in order. Powers the home-page card strip
    (1→full … 5→fifths). Best-effort: returns [] if TMDB is unavailable or no
    item yields a poster. De-duplicates (a franchise of one show's episodes
    would otherwise repeat the same show poster)."""
    try:
        import tmdb_client
    except Exception:
        return []
    out: list[str] = []
    seen: set[str] = set()
    # Scan a generous window so we can find up to `limit` *distinct* posters
    # even when early items repeat (e.g. many episodes of one show).
    for fi in (franchise_items or [])[:60]:
        if len(out) >= limit:
            break
        try:
            poster = None
            if fi.get("item_type") == "movie" and fi.get("tmdb_id"):
                poster = (tmdb_client.get_movie(int(fi["tmdb_id"])) or {}).get("poster")
            elif fi.get("show_tmdb_id"):
                poster = (tmdb_client.get_tv(int(fi["show_tmdb_id"])) or {}).get("poster")
            if poster:
                url = poster.replace("/t/p/w92/", "/t/p/w500/")
                if url not in seen:
                    seen.add(url)
                    out.append(url)
        except Exception:
            continue
    return out


def _franchise_poster_url(franchise_items: list[dict]) -> str | None:
    """A single representative TMDB poster URL (w500) for a franchise — the
    first resolvable item. Best-effort: None if unavailable."""
    urls = _franchise_poster_urls(franchise_items, limit=1)
    return urls[0] if urls else None


_STATIC_FRANCHISE_POSTERS: dict[str, str] | None = None


def _static_franchise_poster(key: str | None) -> str | None:
    """Representative poster for a bundled franchise, by registry key, read
    from defaults/franchises.json (cached). Lets index cards show art for
    franchise definitions stored before poster_url existed — no DB backfill or
    runtime TMDB calls needed."""
    global _STATIC_FRANCHISE_POSTERS
    if not key:
        return None
    if _STATIC_FRANCHISE_POSTERS is None:
        try:
            path = os.path.join(os.path.dirname(__file__), "defaults", "franchises.json")
            with open(path) as f:
                _STATIC_FRANCHISE_POSTERS = {
                    e["key"]: e.get("poster") for e in json.load(f) if e.get("poster")
                }
        except Exception:
            _STATIC_FRANCHISE_POSTERS = {}
    return _STATIC_FRANCHISE_POSTERS.get(key)


def backfill_franchise_posters() -> int:
    """One-shot: persist a poster_url on franchise definitions that lack one.

    Franchise playlists created before v3.2.1 (especially user-built / forked
    ones) never stored a poster_url, so their home-page card is blank. For each
    franchise playlist whose definition has no poster_url, resolve one — TMDB
    poster from the items, else the bundled static poster by key / origin key —
    and persist it. Best-effort; never raises. Returns the count backfilled."""
    filled = 0
    seen: set[int] = set()
    try:
        rows = db.list_playlists()
    except Exception:
        return 0
    for row in rows:
        try:
            if _row_get(row, "playlist_type", "manual") != "franchise":
                continue
            defn_id = dict(row).get("franchise_definition_id")
            if not defn_id or defn_id in seen:
                continue
            seen.add(defn_id)
            defn = db.get_franchise_definition_by_id(defn_id)
            if not defn:
                continue
            # Backfill when the strip list is missing or frozen at 1 poster
            # on a multi-item definition (a TMDB blip during boot can freeze
            # the strip permanently under the old gate, which skipped any
            # non-empty poster_urls).
            raw = (defn.get("poster_urls") or "").strip()
            existing_count = 0
            if raw:
                try:
                    parsed = json.loads(raw)
                    existing_count = len(parsed) if isinstance(parsed, list) else 0
                except Exception:
                    existing_count = 0
            # Done when we already have a real strip (2+), or the franchise only
            # has a single item so 1 poster IS the full strip.
            if existing_count >= 2 or (existing_count >= 1 and (defn.get("item_count") or 0) <= 1):
                continue
            poster_list = _franchise_poster_urls(db.list_franchise_items(defn_id), limit=5)
            poster_url = (
                (poster_list[0] if poster_list else None)
                or defn.get("poster_url")
                or _static_franchise_poster(defn.get("key"))
                or _static_franchise_poster(defn.get("forked_from_key"))
            )
            if not poster_list and poster_url:
                poster_list = [poster_url]
            if poster_list and len(poster_list) > existing_count:
                db.set_franchise_definition_poster(
                    defn_id, poster_url, json.dumps(poster_list)
                )
                filled += 1
                log.info(
                    "Backfilled %d franchise poster(s) for definition %s",
                    len(poster_list), defn_id,
                )
        except Exception:
            log.warning("Franchise poster backfill failed for a definition", exc_info=True)
    return filled


def _apply_franchise_poster(row) -> None:
    """Best-effort: set a deterministic TMDB cover on each backend playlist for
    this franchise row. Fire-and-forget. Shared by create / Maker-save / sync
    so franchise playlists (including pre-existing and Maker-built ones) get a
    real poster rather than the media server's inconsistent auto-composite."""
    try:
        defn_id = dict(row).get("franchise_definition_id")
        if not defn_id:
            return
        poster_url = _franchise_poster_url(db.list_franchise_items(defn_id))
        if not poster_url:
            return
        for _tb, client, pl_id in _clients_for_playlist(row):
            if pl_id:
                client.set_playlist_image(pl_id, poster_url)
    except Exception:
        log.warning("Failed to apply franchise poster", exc_info=True)


def _fetch_and_store_franchise(
    key: str,
    name: str,
    source: str = "trakt",
    trakt_user: str | None = None,
    trakt_slug: str | None = None,
    chronolists_id: str | None = None,
) -> int:
    if source == "local":
        return _fetch_and_store_franchise_local(key, name)

    if source == "chronolists":
        if not chronolists_id:
            raise ValueError("chronolists_id is required for source='chronolists'")
        return _fetch_and_store_franchise_chronolists(key, name, chronolists_id)

    if source == "user":
        defn = db.get_franchise_definition(key)
        if defn:
            return defn["id"]
        raise ValueError(f"User franchise '{key}' not found in DB")

    if source != "trakt":
        raise ValueError(f"Unknown franchise source: {source}")

    if not trakt_user or not trakt_slug:
        raise ValueError("trakt_user and trakt_slug are required for source='trakt'")

    from datetime import datetime, timezone

    trakt = get_trakt_client()
    items = trakt.fetch_list_items(trakt_user, trakt_slug)
    new_hash = trakt.content_hash(items)

    existing = db.get_franchise_definition(key)
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    definition_id = db.upsert_franchise_definition(
        key=key,
        name=name,
        source="trakt",
        trakt_user=trakt_user,
        trakt_slug=trakt_slug,
        fetched_at=fetched_at,
        content_hash=new_hash,
        item_count=len(items),
    )

    if existing is None or existing.get("content_hash") != new_hash:
        db.replace_franchise_items(definition_id, items)
        log.info("Franchise '%s': stored %d items (hash changed)", name, len(items))
    else:
        log.debug("Franchise '%s': no changes (hash match)", name)

    return definition_id


def _fetch_and_store_franchise_local(key: str, name: str) -> int:
    from datetime import datetime, timezone

    path = os.path.join(
        os.path.dirname(__file__), "defaults", "franchise_data", f"{key}.json"
    )
    try:
        with open(path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise FileNotFoundError(f"Local franchise file not found: {path}")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in franchise file {path}: {e}")

    items = raw.get("items", [])
    if not items:
        raise ValueError(f"Franchise file {path} has no 'items' array")

    normalised = []
    for i, item in enumerate(items):
        item = dict(item)
        item.setdefault("rank", i + 1)
        item.setdefault("tmdb_id", None)
        item.setdefault("tvdb_id", None)
        item.setdefault("imdb_id", None)
        item.setdefault("year", None)
        item.setdefault("season_number", None)
        item.setdefault("episode_number", None)
        item.setdefault("show_title", None)
        item.setdefault("show_tvdb_id", None)
        item.setdefault("show_tmdb_id", None)
        normalised.append(item)

    new_hash = get_trakt_client().content_hash(normalised)
    existing = db.get_franchise_definition(key)
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    definition_id = db.upsert_franchise_definition(
        key=key,
        name=name,
        source="local",
        trakt_user=None,
        trakt_slug=None,
        fetched_at=fetched_at,
        content_hash=new_hash,
        item_count=len(normalised),
    )

    if existing is None or existing.get("content_hash") != new_hash:
        db.replace_franchise_items(definition_id, normalised)
        log.info("Franchise '%s' (local): stored %d items", name, len(normalised))
    else:
        log.debug("Franchise '%s' (local): no changes", name)

    return definition_id


def _fetch_and_store_franchise_chronolists(
    key: str, name: str, chronolists_id: str, known_hash: str | None = None,
    auto_discovered: bool = False,
) -> int:
    from datetime import datetime, timezone
    if not chronolists_id:
        raise ValueError("chronolists_id is required for source='chronolists'")
    from chronolists_client import get_chronolists_client, parse_chronolists_items
    cl = get_chronolists_client()
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    existing = db.get_franchise_definition(key)

    target_hash = known_hash if known_hash is not None else cl.list_hash(chronolists_id)

    if existing and target_hash and existing.get("content_hash") == target_hash:
        return db.upsert_franchise_definition(
            key=key, name=name, source="chronolists",
            trakt_user=None, trakt_slug=None, chronolists_id=chronolists_id,
            fetched_at=fetched_at, content_hash=target_hash,
            item_count=existing.get("item_count", 0),
            auto_discovered=int(auto_discovered))

    payload = cl.fetch_list(chronolists_id)
    items = parse_chronolists_items(payload.get("items", []))
    new_hash = (payload.get("metadata") or {}).get("hash") or target_hash or ""
    # Resolve a representative TMDB poster so the franchise's picker card gets
    # frosted-glass art — including auto-discovered Chronolists lists, which
    # flow through here too.
    poster_list = _franchise_poster_urls(items, limit=5)
    poster_url = poster_list[0] if poster_list else None
    definition_id = db.upsert_franchise_definition(
        key=key, name=name, source="chronolists",
        trakt_user=None, trakt_slug=None, chronolists_id=chronolists_id,
        fetched_at=fetched_at, content_hash=new_hash, item_count=len(items),
        auto_discovered=int(auto_discovered), poster_url=poster_url,
        poster_urls=json.dumps(poster_list) if poster_list else None)
    if existing is None or existing.get("content_hash") != new_hash:
        db.replace_franchise_items(definition_id, items)
        log.info("Franchise '%s' (chronolists): stored %d items", name, len(items))
    return definition_id


def _resolve_show_for_item(fi: dict, cache: dict):
    """Resolve a show from a franchise item dict using TVDB, TMDB, or title+year.

    Uses the backend cache's indexes in priority order: TVDB first (Trakt lists),
    then TMDB (Chronolists), then normalized title+year fallback.
    """
    show_tvdb = fi.get("show_tvdb_id") or fi.get("tvdb_id")
    if show_tvdb:
        s = cache["show_by_tvdb"].get(int(show_tvdb))
        if s:
            return s
    show_tmdb = fi.get("show_tmdb_id")
    if show_tmdb:
        s = cache["show_by_tmdb"].get(int(show_tmdb))
        if s:
            return s
        # Bridge TMDB-keyed franchise data to a TVDB/IMDB-scraped library:
        # resolve the show's TMDB id to its TVDB/IMDB id via TMDB and retry.
        ext = _franchise_external_ids("tv", show_tmdb)
        ext_tvdb = ext.get("tvdb_id")
        if ext_tvdb and cache["show_by_tvdb"].get(int(ext_tvdb)):
            return cache["show_by_tvdb"][int(ext_tvdb)]
        ext_imdb = ext.get("imdb_id")
        if ext_imdb and cache.get("show_by_imdb", {}).get(ext_imdb):
            return cache["show_by_imdb"][ext_imdb]
    key = (_normalize_for_match(fi.get("show_title") or fi.get("title") or ""), fi.get("year"))
    return cache["show_by_title_year"].get(key)


def _resolve_franchise_item(fi: dict, cache: dict) -> str | None:
    item_type = fi["item_type"]
    client: MediaClient = cache["client"]

    if item_type == "movie":
        if fi.get("tmdb_id"):
            m = cache["movie_by_tmdb"].get(fi["tmdb_id"])
            if m:
                return m.rating_key
            # Bridge to an IMDB-scraped library (no TMDB ids): resolve the
            # franchise movie's TMDB id to its IMDB id via TMDB and retry.
            ext_imdb = _franchise_external_ids("movie", fi["tmdb_id"]).get("imdb_id")
            if ext_imdb and cache.get("movie_by_imdb", {}).get(ext_imdb):
                return cache["movie_by_imdb"][ext_imdb].rating_key
        key = (_normalize_for_match(fi["title"]), fi.get("year"))
        m = cache["movie_by_title_year"].get(key)
        return m.rating_key if m else None

    elif item_type == "episode":
        show = _resolve_show_for_item(fi, cache)
        if not show:
            return None
        ep_cache = cache["episode_cache"]
        if show.rating_key not in ep_cache:
            try:
                eps = client.episodes_for_show(show.rating_key, include_specials=True)
                ep_cache[show.rating_key] = {(e.season, e.episode): e for e in eps}
            except Exception:
                ep_cache[show.rating_key] = {}
        ep = ep_cache[show.rating_key].get((fi["season_number"], fi["episode_number"]))
        return ep.rating_key if ep else None

    elif item_type in ("show", "season"):
        return None

    return None


def _expand_franchise_show_item(fi: dict, cache: dict) -> list[str]:
    client: MediaClient = cache["client"]
    show = _resolve_show_for_item(fi, cache)
    if not show:
        return []

    try:
        if fi["item_type"] == "show":
            eps = client.episodes_for_show(show.rating_key)
        else:
            season_num = fi["season_number"]
            eps = client.episodes_for_show(
                show.rating_key,
                start_season=season_num,
                end_season=season_num,
            )
        return [e.rating_key for e in eps]
    except Exception:
        log.warning("Failed to expand show/season item '%s'", fi.get("title"), exc_info=True)
        return []


def _match_franchise_to_library(
    definition_id: int,
    playlist_id: int,
    row: dict,
) -> tuple[list[str], list[str], list[str], int]:
    franchise_items = db.list_franchise_items(definition_id)
    if not franchise_items:
        return [], [], [], 0

    backend_caches: dict[str, dict] = {}
    for backend, client_q, _pl_id in _clients_for_playlist(row):
        try:
            backend_caches[backend] = _build_backend_cache(backend, client_q)
        except Exception:
            log.warning("Failed to build library cache for backend=%s", backend, exc_info=True)

    plex_keys: list[str] = []
    jellyfin_keys: list[str] = []
    emby_keys: list[str] = []
    missing_count = 0

    for fi in franchise_items:
        plex_found = False
        plex_item_id: str | None = None
        jellyfin_found = False
        jellyfin_item_id: str | None = None
        emby_found = False
        emby_item_id: str | None = None

        item_type = fi["item_type"]
        if item_type in ("show", "season"):
            for backend, cache in backend_caches.items():
                keys = _expand_franchise_show_item(fi, cache)
                if keys:
                    if backend == "plex":
                        plex_found = True
                        plex_item_id = keys[0]
                        plex_keys.extend(keys)
                    elif backend == "jellyfin":
                        jellyfin_found = True
                        jellyfin_item_id = keys[0]
                        jellyfin_keys.extend(keys)
                    else:
                        emby_found = True
                        emby_item_id = keys[0]
                        emby_keys.extend(keys)

            db.upsert_franchise_match_state(
                franchise_item_id=fi["id"],
                playlist_id=playlist_id,
                plex_found=plex_found,
                plex_item_id=plex_item_id,
                jellyfin_found=jellyfin_found,
                jellyfin_item_id=jellyfin_item_id,
                emby_found=emby_found,
                emby_item_id=emby_item_id,
            )

            if not plex_found and not jellyfin_found and not emby_found:
                missing_count += 1
        else:
            for backend, cache in backend_caches.items():
                found_key = _resolve_franchise_item(fi, cache)
                if found_key:
                    if backend == "plex":
                        plex_found = True
                        plex_item_id = found_key
                        plex_keys.append(found_key)
                    elif backend == "jellyfin":
                        jellyfin_found = True
                        jellyfin_item_id = found_key
                        jellyfin_keys.append(found_key)
                    else:
                        emby_found = True
                        emby_item_id = found_key
                        emby_keys.append(found_key)

            db.upsert_franchise_match_state(
                franchise_item_id=fi["id"],
                playlist_id=playlist_id,
                plex_found=plex_found,
                plex_item_id=plex_item_id,
                jellyfin_found=jellyfin_found,
                jellyfin_item_id=jellyfin_item_id,
                emby_found=emby_found,
                emby_item_id=emby_item_id,
            )

            if not plex_found and not jellyfin_found and not emby_found:
                missing_count += 1

    return plex_keys, jellyfin_keys, emby_keys, missing_count


def _apply_franchise_prune(
    be_keys: list[str], view_counts: dict[str, int], keep_last_n: int
) -> list[str]:
    """Drop watched-beyond-buffer keys from an ordered franchise key list.

    `be_keys` is the full desired order; `view_counts` is library watch state
    (missing key => unwatched). Keeps all unwatched + the last keep_last_n
    watched; removes older watched. Pure + idempotent (re-running on the result
    is a no-op because removed keys were the watched-oldest)."""
    if not be_keys:
        return be_keys
    counts = [int(view_counts.get(k, 0) or 0) for k in be_keys]
    drop = set(rotation.prune_indices_for_counts(counts, keep_last_n))
    return [k for i, k in enumerate(be_keys) if i not in drop]


def _franchise_prune_keys(
    current: list, be_keys: list[str], playlist_id: int, backend: str,
    keep_last_n: int,
) -> tuple[list[str], set[str]]:
    """Detect watched-beyond-buffer items in the LIVE playlist, persist them to
    the pruned set, and return (be_keys minus the whole pruned set, pruned set).

    Watch state comes from `current` (PlaylistItems from get_playlist_items,
    which reports view_count correctly on every backend — unlike Emby's bulk
    get_view_counts). Persisting means a removed-watched item stays removed even
    though franchise sync rebuilds `be_keys` from the static definition.
    Idempotent: re-running with no new watching is a no-op."""
    newly = [
        current[i].rating_key
        for i in rotation.prune_indices(current, keep_last_n)
    ]
    if newly:
        db.add_pruned_items(playlist_id, backend, newly)
    pruned = db.get_pruned_item_ids(playlist_id, backend)
    return [k for k in be_keys if k not in pruned], pruned


def _build_backend_ordered_keys(
    backend: str,
    client: MediaClient,
    franchise_items: list[dict],
    match_state: dict[int, dict],
) -> list[str]:
    keys = []
    cache = _build_backend_cache(backend, client)
    for fi in franchise_items:
        ms = match_state.get(fi["id"], {})
        found = ms.get(f"{backend}_found", 0)
        if not found:
            continue
        if fi["item_type"] in ("show", "season"):
            keys.extend(_expand_franchise_show_item(fi, cache))
        else:
            k = ms.get(f"{backend}_item_id")
            if k:
                keys.append(k)
    return keys


def create_franchise_playlist(
    name: str,
    backend: str,
    franchise_key: str,
    source: str = "trakt",
    trakt_user: str | None = None,
    trakt_slug: str | None = None,
    chronolists_id: str | None = None,
    franchise_name: str = "",
) -> int:
    from datetime import datetime, timezone

    db._validate_backend(backend)

    definition_id = _fetch_and_store_franchise(
        key=franchise_key,
        name=franchise_name,
        source=source,
        trakt_user=trakt_user,
        trakt_slug=trakt_slug,
        chronolists_id=chronolists_id,
    )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.connection() as conn:
        cur = conn.execute(
            """INSERT INTO managed_playlists
               (name, backend, playlist_type, franchise_definition_id,
                sort_mode, block_size, unwatched_only, auto_sync,
                pruning_enabled, created_at)
               VALUES (?, ?, 'franchise', ?, 'franchise', 1, 0, 1, 0, ?)""",
            (name, backend, definition_id, now),
        )
        playlist_id = cur.lastrowid

    row = db.get_playlist(playlist_id)
    plex_keys, jellyfin_keys, emby_keys, missing_count = _match_franchise_to_library(
        definition_id, playlist_id, row
    )

    log.info(
        "Creating franchise playlist '%s': %d plex keys, %d jellyfin keys, %d emby keys, %d missing",
        name, len(plex_keys), len(jellyfin_keys), len(emby_keys), missing_count,
    )

    # Deterministic franchise cover (TMDB) — set after each backend create so
    # franchise playlists get a real poster rather than relying on the media
    # server's inconsistent auto-composite. Best-effort; never blocks creation.
    poster_list = _franchise_poster_urls(db.list_franchise_items(definition_id), limit=5)
    poster_url = poster_list[0] if poster_list else None
    if poster_list:
        defn = db.get_franchise_definition_by_id(definition_id) or {}
        raw = (defn.get("poster_urls") or "").strip()
        try:
            stored = len(json.loads(raw)) if raw else 0
        except Exception:
            stored = 0
        if len(poster_list) > stored:
            db.set_franchise_definition_poster(
                definition_id, poster_url, json.dumps(poster_list))

    for tb, client, pl_id in _clients_for_playlist(row):
        backend_keys = {"plex": plex_keys, "jellyfin": jellyfin_keys, "emby": emby_keys}.get(tb, [])
        if backend_keys:
            try:
                new_pl_id = client.create_playlist(name, backend_keys)
                if tb == "plex":
                    with db.connection() as conn2:
                        conn2.execute(
                            "UPDATE managed_playlists SET plex_rating_key=? WHERE id=?",
                            (new_pl_id, playlist_id),
                        )
                elif tb == "jellyfin":
                    with db.connection() as conn2:
                        conn2.execute(
                            "UPDATE managed_playlists SET jellyfin_playlist_id=? WHERE id=?",
                            (new_pl_id, playlist_id),
                        )
                else:
                    db.set_emby_playlist_id(playlist_id, new_pl_id)
                if poster_url:
                    client.set_playlist_image(new_pl_id, poster_url)
            except Exception:
                log.warning("Failed to create franchise playlist on %s", tb, exc_info=True)

    try:
        _webhooks.fire(
            "playlist.created",
            playlist=_webhooks._playlist_info(dict(db.get_playlist(playlist_id))),
        )
    except Exception:
        pass

    return playlist_id


def sync_franchise_playlist(playlist_id: int, force: bool = False) -> tuple[int, int]:
    row = db.get_playlist(playlist_id)
    if not row:
        return (0, 0)
    if not force and not bool(row["auto_sync"]):
        return (0, 0)
    row_d = dict(row)

    definition_id = row_d.get("franchise_definition_id")
    if not definition_id:
        log.warning("Franchise playlist %d has no definition_id", playlist_id)
        return (0, 0)

    plex_keys, jellyfin_keys, emby_keys, missing_count = _match_franchise_to_library(
        definition_id, playlist_id, row
    )
    log.debug(
        "Franchise sync '%s': %d plex, %d jf, %d emby, %d missing",
        row["name"], len(plex_keys), len(jellyfin_keys), len(emby_keys), missing_count,
    )

    added = removed = 0
    for tb, client, pl_id in _clients_for_playlist(row):
        if not pl_id:
            continue
        try:
            current = client.get_playlist_items(pl_id)
            current_keys = [it.rating_key for it in current]
            be_items = db.list_franchise_items(definition_id)
            be_match = db.list_franchise_match_state(playlist_id)
            be_keys = _build_backend_ordered_keys(tb, client, be_items, be_match)

            if bool(_row_get(row, "pruning_enabled", 1)) and be_keys:
                _before = len(be_keys)
                be_keys, pruned = _franchise_prune_keys(
                    current, be_keys, playlist_id, tb, _watched_keep()
                )
                log.info(
                    "Franchise prune '%s' on %s: %d items, %d pruned-set, %d kept",
                    row["name"], tb, _before, len(pruned), len(be_keys),
                )

            added_keys = [k for k in be_keys if k not in set(current_keys)]
            removed_keys = [k for k in current_keys if k not in set(be_keys)]
            if added_keys or removed_keys:
                client.replace_playlist_items(pl_id, be_keys)
                added += len(added_keys)
                removed += len(removed_keys)
                log.info("Franchise sync '%s' on %s: +%d, -%d",
                         row["name"], tb, len(added_keys), len(removed_keys))
        except Exception:
            log.warning(
                "Franchise sync failed for backend=%s playlist=%d",
                tb, playlist_id, exc_info=True
            )

    # Heal cover art when the playlist changed, or on any forced sync (the
    # "Sync Now" button) — the latter backfills posters on franchise playlists
    # created before deterministic covers existed.
    if added or removed or force:
        _apply_franchise_poster(row)
    if added or removed:
        try:
            _webhooks.fire(
                "playlist.synced",
                playlist=_webhooks._playlist_info(dict(row)),
                data={"added": added, "removed": removed},
            )
        except Exception:
            pass

    return added, removed


def franchise_items_for_maker(definition_id: int) -> list[dict]:
    rows = db.list_franchise_items(definition_id)
    out = []
    for r in rows:
        out.append({
            "rank": r["rank"],
            "item_type": r["item_type"],
            "title": r["title"],
            "year": r.get("year"),
            "tmdb_id": r.get("tmdb_id"),
            "imdb_id": r.get("imdb_id"),
            "tvdb_id": r.get("tvdb_id"),
            "season_number": r.get("season_number"),
            "episode_number": r.get("episode_number"),
            "show_title": r.get("show_title"),
            "show_tvdb_id": r.get("show_tvdb_id"),
            "show_tmdb_id": r.get("show_tmdb_id"),
        })
    return out


def save_user_franchise_playlist(
    *,
    playlist_id: int | None,
    name: str,
    backend: str,
    items: list[dict],
    description: str = "",
    forked_from_key: str | None = None,
) -> int:
    import time as _time
    from datetime import datetime, timezone

    if not name.strip():
        raise ValueError("Playlist name is required")
    db._validate_backend(backend)
    if not items:
        raise ValueError("At least one franchise item is required")

    normalised = []
    for i, item in enumerate(items):
        item = dict(item)
        item.setdefault("rank", i + 1)
        item.setdefault("item_type", "movie")
        item.setdefault("title", "")
        item.setdefault("year", None)
        item.setdefault("tmdb_id", None)
        item.setdefault("imdb_id", None)
        item.setdefault("tvdb_id", None)
        item.setdefault("season_number", None)
        item.setdefault("episode_number", None)
        item.setdefault("show_title", None)
        item.setdefault("show_tvdb_id", None)
        item.setdefault("show_tmdb_id", None)
        normalised.append(item)

    new_hash = get_trakt_client().content_hash(normalised)
    # Persist TMDB posters on the definition so the home-page card shows the
    # up-to-5 strip for user-built / forked franchises (the media-server cover is
    # set separately by _apply_franchise_poster below). Best-effort: empty if
    # TMDB is unavailable, in which case a forked def still falls back to its
    # bundled origin's static poster at render time.
    poster_list = _franchise_poster_urls(normalised, limit=5)
    poster_url = poster_list[0] if poster_list else None
    poster_urls_json = json.dumps(poster_list) if poster_list else None

    if playlist_id is not None:
        row = db.get_playlist(playlist_id)
        if not row:
            raise ValueError(f"Playlist {playlist_id} not found")
        row_d = dict(row)

        old_defn_id = row_d.get("franchise_definition_id")
        old_defn = db.get_franchise_definition_by_id(old_defn_id) if old_defn_id else None

        if old_defn and old_defn.get("source") in ("trakt", "local"):
            old_key = old_defn["key"]
            new_key = f"user_{playlist_id}_{int(_time.time())}"
            defn_id = db.insert_franchise_definition(
                key=new_key,
                name=name,
                source="user",
                forked_from_key=old_key,
                content_hash=new_hash,
                item_count=len(normalised),
                poster_url=poster_url,
                poster_urls=poster_urls_json,
            )
            db.replace_franchise_items(defn_id, normalised)
            db.rebind_playlist_franchise(playlist_id, defn_id)
            with db.connection() as conn:
                conn.execute(
                    "UPDATE managed_playlists SET name = ? WHERE id = ?",
                    (name, playlist_id),
                )
            log.info(
                "Forked franchise '%s' (from '%s') for playlist %d",
                name, old_key, playlist_id,
            )

        elif old_defn and old_defn.get("source") == "user":
            defn_id = old_defn_id
            db.replace_franchise_items(defn_id, normalised)
            db.update_franchise_definition_metadata(
                defn_id, content_hash=new_hash, item_count=len(normalised),
                poster_url=poster_url, poster_urls=poster_urls_json,
            )
            with db.connection() as conn:
                conn.execute(
                    "UPDATE managed_playlists SET name = ? WHERE id = ?",
                    (name, playlist_id),
                )
            log.info("Updated user franchise '%s' for playlist %d", name, playlist_id)

        else:
            raise ValueError("Playlist is not a franchise playlist")

        row2 = db.get_playlist(playlist_id)
        sync_franchise_playlist(playlist_id, force=True)
        _apply_franchise_poster(db.get_playlist(playlist_id))
        try:
            _webhooks.fire(
                "playlist.synced",
                playlist=_webhooks._playlist_info(dict(row2)),
            )
        except Exception:
            pass
        return playlist_id

    else:
        # Pre-check name uniqueness for a clean user-facing error before SQLite raises.
        with db.connection() as conn:
            existing = conn.execute(
                "SELECT 1 FROM managed_playlists WHERE name = ?", (name,),
            ).fetchone()
        if existing:
            raise ValueError(
                f"A playlist named {name!r} already exists. Pick a different name."
            )

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with db.connection() as conn:
            cur = conn.execute(
                """INSERT INTO managed_playlists
                   (name, backend, playlist_type, sort_mode,
                    block_size, unwatched_only, auto_sync, pruning_enabled, created_at)
                   VALUES (?, ?, 'franchise', 'franchise', 1, 0, 1, 0, ?)""",
                (name, backend, now),
            )
            playlist_id = cur.lastrowid

        defn_key = f"user_{playlist_id}"
        defn_id = db.insert_franchise_definition(
            key=defn_key,
            name=name,
            source="user",
            forked_from_key=forked_from_key,
            content_hash=new_hash,
            item_count=len(normalised),
            poster_url=poster_url,
            poster_urls=poster_urls_json,
        )
        db.replace_franchise_items(defn_id, normalised)

        with db.connection() as conn:
            conn.execute(
                "UPDATE managed_playlists SET franchise_definition_id = ? WHERE id = ?",
                (defn_id, playlist_id),
            )

        row = db.get_playlist(playlist_id)
        plex_keys, jellyfin_keys, emby_keys, missing_count = _match_franchise_to_library(
            defn_id, playlist_id, row
        )

        log.info(
            "Creating user franchise playlist '%s': %d plex keys, %d jellyfin keys, %d emby keys, %d missing",
            name, len(plex_keys), len(jellyfin_keys), len(emby_keys), missing_count,
        )

        for tb, client, pl_id in _clients_for_playlist(row):
            backend_keys = {"plex": plex_keys, "jellyfin": jellyfin_keys, "emby": emby_keys}.get(tb, [])
            if backend_keys:
                try:
                    new_pl_id = client.create_playlist(name, backend_keys)
                    if tb == "plex":
                        with db.connection() as conn2:
                            conn2.execute(
                                "UPDATE managed_playlists SET plex_rating_key=? WHERE id=?",
                                (new_pl_id, playlist_id),
                            )
                    elif tb == "jellyfin":
                        with db.connection() as conn2:
                            conn2.execute(
                                "UPDATE managed_playlists SET jellyfin_playlist_id=? WHERE id=?",
                                (new_pl_id, playlist_id),
                            )
                    else:
                        db.set_emby_playlist_id(playlist_id, new_pl_id)
                except Exception:
                    log.warning("Failed to create franchise playlist on %s", tb, exc_info=True)

        _apply_franchise_poster(db.get_playlist(playlist_id))

        try:
            _webhooks.fire(
                "playlist.created",
                playlist=_webhooks._playlist_info(dict(db.get_playlist(playlist_id))),
            )
        except Exception:
            pass

        return playlist_id


def restore_bundled_franchise(playlist_id: int) -> bool:
    row = db.get_playlist(playlist_id)
    if not row:
        return False
    row_d = dict(row)

    defn_id = row_d.get("franchise_definition_id")
    if not defn_id:
        return False

    defn = db.get_franchise_definition_by_id(defn_id)
    if not defn or defn.get("source") != "user":
        return False

    fork_key = defn.get("forked_from_key")
    if not fork_key:
        return False

    bundled = db.get_franchise_definition(fork_key)
    if not bundled:
        return False

    db.rebind_playlist_franchise(playlist_id, bundled["id"])

    ref_count = db.count_playlists_by_franchise_definition(defn_id)
    if ref_count <= 0:
        db.delete_franchise_definition(defn_id)

    sync_franchise_playlist(playlist_id, force=True)
    return True


def migrate_bundled_franchise_sources() -> None:
    """Auto-migrate existing franchise playlists from Trakt to Chronolists when
    the bundled registry has switched sources. Runs once on refresh cycles.

    Only upgrades definitions that are currently trakt-sourced; user and local
    definitions are left alone. Chronolists-sourced definitions are already
    migrated — skip them.
    """
    registry = {f["key"]: f for f in _load_prebaked_franchises()}
    for defn in db.list_franchise_definitions():
        reg = registry.get(defn["key"])
        if not reg or reg.get("source") != "chronolists":
            continue
        if defn["source"] == "chronolists":
            continue
        try:
            definition_id = _fetch_and_store_franchise(
                defn["key"], reg["name"], source="chronolists",
                chronolists_id=reg["chronolists_id"])
            for pl in db.get_playlists_by_franchise_definition(definition_id):
                try:
                    sync_franchise_playlist(pl["id"], force=True)
                except Exception:
                    log.warning("migrate: re-sync failed for pl=%s", pl["id"], exc_info=True)
            log.info("Migrated franchise '%s' Trakt -> Chronolists", defn["key"])
        except Exception:
            log.warning("Source migration failed for '%s'", defn["key"], exc_info=True)


def _discover_new_chronolists_franchises(cl_index: dict) -> None:
    if not cl_index:
        return

    registry = _load_prebaked_franchises()
    known_cl_ids = {
        f["chronolists_id"]
        for f in registry
        if f.get("chronolists_id")
    }
    registry_by_key = {f["key"]: f for f in registry}

    for cl_id, cl_meta in cl_index.items():
        if cl_id in known_cl_ids:
            continue

        normalized = _normalize_cl_key(cl_id)
        cl_name = cl_meta.get("name", cl_id)
        cl_hash = cl_meta.get("hash")

        if normalized in registry_by_key:
            reg_entry = registry_by_key[normalized]
            if reg_entry.get("source") == "chronolists":
                continue
            try:
                existing_defn = db.get_franchise_definition(normalized)
                old_hash = existing_defn.get("content_hash") if existing_defn else None
                definition_id = _fetch_and_store_franchise_chronolists(
                    normalized, reg_entry["name"], cl_id, known_hash=cl_hash)
                new_defn = db.get_franchise_definition(normalized)
                if not existing_defn or (new_defn and new_defn.get("content_hash") != old_hash):
                    for pl in db.get_playlists_by_franchise_definition(definition_id):
                        try:
                            sync_franchise_playlist(pl["id"], force=True)
                        except Exception:
                            log.warning("Auto-migrate re-sync failed for pl=%d", pl["id"], exc_info=True)
                log.info(
                    "Franchise '%s' auto-migrated from %s to Chronolists (cl_id=%s)",
                    reg_entry["name"], reg_entry.get("source"), cl_id)
            except Exception:
                log.warning("Auto-migration failed for cl_id=%s", cl_id, exc_info=True)

        else:
            existing_defn = db.get_franchise_definition(normalized)
            if existing_defn and existing_defn.get("content_hash") == cl_hash:
                continue
            try:
                _fetch_and_store_franchise_chronolists(
                    normalized, cl_name, cl_id, known_hash=cl_hash,
                    auto_discovered=True)
                if not existing_defn:
                    log.info(
                        "Auto-discovered new Chronolists franchise: '%s' (cl_id=%s)",
                        cl_name, cl_id)
            except Exception:
                log.warning("Auto-discovery failed for cl_id=%s", cl_id, exc_info=True)


def refresh_franchise_definitions() -> None:
    definitions = db.list_franchise_definitions()

    cl_index: dict[str, dict] = {}
    try:
        from chronolists_client import get_chronolists_client
        cl_index = get_chronolists_client().fetch_index()
    except Exception:
        log.warning("Chronolists index fetch failed", exc_info=True)

    for defn in definitions:
        src = defn["source"]
        if src in ("local", "user"):
            continue
        try:
            old_hash = defn.get("content_hash")
            if src == "trakt":
                definition_id = _fetch_and_store_franchise(
                    key=defn["key"], name=defn["name"],
                    source="trakt", trakt_user=defn["trakt_user"],
                    trakt_slug=defn["trakt_slug"])
            elif src == "chronolists":
                cid = defn.get("chronolists_id")
                if not cl_index or cid not in cl_index:
                    continue
                definition_id = _fetch_and_store_franchise_chronolists(
                    defn["key"], defn["name"], cid,
                    known_hash=cl_index[cid].get("hash"))
            else:
                continue
            new_defn = db.get_franchise_definition(defn["key"])
            if new_defn and new_defn.get("content_hash") != old_hash:
                log.info(
                    "Franchise '%s' changed, re-syncing dependent playlists",
                    defn["name"],
                )
                for pl_row in db.get_playlists_by_franchise_definition(definition_id):
                    try:
                        sync_franchise_playlist(pl_row["id"], force=True)
                    except Exception:
                        log.warning(
                            "Re-sync failed for franchise playlist %d",
                            pl_row["id"], exc_info=True,
                        )
        except Exception:
            log.warning(
                "refresh_franchise_definitions failed for '%s'",
                defn.get("key"), exc_info=True,
            )

    if cl_index:
        try:
            _discover_new_chronolists_franchises(cl_index)
        except Exception:
            log.warning("Chronolists auto-discovery failed", exc_info=True)


def sync_all() -> None:
    for row in db.list_playlists():
        try:
            sync_playlist(row["id"])
        except Exception:
            log.exception("Sync failed for playlist id=%s", row["id"])


# --------------------------------------------------------------------------- #
# Delete a managed playlist
# --------------------------------------------------------------------------- #


def rename_managed_playlist(playlist_id: int, new_name: str) -> list[str]:
    """Rename the playlist on every backend it targets, then update the local
    row. Returns the list of backends whose rename FAILED (empty == all good)
    so the caller can warn the user. The local row is renamed even on partial
    backend failure — the server-side name can be fixed by hand; a mismatch
    never breaks sync (dispatch is by stored id, not name)."""
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError("Playlist not found")
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("Playlist name cannot be empty.")
    old_name = row["name"]
    if new_name == old_name:
        return []
    # Pre-check name uniqueness for a clean user-facing error before SQLite raises.
    with db.connection() as conn:
        clash = conn.execute(
            "SELECT 1 FROM managed_playlists WHERE name = ? AND id != ?",
            (new_name, playlist_id),
        ).fetchone()
    if clash:
        raise ValueError(
            f"A playlist named {new_name!r} already exists. Pick a different name."
        )
    failed: list[str] = []
    for tb, client, pl_id in _clients_for_playlist(row):
        if not pl_id:
            continue
        try:
            client.rename_playlist(pl_id, new_name)
        except Exception:
            log.warning(
                "Failed to rename %s playlist for '%s'", tb, old_name, exc_info=True
            )
            failed.append(tb)
    db.rename_playlist(playlist_id, new_name)
    log.info(
        "Renamed managed playlist '%s' -> '%s' (backend failures: %s)",
        old_name, new_name, failed or "none",
    )
    return failed


def delete_managed_playlist(playlist_id: int) -> list[str]:
    """Delete the playlist on every backend it targets, then remove the local
    row. Returns the list of backends whose deletion FAILED (empty == all good)
    so the caller can warn the user instead of silently claiming success.

    `delete_playlist` is called directly (no `playlist_exists` pre-check): each
    backend's impl already no-ops when the playlist is gone, and a false-negative
    existence check must never cause us to skip the real delete.
    """
    row = db.get_playlist(playlist_id)
    if not row:
        return []
    try:
        _webhooks.fire("playlist.deleted", playlist=_webhooks._playlist_info(dict(row)))
    except Exception:
        pass
    failed: list[str] = []
    for tb, client, pl_id in _clients_for_playlist(row):
        if not pl_id:
            continue
        try:
            client.delete_playlist(pl_id)
        except Exception:
            # Log the real cause (e.g. an Emby permission error) — don't bury it.
            log.warning("Failed to delete %s playlist for '%s'", tb, row["name"], exc_info=True)
            failed.append(tb)
    db.delete_playlist(playlist_id)
    log.info("Deleted managed playlist '%s' (backend failures: %s)", row["name"], failed or "none")
    return failed


# --------------------------------------------------------------------------- #
# Prune watched items
# --------------------------------------------------------------------------- #


def prune_playlist(playlist_id: int, keep_last_n: int | None = None) -> int:
    if keep_last_n is None:
        keep_last_n = _watched_keep()
    row = db.get_playlist(playlist_id)
    if not row:
        return 0

    total = 0
    for tb, client, pl_id in _clients_for_playlist(row):
        if not pl_id:
            continue
        try:
            items = client.get_playlist_items(pl_id)
            indices = rotation.prune_indices(items, keep_last_n)
            if not indices:
                continue
            remove_keys = [items[i].rating_key for i in indices]
            client.remove_items_from_playlist(pl_id, remove_keys)
            # Franchise sync rebuilds from the static definition, so remember
            # what was pruned to keep it out (see sync_franchise_playlist).
            if _row_get(row, "playlist_type", "manual") == "franchise":
                db.add_pruned_items(playlist_id, tb, remove_keys)
            log.info("Pruned %d watched item(s) from '%s' on %s",
                     len(remove_keys), row["name"], tb)
            total += len(remove_keys)
        except Exception:
            log.exception("prune failed on %s for '%s'", tb, row["name"])
    return total


def prune_all() -> None:
    for row in db.list_playlists():
        try:
            if not _row_get(row, "pruning_enabled", 1):
                continue
            prune_playlist(row["id"])
        except Exception:
            log.exception("Prune failed for playlist id=%s", row["id"])


# --------------------------------------------------------------------------- #
# Views for the web UI
# --------------------------------------------------------------------------- #


def _annotate_crossover_titles(groups: list[dict], show_rows: list[dict]) -> list[dict]:
    """Add show_title to each crossover link dict, falling back to the raw key."""
    title_by_key = {s["show_rating_key"]: s["show_title"] for s in show_rows}
    for group in groups:
        for link in group["links"]:
            link["show_title"] = title_by_key.get(
                link["show_rating_key"], link["show_rating_key"]
            )
    return groups


def get_playlist_view(playlist_id: int) -> PlaylistView | None:
    row = db.get_playlist(playlist_id)
    if not row:
        return None
    all_rows = [dict(s) for s in db.list_shows(playlist_id)]
    shows = [s for s in all_rows if not s.get("is_excluded")]
    excluded_shows = [s for s in all_rows if s.get("is_excluded")]
    item_count = 0
    for tb, client, pl_id in _clients_for_playlist(row):
        if not pl_id:
            continue
        try:
            c = client.playlist_item_count(pl_id)
            if c > item_count:
                item_count = c
        except Exception:
            log.debug("item count failed on %s for '%s'", tb, row["name"])

    # v2.3.0 — movie + episode counts for index card stats
    playlist_type_val = _row_get(row, "playlist_type", "manual") or "manual"
    movies_count = 0
    franchise_poster: str | None = None
    franchise_posters: list[str] = []
    if playlist_type_val == "franchise":
        defn_id = _row_get(row, "franchise_definition_id", None)
        if defn_id:
            try:
                for fi in db.list_franchise_items(defn_id):
                    if fi.get("item_type") == "movie":
                        movies_count += 1
            except Exception:
                pass
            try:
                _defn = db.get_franchise_definition_by_id(defn_id)
                if _defn:
                    franchise_poster = (
                        _defn.get("poster_url")
                        or _static_franchise_poster(_defn.get("key"))
                        or _static_franchise_poster(_defn.get("forked_from_key"))
                    )
                    # Up-to-5 strip: prefer the stored JSON list, else fall back
                    # to the single cover so the card still shows something.
                    raw_urls = (_defn.get("poster_urls") or "").strip()
                    if raw_urls:
                        try:
                            parsed = json.loads(raw_urls)
                            if isinstance(parsed, list):
                                franchise_posters = [u for u in parsed if u][:5]
                        except Exception:
                            franchise_posters = []
                    if not franchise_posters and franchise_poster:
                        franchise_posters = [franchise_poster]
            except Exception:
                pass
    else:
        for s in shows:
            for csv_col in ("movie_rating_keys", "jellyfin_movie_item_ids"):
                csv = (s.get(csv_col) or "").strip()
                if csv:
                    movies_count = max(
                        movies_count,
                        sum(1 for x in csv.split(",") if x.strip()),
                    )
            # Per-show movie counts add up across the playlist
        # Simpler: count total non-empty CSV entries across shows on the
        # primary backend's column (Plex's movie_rating_keys by default;
        # jellyfin column mirrors it).
        movies_count = 0
        for s in shows:
            csv = (s.get("movie_rating_keys") or "").strip()
            if not csv:
                csv = (s.get("jellyfin_movie_item_ids") or "").strip()
            if csv:
                movies_count += sum(1 for x in csv.split(",") if x.strip())

    episodes_count = max(0, item_count - movies_count)

    backend_val = row["backend"] if "backend" in row.keys() else "plex"
    backend_set = parse_backend_set(backend_val)
    badge_map = {"plex": "P", "jellyfin": "J", "emby": "E"}
    badge_label = "+".join(badge_map.get(b, b.upper()) for b in backend_set)
    if len(backend_set) == 1:
        badge_css = f"is-{backend_set[0]}"
    else:
        badge_css = "is-multi"

    # v3.6.0 — per-playlist card artwork override
    mode = _row_get(row, "card_poster_mode", "auto") or "auto"
    if mode == "custom" and _row_get(row, "card_poster_file", None):
        franchise_posters = [f"/card-art/{row['id']}"]
        franchise_poster = franchise_posters[0]
    elif mode == "pick":
        raw = _row_get(row, "card_posters", None)
        try:
            picked = [u for u in json.loads(raw) if u] if raw else []
        except Exception:
            picked = []
        if picked:
            franchise_posters = picked[:5]
            franchise_poster = picked[0]
    # card_poster_keys — parse from JSON column for the detail page UI
    card_keys = []
    raw_keys = _row_get(row, "card_poster_keys", None)
    if raw_keys:
        try:
            card_keys = [k for k in json.loads(raw_keys) if k]
        except Exception:
            card_keys = []

    return PlaylistView(
        id=row["id"],
        name=row["name"],
        plex_rating_key=row["plex_rating_key"],
        jellyfin_playlist_id=(
            row["jellyfin_playlist_id"] if "jellyfin_playlist_id" in row.keys() else None
        ),
        emby_playlist_id=(
            row["emby_playlist_id"] if "emby_playlist_id" in row.keys() else None
        ),
        backend=row["backend"] if "backend" in row.keys() else "plex",
        shows=shows,
        item_count=item_count,
        sort_mode=row["sort_mode"] or "rotation",
        unwatched_only=bool(row["unwatched_only"]),
        auto_sync=bool(row["auto_sync"]) if "auto_sync" in row.keys() else True,
        block_size=int(_row_get(row, "block_size", 1) or 1),
        shuffle_seed=_row_get(row, "shuffle_seed", None),
        playlist_type=_row_get(row, "playlist_type", "manual") or "manual",
        genre_filter=_row_get(row, "genre_filter", None),
        excluded_shows=excluded_shows,
        crossover_groups=_annotate_crossover_titles(
            db.list_crossover_groups(playlist_id), all_rows
        ),
        rule_mode=_row_get(row, "rule_mode", "genre") or "genre",
        last_stats=(json.loads(_row_get(row, "last_stats", None) or ""
                              ) if _row_get(row, "last_stats", None) else None),
        pruning_enabled=int(_pe) if (_pe := _row_get(row, "pruning_enabled", 1)) is not None else 1,
        movies_count=movies_count,
        episodes_count=episodes_count,
        badge_label=badge_label,
        badge_css=badge_css,
        poster=franchise_poster,
        posters=franchise_posters,
        card_poster_mode=mode,
        card_poster_keys=card_keys,
    )


def list_playlist_views() -> list[PlaylistView]:
    out: list[PlaylistView] = []
    for row in db.list_playlists():
        view = get_playlist_view(row["id"])
        if view:
            out.append(view)
    return out


def resolve_card_posters(playlist_id: int, keys: list[str]) -> list[str]:
    """Resolve up to 5 picked member identifiers to poster URLs for the
    home card, in the given order. Show/genre: keys are playlist_shows
    .show_rating_key values -> '/thumb?path=<urlencoded thumb>&w=240&h=360'
    (skip shows with no thumb). Franchise: keys are franchise_items.id values
    (as strings) -> TMDB poster via the same per-item logic as
    _franchise_poster_urls (movie+tmdb_id -> get_movie; show_tmdb_id ->
    get_tv; w92->w500 swap). Unresolvable keys are skipped. Best-effort:
    never raises; returns []."""
    from urllib.parse import quote
    row = db.get_playlist(playlist_id)
    if not row:
        return []
    ptype = _row_get(row, "playlist_type", "manual") or "manual"
    out: list[str] = []
    if ptype == "franchise":
        defn_id = dict(row).get("franchise_definition_id")
        if not defn_id:
            return []
        items = {str(fi["id"]): fi for fi in db.list_franchise_items(defn_id)}
        try:
            import tmdb_client
        except Exception:
            return []
        seen: set[str] = set()
        for k in keys:
            if len(out) >= 5:
                break
            fi = items.get(str(k))
            if not fi:
                continue
            try:
                poster = None
                if fi.get("item_type") == "movie" and fi.get("tmdb_id"):
                    poster = (tmdb_client.get_movie(int(fi["tmdb_id"])) or {}).get("poster")
                elif fi.get("show_tmdb_id"):
                    poster = (tmdb_client.get_tv(int(fi["show_tmdb_id"])) or {}).get("poster")
                if poster:
                    url = poster.replace("/t/p/w92/", "/t/p/w500/")
                    if url not in seen:
                        seen.add(url)
                        out.append(url)
            except Exception:
                continue
    else:
        shows = [dict(s) for s in db.list_shows(playlist_id)]
        thumb_by_key = {s["show_rating_key"]: s.get("show_thumb") for s in shows}
        for k in keys:
            if len(out) >= 5:
                break
            thumb = thumb_by_key.get(k)
            if thumb:
                out.append(f"/thumb?path={quote(thumb)}&w=240&h=360")
    return out


def get_live_playlist_order(playlist_id: int, backend: str) -> list[dict] | None:
    """Ordered live contents of this playlist on one backend, for display.
    Returns None when the playlist/backend pairing is invalid (no row, the
    backend isn't in the row's backend set, or no playlist id on that side).
    Each dict: {pos, kind, title, show_title, season, episode, air_date,
    watched}. Show titles map via the playlist's playlist_shows rows
    (backend-keyed item id OR primary show_rating_key); fall back to '' when
    unknown. Raises nothing; backend/API errors return None."""
    row = db.get_playlist(playlist_id)
    if not row:
        return None
    backend_cols = {"plex": "plex_show_item_id", "jellyfin": "jellyfin_show_item_id", "emby": "emby_show_item_id"}
    if backend not in parse_backend_set(row["backend"] if "backend" in row.keys() else "plex"):
        return None
    client = None
    pl_id = None
    for be, cl, pid in _clients_for_playlist(row):
        if be == backend:
            client, pl_id = cl, pid
            break
    if not client or not pl_id:
        return None
    try:
        items = client.get_playlist_items(pl_id)
    except Exception:
        return None
    show_rows = [dict(s) for s in db.list_shows(playlist_id)]
    col = backend_cols.get(backend, "plex_show_item_id")
    show_title_map: dict[str, str] = {}
    for s in show_rows:
        bid = s.get(col) or s.get("show_rating_key")
        if bid:
            show_title_map[str(bid)] = s.get("show_title", "")
    out: list[dict] = []
    for i, it in enumerate(items[:2000]):
        title = it.title or ""
        show_title = show_title_map.get(str(it.show_rating_key), "")
        out.append({
            "pos": i + 1,
            "kind": it.kind,
            "title": title,
            "show_title": show_title,
            "season": it.season,
            "episode": it.episode,
            "air_date": it.air_date or "",
            "watched": it.view_count > 0,
        })
    return out
