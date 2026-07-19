"""SQLite persistence for managed rotating playlists.

For sort_mode validation, the source of truth is `rotation.VALID_SORT_MODES`.

Schema:
  managed_playlists
    id                   INTEGER PK
    name                 TEXT     — display name (and the playlist title on every backend)
    plex_rating_key      TEXT     — ratingKey of the Plex playlist (nullable; NULL when backend=jellyfin)
    jellyfin_playlist_id TEXT     — Id of the Jellyfin playlist (nullable; NULL when backend=plex)
    backend              TEXT     — 'plex' | 'jellyfin' | 'both' (default 'plex' for legacy rows)
    playlist_type        TEXT     — 'manual' | 'genre' (default 'manual')
    genre_filter         TEXT     — CSV of genre names for genre playlists (nullable)
    created_at           TEXT
    sort_mode            TEXT     — see rotation.VALID_SORT_MODES
    block_size           INTEGER  — episodes per block in rotation_blocks (default 1)
    shuffle_seed         INTEGER  — seed for shuffle_chronological (nullable)
    unwatched_only       INTEGER  — 0/1
    auto_sync            INTEGER  — 0/1, per-playlist sync opt-out
  playlist_shows
    playlist_id              INTEGER FK -> managed_playlists.id
    show_rating_key          TEXT     — primary key part; for legacy Plex rows equals plex_show_item_id;
                                        for Jellyfin-originated rows it's the Jellyfin Id (opaque, just a PK)
    plex_show_item_id        TEXT     — Plex ratingKey for this show, when present (nullable)
    jellyfin_show_item_id    TEXT     — Jellyfin Id for this show, when present (nullable)
    show_title               TEXT     — cached for UI when the backend is unreachable
    show_thumb               TEXT     — cached thumb reference
    position                 INTEGER  — user-defined order in the rotation
    weight                   INTEGER  — per-show weight for rotation_weighted (default 1)
    start_season             INTEGER  — lowest season to include (default 1)
    end_season               INTEGER  — highest season to include (NULL = no cap)
    include_specials         INTEGER  — 0/1: include Season 0 in the rotation
    include_movies           INTEGER  — 0/1
    movie_rating_keys        TEXT     — comma-separated Plex movie ratingKeys
    jellyfin_movie_item_ids  TEXT     — comma-separated Jellyfin movie Ids (parallel to movie_rating_keys)
    excluded_episode_keys    TEXT     — comma-separated "S:E" pairs to skip (e.g. "1:1,3:14")
    is_excluded              INTEGER  — 0/1, soft-delete for genre playlists (default 0)
    PRIMARY KEY (playlist_id, show_rating_key)

The Plex columns and the Jellyfin columns are each nullable — a playlist with
backend='jellyfin' only populates the jellyfin_* columns, a playlist with
backend='both' tries to populate both via title+year matching at show-add time.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from rotation import VALID_SORT_MODES

DB_PATH = os.environ.get("DB_PATH", "rotator.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


VALID_BACKENDS = ("plex", "jellyfin", "emby", "both")
VALID_PLAYLIST_TYPES = ("manual", "genre", "franchise")
VALID_FRANCHISE_SOURCES = ("trakt", "local", "user", "chronolists")


def _validate_backend(backend: str) -> None:
    if not backend or not backend.strip():
        raise ValueError("Empty or invalid backend set")
    from media_client import ALL_BACKENDS
    tokens = [t.strip() for t in backend.replace(";", ",").split(",") if t.strip()]
    if not tokens:
        raise ValueError(f"Empty backend set")
    for t in tokens:
        if t not in ALL_BACKENDS and t != "both":
            raise ValueError(f"Unknown backend token: {t!r}")


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS managed_playlists (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                name                 TEXT NOT NULL UNIQUE,
                plex_rating_key      TEXT,
                jellyfin_playlist_id TEXT,
                emby_playlist_id     TEXT,
                backend              TEXT NOT NULL DEFAULT 'plex'
                    CHECK(backend IN ('plex','jellyfin','both')),
                playlist_type        TEXT NOT NULL DEFAULT 'manual'
                    CHECK(playlist_type IN ('manual','genre','franchise')),
                genre_filter         TEXT,
                created_at           TEXT NOT NULL,
                sort_mode            TEXT NOT NULL DEFAULT 'rotation',
                block_size           INTEGER NOT NULL DEFAULT 1,
                shuffle_seed         INTEGER,
                unwatched_only       INTEGER NOT NULL DEFAULT 0,
                auto_sync            INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS crossover_groups (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_id  INTEGER NOT NULL,
                label        TEXT    NOT NULL DEFAULT '',
                sort_index   INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (playlist_id) REFERENCES managed_playlists(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS crossover_links (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id        INTEGER NOT NULL,
                show_rating_key TEXT    NOT NULL,
                season          INTEGER NOT NULL,
                episode         INTEGER NOT NULL,
                sort_index      INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (group_id) REFERENCES crossover_groups(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS playlist_shows (
                playlist_id              INTEGER NOT NULL,
                show_rating_key          TEXT    NOT NULL,
                plex_show_item_id        TEXT,
                jellyfin_show_item_id    TEXT,
                show_title               TEXT    NOT NULL,
                show_thumb               TEXT,
                position                 INTEGER NOT NULL,
                weight                   INTEGER NOT NULL DEFAULT 1,
                start_season             INTEGER NOT NULL DEFAULT 1,
                end_season               INTEGER,
                include_specials         INTEGER NOT NULL DEFAULT 0,
                include_movies           INTEGER NOT NULL DEFAULT 0,
                movie_rating_keys        TEXT    NOT NULL DEFAULT '',
                jellyfin_movie_item_ids  TEXT    NOT NULL DEFAULT '',
                emby_show_item_id     TEXT,
                emby_movie_item_ids   TEXT NOT NULL DEFAULT '',
                excluded_episode_keys    TEXT    NOT NULL DEFAULT '',
                is_excluded              INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (playlist_id, show_rating_key),
                FOREIGN KEY (playlist_id) REFERENCES managed_playlists(id) ON DELETE CASCADE
            );

            -- v3.2.7 — user-asserted "these are the same show" links across
            -- backends, for shows that can't be auto-matched (different titles,
            -- no shared provider id, e.g. Emby has no metadata). All rows sharing
            -- a group_key are treated as one show by the dedup/matching layer.
            CREATE TABLE IF NOT EXISTS manual_show_links (
                backend     TEXT NOT NULL,
                item_id     TEXT NOT NULL,
                group_key   TEXT NOT NULL,
                label       TEXT,
                created_at  TEXT,
                PRIMARY KEY (backend, item_id)
            );

            -- v3.3.5 — persisted "pruned" set per (playlist, backend). Franchise
            -- sync rebuilds from the static definition, so to keep watched items
            -- removed it must know which were pruned (Emby's bulk get_view_counts
            -- can't report watch state; the live playlist can). item_id is the
            -- media item id on that backend.
            CREATE TABLE IF NOT EXISTS pruned_items (
                playlist_id INTEGER NOT NULL,
                backend     TEXT    NOT NULL,
                item_id     TEXT    NOT NULL,
                PRIMARY KEY (playlist_id, backend, item_id),
                FOREIGN KEY (playlist_id) REFERENCES managed_playlists(id) ON DELETE CASCADE
            );
            """
        )
        # Lightweight migration for older schemas
        cols = _columns(conn, "playlist_shows")
        if "show_thumb" not in cols:
            conn.execute("ALTER TABLE playlist_shows ADD COLUMN show_thumb TEXT")
        if "start_season" not in cols:
            conn.execute("ALTER TABLE playlist_shows ADD COLUMN start_season INTEGER NOT NULL DEFAULT 1")
        if "end_season" not in cols:
            conn.execute("ALTER TABLE playlist_shows ADD COLUMN end_season INTEGER")
        if "include_specials" not in cols:
            conn.execute("ALTER TABLE playlist_shows ADD COLUMN include_specials INTEGER NOT NULL DEFAULT 0")
        if "include_movies" not in cols:
            conn.execute("ALTER TABLE playlist_shows ADD COLUMN include_movies INTEGER NOT NULL DEFAULT 0")
        if "movie_rating_keys" not in cols:
            conn.execute("ALTER TABLE playlist_shows ADD COLUMN movie_rating_keys TEXT NOT NULL DEFAULT ''")
        # v1.1.0 — Jellyfin columns
        if "plex_show_item_id" not in cols:
            conn.execute("ALTER TABLE playlist_shows ADD COLUMN plex_show_item_id TEXT")
            # One-time backfill: every legacy row was Plex-originated, so the
            # PK show_rating_key IS the Plex ratingKey. Future rows set this
            # explicitly via add_shows().
            conn.execute(
                "UPDATE playlist_shows SET plex_show_item_id = show_rating_key "
                "WHERE plex_show_item_id IS NULL"
            )
        if "jellyfin_show_item_id" not in cols:
            conn.execute("ALTER TABLE playlist_shows ADD COLUMN jellyfin_show_item_id TEXT")
        if "jellyfin_movie_item_ids" not in cols:
            conn.execute("ALTER TABLE playlist_shows ADD COLUMN jellyfin_movie_item_ids TEXT NOT NULL DEFAULT ''")
        # v1.2.0 — per-episode exclusions
        if "excluded_episode_keys" not in cols:
            conn.execute("ALTER TABLE playlist_shows ADD COLUMN excluded_episode_keys TEXT NOT NULL DEFAULT ''")
        # v1.3.0 — weighted rotation
        if "weight" not in cols:
            conn.execute("ALTER TABLE playlist_shows ADD COLUMN weight INTEGER NOT NULL DEFAULT 1")
        # v1.4.0 — genre playlist soft-delete flag
        if "is_excluded" not in cols:
            conn.execute("ALTER TABLE playlist_shows ADD COLUMN is_excluded INTEGER NOT NULL DEFAULT 0")

        pl_cols = _columns(conn, "managed_playlists")
        if "sort_mode" not in pl_cols:
            conn.execute("ALTER TABLE managed_playlists ADD COLUMN sort_mode TEXT NOT NULL DEFAULT 'rotation'")
        if "unwatched_only" not in pl_cols:
            conn.execute("ALTER TABLE managed_playlists ADD COLUMN unwatched_only INTEGER NOT NULL DEFAULT 0")
        if "auto_sync" not in pl_cols:
            conn.execute("ALTER TABLE managed_playlists ADD COLUMN auto_sync INTEGER NOT NULL DEFAULT 1")
        # v1.1.0 — Jellyfin columns
        if "jellyfin_playlist_id" not in pl_cols:
            conn.execute("ALTER TABLE managed_playlists ADD COLUMN jellyfin_playlist_id TEXT")
        if "backend" not in pl_cols:
            # SQLite ALTER TABLE can't add a CHECK constraint; the helpers below
            # validate writes. Existing rows default to 'plex' as intended.
            conn.execute(
                "ALTER TABLE managed_playlists ADD COLUMN backend TEXT NOT NULL DEFAULT 'plex'"
            )
        # v1.3.0 — block scheduling + shuffle
        if "block_size" not in pl_cols:
            conn.execute(
                "ALTER TABLE managed_playlists ADD COLUMN block_size INTEGER NOT NULL DEFAULT 1"
            )
        if "shuffle_seed" not in pl_cols:
            conn.execute(
                "ALTER TABLE managed_playlists ADD COLUMN shuffle_seed INTEGER"
            )
        # v1.4.0 — genre playlists
        if "playlist_type" not in pl_cols:
            conn.execute(
                "ALTER TABLE managed_playlists ADD COLUMN playlist_type TEXT NOT NULL DEFAULT 'manual'"
            )
        if "genre_filter" not in pl_cols:
            conn.execute("ALTER TABLE managed_playlists ADD COLUMN genre_filter TEXT")

        # v1.5.0 — crossover groups (migration for pre-existing DBs)
        existing_tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "crossover_groups" not in existing_tables:
            conn.execute(
                """CREATE TABLE crossover_groups (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    playlist_id  INTEGER NOT NULL,
                    label        TEXT    NOT NULL DEFAULT '',
                    sort_index   INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (playlist_id) REFERENCES managed_playlists(id) ON DELETE CASCADE
                )"""
            )
        if "crossover_links" not in existing_tables:
            conn.execute(
                """CREATE TABLE crossover_links (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id        INTEGER NOT NULL,
                    show_rating_key TEXT    NOT NULL,
                    season          INTEGER NOT NULL,
                    episode         INTEGER NOT NULL,
                    sort_index      INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (group_id) REFERENCES crossover_groups(id) ON DELETE CASCADE
                )"""
            )

        # v1.7.0 — genre cache
        if "genre_cache" not in existing_tables:
            conn.execute(
                """CREATE TABLE genre_cache (
                    backend    TEXT NOT NULL,
                    genre      TEXT NOT NULL,
                    PRIMARY KEY (backend, genre)
                )"""
            )
        if "genre_cache_meta" not in existing_tables:
            conn.execute(
                """CREATE TABLE genre_cache_meta (
                    backend    TEXT PRIMARY KEY,
                    updated_at TEXT NOT NULL
                )"""
            )

        # v1.8.0 — smart playlist rules
        if "playlist_rules" not in existing_tables:
            conn.execute(
                """CREATE TABLE playlist_rules (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    playlist_id INTEGER NOT NULL,
                    rule_type   TEXT    NOT NULL,
                    operator    TEXT    NOT NULL DEFAULT 'include',
                    value       TEXT    NOT NULL DEFAULT '',
                    FOREIGN KEY (playlist_id) REFERENCES managed_playlists(id) ON DELETE CASCADE
                )"""
            )

        pl_cols2 = _columns(conn, "managed_playlists")
        if "rule_mode" not in pl_cols2:
            conn.execute(
                "ALTER TABLE managed_playlists ADD COLUMN rule_mode TEXT NOT NULL DEFAULT 'genre'"
            )

        # v2.2.0 — franchise playlists + per-playlist pruning toggle
        pl_cols3 = _columns(conn, "managed_playlists")
        if "franchise_definition_id" not in pl_cols3:
            conn.execute(
                "ALTER TABLE managed_playlists ADD COLUMN franchise_definition_id INTEGER"
            )
        if "pruning_enabled" not in pl_cols3:
            conn.execute(
                "ALTER TABLE managed_playlists ADD COLUMN pruning_enabled INTEGER NOT NULL DEFAULT 1"
            )

        # v2.0.0 — settings store for API key
        if "managed_settings" not in existing_tables:
            conn.execute(
                """CREATE TABLE managed_settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )"""
            )

        # v2.0.0 — analytics stats cache
        if "last_stats" not in pl_cols2:
            conn.execute(
                "ALTER TABLE managed_playlists ADD COLUMN last_stats TEXT"
            )

        # v2.1.0 — outbound webhooks
        if "webhooks" not in existing_tables:
            conn.execute(
                """CREATE TABLE webhooks (
                    id    INTEGER PRIMARY KEY AUTOINCREMENT,
                    url   TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT ''
                )"""
            )

        # v2.2.0 — franchise definitions cache
        if "franchise_definitions" not in existing_tables:
            conn.execute(
                """CREATE TABLE franchise_definitions (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    key          TEXT NOT NULL UNIQUE,
                    name         TEXT NOT NULL,
                    source       TEXT NOT NULL DEFAULT 'trakt',
                    trakt_user   TEXT,
                    trakt_slug   TEXT,
                    chronolists_id TEXT,
                    fetched_at   TEXT,
                    content_hash TEXT,
                    item_count   INTEGER NOT NULL DEFAULT 0,
                    auto_discovered INTEGER NOT NULL DEFAULT 0
                )"""
            )

        if "franchise_items" not in existing_tables:
            conn.execute(
                """CREATE TABLE franchise_items (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    definition_id    INTEGER NOT NULL,
                    rank             INTEGER NOT NULL,
                    item_type        TEXT NOT NULL,
                    title            TEXT NOT NULL,
                    year             INTEGER,
                    tmdb_id          INTEGER,
                    tvdb_id          INTEGER,
                    imdb_id          TEXT,
                    season_number    INTEGER,
                    episode_number   INTEGER,
                    show_title       TEXT,
                    show_tvdb_id     INTEGER,
                    show_tmdb_id     INTEGER,
                    FOREIGN KEY (definition_id) REFERENCES franchise_definitions(id)
                        ON DELETE CASCADE
                )"""
            )

        if "franchise_match_state" not in existing_tables:
            conn.execute(
                """CREATE TABLE franchise_match_state (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    franchise_item_id INTEGER NOT NULL,
                    playlist_id       INTEGER NOT NULL,
                    plex_found        INTEGER NOT NULL DEFAULT 0,
                    plex_item_id      TEXT,
                    jellyfin_found    INTEGER NOT NULL DEFAULT 0,
                    jellyfin_item_id  TEXT,
                    emby_found        INTEGER NOT NULL DEFAULT 0,
                    emby_item_id      TEXT,
                    UNIQUE(franchise_item_id, playlist_id),
                    FOREIGN KEY (franchise_item_id) REFERENCES franchise_items(id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (playlist_id) REFERENCES managed_playlists(id)
                        ON DELETE CASCADE
                )"""
            )

        # v2.3.0 — fork-on-edit for franchise definitions
        fd_cols = _columns(conn, "franchise_definitions")
        if "forked_from_key" not in fd_cols:
            conn.execute(
                "ALTER TABLE franchise_definitions ADD COLUMN forked_from_key TEXT"
            )

        # v2.4.0 — Chronolists integration
        if "show_tmdb_id" not in _columns(conn, "franchise_items"):
            conn.execute(
                "ALTER TABLE franchise_items ADD COLUMN show_tmdb_id INTEGER"
            )
        fd_cols_v24 = _columns(conn, "franchise_definitions")
        if "chronolists_id" not in fd_cols_v24:
            conn.execute(
                "ALTER TABLE franchise_definitions ADD COLUMN chronolists_id TEXT"
            )

        # v2.5.0 — Chronolists auto-discovery
        if "auto_discovered" not in _columns(conn, "franchise_definitions"):
            conn.execute(
                "ALTER TABLE franchise_definitions ADD COLUMN auto_discovered INTEGER NOT NULL DEFAULT 0"
            )

        # v3.0.0 — franchise card poster (TMDB), resolved at fetch time so
        # auto-discovered Chronolists franchises get a card poster too.
        if "poster_url" not in _columns(conn, "franchise_definitions"):
            conn.execute(
                "ALTER TABLE franchise_definitions ADD COLUMN poster_url TEXT"
            )

        # v3.2.3 — up to 5 representative posters (JSON array) for the home-page
        # card strip (1→full, 2→50/50, … 5→fifths, capped at 5).
        if "poster_urls" not in _columns(conn, "franchise_definitions"):
            conn.execute(
                "ALTER TABLE franchise_definitions ADD COLUMN poster_urls TEXT"
            )

        # v3.0.0 — Emby columns
        pl_shows_cols = _columns(conn, "playlist_shows")
        if "emby_show_item_id" not in pl_shows_cols:
            conn.execute("ALTER TABLE playlist_shows ADD COLUMN emby_show_item_id TEXT")
        if "emby_movie_item_ids" not in pl_shows_cols:
            conn.execute("ALTER TABLE playlist_shows ADD COLUMN emby_movie_item_ids TEXT NOT NULL DEFAULT ''")

        managed_pl_cols_v3 = _columns(conn, "managed_playlists")
        if "emby_playlist_id" not in managed_pl_cols_v3:
            conn.execute("ALTER TABLE managed_playlists ADD COLUMN emby_playlist_id TEXT")

        # Relax backend CHECK from enum to CSV set (SQLite requires rebuild)
        existing_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='managed_playlists'"
        ).fetchone()
        if existing_sql and "both" in (existing_sql["sql"] or ""):
            conn.executescript("""
                PRAGMA foreign_keys=OFF;
                BEGIN;
                CREATE TABLE managed_playlists_new (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    name                 TEXT NOT NULL UNIQUE,
                    plex_rating_key      TEXT,
                    jellyfin_playlist_id TEXT,
                    emby_playlist_id     TEXT,
                    backend              TEXT NOT NULL DEFAULT 'plex',
                    playlist_type        TEXT NOT NULL DEFAULT 'manual'
                        CHECK(playlist_type IN ('manual','genre','franchise')),
                    genre_filter         TEXT,
                    created_at           TEXT NOT NULL,
                    sort_mode            TEXT NOT NULL DEFAULT 'rotation',
                    block_size           INTEGER NOT NULL DEFAULT 1,
                    shuffle_seed         INTEGER,
                    unwatched_only       INTEGER NOT NULL DEFAULT 0,
                    auto_sync            INTEGER NOT NULL DEFAULT 1,
                    franchise_definition_id INTEGER,
                    pruning_enabled      INTEGER NOT NULL DEFAULT 1,
                    rule_mode            TEXT NOT NULL DEFAULT 'genre',
                    last_stats           TEXT
                );
                -- Copy by explicit column NAME, never SELECT *: the live
                -- managed_playlists physical column order depends on upgrade
                -- history (columns added by ALTER are appended last), so a
                -- positional copy misaligns — e.g. NULL shuffle_seed landing in
                -- NOT NULL block_size (issue #8).
                INSERT INTO managed_playlists_new (
                    id, name, plex_rating_key, jellyfin_playlist_id,
                    emby_playlist_id, backend, playlist_type, genre_filter,
                    created_at, sort_mode, block_size, shuffle_seed,
                    unwatched_only, auto_sync, franchise_definition_id,
                    pruning_enabled, rule_mode, last_stats
                )
                SELECT
                    id, name, plex_rating_key, jellyfin_playlist_id,
                    emby_playlist_id, backend, playlist_type, genre_filter,
                    created_at, sort_mode, block_size, shuffle_seed,
                    unwatched_only, auto_sync, franchise_definition_id,
                    pruning_enabled, rule_mode, last_stats
                FROM managed_playlists;
                DROP TABLE managed_playlists;
                ALTER TABLE managed_playlists_new RENAME TO managed_playlists;
                PRAGMA foreign_key_check;
                COMMIT;
                PRAGMA foreign_keys=ON;
            """)

        # Emby columns in franchise_match_state
        fms_cols = _columns(conn, "franchise_match_state")
        if "emby_found" not in fms_cols:
            conn.execute("ALTER TABLE franchise_match_state ADD COLUMN emby_found INTEGER NOT NULL DEFAULT 0")
        if "emby_item_id" not in fms_cols:
            conn.execute("ALTER TABLE franchise_match_state ADD COLUMN emby_item_id TEXT")

        # v3.6.0 — per-playlist card artwork control
        managed_card_cols = _columns(conn, "managed_playlists")
        if "card_poster_mode" not in managed_card_cols:
            conn.execute("ALTER TABLE managed_playlists ADD COLUMN card_poster_mode TEXT DEFAULT 'auto'")
        if "card_poster_keys" not in managed_card_cols:
            conn.execute("ALTER TABLE managed_playlists ADD COLUMN card_poster_keys TEXT")
        if "card_posters" not in managed_card_cols:
            conn.execute("ALTER TABLE managed_playlists ADD COLUMN card_posters TEXT")
        if "card_poster_file" not in managed_card_cols:
            conn.execute("ALTER TABLE managed_playlists ADD COLUMN card_poster_file TEXT")


def create_playlist(
    name: str,
    sort_mode: str = "rotation",
    unwatched_only: bool = False,
    auto_sync: bool = True,
    backend: str = "plex",
    playlist_type: str = "manual",
    genre_filter: str | None = None,
) -> int:
    if backend not in VALID_BACKENDS:
        _validate_backend(backend)
    if playlist_type not in VALID_PLAYLIST_TYPES:
        raise ValueError(f"Invalid playlist_type: {playlist_type!r}. Must be one of {VALID_PLAYLIST_TYPES}")
    with connection() as conn:
        cur = conn.execute(
            """INSERT INTO managed_playlists
               (name, created_at, sort_mode, unwatched_only, auto_sync, backend,
                playlist_type, genre_filter)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                name,
                datetime.now(timezone.utc).isoformat(),
                sort_mode,
                1 if unwatched_only else 0,
                1 if auto_sync else 0,
                backend,
                playlist_type,
                genre_filter,
            ),
        )
        return int(cur.lastrowid)


def set_genre_filter(playlist_id: int, genre_filter: str | None) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE managed_playlists SET genre_filter = ? WHERE id = ?",
            (genre_filter, playlist_id),
        )


def set_show_excluded(
    playlist_id: int, show_rating_key: str, excluded: bool
) -> None:
    with connection() as conn:
        conn.execute(
            """UPDATE playlist_shows SET is_excluded = ?
               WHERE playlist_id = ? AND show_rating_key = ?""",
            (1 if excluded else 0, playlist_id, show_rating_key),
        )


def set_sort_mode(playlist_id: int, sort_mode: str) -> None:
    if sort_mode not in VALID_SORT_MODES:
        raise ValueError(f"Invalid sort_mode: {sort_mode!r}")
    with connection() as conn:
        conn.execute(
            "UPDATE managed_playlists SET sort_mode = ? WHERE id = ?",
            (sort_mode, playlist_id),
        )


def set_block_size(playlist_id: int, block_size: int) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE managed_playlists SET block_size = ? WHERE id = ?",
            (max(1, int(block_size)), playlist_id),
        )


def set_shuffle_seed(playlist_id: int, seed: int | None) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE managed_playlists SET shuffle_seed = ? WHERE id = ?",
            (seed, playlist_id),
        )


def set_show_weight(playlist_id: int, show_rating_key: str, weight: int) -> None:
    with connection() as conn:
        conn.execute(
            """UPDATE playlist_shows SET weight = ?
               WHERE playlist_id = ? AND show_rating_key = ?""",
            (max(1, int(weight)), playlist_id, show_rating_key),
        )


def set_unwatched_only(playlist_id: int, unwatched_only: bool) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE managed_playlists SET unwatched_only = ? WHERE id = ?",
            (1 if unwatched_only else 0, playlist_id),
        )


def set_auto_sync(playlist_id: int, auto_sync: bool) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE managed_playlists SET auto_sync = ? WHERE id = ?",
            (1 if auto_sync else 0, playlist_id),
        )


def rename_playlist(playlist_id: int, name: str) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE managed_playlists SET name = ? WHERE id = ?",
            (name, playlist_id),
        )


def set_pruning_enabled(playlist_id: int, enabled: bool) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE managed_playlists SET pruning_enabled = ? WHERE id = ?",
            (int(enabled), playlist_id),
        )


def set_card_art(playlist_id: int, mode: str, keys_json: str | None,
                 posters_json: str | None, file_name: str | None) -> None:
    if mode not in ("auto", "pick", "custom"):
        raise ValueError(f"Invalid card_poster_mode: {mode!r}")
    with connection() as conn:
        conn.execute(
            """UPDATE managed_playlists
               SET card_poster_mode = ?, card_poster_keys = ?,
                   card_posters = ?, card_poster_file = ?
               WHERE id = ?""",
            (mode, keys_json, posters_json, file_name, playlist_id),
        )


# Column allow-lists: every per-backend setter funnels through one of these two
# helpers so the UPDATE shape lives in exactly one place.
_PLAYLIST_ID_COLS = {
    "plex_rating_key", "jellyfin_playlist_id", "emby_playlist_id",
}
_PLAYLIST_SHOW_COLS = {
    "plex_show_item_id", "jellyfin_show_item_id", "emby_show_item_id",
    "movie_rating_keys", "jellyfin_movie_item_ids", "emby_movie_item_ids",
}


def _set_playlist_column(column: str, playlist_id: int, value) -> None:
    assert column in _PLAYLIST_ID_COLS, column
    with connection() as conn:
        conn.execute(
            f"UPDATE managed_playlists SET {column} = ? WHERE id = ?",
            (value, playlist_id),
        )


def _set_playlist_show_column(
    column: str, playlist_id: int, show_rating_key: str, value
) -> None:
    assert column in _PLAYLIST_SHOW_COLS, column
    with connection() as conn:
        conn.execute(
            f"""UPDATE playlist_shows SET {column} = ?
               WHERE playlist_id = ? AND show_rating_key = ?""",
            (value, playlist_id, show_rating_key),
        )


def set_plex_rating_key(playlist_id: int, rating_key: str | None) -> None:
    _set_playlist_column("plex_rating_key", playlist_id, rating_key)


def set_jellyfin_playlist_id(playlist_id: int, jellyfin_id: str | None) -> None:
    _set_playlist_column("jellyfin_playlist_id", playlist_id, jellyfin_id)


def set_backend(playlist_id: int, backend: str) -> None:
    if backend not in VALID_BACKENDS:
        _validate_backend(backend)
    with connection() as conn:
        conn.execute(
            "UPDATE managed_playlists SET backend = ? WHERE id = ?",
            (backend, playlist_id),
        )


def set_plex_show_item_id(
    playlist_id: int, show_rating_key: str, plex_show_item_id: str | None
) -> None:
    """Persist the Plex show ratingKey matched at add-time or healed at sync-time."""
    _set_playlist_show_column("plex_show_item_id", playlist_id, show_rating_key, plex_show_item_id)


def set_jellyfin_show_item_id(
    playlist_id: int, show_rating_key: str, jellyfin_show_item_id: str | None
) -> None:
    """Persist the Jellyfin show id matched at add-time (or healed at sync-time).

    `show_rating_key` is the row's PK component — for legacy Plex-originated
    rows it equals the Plex ratingKey; for Jellyfin-originated rows it's the
    Jellyfin Id.
    """
    _set_playlist_show_column("jellyfin_show_item_id", playlist_id, show_rating_key, jellyfin_show_item_id)


def set_excluded_episodes(
    playlist_id: int,
    show_rating_key: str,
    excluded_episodes: set[tuple[int, int]] | list[tuple[int, int]] | str,
) -> None:
    """Persist the set of (season, episode) pairs to skip for one show."""
    if isinstance(excluded_episodes, str):
        s = excluded_episodes
    else:
        s = ",".join(f"{int(se):d}:{int(ep):d}" for se, ep in sorted(excluded_episodes))
    with connection() as conn:
        conn.execute(
            """UPDATE playlist_shows SET excluded_episode_keys = ?
               WHERE playlist_id = ? AND show_rating_key = ?""",
            (s, playlist_id, show_rating_key),
        )


def set_jellyfin_movie_item_ids(
    playlist_id: int, show_rating_key: str, jellyfin_movie_item_ids: list[str] | str
) -> None:
    s = jellyfin_movie_item_ids if isinstance(jellyfin_movie_item_ids, str) else ",".join(str(k) for k in jellyfin_movie_item_ids if k)
    _set_playlist_show_column("jellyfin_movie_item_ids", playlist_id, show_rating_key, s)


def set_emby_playlist_id(playlist_id: int, emby_id: str | None) -> None:
    _set_playlist_column("emby_playlist_id", playlist_id, emby_id)


def set_emby_show_item_id(
    playlist_id: int, show_rating_key: str, emby_show_item_id: str | None
) -> None:
    _set_playlist_show_column("emby_show_item_id", playlist_id, show_rating_key, emby_show_item_id)


def set_emby_movie_item_ids(
    playlist_id: int, show_rating_key: str, emby_movie_item_ids: list[str] | str
) -> None:
    s = emby_movie_item_ids if isinstance(emby_movie_item_ids, str) else ",".join(str(k) for k in emby_movie_item_ids if k)
    _set_playlist_show_column("emby_movie_item_ids", playlist_id, show_rating_key, s)


def list_playlists() -> list[sqlite3.Row]:
    with connection() as conn:
        return list(
            conn.execute("SELECT * FROM managed_playlists ORDER BY name").fetchall()
        )


def get_playlist(playlist_id: int) -> sqlite3.Row | None:
    with connection() as conn:
        return conn.execute(
            "SELECT * FROM managed_playlists WHERE id = ?", (playlist_id,)
        ).fetchone()


def delete_playlist(playlist_id: int) -> None:
    with connection() as conn:
        conn.execute("DELETE FROM managed_playlists WHERE id = ?", (playlist_id,))


def list_shows(playlist_id: int) -> list[sqlite3.Row]:
    with connection() as conn:
        return list(
            conn.execute(
                "SELECT * FROM playlist_shows WHERE playlist_id = ? ORDER BY position",
                (playlist_id,),
            ).fetchall()
        )


def get_show(playlist_id: int, show_rating_key: str) -> sqlite3.Row | None:
    with connection() as conn:
        return conn.execute(
            "SELECT * FROM playlist_shows WHERE playlist_id = ? AND show_rating_key = ?",
            (playlist_id, show_rating_key),
        ).fetchone()


def add_shows(playlist_id: int, configs: list[dict]) -> None:
    """configs: list of dicts with keys: rating_key, title, thumb,
    start_season, end_season, include_specials, include_movies,
    movie_rating_keys (list of strings). Appended after existing positions.

    v1.1.0 — optional Jellyfin fields (all default to None / empty):
      jellyfin_show_item_id    — opaque Jellyfin Id matched at add-time
      jellyfin_movie_item_ids  — list[str] parallel to movie_rating_keys
    """
    if not configs:
        return
    with connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(position), -1) AS m FROM playlist_shows WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()
        next_pos = int(row["m"]) + 1
        for cfg in configs:
            movie_keys = cfg.get("movie_rating_keys") or []
            if isinstance(movie_keys, str):
                movie_keys_str = movie_keys
            else:
                movie_keys_str = ",".join(str(k) for k in movie_keys)
            jf_movie_keys = cfg.get("jellyfin_movie_item_ids") or []
            if isinstance(jf_movie_keys, str):
                jf_movie_keys_str = jf_movie_keys
            else:
                jf_movie_keys_str = ",".join(str(k) for k in jf_movie_keys if k)
            emby_movie_keys = cfg.get("emby_movie_item_ids") or []
            if isinstance(emby_movie_keys, str):
                emby_movie_keys_str = emby_movie_keys
            else:
                emby_movie_keys_str = ",".join(str(k) for k in emby_movie_keys if k)
            # Default plex_show_item_id: if not supplied AND the PK looks
            # like a Plex ratingKey (digits only), assume Plex. Otherwise
            # caller must set it explicitly. This keeps single-backend
            # callers backward-compatible.
            plex_show_id = cfg.get("plex_show_item_id")
            if plex_show_id is None and str(cfg["rating_key"]).isdigit():
                plex_show_id = str(cfg["rating_key"])
            # Serialize excluded-episode set into "S:E,S:E,..." form.
            excl_raw = cfg.get("excluded_episodes") or cfg.get("excluded_episode_keys") or ""
            if isinstance(excl_raw, str):
                excl_str = excl_raw
            else:
                excl_str = ",".join(f"{int(s):d}:{int(e):d}" for s, e in sorted(excl_raw))
            weight_val = max(1, int(cfg.get("weight", 1) or 1))
            conn.execute(
                """INSERT OR IGNORE INTO playlist_shows
                   (playlist_id, show_rating_key, plex_show_item_id, jellyfin_show_item_id,
                    emby_show_item_id,
                    show_title, show_thumb, position, weight,
                    start_season, end_season, include_specials,
                    include_movies, movie_rating_keys, jellyfin_movie_item_ids,
                    emby_movie_item_ids,
                    excluded_episode_keys)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    playlist_id,
                    cfg["rating_key"],
                    plex_show_id,
                    cfg.get("jellyfin_show_item_id"),
                    cfg.get("emby_show_item_id"),
                    cfg["title"],
                    cfg.get("thumb"),
                    next_pos,
                    weight_val,
                    int(cfg.get("start_season", 1)),
                    cfg.get("end_season"),
                    1 if cfg.get("include_specials") else 0,
                    1 if cfg.get("include_movies") else 0,
                    movie_keys_str,
                    jf_movie_keys_str,
                    emby_movie_keys_str,
                    excl_str,
                ),
            )
            next_pos += 1


def remove_show(playlist_id: int, show_rating_key: str) -> None:
    with connection() as conn:
        conn.execute(
            "DELETE FROM playlist_shows WHERE playlist_id = ? AND show_rating_key = ?",
            (playlist_id, show_rating_key),
        )


# --------------------------------------------------------------------------- #
# Manual cross-backend "same show" links (v3.2.7)
# --------------------------------------------------------------------------- #


def link_shows_same(entries: list[tuple[str, str]], label: str | None = None) -> str | None:
    """Record that the given (backend, item_id) pairs are the SAME show.

    Reuses an existing group if any entry already belongs to one (merging
    groups when several do), else mints a new group token. `label` is a
    human-readable name for the Settings management list. Returns the
    group_key, or None if fewer than two distinct entries are supplied.
    """
    import secrets as _secrets
    from datetime import datetime, timezone

    seen: list[tuple[str, str]] = []
    for be, iid in entries:
        if not be or iid in (None, ""):
            continue
        pair = (str(be), str(iid))
        if pair not in seen:
            seen.append(pair)
    if len(seen) < 2:
        return None

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with connection() as conn:
        existing: set[str] = set()
        for be, iid in seen:
            r = conn.execute(
                "SELECT group_key FROM manual_show_links WHERE backend=? AND item_id=?",
                (be, iid),
            ).fetchone()
            if r:
                existing.add(r["group_key"])
        if existing:
            group_key = sorted(existing)[0]
            for g in existing:
                if g != group_key:
                    conn.execute(
                        "UPDATE manual_show_links SET group_key=? WHERE group_key=?",
                        (group_key, g),
                    )
        else:
            group_key = _secrets.token_hex(8)
        for be, iid in seen:
            conn.execute(
                """INSERT INTO manual_show_links (backend, item_id, group_key, label, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(backend, item_id)
                   DO UPDATE SET group_key=excluded.group_key,
                                 label=COALESCE(excluded.label, label)""",
                (be, iid, group_key, label, now),
            )
    return group_key


def get_manual_show_link_map() -> dict[tuple[str, str], str]:
    """{(backend, item_id) -> group_key} for every manual same-show link."""
    with connection() as conn:
        rows = conn.execute(
            "SELECT backend, item_id, group_key FROM manual_show_links"
        ).fetchall()
    return {(r["backend"], str(r["item_id"])): r["group_key"] for r in rows}


def list_manual_show_links() -> list[dict]:
    """Manual links grouped for display:
    [{group_key, label, members:[{backend,item_id}]}], sorted by label."""
    with connection() as conn:
        rows = conn.execute(
            "SELECT backend, item_id, group_key, label FROM manual_show_links "
            "ORDER BY group_key"
        ).fetchall()
    groups: dict[str, dict] = {}
    for r in rows:
        g = groups.setdefault(
            r["group_key"], {"group_key": r["group_key"], "label": None, "members": []}
        )
        if not g["label"] and r["label"]:
            g["label"] = r["label"]
        g["members"].append({"backend": r["backend"], "item_id": str(r["item_id"])})
    return sorted(groups.values(), key=lambda g: (g["label"] or "").lower())


def remove_manual_show_link_group(group_key: str) -> None:
    with connection() as conn:
        conn.execute(
            "DELETE FROM manual_show_links WHERE group_key = ?", (group_key,)
        )


def set_positions(playlist_id: int, ordered_keys: list[str]) -> None:
    """Rewrite the position column to match the given order."""
    with connection() as conn:
        for i, key in enumerate(ordered_keys):
            conn.execute(
                "UPDATE playlist_shows SET position = ? WHERE playlist_id = ? AND show_rating_key = ?",
                (i, playlist_id, key),
            )


# --------------------------------------------------------------------------- #
# v1.5.0 — Crossover groups
# --------------------------------------------------------------------------- #


def list_crossover_groups(playlist_id: int) -> list[dict]:
    """Return crossover groups with nested links for a playlist."""
    with connection() as conn:
        groups = conn.execute(
            "SELECT * FROM crossover_groups WHERE playlist_id = ? ORDER BY sort_index, id",
            (playlist_id,),
        ).fetchall()
        out: list[dict] = []
        for g in groups:
            links = conn.execute(
                "SELECT * FROM crossover_links WHERE group_id = ? ORDER BY sort_index, id",
                (g["id"],),
            ).fetchall()
            out.append({
                "id": g["id"],
                "playlist_id": g["playlist_id"],
                "label": g["label"],
                "sort_index": g["sort_index"],
                "links": [dict(li) for li in links],
            })
        return out


def create_crossover_group(playlist_id: int, label: str = "") -> int:
    with connection() as conn:
        cur = conn.execute(
            "INSERT INTO crossover_groups (playlist_id, label) VALUES (?, ?)",
            (playlist_id, label),
        )
        return int(cur.lastrowid)


def delete_crossover_group(group_id: int) -> None:
    with connection() as conn:
        conn.execute("DELETE FROM crossover_groups WHERE id = ?", (group_id,))


def add_crossover_link(
    group_id: int, show_rating_key: str, season: int, episode: int, sort_index: int = 0
) -> int:
    with connection() as conn:
        cur = conn.execute(
            """INSERT INTO crossover_links (group_id, show_rating_key, season, episode, sort_index)
               VALUES (?, ?, ?, ?, ?)""",
            (group_id, show_rating_key, season, episode, sort_index),
        )
        return int(cur.lastrowid)


def remove_crossover_link(link_id: int) -> None:
    with connection() as conn:
        conn.execute("DELETE FROM crossover_links WHERE id = ?", (link_id,))


# --------------------------------------------------------------------------- #
# v1.7.0 — Genre cache
# --------------------------------------------------------------------------- #

GENRE_CACHE_TTL_DAYS = 7


def get_genre_cache(backend: str) -> list[str] | None:
    """Return cached genre list for `backend`, or None if missing/expired.

    Expiry = GENRE_CACHE_TTL_DAYS (7 days). Returns None on expiry so the
    caller knows to refresh; returns [] when the backend reported no genres.
    """
    with connection() as conn:
        meta = conn.execute(
            "SELECT updated_at FROM genre_cache_meta WHERE backend = ?",
            (backend,),
        ).fetchone()
        if not meta:
            return None
        try:
            updated = datetime.fromisoformat(meta["updated_at"])
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            return None
        if (datetime.now(timezone.utc) - updated).days >= GENRE_CACHE_TTL_DAYS:
            return None
        rows = conn.execute(
            "SELECT genre FROM genre_cache WHERE backend = ? ORDER BY genre",
            (backend,),
        ).fetchall()
        return [r["genre"] for r in rows]


def set_genre_cache(backend: str, genres: list[str]) -> None:
    """Overwrite the genre cache for `backend` and reset its timestamp."""
    with connection() as conn:
        conn.execute("DELETE FROM genre_cache WHERE backend = ?", (backend,))
        if genres:
            conn.executemany(
                "INSERT INTO genre_cache (backend, genre) VALUES (?, ?)",
                [(backend, g) for g in genres],
            )
        conn.execute(
            "INSERT OR REPLACE INTO genre_cache_meta (backend, updated_at) VALUES (?, ?)",
            (backend, datetime.now(timezone.utc).isoformat()),
        )


# --------------------------------------------------------------------------- #
# v1.8.0 — Smart playlist rules
# --------------------------------------------------------------------------- #


def list_rules(playlist_id: int) -> list[sqlite3.Row]:
    with connection() as conn:
        return list(conn.execute(
            "SELECT * FROM playlist_rules WHERE playlist_id = ? ORDER BY id",
            (playlist_id,),
        ).fetchall())


def add_rule(playlist_id: int, rule_type: str, operator: str, value: str) -> int:
    with connection() as conn:
        cur = conn.execute(
            "INSERT INTO playlist_rules (playlist_id, rule_type, operator, value) VALUES (?,?,?,?)",
            (playlist_id, rule_type, operator, value),
        )
        return int(cur.lastrowid)


def remove_rule(rule_id: int) -> None:
    with connection() as conn:
        conn.execute("DELETE FROM playlist_rules WHERE id = ?", (rule_id,))


def set_rule_mode(playlist_id: int, mode: str) -> None:
    assert mode in ("genre", "rules")
    with connection() as conn:
        conn.execute(
            "UPDATE managed_playlists SET rule_mode = ? WHERE id = ?",
            (mode, playlist_id),
        )


# --------------------------------------------------------------------------- #
# v2.0.0 — Settings store
# --------------------------------------------------------------------------- #


def get_setting(key: str) -> str | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT value FROM managed_settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO managed_settings (key, value) VALUES (?, ?)",
            (key, value),
        )


def update_playlist_stats(playlist_id: int, stats: dict) -> None:
    import json as _json
    with connection() as conn:
        conn.execute(
            "UPDATE managed_playlists SET last_stats = ? WHERE id = ?",
            (_json.dumps(stats), playlist_id),
        )


# --------------------------------------------------------------------------- #
# v2.1.0 — Outbound webhooks
# --------------------------------------------------------------------------- #


def list_webhooks() -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT id, url, label FROM webhooks ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def add_webhook(url: str, label: str = "") -> int:
    with connection() as conn:
        cur = conn.execute(
            "INSERT INTO webhooks (url, label) VALUES (?, ?)",
            (url.strip(), label.strip()),
        )
        return cur.lastrowid


def delete_webhook(webhook_id: int) -> None:
    with connection() as conn:
        conn.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,))


# ── v2.2.0 — Franchise definitions ────────────────────────────────────────────

def get_franchise_definition(key: str) -> dict | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM franchise_definitions WHERE key = ?", (key,)
        ).fetchone()
        return dict(row) if row else None


def upsert_franchise_definition(
    key: str,
    name: str,
    source: str,
    trakt_user: str | None,
    trakt_slug: str | None,
    chronolists_id: str | None = None,
    fetched_at: str = "",
    content_hash: str = "",
    item_count: int = 0,
    auto_discovered: int = 0,
    poster_url: str | None = None,
    poster_urls: str | None = None,
) -> int:
    with connection() as conn:
        existing = conn.execute(
            "SELECT id FROM franchise_definitions WHERE key = ?", (key,)
        ).fetchone()
        if existing:
            # COALESCE keeps an existing poster when this call passes None
            # (e.g. the hash-match early return that doesn't re-resolve items).
            conn.execute(
                """UPDATE franchise_definitions
                   SET name=?, source=?, trakt_user=?, trakt_slug=?,
                       chronolists_id=?,
                       fetched_at=?, content_hash=?, item_count=?,
                       poster_url=COALESCE(?, poster_url),
                       poster_urls=COALESCE(?, poster_urls)
                   WHERE key=?""",
                (name, source, trakt_user, trakt_slug, chronolists_id,
                 fetched_at, content_hash, item_count, poster_url,
                 poster_urls, key),
            )
            return existing["id"]
        else:
            cur = conn.execute(
                """INSERT INTO franchise_definitions
                   (key, name, source, trakt_user, trakt_slug,
                    chronolists_id, fetched_at, content_hash, item_count,
                    auto_discovered, poster_url, poster_urls)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (key, name, source, trakt_user, trakt_slug,
                 chronolists_id, fetched_at, content_hash, item_count,
                 auto_discovered, poster_url, poster_urls),
            )
            return cur.lastrowid


def list_franchise_definitions() -> list[dict]:
    with connection() as conn:
        rows = conn.execute("SELECT * FROM franchise_definitions").fetchall()
        return [dict(r) for r in rows]


def list_auto_discovered_franchise_definitions() -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM franchise_definitions WHERE auto_discovered = 1 ORDER BY name ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_playlists_by_franchise_definition(definition_id: int) -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM managed_playlists WHERE franchise_definition_id = ?",
            (definition_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ── v2.2.0 — Franchise items ─────────────────────────────────────────────────

def replace_franchise_items(definition_id: int, items: list[dict]) -> None:
    with connection() as conn:
        conn.execute(
            "DELETE FROM franchise_items WHERE definition_id = ?", (definition_id,)
        )
        conn.executemany(
            """INSERT INTO franchise_items
               (definition_id, rank, item_type, title, year,
                tmdb_id, tvdb_id, imdb_id,
                season_number, episode_number, show_title, show_tvdb_id, show_tmdb_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    definition_id,
                    item["rank"],
                    item["item_type"],
                    item["title"],
                    item.get("year"),
                    item.get("tmdb_id"),
                    item.get("tvdb_id"),
                    item.get("imdb_id"),
                    item.get("season_number"),
                    item.get("episode_number"),
                    item.get("show_title"),
                    item.get("show_tvdb_id"),
                    item.get("show_tmdb_id"),
                )
                for item in items
            ],
        )


def list_franchise_items(definition_id: int) -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM franchise_items WHERE definition_id = ? ORDER BY rank ASC",
            (definition_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ── v2.2.0 — Franchise match state ────────────────────────────────────────────

def upsert_franchise_match_state(
    franchise_item_id: int,
    playlist_id: int,
    plex_found: bool,
    plex_item_id: str | None,
    jellyfin_found: bool,
    jellyfin_item_id: str | None,
    emby_found: bool = False,
    emby_item_id: str | None = None,
) -> None:
    with connection() as conn:
        conn.execute(
            """INSERT INTO franchise_match_state
               (franchise_item_id, playlist_id,
                plex_found, plex_item_id,
                jellyfin_found, jellyfin_item_id,
                emby_found, emby_item_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(franchise_item_id, playlist_id) DO UPDATE SET
                 plex_found=excluded.plex_found,
                 plex_item_id=excluded.plex_item_id,
                 jellyfin_found=excluded.jellyfin_found,
                 jellyfin_item_id=excluded.jellyfin_item_id,
                 emby_found=excluded.emby_found,
                 emby_item_id=excluded.emby_item_id""",
            (franchise_item_id, playlist_id,
             int(plex_found), plex_item_id,
             int(jellyfin_found), jellyfin_item_id,
             int(emby_found), emby_item_id),
        )


def list_franchise_match_state(playlist_id: int) -> dict[int, dict]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM franchise_match_state WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchall()
        return {r["franchise_item_id"]: dict(r) for r in rows}


def get_franchise_definition_by_id(definition_id: int) -> dict | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM franchise_definitions WHERE id = ?", (definition_id,)
        ).fetchone()
        return dict(row) if row else None


def add_pruned_items(playlist_id: int, backend: str, item_ids) -> None:
    """Record media item ids pruned from a playlist on a backend (idempotent)."""
    rows = [(playlist_id, backend, str(i)) for i in item_ids if i]
    if not rows:
        return
    with connection() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO pruned_items (playlist_id, backend, item_id) "
            "VALUES (?, ?, ?)",
            rows,
        )


def get_pruned_item_ids(playlist_id: int, backend: str) -> set[str]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT item_id FROM pruned_items WHERE playlist_id = ? AND backend = ?",
            (playlist_id, backend),
        ).fetchall()
    return {str(r["item_id"]) for r in rows}


def clear_pruned_items(playlist_id: int) -> None:
    """Forget the pruned set (e.g. when pruning is turned off) so items return."""
    with connection() as conn:
        conn.execute("DELETE FROM pruned_items WHERE playlist_id = ?", (playlist_id,))


def set_franchise_definition_poster(
    definition_id: int, poster_url: str | None,
    poster_urls: str | None = None,
) -> None:
    """Persist poster art on a franchise definition (used by the startup
    backfill for definitions created before posters were stored). `poster_url`
    is the single representative cover; `poster_urls` is a JSON array of up to 5
    for the home-page card strip. Only non-None values are written."""
    sets = []
    params: list = []
    if poster_url is not None:
        sets.append("poster_url = ?")
        params.append(poster_url)
    if poster_urls is not None:
        sets.append("poster_urls = ?")
        params.append(poster_urls)
    if not sets:
        return
    params.append(definition_id)
    with connection() as conn:
        conn.execute(
            f"UPDATE franchise_definitions SET {', '.join(sets)} WHERE id = ?",
            params,
        )


def delete_franchise_definition(definition_id: int) -> None:
    with connection() as conn:
        conn.execute(
            "DELETE FROM franchise_items WHERE definition_id = ?", (definition_id,)
        )
        conn.execute(
            "DELETE FROM franchise_definitions WHERE id = ?", (definition_id,)
        )


def insert_franchise_definition(
    key: str,
    name: str,
    source: str,
    forked_from_key: str | None = None,
    content_hash: str = "",
    item_count: int = 0,
    poster_url: str | None = None,
    poster_urls: str | None = None,
) -> int:
    from datetime import datetime, timezone
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with connection() as conn:
        cur = conn.execute(
            """INSERT INTO franchise_definitions
               (key, name, source, forked_from_key, fetched_at, content_hash,
                item_count, poster_url, poster_urls)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (key, name, source, forked_from_key, fetched_at, content_hash,
             item_count, poster_url, poster_urls),
        )
        return cur.lastrowid


def count_playlists_by_franchise_definition(definition_id: int) -> int:
    with connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM managed_playlists WHERE franchise_definition_id = ?",
            (definition_id,),
        ).fetchone()
        return row["cnt"] if row else 0


def update_franchise_definition_metadata(
    definition_id: int, *, content_hash: str, item_count: int,
    poster_url: str | None = None, poster_urls: str | None = None,
) -> None:
    from datetime import datetime, timezone
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with connection() as conn:
        # COALESCE keeps existing posters when this call passes None.
        conn.execute(
            """UPDATE franchise_definitions
               SET content_hash = ?, item_count = ?, fetched_at = ?,
                   poster_url = COALESCE(?, poster_url),
                   poster_urls = COALESCE(?, poster_urls)
               WHERE id = ?""",
            (content_hash, item_count, fetched_at, poster_url, poster_urls,
             definition_id),
        )


def rebind_playlist_franchise(playlist_id: int, definition_id: int) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE managed_playlists SET franchise_definition_id = ? WHERE id = ?",
            (definition_id, playlist_id),
        )
