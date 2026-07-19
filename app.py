"""Flask web UI for managing rotating playlists across Plex and Jellyfin."""

from __future__ import annotations

__version__ = "3.8.1"

import logging
import os
import secrets

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

load_dotenv()

from datetime import timedelta  # noqa: E402

from urllib.parse import urlsplit  # noqa: E402

import auth  # noqa: E402
import db  # noqa: E402
import scheduler  # noqa: E402
import service  # noqa: E402
import json as _json  # noqa: E402
from media_client import (  # noqa: E402
    ALL_BACKENDS,
    available_backends,
    format_backend_set,
    get_client,
    normalize_title,
    parse_backend_set,
    primary_backend,
    titles_match,
)
from rotation import VALID_SORT_MODES  # noqa: E402
from service import ShowConfig  # noqa: E402
import webhooks as _webhooks  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("app")


# --------------------------------------------------------------------------- #
# Show aggregation across configured backends
# --------------------------------------------------------------------------- #


_BACKEND_DISPLAY = {"plex": "Plex", "jellyfin": "Jellyfin", "emby": "Emby"}

_UNREACHABLE_EXC_NAMES = {
    "ConnectTimeout", "ConnectTimeoutError", "ConnectionError",
    "ReadTimeout", "Timeout", "MaxRetryError", "NewConnectionError",
}


def _backend_unreachable_message(backend: str, exc: Exception) -> str:
    """Turn a list-shows/list-genres failure into a user-facing one-liner."""
    name = _BACKEND_DISPLAY.get(backend, backend.capitalize())
    if type(exc).__name__ in _UNREACHABLE_EXC_NAMES:
        return (f"Couldn't reach {name} — the connection timed out or was refused. "
                f"Check its URL in Settings and that the server is reachable from the "
                f"Linearr container.")
    short = str(exc).strip().splitlines()[0][:160] if str(exc).strip() else type(exc).__name__
    return f"Couldn't list from {name}: {short}"


def _is_cross_site(origin: str | None, referer: str | None, host: str) -> bool:
    """True if a browser-issued request demonstrably came from another site.

    CSRF guard for a LAN app where auth is off by default: a malicious page in
    the user's browser can otherwise fire blind form POSTs at the instance.
    `Origin` wins when present (modern browsers send it on every cross-origin
    POST; literal "null" = sandboxed iframe -> treat as cross-site). Falls back
    to `Referer`. Neither header present -> not provably cross-site (curl,
    scripts, very old browsers) -> allowed.
    """
    src = origin or referer
    if not src:
        return False
    if src == "null":
        return True
    netloc = urlsplit(src).netloc
    if not netloc:
        return True  # malformed header — fail closed
    return netloc.lower() != host.lower()


def _sniff_image(data: bytes) -> str | None:
    """Return 'png' | 'jpg' | 'webp' | None from magic bytes."""
    if data[:4] == b"\x89PNG":
        return "png"
    if data[:2] == b"\xff\xd8":
        return "jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _wants_json() -> bool:
    """True when the request came from static/linearr.js (AJAX form layer)."""
    return request.headers.get("X-Linearr-Ajax") == "1"


def _playlist_action(playlist_id, fn, ok_msg, fail_label, changed=True):
    """Run a playlist mutation and answer in the caller's dialect.

    Native form post -> flash + redirect back to the playlist (existing UX).
    linearr.js fetch (X-Linearr-Ajax: 1) -> JSON {ok, message, changed}; the
    client toasts `message` and reloads the page iff `changed`.

    `ok_msg` and `changed` may be callables taking fn's return value.
    """
    try:
        result = fn()
    except Exception as e:
        log.exception("%s failed", fail_label)
        if _wants_json():
            return jsonify({"ok": False, "message": f"{fail_label} failed: {e}"}), 500
        flash(f"{fail_label} failed: {e}", "error")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))
    msg = ok_msg(result) if callable(ok_msg) else ok_msg
    did_change = changed(result) if callable(changed) else changed
    if _wants_json():
        return jsonify({"ok": True, "message": msg, "changed": bool(did_change)})
    flash(msg, "ok")
    return redirect(url_for("view_playlist", playlist_id=playlist_id))


def _aggregated_shows() -> tuple[list[dict], list[tuple[str, str]]]:
    """List every show across configured backends, deduplicated by
    title+year. Each row carries `plex_rating_key` and `jellyfin_rating_key`
    (each nullable) plus a `backends` set indicating which backends have it.

    Single-backend installs get the same list shape that `client.list_all_shows()`
    returns, just wrapped in dicts.

    Returns (rows, errors) where errors is a list of (backend, message) for
    backends that failed to list.
    """
    backends = available_backends()
    out: list[dict] = []
    errors: list[tuple[str, str]] = []
    # Primary key: normalized title (year disambiguates reboots).
    # Secondary key: any shared provider id (TVDB/TMDB/IMDB) — catches title
    # discrepancies between backends (e.g. "Yellowstone (2018)" on Plex ↔
    # "Yellowstone" on Jellyfin) even when they were scraped with different
    # metadata agents.
    seen: dict[str, list[int]] = {}  # normalized title -> indices in out
    id_seen: dict = {}               # (idtype, idval) -> index in out
    link_map = db.get_manual_show_link_map()  # (backend, id) -> group_key
    group_seen: dict = {}            # group_key -> index in out

    for backend in backends:
        try:
            shows = get_client(backend).list_all_shows()
        except Exception as exc:
            log.exception("Failed to list shows on %s", backend)
            errors.append((backend, _backend_unreachable_message(backend, exc)))
            continue
        for s in shows:
            nk = normalize_title(s.title)
            grp = link_map.get((backend, str(s.rating_key)))

            # 0. User-asserted manual same-show link wins (explicit override).
            match_idx: int | None = None
            if grp is not None and grp in group_seen:
                match_idx = group_seen[grp]

            # 1. Try title+year match (existing logic).
            if match_idx is None:
                for idx in seen.get(nk, []):
                    existing = out[idx]
                    # Only split into separate entries when both sides carry a
                    # non-None year and they differ.
                    if (
                        existing["year"] is not None
                        and s.year is not None
                        and existing["year"] != s.year
                    ):
                        continue
                    match_idx = idx
                    break

            # 2. Fall back to any shared provider id if titles didn't match.
            if match_idx is None:
                for pid in service._show_id_set(s):
                    if pid in id_seen:
                        match_idx = id_seen[pid]
                        break

            if match_idx is not None:
                existing = out[match_idx]
                existing[f"{backend}_rating_key"] = s.rating_key
                existing["backends"].add(backend)
                if not existing["year"] and s.year:
                    existing["year"] = s.year
                if not existing["thumb"] and s.thumb:
                    existing["thumb"] = s.thumb
                    existing["thumb_backend"] = backend
                # Index this backend's title too, and record its provider ids.
                if nk not in seen or match_idx not in seen[nk]:
                    seen.setdefault(nk, []).append(match_idx)
                for pid in service._show_id_set(s):
                    id_seen.setdefault(pid, match_idx)
                if grp is not None:
                    group_seen.setdefault(grp, match_idx)
                continue

            # New entry.
            new_idx = len(out)
            row = {
                "rating_key": s.rating_key,
                "title": s.title,
                "year": s.year,
                "library": s.library,
                "thumb": s.thumb,
                "thumb_backend": backend if s.thumb else None,
                "plex_rating_key": s.rating_key if backend == "plex" else None,
                "jellyfin_rating_key": s.rating_key if backend == "jellyfin" else None,
                "emby_rating_key": s.rating_key if backend == "emby" else None,
                "backends": {backend},
            }
            seen.setdefault(nk, []).append(new_idx)
            for pid in service._show_id_set(s):
                id_seen.setdefault(pid, new_idx)
            if grp is not None:
                group_seen.setdefault(grp, new_idx)
            out.append(row)

    out.sort(key=lambda r: r["title"].lower())
    return out, errors


def _lookup_show_record(aggregated: list[dict], rating_key: str) -> dict | None:
    """Find an aggregated-show row by its source-backend rating_key."""
    for r in aggregated:
        if r["rating_key"] == rating_key:
            return r
        # Same record might be addressed via any backend ID:
        if (r.get("plex_rating_key") == rating_key
                or r.get("jellyfin_rating_key") == rating_key
                or r.get("emby_rating_key") == rating_key):
            return r
    return None


# Module-level cache of per-backend library lookup dicts for franchise preview
# matching. TTL = 60s. Cleared automatically when expired.
_franchise_lib_cache: dict[str, tuple[float, dict]] = {}
_FRANCHISE_LIB_CACHE_TTL = 60.0


def _franchise_library_cache(backend: str) -> tuple[dict | None, str | None]:
    """(data, error). Return a cached full backend match-cache for `backend`,
    rebuilding if missing or stale.

    This is the SAME cache shape the real franchise matcher uses
    (`service._build_backend_cache`) — including a per-show `episode_cache` —
    so the preview resolves the actual episode/movie items rather than merely
    checking whether a show exists. The cache (and its accumulated
    episode_cache) is held for `_FRANCHISE_LIB_CACHE_TTL` so previewing several
    franchises in succession stays fast. On build failure returns (None, message)
    and does NOT cache the failure, so the next call retries.
    """
    import time as _time
    now = _time.monotonic()
    cached = _franchise_lib_cache.get(backend)
    if cached and (now - cached[0]) < _FRANCHISE_LIB_CACHE_TTL:
        return cached[1], None
    try:
        data = service._build_backend_cache(backend, get_client(backend))
    except Exception as exc:
        log.warning("franchise lib cache rebuild failed for %s", backend, exc_info=True)
        return None, _backend_unreachable_message(backend, exc)
    _franchise_lib_cache[backend] = (now, data)
    return data, None


# Module-level cache of fetched Trakt list items. TTL = 5 minutes.
_trakt_list_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_TRAKT_LIST_CACHE_TTL = 300.0


def _cached_trakt_list_items(user: str, slug: str) -> list[dict]:
    """Fetch a Trakt list via TraktClient, caching results for 5 minutes."""
    import time as _time
    now = _time.monotonic()
    key = (user, slug)
    cached = _trakt_list_cache.get(key)
    if cached and (now - cached[0]) < _TRAKT_LIST_CACHE_TTL:
        return cached[1]
    from trakt_client import get_trakt_client
    items = get_trakt_client().fetch_list_items(user, slug)
    _trakt_list_cache[key] = (now, items)
    return items


def _load_prebaked_franchises() -> list[dict]:
    path = os.path.join(os.path.dirname(__file__), "defaults", "franchises.json")
    try:
        with open(path) as f:
            return _json.load(f)
    except (FileNotFoundError, _json.JSONDecodeError):
        return []


def _merged_franchise_list() -> list[dict]:
    static = _load_prebaked_franchises()
    db_defs = {d["key"]: d for d in db.list_franchise_definitions()}

    merged = []
    for f in static:
        key = f["key"]
        db_defn = db_defs.get(key)
        if (db_defn
                and db_defn.get("source") == "chronolists"
                and f.get("source") != "chronolists"):
            f = dict(f)
            f["source"] = "chronolists"
            f["chronolists_id"] = db_defn.get("chronolists_id")
            f["trakt_user"] = None
            f["trakt_slug"] = None
        # Prefer the precomputed static poster; fall back to a poster resolved
        # and stored on the DB definition (e.g. after first fetch).
        if not f.get("poster") and db_defn and db_defn.get("poster_url"):
            f = dict(f)
            f["poster"] = db_defn["poster_url"]
        merged.append(f)

    static_keys = {f["key"] for f in static}
    for d in db.list_auto_discovered_franchise_definitions():
        if d["key"] not in static_keys:
            merged.append({
                "key": d["key"],
                "name": d["name"],
                "source": "chronolists",
                "chronolists_id": d.get("chronolists_id"),
                "trakt_user": None,
                "trakt_slug": None,
                "poster": d.get("poster_url"),
            })

    return merged


def _parse_excluded_form_value(raw: str) -> set[tuple[int, int]]:
    """Parse the hidden `exclude_<rk>` form field ('S:E,S:E,...') into a set."""
    out: set[tuple[int, int]] = set()
    for token in (raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            s_str, e_str = token.split(":", 1)
            out.add((int(s_str), int(e_str)))
        except ValueError:
            continue
    return out


def _parse_configs_from_form(
    form,
    show_keys: list[str],
    aggregated: list[dict] | None = None,
) -> list[ShowConfig]:
    """Pull per-show fields out of the configure form.

    Field names: start_<rk>, end_<rk>, specials_<rk>, include_movies_<rk>,
    movies_<rk> (multi-valued — Plex movie IDs),
    jf_movies_<rk> (multi-valued — Jellyfin movie IDs),
    emby_movies_<rk> (multi-valued — Emby movie IDs).
    """
    configs: list[ShowConfig] = []
    for rk in show_keys:
        start = form.get(f"start_{rk}", "1")
        end = form.get(f"end_{rk}", "")
        specials = form.get(f"specials_{rk}", "")
        inc_movies = form.get(f"include_movies_{rk}", "")
        selected_movies = form.getlist(f"movies_{rk}")
        selected_jf_movies = form.getlist(f"jf_movies_{rk}")
        selected_emby_movies = form.getlist(f"emby_movies_{rk}")
        try:
            start_i = max(1, int(start))
        except ValueError:
            start_i = 1
        try:
            end_i = int(end) if end.strip() else None
        except ValueError:
            end_i = None
        if end_i is not None and end_i < start_i:
            end_i = None

        # Aggregated lookup gives us backend IDs. When no aggregated list,
        # fall back to ShowConfig's __post_init__ auto-fill.
        plex_id = None
        jf_id = None
        emby_id = None
        title = ""
        thumb = None
        if aggregated:
            rec = _lookup_show_record(aggregated, rk)
            if rec:
                plex_id = rec.get("plex_rating_key")
                jf_id = rec.get("jellyfin_rating_key")
                emby_id = rec.get("emby_rating_key")
                title = rec.get("title") or ""
                thumb = rec.get("thumb")

        # Single-backend installs don't build an aggregated cross-backend list,
        # so the picked rating_key IS that backend's own item id. Mirror it into
        # the right slot; otherwise the config has no id on any backend and every
        # episode/preview/create path filters it out → "0 episodes" (issue #5).
        if not (plex_id or jf_id or emby_id):
            _avail = available_backends()
            if len(_avail) == 1:
                _be = _avail[0]
                if _be == "plex":
                    plex_id = rk
                elif _be == "jellyfin":
                    jf_id = rk
                elif _be == "emby":
                    emby_id = rk

        excluded = _parse_excluded_form_value(form.get(f"exclude_{rk}", ""))
        try:
            weight_i = max(1, int(form.get(f"weight_{rk}", "1") or "1"))
        except ValueError:
            weight_i = 1
        configs.append(
            ShowConfig(
                rating_key=rk,
                title=title,
                thumb=thumb,
                start_season=start_i,
                end_season=end_i,
                include_specials=bool(specials),
                include_movies=bool(inc_movies),
                movie_rating_keys=[k for k in selected_movies if k],
                plex_rating_key=plex_id,
                jellyfin_rating_key=jf_id,
                emby_rating_key=emby_id,
                jellyfin_movie_rating_keys=[k for k in selected_jf_movies if k],
                emby_movie_rating_keys=[k for k in selected_emby_movies if k],
                excluded_episodes=excluded,
                weight=weight_i,
            )
        )
    return configs


def _missing_side_shows(
    configs: list[ShowConfig], backend_choice: str, available: list[str]
) -> list[dict]:
    if len(available) < 2:
        return []
    out: list[dict] = []
    BACKEND_LABEL = {"plex": "Plex", "jellyfin": "Jellyfin", "emby": "Emby"}
    target_backends = parse_backend_set(backend_choice)
    for tb in target_backends:
        label = BACKEND_LABEL.get(tb, tb.capitalize())
        for c in configs:
            if c.id_for(tb) is None:
                out.append({
                    "title": c.title or c.rating_key,
                    "missing": label,
                    "rating_key": c.rating_key,
                })
    return out


def _gather_season_meta(configs: list[ShowConfig], primary_be: str) -> dict:
    backends = available_backends()
    clients: dict[str, object] = {}
    for be in backends:
        try:
            clients[be] = get_client(be)
        except Exception:
            clients[be] = None

    out: dict = {}
    for cfg in configs:
        for be in ALL_BACKENDS:
            if be not in clients or clients[be] is None:
                continue
            target_id = cfg.id_for(be)
            if not target_id:
                continue

            try:
                seasons = clients[be].season_summaries(target_id)
            except Exception:
                log.warning("season_summaries failed on %s for %s", be, target_id, exc_info=True)
                continue
            if not seasons:
                continue

            try:
                summary = clients[be].get_show_summary(target_id)
            except Exception:
                log.warning("get_show_summary failed on %s for %s; using fallback title", be, target_id, exc_info=True)
                summary = None

            try:
                movies = clients[be].find_associated_movies(summary.title) if summary else []
            except Exception:
                log.warning("find_associated_movies failed on %s for %s", be, target_id, exc_info=True)
                movies = []

            out[cfg.rating_key] = {
                "summary": summary,
                "seasons": seasons,
                "movies": movies,
                "source_backend": be,
            }
            break
    return out


def _compute_display_titles(
    meta: dict, configs: list[ShowConfig], agg: list[dict] | None
) -> None:
    """Annotate each meta entry with a safe '_show_title' for templates, falling
    back through summary title → config title → aggregated-show title →
    rating_key when get_show_summary returned None.

    `agg` is the list returned by `_aggregated_shows()` (or None for single-
    backend installs). Each row is a dict carrying `rating_key`, `title`, and
    the per-backend `*_rating_key` aliases; we index by every id a config might
    reference."""
    configs_by_rk = {c.rating_key: c for c in configs}
    agg_title_by_rk: dict[str, str] = {}
    for row in agg or []:
        title = row.get("title")
        if not title:
            continue
        for key in ("rating_key", "plex_rating_key",
                    "jellyfin_rating_key", "emby_rating_key"):
            rid = row.get(key)
            if rid:
                agg_title_by_rk.setdefault(rid, title)
    for rk, m in meta.items():
        if m.get("summary") and m["summary"].title:
            m["_show_title"] = m["summary"].title
        else:
            cfg = configs_by_rk.get(rk)
            if cfg and cfg.title:
                m["_show_title"] = cfg.title
            elif rk in agg_title_by_rk:
                m["_show_title"] = agg_title_by_rk[rk]
            else:
                m["_show_title"] = rk


def _backend_from_form(form, available: list[str]) -> str:
    """Extract backend value from form (checkbox list or legacy single value).
    Falls back to primary backend when nothing selected."""
    values = form.getlist("backend")
    if not values:
        single = form.get("backend", "").strip()
        if single:
            values = [single]
    if values:
        return format_backend_set(values)
    return available[0] if available else "plex"


def _next_auto_name(existing: list[str]) -> str:
    existing_set = set(existing)
    for i in range(1, 1000):
        candidate = f"Linearr {i:03d}"
        if candidate not in existing_set:
            return candidate
    return "Linearr Playlist"


def _infer_thumb_backend(ref: str, explicit: str | None, available: list[str]) -> str:
    """Pick the backend for a /thumb ref. Explicit b= wins; Plex refs start with
    '/'; a bare GUID (Jellyfin/Emby) falls back to the first configured non-Plex
    backend so single-backend Emby installs resolve (was hardcoded 'jellyfin',
    which 404s when Jellyfin isn't configured)."""
    if explicit in ("plex", "jellyfin", "emby"):
        return explicit
    if ref.startswith("/"):
        return "plex"
    return next((b for b in ("jellyfin", "emby") if b in available), "jellyfin")


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #


def create_app() -> Flask:
    app = Flask(__name__)
    # Session cookie hardening for the optional login (v3.3.0). Not Secure-only
    # since most installs are http on the LAN.
    app.permanent_session_lifetime = timedelta(days=30)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

    db.init_db()

    # Session signing key (v3.3.3): env FLASK_SECRET → persisted secret →
    # auto-generated+persisted. Never the publicly-known dev default, which would
    # let anyone forge an authenticated session cookie. Resolved AFTER init_db
    # since it may read/write managed_settings.
    app.secret_key = auth.resolve_session_secret()

    # Optional web-UI auth (default off). Honor LINEARR_AUTH_PASSWORD env reset.
    auth.apply_env_reset()

    # Ensure an API key exists (generate once, persist).
    _env_key = os.environ.get("LINEARR_API_KEY", "").strip()
    if _env_key:
        db.set_setting("api_key", _env_key)
    elif not db.get_setting("api_key"):
        db.set_setting("api_key", secrets.token_urlsafe(32))

    scheduler.start()

    # Make available_backends visible to all templates (used by the picker
    # and badges to decide what to render).
    @app.context_processor
    def _inject_backends():
        return {
            "AVAILABLE_BACKENDS": available_backends(),
            "APP_VERSION": __version__,
            "AUTH_ENABLED": auth.auth_enabled(),
        }

    # ------------------------------------------------------------------ #
    # Cross-site write protection (v3.5.0). Browsers attach Origin/Referer
    # to form posts; if one is present and points at another site, reject.
    # /api/v1/* is exempt (keyed Bearer auth, called by external scripts).
    # ------------------------------------------------------------------ #
    @app.before_request
    def _block_cross_site_writes():
        if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
            return
        if request.path.startswith("/api/v1/"):
            return
        if os.environ.get("LINEARR_DISABLE_ORIGIN_CHECK", "").strip() == "1":
            return  # escape hatch for proxies that rewrite the Host header
        if _is_cross_site(
            request.headers.get("Origin"),
            request.headers.get("Referer"),
            request.host,
        ):
            log.warning(
                "Blocked cross-site %s to %s (Origin=%r, Referer=%r)",
                request.method, request.path,
                request.headers.get("Origin"), request.headers.get("Referer"),
            )
            abort(403, description="Cross-site request blocked.")

    # ------------------------------------------------------------------ #
    # Optional web-UI login (v3.3.0). Default off — active only once a
    # password is set. Protects every page except the login/logout routes,
    # static assets, and /api/* (which keeps its own API-key auth).
    # ------------------------------------------------------------------ #
    @app.before_request
    def _require_login():
        if not auth.auth_enabled():
            return
        if request.endpoint in ("login", "logout", "static"):
            return
        # Only the keyed REST API bypasses the session (it has its own Bearer
        # auth and is called by external scripts). Internal /api/ endpoints
        # (episodes/genres/preview/franchise), used by the logged-in UI's own
        # JS, still require a session when login is enabled.
        if request.path.startswith("/api/v1/"):
            return
        if not session.get("authed"):
            nxt = request.full_path if request.query_string else request.path
            return redirect(url_for("login", next=nxt))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not auth.auth_enabled():
            return redirect(url_for("index"))
        if session.get("authed"):
            return redirect(url_for("index"))
        nxt = request.args.get("next", "")
        if request.method == "POST":
            ip = request.remote_addr or "?"
            if not auth.throttle_ok(ip):
                flash("Too many attempts. Wait a minute and try again.", "error")
                return render_template("login.html", next=nxt), 429
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            if auth.verify(username, password):
                auth.throttle_clear(ip)
                session["authed"] = True
                session.permanent = True
                dest = request.form.get("next") or nxt
                return redirect(dest if auth.is_safe_next(dest) else url_for("index"))
            auth.throttle_record_failure(ip)
            flash("Invalid username or password.", "error")
        return render_template("login.html", next=nxt)

    @app.route("/logout", methods=["POST"])
    def logout():
        session.pop("authed", None)
        flash("Signed out.", "ok")
        return redirect(url_for("login") if auth.auth_enabled() else url_for("index"))

    # ------------------------------------------------------------------ #
    # Auth helper for REST API
    # ------------------------------------------------------------------ #
    def _api_key_required(f):
        from functools import wraps
        @wraps(f)
        def wrapper(*args, **kwargs):
            expected = db.get_setting("api_key") or ""
            if not expected:
                return jsonify({"error": "API key not configured"}), 503
            auth_header = request.headers.get("Authorization", "")
            token = ""
            if auth_header.startswith("Bearer "):
                token = auth_header[7:].strip()
            if not token:
                token = (request.args.get("api_key") or "").strip()
            if not secrets.compare_digest(token, expected):
                return jsonify({"error": "Unauthorized"}), 401
            return f(*args, **kwargs)
        return wrapper

    # ------------------------------------------------------------------ #
    # Thumb proxy — dispatches by thumb reference shape
    # ------------------------------------------------------------------ #
    @app.route("/thumb")
    def thumb():
        ref = request.args.get("path") or ""
        if not ref:
            abort(400)
        explicit_backend = request.args.get("b")
        try:
            w = int(request.args.get("w", 240))
            h = int(request.args.get("h", 360))
        except ValueError:
            w, h = 240, 360

        # Dispatch: explicit b= wins; otherwise infer from shape.
        # Plex thumb refs start with '/'. Emby/Jellyfin refs are bare GUIDs.
        avail = available_backends()
        backend = _infer_thumb_backend(ref, explicit_backend, avail)

        # Build the try-order. For a bare-GUID ref (not a Plex '/...' path),
        # Jellyfin and Emby ids are indistinguishable 32-char GUIDs, so the
        # inferred backend can be wrong. Fall back to the other configured
        # non-Plex backend before giving up — fixes posters for shows whose
        # thumb was stored from a different backend than inference guesses
        # (e.g. genre shows tagged only on Emby; existing playlists too).
        candidates = [backend] if backend in avail else []
        if not ref.startswith("/"):
            for alt in ("jellyfin", "emby"):
                if alt in avail and alt not in candidates:
                    candidates.append(alt)
        if not candidates:
            abort(404)

        for be in candidates:
            try:
                data, ctype = get_client(be).fetch_image(ref, width=w, height=h)
            except Exception:
                continue
            resp = Response(data, mimetype=ctype)
            resp.headers["Cache-Control"] = "public, max-age=3600"
            return resp

        log.warning("thumb fetch failed: %s (tried %s)", ref, candidates)
        abort(502)

    # ------------------------------------------------------------------ #
    # Episodes list (JSON) — used by the per-episode exclusion picker
    # ------------------------------------------------------------------ #
    @app.route("/api/episodes/<rk>")
    def api_episodes(rk: str):
        backend = request.args.get("b") or (available_backends() or ["plex"])[0]
        if backend not in available_backends():
            abort(400)
        try:
            eps = get_client(backend).episodes_for_show(
                rk, start_season=1, end_season=None, include_specials=True
            )
        except Exception:
            log.exception("api_episodes failed for %s on %s", rk, backend)
            abort(502)
        return jsonify([
            {"season": e.season, "episode": e.episode, "title": e.title,
             "air_date": e.air_date}
            for e in eps
        ])

    # ------------------------------------------------------------------ #
    # Genre list (JSON) — used by the genre picker in new_genre.html
    # ------------------------------------------------------------------ #
    @app.route("/api/genres")
    def api_genres():
        result: dict[str, list[str]] = {}
        errors: dict[str, str] = {}
        for backend in available_backends():
            cached = db.get_genre_cache(backend)
            if cached is None:
                try:
                    genres = get_client(backend).list_all_genres()
                    db.set_genre_cache(backend, genres)
                    cached = genres
                except Exception as exc:
                    log.exception("live genre fetch failed for %s", backend)
                    errors[backend] = _backend_unreachable_message(backend, exc)
                    cached = []
            result[backend] = cached
        return jsonify({"genres": result, "errors": errors})

    # ------------------------------------------------------------------ #
    # AJAX preview (returns rendered HTML partial)
    # ------------------------------------------------------------------ #
    @app.route("/api/preview", methods=["POST"])
    @app.route("/api/preview/<int:playlist_id>", methods=["POST"])
    def api_preview(playlist_id: int | None = None):
        show_keys = request.form.getlist("shows")
        # Aggregated lookup only needed when both backends are configured.
        agg = _aggregated_shows()[0] if len(available_backends()) > 1 else None
        configs = _parse_configs_from_form(request.form, show_keys, aggregated=agg)

        if playlist_id is not None:
            view = service.get_playlist_view(playlist_id)
            if not view:
                return ("", 404)
            sort_mode = view.sort_mode
            unwatched_only = view.unwatched_only
            preview_backend = primary_backend(view.backend)
            block_size = view.block_size
            shuffle_seed = view.shuffle_seed
            existing_rows = db.list_shows(playlist_id)
            existing_configs = [service._config_from_row(r) for r in existing_rows]
            all_configs = existing_configs + configs
        else:
            sort_mode = request.form.get("sort_mode", "rotation")
            if sort_mode not in VALID_SORT_MODES:
                sort_mode = "rotation"
            unwatched_only = bool(request.form.get("unwatched_only"))
            target_backend = _backend_from_form(request.form, available_backends())
            preview_backend = primary_backend(target_backend)
            try:
                block_size = max(1, int(request.form.get("block_size", "1") or "1"))
            except ValueError:
                block_size = 1
            # Preview uses a stable seed during a single editing session so
            # the user doesn't see a fresh random order on every keystroke.
            shuffle_seed = 12345 if sort_mode == "shuffle_chronological" else None
            all_configs = configs

        try:
            preview = service.preview_playlist(
                all_configs,
                limit=2000,
                sort_mode=sort_mode,
                unwatched_only=unwatched_only,
                backend=preview_backend,
                block_size=block_size,
                shuffle_seed=shuffle_seed,
            )
        except Exception:
            log.exception("api_preview failed")
            preview = []
        return render_template("_preview_partial.html", preview=preview)

    # -- v3.7.0 live playlist order ----------------------------------- #
    @app.route("/api/playlist/<int:playlist_id>/order")
    def api_playlist_order(playlist_id: int):
        row = db.get_playlist(playlist_id)
        if not row:
            abort(404)
        backend = request.args.get("backend") or primary_backend(
            row["backend"] if "backend" in row.keys() else "plex")
        entries = service.get_live_playlist_order(playlist_id, backend)
        return render_template("_order_partial.html", entries=entries,
                               backend=backend, truncated=(entries is not None and len(entries) == 2000))

    # ------------------------------------------------------------------ #
    # Index
    # ------------------------------------------------------------------ #
    @app.route("/")
    def index():
        views = service.list_playlist_views()
        tmdb_key_set = bool(db.get_setting("tmdb_api_key") or os.environ.get("TMDB_API_KEY"))
        return render_template(
            "index.html", playlists=views, tmdb_key_set=tmdb_key_set,
        )

    # ------------------------------------------------------------------ #
    # Create playlist: type picker → pick → configure → commit
    # ------------------------------------------------------------------ #
    @app.route("/new", methods=["GET"])
    def new_type():
        return render_template("new_type.html")

    @app.route("/new/show", methods=["GET"])
    def new_show_playlist():
        backends = available_backends()
        with db.connection() as conn:
            existing_names = [r[0] for r in conn.execute(
                "SELECT name FROM managed_playlists").fetchall()]
        if not backends:
            flash("No backends configured. Set PLEX_URL+PLEX_TOKEN and/or "
                  "JELLYFIN_URL+JELLYFIN_USERNAME+JELLYFIN_PASSWORD and/or "
                  "EMBY_URL+EMBY_API_KEY.", "error")
            return render_template("new.html", shows=[], prev_name="", selected=set(),
                                   default_backend="plex", existing_names=existing_names)
        try:
            shows, agg_errors = _aggregated_shows()
            for _be, _msg in agg_errors:
                flash(_msg, "warning")
        except Exception as e:
            log.exception("listing shows failed")
            flash(f"Couldn't reach backend: {e}", "error")
            shows = []
        prev_name = request.args.get("name", "")
        selected = {k for k in request.args.get("selected", "").split(",") if k}
        return render_template(
            "new.html",
            shows=shows,
            prev_name=prev_name,
            selected=selected,
            default_backend=",".join(backends),
            existing_names=existing_names,
        )

    @app.route("/new/configure", methods=["POST"])
    def new_configure():
        show_keys = request.form.getlist("shows")
        if not show_keys:
            flash("Pick at least one show.", "error")
            return redirect(url_for("new_show_playlist"))

        with db.connection() as _conn:
            existing_names = [r[0] for r in _conn.execute(
                "SELECT name FROM managed_playlists").fetchall()]
        existing_names_set = set(existing_names)

        raw_name = (request.form.get("name") or "").strip()
        if not raw_name:
            for _i in range(1, 1000):
                candidate = f"Linearr {_i:03d}"
                if candidate not in existing_names_set:
                    raw_name = candidate
                    break
            else:
                raw_name = "Linearr Playlist"
        name = raw_name

        action = request.form.get("action", "preview")
        sort_mode = request.form.get("sort_mode", "rotation")
        if sort_mode not in VALID_SORT_MODES:
            sort_mode = "rotation"
        try:
            block_size = max(1, int(request.form.get("block_size", "1") or "1"))
        except ValueError:
            block_size = 1
        unwatched_only = bool(request.form.get("unwatched_only"))
        auto_sync = bool(request.form.get("auto_sync")) if "shows" in request.form else True
        pruning_enabled = max(0, min(1, int(request.form.get("pruning_enabled", "1") or "1")))

        backends = available_backends()
        backend_choice = _backend_from_form(request.form, backends)
        primary_be = primary_backend(backend_choice)

        if len(backends) > 1:
            agg, agg_errors = _aggregated_shows()
            for _be, _msg in agg_errors:
                flash(_msg, "warning")
        else:
            agg = None
        configs = _parse_configs_from_form(request.form, show_keys, aggregated=agg)
        meta = _gather_season_meta(configs, primary_be)
        _compute_display_titles(meta, configs, agg)
        missing_shows = _missing_side_shows(configs, backend_choice, backends)

        if action == "commit":
            try:
                pid = service.create_managed_playlist(
                    name, configs,
                    sort_mode=sort_mode,
                    unwatched_only=unwatched_only,
                    auto_sync=auto_sync,
                    backend=backend_choice,
                    block_size=block_size,
                    pruning_enabled=pruning_enabled,
                )
            except Exception as e:
                log.exception("create failed")
                flash(f"Failed to create playlist: {e}", "error")
                return render_template(
                    "configure.html",
                    mode="new",
                    form_action=url_for("new_configure"),
                    hidden={},
                    show_keys=show_keys,
                    meta=meta,
                    configs={c.rating_key: c for c in configs},
                    preview=[],
                    name=name,
                    sort_mode=sort_mode,
                    block_size=block_size,
                    unwatched_only=unwatched_only,
                    auto_sync=auto_sync,
                    backend=backend_choice,
                    pruning_enabled=pruning_enabled,
                    missing_shows=missing_shows,
                    preview_api_url=url_for("api_preview"),
                    existing_names=existing_names,
                )
            flash(f"Created '{name}'.", "ok")
            return redirect(url_for("view_playlist", playlist_id=pid))

        try:
            preview = service.preview_playlist(
                configs, limit=2000, sort_mode=sort_mode, unwatched_only=unwatched_only,
                backend=primary_be, block_size=block_size,
                shuffle_seed=12345 if sort_mode == "shuffle_chronological" else None,
            )
        except Exception:
            log.exception("preview failed")
            preview = []
        return render_template(
            "configure.html",
            mode="new",
            form_action=url_for("new_configure"),
            hidden={},
            show_keys=show_keys,
            meta=meta,
            configs={c.rating_key: c for c in configs},
            preview=preview,
            name=name,
            sort_mode=sort_mode,
            block_size=block_size,
            unwatched_only=unwatched_only,
            auto_sync=auto_sync,
            backend=backend_choice,
            pruning_enabled=pruning_enabled,
            missing_shows=missing_shows,
            preview_api_url=url_for("api_preview"),
            existing_names=existing_names,
        )

    # ------------------------------------------------------------------ #
    # Playlist detail
    # ------------------------------------------------------------------ #
    @app.route("/playlist/<int:playlist_id>")
    def view_playlist(playlist_id: int):
        view = service.get_playlist_view(playlist_id)
        if not view:
            abort(404)

        # Available-to-add list, filtered to exclude shows already in the playlist.
        try:
            agg, agg_errors = _aggregated_shows()
            for _be, _msg in agg_errors:
                flash(_msg, "warning")
        except Exception:
            agg = []
        existing_keys = {s["show_rating_key"] for s in view.shows}
        # Also exclude by existing backend id matches.
        existing_plex = {s.get("plex_show_item_id") for s in view.shows if s.get("plex_show_item_id")}
        existing_jf = {s.get("jellyfin_show_item_id") for s in view.shows if s.get("jellyfin_show_item_id")}
        existing_emby = {s.get("emby_show_item_id") for s in view.shows if s.get("emby_show_item_id")}
        available = [
            r for r in agg
            if r["rating_key"] not in existing_keys
            and r.get("plex_rating_key") not in existing_plex
            and r.get("jellyfin_rating_key") not in existing_jf
            and r.get("emby_rating_key") not in existing_emby
        ]
        # Full deduplicated list for the manual-link widget (needs shows on both sides).
        all_shows = agg

        # Build the missing-side warning data: any show that lacks an id
        # for a backend this playlist targets is reported.
        missing_on = []
        playlist_backends = parse_backend_set(view.backend)
        BACKEND_LABEL_MAP = {"plex": "Plex", "jellyfin": "Jellyfin", "emby": "Emby"}
        for s in view.shows:
            for be in playlist_backends:
                col = f"{be}_show_item_id"
                if not s.get(col):
                    missing_on.append({
                        "title": s["show_title"],
                        "missing": BACKEND_LABEL_MAP.get(be, be.capitalize()),
                        "rating_key": s["show_rating_key"],
                    })

        selected = {k for k in request.args.get("selected", "").split(",") if k}
        rules = db.list_rules(playlist_id) if view.playlist_type == "genre" else []
        import os as _os

        franchise_items = []
        if view.playlist_type == "franchise":
            row = db.get_playlist(playlist_id)
            definition_id = dict(row).get("franchise_definition_id") if row else None
            if definition_id:
                fi_list = db.list_franchise_items(definition_id)
                match_state = db.list_franchise_match_state(playlist_id)
                playlist_backends = parse_backend_set(view.backend)
                for fi in fi_list:
                    ms = match_state.get(fi["id"], {})
                    plex_found = bool(ms.get("plex_found", 0))
                    jellyfin_found = bool(ms.get("jellyfin_found", 0))
                    emby_found = bool(ms.get("emby_found", 0))

                    if len(playlist_backends) > 1:
                        be_found = [b for b in playlist_backends
                                    if {"plex": plex_found, "jellyfin": jellyfin_found, "emby": emby_found}.get(b)]
                        found = len(be_found) > 0
                        if len(be_found) == len(playlist_backends):
                            lib_status = "found"
                        elif len(be_found) == 0:
                            lib_status = "missing"
                        elif len(be_found) == 1 and be_found[0] == "plex":
                            lib_status = "plex_only"
                        elif len(be_found) == 1 and be_found[0] == "jellyfin":
                            lib_status = "jellyfin_only"
                        elif len(be_found) == 1 and be_found[0] == "emby":
                            lib_status = "emby_only"
                        else:
                            lib_status = "found"
                    elif playlist_backends[0] == "jellyfin":
                        found = jellyfin_found
                        lib_status = "found" if jellyfin_found else "missing"
                    elif playlist_backends[0] == "emby":
                        found = emby_found
                        lib_status = "found" if emby_found else "missing"
                    else:
                        found = plex_found
                        lib_status = "found" if plex_found else "missing"

                    display_title = fi["title"]
                    if fi["item_type"] == "episode" and fi.get("show_title"):
                        s = fi.get("season_number", 0)
                        e = fi.get("episode_number", 0)
                        display_title = f"{fi['show_title']} S{s:02d}E{e:02d} — {fi['title']}"
                    elif fi["item_type"] == "season" and fi.get("show_title"):
                        display_title = f"{fi['show_title']} — Season {fi['season_number']}"

                    franchise_items.append({
                        **fi,
                        "found": found,
                        "lib_status": lib_status,
                        "display_title": display_title,
                    })

        is_forked = False
        bundled_origin_name = None
        franchise_pick_options = []
        if view.playlist_type == "franchise":
            row = db.get_playlist(playlist_id)
            row_d = dict(row) if row else {}
            defn_id = row_d.get("franchise_definition_id")
            if defn_id:
                seen_titles: set[str] = set()
                for fi in fi_list:
                    if fi["item_type"] == "movie":
                        franchise_pick_options.append({"id": fi["id"], "title": fi["title"]})
                    else:
                        # episode / season / show — one entry per distinct show
                        key = str(fi.get("show_tmdb_id") or fi.get("show_title") or fi.get("title"))
                        if key and key not in seen_titles:
                            seen_titles.add(key)
                            franchise_pick_options.append({
                                "id": fi["id"],
                                "title": fi.get("show_title") or fi["title"],
                            })
                fr_defn = db.get_franchise_definition_by_id(defn_id)
                if fr_defn and fr_defn.get("forked_from_key"):
                    for f in _load_prebaked_franchises():
                        if f["key"] == fr_defn["forked_from_key"]:
                            is_forked = True
                            bundled_origin_name = f["name"]
                            break

        return render_template(
            "playlist.html",
            playlist=view,
            available=available,
            all_shows=all_shows,
            selected=selected,
            missing_on=missing_on,
            rules=rules,
            franchise_items=franchise_items,
            franchise_pick_options=franchise_pick_options,
            watched_keep=max(0, int(_os.environ.get("WATCHED_KEEP", "2"))),
            is_forked=is_forked,
            bundled_origin_name=bundled_origin_name,
        )

    # ------------------------------------------------------------------ #
    # Add to existing playlist: pick → configure → commit
    # ------------------------------------------------------------------ #
    @app.route("/playlist/<int:playlist_id>/add/configure", methods=["POST"])
    def add_configure(playlist_id: int):
        view = service.get_playlist_view(playlist_id)
        if not view:
            abort(404)
        show_keys = request.form.getlist("shows")
        if not show_keys:
            flash("Pick at least one show to add.", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))

        action = request.form.get("action", "preview")
        agg = _aggregated_shows()[0] if len(available_backends()) > 1 else None
        configs = _parse_configs_from_form(request.form, show_keys, aggregated=agg)
        primary_be = primary_backend(view.backend)
        meta = _gather_season_meta(configs, primary_be)
        _compute_display_titles(meta, configs, agg)
        missing_shows = _missing_side_shows(configs, view.backend, available_backends())

        if action == "commit":
            try:
                service.add_shows_to_playlist(playlist_id, configs)
            except Exception as e:
                log.exception("add failed")
                flash(f"Failed to add shows: {e}", "error")
                return redirect(url_for("view_playlist", playlist_id=playlist_id))
            flash(f"Added {len(configs)} show(s).", "ok")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))

        existing_rows = db.list_shows(playlist_id)
        existing_configs = [service._config_from_row(r) for r in existing_rows]
        try:
            preview = service.preview_playlist(
                existing_configs + configs,
                limit=2000,
                sort_mode=view.sort_mode,
                unwatched_only=view.unwatched_only,
                backend=primary_be,
                block_size=view.block_size,
                shuffle_seed=view.shuffle_seed,
            )
        except Exception:
            log.exception("preview failed")
            preview = []

        return render_template(
            "configure.html",
            mode="add",
            form_action=url_for("add_configure", playlist_id=playlist_id),
            hidden={},
            show_keys=show_keys,
            meta=meta,
            configs={c.rating_key: c for c in configs},
            preview=preview,
            name=view.name,
            playlist_id=playlist_id,
            sort_mode=view.sort_mode,
            block_size=view.block_size,
            unwatched_only=view.unwatched_only,
            auto_sync=view.auto_sync,
            backend=view.backend,
            missing_shows=missing_shows,
            preview_api_url=url_for("api_preview", playlist_id=playlist_id),
        )

    @app.route("/playlist/<int:playlist_id>/remove", methods=["POST"])
    def remove_show(playlist_id: int):
        show = (request.form.get("show") or "").strip()
        if not show:
            abort(400)
        try:
            service.remove_show_from_playlist(playlist_id, show)
        except Exception as e:
            log.exception("remove failed")
            flash(f"Failed to remove show: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        flash("Show removed.", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    _MODE_LABELS = {
        "rotation": "Rotation",
        "rotation_weighted": "Weighted Rotation",
        "rotation_blocks": "Block Scheduling",
        "air_date": "Air Date",
        "shuffle_chronological": "Shuffle",
    }

    @app.route("/playlist/<int:playlist_id>/sort_mode", methods=["POST"])
    def change_sort_mode(playlist_id: int):
        mode = (request.form.get("sort_mode") or "").strip()
        if mode not in VALID_SORT_MODES:
            abort(400)
        return _playlist_action(
            playlist_id,
            lambda: service.set_playlist_sort_mode(playlist_id, mode),
            ok_msg=f"Sort mode set to {_MODE_LABELS.get(mode, mode)}.",
            fail_label="Sort change",
        )

    @app.route("/playlist/<int:playlist_id>/block_size", methods=["POST"])
    def change_block_size(playlist_id: int):
        try:
            size = max(1, int(request.form.get("block_size", "1")))
        except ValueError:
            abort(400)
        return _playlist_action(
            playlist_id,
            lambda: service.set_playlist_block_size(playlist_id, size),
            ok_msg=f"Block size set to {size}.",
            fail_label="Block size change",
        )

    @app.route("/playlist/<int:playlist_id>/reshuffle", methods=["POST"])
    def reshuffle(playlist_id: int):
        return _playlist_action(
            playlist_id,
            lambda: service.reshuffle_playlist(playlist_id),
            ok_msg="Playlist reshuffled.",
            fail_label="Reshuffle",
        )

    # ------------------------------------------------------------------ #
    # Genre playlist creation (v1.4.0)
    # ------------------------------------------------------------------ #
    @app.route("/new/genre", methods=["GET"])
    def new_genre():
        backends = available_backends()
        if not backends:
            flash("No backends configured. Set credentials in your .env first.", "error")
            return redirect(url_for("index"))
        with db.connection() as conn:
            existing_names = [r[0] for r in conn.execute(
                "SELECT name FROM managed_playlists").fetchall()]
        return render_template(
            "new_genre.html",
            backends=backends,
            default_backend=("both" if len(backends) > 1 else backends[0]),
            matched_shows=None,
            prev_name=request.args.get("name", ""),
            prev_genres=request.args.get("genres", ""),
            prev_sort_mode=request.args.get("sort_mode", "rotation"),
            prev_rule_mode=request.args.get("rule_mode", "genre"),
            existing_names=existing_names,
        )

    @app.route("/new/genre", methods=["POST"])
    def new_genre_action():
        backends = available_backends()
        if not backends:
            abort(400)
        name = (request.form.get("name") or "").strip()
        genres_raw = (request.form.get("genres") or "").strip()
        genre_list = [g.strip() for g in genres_raw.split(",") if g.strip()]
        backend_choice = _backend_from_form(request.form, backends)
        sort_mode = request.form.get("sort_mode", "rotation")
        if sort_mode not in VALID_SORT_MODES:
            sort_mode = "rotation"
        try:
            block_size = max(1, int(request.form.get("block_size", "1") or "1"))
        except ValueError:
            block_size = 1
        unwatched_only = bool(request.form.get("unwatched_only"))
        auto_sync = bool(request.form.get("auto_sync")) if request.method == "POST" else True
        rule_mode = (request.form.get("rule_mode") or "genre").strip()

        action = request.form.get("action", "preview")
        target_backends = parse_backend_set(backend_choice)

        # Per-show weights submitted from the genre creation form (rotation_weighted only).
        weights_from_form: dict[str, int] = {}
        for k, v in request.form.items():
            if k.startswith("weight_") and v:
                rk = k[len("weight_"):]
                try:
                    weights_from_form[rk] = max(1, int(v))
                except (ValueError, TypeError):
                    pass

        matched_shows = None
        if rule_mode == "rules":
            rule_types = request.form.getlist("rule_type[]")
            rule_operators = request.form.getlist("rule_operator[]")
            rule_values = request.form.getlist("rule_value[]")
            rules = [
                {"rule_type": rt, "operator": (rule_operators[i] if i < len(rule_operators) else "include"), "value": rv}
                for i, (rt, rv) in enumerate(zip(rule_types, rule_values))
            ] if rule_types else []
            if rules:
                try:
                    configs = service._resolve_smart_shows(rules, target_backends)
                    matched_shows = [
                        {
                            "title": c.title,
                            "rating_key": c.rating_key,
                            "thumb": c.thumb,
                            "thumb_backend": c.thumb_backend or primary_backend(",".join(
                                b for b in ("plex","jellyfin","emby") if c.id_for(b)
                            ) or "plex"),
                            "plex": bool(c.plex_rating_key),
                            "jellyfin": bool(c.jellyfin_rating_key),
                            "emby": bool(c.emby_rating_key),
                        }
                        for c in configs
                    ]
                except Exception as e:
                    log.exception("smart rule preview failed")
                    flash(f"Couldn't resolve rules: {e}", "error")
        elif genre_list:
            try:
                configs = service._resolve_genre_shows(genre_list, target_backends)
                matched_shows = [
                    {
                        "title": c.title,
                        "rating_key": c.rating_key,
                        "thumb": c.thumb,
                        "thumb_backend": c.thumb_backend or primary_backend(",".join(
                            b for b in ("plex","jellyfin","emby") if c.id_for(b)
                        ) or "plex"),
                        "plex": bool(c.plex_rating_key),
                        "jellyfin": bool(c.jellyfin_rating_key),
                        "emby": bool(c.emby_rating_key),
                    }
                    for c in configs
                ]
            except Exception as e:
                log.exception("genre preview failed")
                flash(f"Couldn't resolve genres: {e}", "error")

        if action == "create":
            if not name:
                flash("Playlist name is required.", "error")
            elif rule_mode == "rules":
                rule_types = request.form.getlist("rule_type[]")
                rule_operators = request.form.getlist("rule_operator[]")
                rule_values = request.form.getlist("rule_value[]")
                rules_list = [
                    {"rule_type": rt, "operator": (rule_operators[i] if i < len(rule_operators) else "include"), "value": rv}
                    for i, (rt, rv) in enumerate(zip(rule_types, rule_values))
                ] if rule_types else []
                if not rules_list:
                    flash("Add at least one rule.", "error")
                else:
                    try:
                        configs = service._resolve_smart_shows(rules_list, target_backends)
                        if not configs:
                            flash("No shows match those rules. Try broadening the criteria.", "error")
                        else:
                            # Apply any per-show weights from the form.
                            if weights_from_form:
                                for cfg in configs:
                                    if cfg.rating_key in weights_from_form:
                                        cfg.weight = weights_from_form[cfg.rating_key]
                            pid = service.create_managed_playlist(
                                name, configs,
                                sort_mode=sort_mode,
                                unwatched_only=unwatched_only,
                                auto_sync=auto_sync,
                                backend=backend_choice,
                                block_size=block_size,
                            )
                            # Mark as genre type + persist rules.
                            with db.connection() as conn:
                                conn.execute(
                                    "UPDATE managed_playlists SET playlist_type = 'genre', rule_mode = 'rules' WHERE id = ?",
                                    (pid,),
                                )
                            for r in rules_list:
                                db.add_rule(pid, r["rule_type"], r["operator"], r["value"])
                    except Exception as e:
                        log.exception("smart rule create failed")
                        flash(f"Failed to create playlist: {e}", "error")
                    else:
                        flash(f"Created smart rules playlist '{name}'.", "ok")
                        return redirect(url_for("view_playlist", playlist_id=pid))
            elif not genre_list:
                flash("Enter at least one genre.", "error")
            else:
                try:
                    pid = service.create_genre_playlist(
                        name, genre_list,
                        sort_mode=sort_mode,
                        unwatched_only=unwatched_only,
                        auto_sync=auto_sync,
                        backend=backend_choice,
                        block_size=block_size,
                        weights=weights_from_form or None,
                    )
                except Exception as e:
                    log.exception("genre create failed")
                    flash(f"Failed to create genre playlist: {e}", "error")
                else:
                    flash(f"Created genre playlist '{name}' "
                          f"({len(matched_shows or [])} shows matched).", "ok")
                    return redirect(url_for("view_playlist", playlist_id=pid))

        with db.connection() as conn:
            existing_names = [r[0] for r in conn.execute(
                "SELECT name FROM managed_playlists").fetchall()]
        return render_template(
            "new_genre.html",
            backends=backends,
            default_backend=backend_choice,
            matched_shows=matched_shows,
            prev_name=name,
            prev_genres=genres_raw,
            prev_sort_mode=sort_mode,
            prev_block_size=block_size,
            prev_unwatched=unwatched_only,
            prev_auto_sync=auto_sync,
            prev_rule_mode=rule_mode,
            existing_names=existing_names,
        )

    # -- v2.2.0 franchise playlist creation ------------------------------- #

    @app.route("/new/franchise", methods=["GET", "POST"])
    def new_franchise():
        backends = available_backends()
        franchises = _merged_franchise_list()

        if request.method == "GET":
            existing_names = [
                r["name"] for r in db.list_playlists()
            ]
            return render_template(
                "new_franchise.html",
                franchises=franchises,
                backends=backends,
                existing_names=existing_names,
            )

        name = request.form.get("name", "").strip()
        backend = _backend_from_form(request.form, backends)
        franchise_key = request.form.get("franchise_key", "").strip()
        franchise_source = request.form.get("franchise_source", "trakt").strip()
        trakt_user = request.form.get("trakt_user", "").strip()
        trakt_slug = request.form.get("trakt_slug", "").strip()
        chronolists_id = request.form.get("chronolists_id", "").strip() or None
        franchise_name = request.form.get("franchise_name", "").strip()

        if not name:
            existing = [r["name"] for r in db.list_playlists()]
            name = _next_auto_name(existing)

        if franchise_source == "chronolists":
            if not chronolists_id:
                flash("Please select a franchise.")
                return redirect(url_for("new_franchise"))
        elif not trakt_user or not trakt_slug:
            flash("Please select a franchise or enter a valid Trakt list URL.")
            return redirect(url_for("new_franchise"))

        if not franchise_key:
            franchise_key = f"custom_{trakt_user}_{trakt_slug}"
        if not franchise_name:
            franchise_name = name

        try:
            playlist_id = service.create_franchise_playlist(
                name=name,
                backend=backend,
                franchise_key=franchise_key,
                source=franchise_source,
                trakt_user=trakt_user,
                trakt_slug=trakt_slug,
                chronolists_id=chronolists_id,
                franchise_name=franchise_name,
            )
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        except Exception as e:
            log.warning("create_franchise_playlist failed: %s", e, exc_info=True)
            flash(f"Error creating franchise playlist: {e}")
            return redirect(url_for("new_franchise"))

    @app.route("/api/franchise/preview")
    def api_franchise_preview():
        trakt_user = request.args.get("trakt_user", "").strip()
        trakt_slug = request.args.get("trakt_slug", "").strip()
        backend = request.args.get("backend", "plex")
        source = request.args.get("source", "trakt").strip()
        franchise_key = request.args.get("franchise_key", "").strip()
        chronolists_id = request.args.get("chronolists_id", "").strip()

        raw_items: list[dict] = []
        if source == "chronolists" and chronolists_id:
            try:
                from chronolists_client import get_chronolists_client
                raw_items = get_chronolists_client().fetch_list_items(chronolists_id)
            except Exception as e:
                return render_template(
                    "_franchise_preview_partial.html",
                    error=f"Could not fetch Chronolists list: {e}",
                    items=[], found_count=0, total_count=0, missing_count=0,
                )
        elif source == "local" and franchise_key:
            local_path = os.path.join(
                os.path.dirname(__file__), "defaults", "franchise_data", f"{franchise_key}.json"
            )
            try:
                with open(local_path) as _f:
                    raw_items = _json.load(_f).get("items", [])
            except FileNotFoundError:
                return render_template(
                    "_franchise_preview_partial.html",
                    error=f"Local franchise file not found: {franchise_key}.json",
                    items=[], found_count=0, total_count=0, missing_count=0,
                )
        else:
            if not trakt_user or not trakt_slug or trakt_user == "null" or trakt_slug == "null":
                return render_template(
                    "_franchise_preview_partial.html",
                    error="Missing trakt_user or trakt_slug.",
                    items=[], found_count=0, total_count=0, missing_count=0,
                )
            try:
                raw_items = _cached_trakt_list_items(trakt_user, trakt_slug)
            except Exception as e:
                return render_template(
                    "_franchise_preview_partial.html",
                    error=f"Could not fetch Trakt list: {e}",
                    items=[], found_count=0, total_count=0, missing_count=0,
                )

        # "both" is not a valid client key — check each configured backend
        backends_to_check = [b for b in parse_backend_set(backend) if b in available_backends()]

        # Per-backend full match-caches — cached at module level with 60s TTL
        # so previewing several franchises in succession is fast. Backends whose
        # cache build failed are dropped from be_caches so they count as "not
        # found", but the error message is surfaced to the user.
        be_caches: dict[str, dict] = {}
        cache_errors: list[str] = []
        for _be in backends_to_check:
            _cache, _err = _franchise_library_cache(_be)
            if _cache is not None:
                be_caches[_be] = _cache
            elif _err:
                cache_errors.append(_err)

        def _item_found_on(fi: dict, cache: dict) -> bool:
            # Delegate to the SAME resolution the real build uses, so the
            # preview reflects what will actually land in the playlist —
            # episode/season items are resolved to the specific episode(s),
            # not just "the show exists".
            if fi["item_type"] in ("show", "season"):
                return bool(service._expand_franchise_show_item(fi, cache))
            return service._resolve_franchise_item(fi, cache) is not None

        preview_items = []
        for fi in raw_items:
            found_on = {_be for _be, _cache in be_caches.items() if _item_found_on(fi, _cache)}
            both_configured = len(backends_to_check) > 1

            if not found_on:
                lib_status = "missing"
            elif both_configured and found_on == {"plex"}:
                lib_status = "plex_only"
            elif both_configured and found_on == {"jellyfin"}:
                lib_status = "jellyfin_only"
            elif both_configured and found_on == {"emby"}:
                lib_status = "emby_only"
            else:
                lib_status = "found"

            display_title = fi["title"]
            if fi["item_type"] == "episode" and fi.get("show_title"):
                s = fi.get("season_number", 0)
                e = fi.get("episode_number", 0)
                display_title = f"{fi['show_title']} S{s:02d}E{e:02d} — {fi['title']}"
            elif fi["item_type"] == "season" and fi.get("show_title"):
                display_title = f"{fi['show_title']} — Season {fi['season_number']}"

            preview_items.append({
                "item_type": fi["item_type"],
                "display_title": display_title,
                "year": fi.get("year"),
                "show_title": fi.get("show_title"),
                "found": bool(found_on),
                "lib_status": lib_status,
            })

        found_count = sum(1 for i in preview_items if i["found"])
        missing_count = len(preview_items) - found_count

        return render_template(
            "_franchise_preview_partial.html",
            error=None,
            items=preview_items,
            found_count=found_count,
            total_count=len(preview_items),
            missing_count=missing_count,
            library_warning=" ".join(cache_errors) if cache_errors else None,
        )

    # -- v2.3.0 franchise maker ----------------------------------------- #

    @app.route("/franchise-maker", methods=["GET"])
    def franchise_maker():
        backends = available_backends()
        existing_names = [r["name"] for r in db.list_playlists()]
        has_key = bool(db.get_setting("tmdb_api_key") or os.environ.get("TMDB_API_KEY"))
        tmdb_key_set_in_db = bool(db.get_setting("tmdb_api_key"))

        # v2.3.0 — optional preload from a bundled franchise or Trakt URL
        items: list[dict] = []
        franchise_name = ""
        forked_from_key = None
        bundled_origin_name = None
        is_bundled = False

        import_local = (request.args.get("import_local") or "").strip()
        import_trakt_user = (request.args.get("import_trakt_user") or "").strip()
        import_trakt_slug = (request.args.get("import_trakt_slug") or "").strip()
        import_chronolists = (request.args.get("import_chronolists") or "").strip()

        if import_local:
            for f in _load_prebaked_franchises():
                if f.get("key") == import_local:
                    franchise_name = f.get("name", "")
                    forked_from_key = import_local
                    bundled_origin_name = f.get("name")
                    is_bundled = True
                    src = (f.get("source") or "trakt")
                    try:
                        if src == "local":
                            path = os.path.join(
                                os.path.dirname(__file__), "defaults", "franchise_data",
                                f"{import_local}.json",
                            )
                            with open(path) as _f:
                                items = _json.load(_f).get("items", [])
                        elif src == "trakt":
                            items = _cached_trakt_list_items(
                                f.get("trakt_user", ""), f.get("trakt_slug", "")
                            )
                    except Exception as e:
                        log.warning("franchise maker preload from %s failed: %s",
                                    import_local, e)
                    break
        elif import_chronolists:
            for f in _load_prebaked_franchises():
                if f.get("key") == import_chronolists:
                    franchise_name = f.get("name", "")
                    forked_from_key = import_chronolists
                    bundled_origin_name = f.get("name")
                    is_bundled = True
                    try:
                        from chronolists_client import get_chronolists_client
                        items = get_chronolists_client().fetch_list_items(
                            f.get("chronolists_id", "")
                        )
                    except Exception as e:
                        log.warning("franchise maker preload from chronolists %s failed: %s",
                                    import_chronolists, e)
                    break
        elif import_trakt_user and import_trakt_slug:
            try:
                items = _cached_trakt_list_items(import_trakt_user, import_trakt_slug)
                franchise_name = f"{import_trakt_user}/{import_trakt_slug}"
            except Exception as e:
                log.warning("franchise maker preload from Trakt failed: %s", e)

        return render_template(
            "franchise_maker.html",
            mode="new",
            playlist_id=None,
            franchise_name=franchise_name,
            description="",
            items=items,
            forked_from_key=forked_from_key,
            is_bundled=is_bundled,
            bundled_origin_name=bundled_origin_name,
            existing_names=existing_names,
            backends=backends,
            has_tmdb_key=has_key,
            tmdb_key_set_in_db=tmdb_key_set_in_db,
            default_backend=",".join(backends) if backends else "plex",
        )

    @app.route("/franchise-maker/<int:playlist_id>/edit", methods=["GET"])
    def franchise_maker_edit(playlist_id: int):
        row = db.get_playlist(playlist_id)
        if not row:
            abort(404)
        row = dict(row)
        if row.get("playlist_type") != "franchise":
            abort(404)

        defn_id = row.get("franchise_definition_id")
        if not defn_id:
            abort(404)

        defn = db.get_franchise_definition_by_id(defn_id)
        if not defn:
            abort(404)

        backends = available_backends()
        existing_names = [r["name"] for r in db.list_playlists()]
        has_key = bool(db.get_setting("tmdb_api_key") or os.environ.get("TMDB_API_KEY"))
        tmdb_key_set_in_db = bool(db.get_setting("tmdb_api_key"))

        is_bundled = defn.get("source") in ("trakt", "local")
        forked_from_key = defn.get("forked_from_key")

        items = service.franchise_items_for_maker(defn_id)

        return render_template(
            "franchise_maker.html",
            mode="edit",
            playlist_id=playlist_id,
            franchise_name=defn.get("name", row["name"]),
            description="",
            items=items,
            forked_from_key=forked_from_key,
            is_bundled=is_bundled,
            bundled_origin_name=None,
            existing_names=existing_names,
            backends=backends,
            has_tmdb_key=has_key,
            tmdb_key_set_in_db=tmdb_key_set_in_db,
            default_backend=row.get("backend", backends[0] if backends else "plex"),
        )

    @app.route("/api/franchise-maker/search", methods=["GET"])
    def api_fm_search():
        from tmdb_client import search
        q = request.args.get("q", "")
        media_type = request.args.get("type", "movie")
        try:
            results = search(q, media_type)
            return jsonify(results)
        except ValueError as e:
            return jsonify({"error": str(e), "needs_key": True}), 400
        except Exception as e:
            log.warning("TMDB search failed: %s", e)
            return jsonify({"error": str(e)}), 500

    @app.route("/api/franchise-maker/movie/<int:tmdb_id>", methods=["GET"])
    def api_fm_movie(tmdb_id: int):
        from tmdb_client import get_movie
        try:
            return jsonify(get_movie(tmdb_id))
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/franchise-maker/tv/<int:tmdb_id>", methods=["GET"])
    def api_fm_tv(tmdb_id: int):
        from tmdb_client import get_tv
        try:
            return jsonify(get_tv(tmdb_id))
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/franchise-maker/tv/<int:tmdb_id>/season/<int:season_number>", methods=["GET"])
    def api_fm_season(tmdb_id: int, season_number: int):
        from tmdb_client import get_season
        try:
            return jsonify(get_season(tmdb_id, season_number))
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/franchise-maker/import-trakt", methods=["POST"])
    def api_fm_import_trakt():
        payload = request.json or {}
        url = (payload.get("url") or "").strip()
        import re
        m = re.search(r"trakt\.tv/users/([^/]+)/lists/([^/?#]+)", url)
        if not m:
            return jsonify({"error": "Invalid Trakt list URL"}), 400
        user, slug = m.group(1), m.group(2)
        try:
            from trakt_client import get_trakt_client
            items = get_trakt_client().fetch_list_items(user, slug)
            return jsonify({"items": items})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/franchise-maker/save", methods=["POST"])
    def api_fm_save():
        payload = request.json or {}
        try:
            forked = payload.get("forked_from_key")
            forked_from_key = forked.strip() if isinstance(forked, str) and forked.strip() else None
            pid = service.save_user_franchise_playlist(
                playlist_id=payload.get("playlist_id"),
                name=(payload.get("name") or "").strip(),
                backend=payload.get("backend", "plex"),
                items=payload.get("items", []),
                description=(payload.get("description") or "").strip(),
                forked_from_key=forked_from_key,
            )
            return jsonify({
                "ok": True,
                "playlist_id": pid,
                "redirect_url": url_for("view_playlist", playlist_id=pid),
            })
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except Exception as e:
            log.warning("franchise maker save failed", exc_info=True)
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/playlist/<int:playlist_id>/restore-default", methods=["POST"])
    def restore_default(playlist_id: int):
        try:
            ok = service.restore_bundled_franchise(playlist_id)
            if ok:
                flash("Restored original default franchise list.", "ok")
            else:
                flash("Nothing to restore — this playlist isn't a fork.", "error")
        except Exception as e:
            log.exception("restore failed")
            flash(f"Restore failed: {e}", "error")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    @app.route("/playlist/<int:playlist_id>/exclude", methods=["POST"])
    def exclude_show(playlist_id: int):
        show = (request.form.get("show") or "").strip()
        if not show:
            abort(400)
        try:
            service.set_show_excluded(playlist_id, show, True)
        except Exception as e:
            log.exception("exclude failed")
            flash(f"Failed to exclude show: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        flash("Show excluded.", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    @app.route("/playlist/<int:playlist_id>/include", methods=["POST"])
    def include_show(playlist_id: int):
        show = (request.form.get("show") or "").strip()
        if not show:
            abort(400)
        try:
            service.set_show_excluded(playlist_id, show, False)
        except Exception as e:
            log.exception("re-include failed")
            flash(f"Failed to re-include show: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        flash("Show re-included.", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    # -- v1.5.0 crossover groups ------------------------------------------ #

    @app.route("/playlist/<int:playlist_id>/crossover/create", methods=["POST"])
    def crossover_create(playlist_id: int):
        label = (request.form.get("label") or "").strip()
        if not label:
            # Auto-name: count existing groups + 1
            existing = db.list_crossover_groups(playlist_id)
            label = f"Group {len(existing) + 1}"
        try:
            group_id = db.create_crossover_group(playlist_id, label)
        except Exception as e:
            log.exception("crossover group create failed")
            flash(f"Failed to create crossover group: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        # Rebuild tails with new group (even though empty, keeps the path consistent)
        try:
            row = db.get_playlist(playlist_id)
            if row and row["sort_mode"] == "air_date":
                configs = [service._config_from_row(r) for r in db.list_shows(playlist_id)]
                service._rebuild_playlist_tails(row, configs, op_label="crossover create")
        except Exception:
            log.exception("tail rebuild after crossover create failed")
        flash(f"Crossover group '{label}' created.", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    @app.route("/playlist/<int:playlist_id>/crossover/<int:group_id>/add", methods=["POST"])
    def crossover_add_link(playlist_id: int, group_id: int):
        show = (request.form.get("show") or "").strip()
        if not show:
            abort(400)
        try:
            season = int(request.form.get("season", "1"))
            episode = int(request.form.get("episode", "1"))
        except ValueError:
            abort(400)
        # Verify ownership and compute sort_index = max existing + 1
        groups = db.list_crossover_groups(playlist_id)
        group_ids = {g["id"] for g in groups}
        if group_id not in group_ids:
            abort(404)
        max_idx = 0
        for g in groups:
            if g["id"] == group_id:
                for li in g["links"]:
                    if li["sort_index"] > max_idx:
                        max_idx = li["sort_index"]
                break
        try:
            db.add_crossover_link(group_id, show, season, episode, max_idx + 1)
        except Exception as e:
            log.exception("crossover link add failed")
            flash(f"Failed to add episode to group: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        # Rebuild tails
        try:
            row = db.get_playlist(playlist_id)
            if row and row["sort_mode"] == "air_date":
                configs = [service._config_from_row(r) for r in db.list_shows(playlist_id)]
                service._rebuild_playlist_tails(row, configs, op_label="crossover add link")
        except Exception:
            log.exception("tail rebuild after crossover add failed")
        flash("Episode added to crossover group.", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    @app.route("/playlist/<int:playlist_id>/crossover/<int:group_id>/delete", methods=["POST"])
    def crossover_delete_group(playlist_id: int, group_id: int):
        # Verify the group belongs to this playlist before deleting
        if not any(g["id"] == group_id for g in db.list_crossover_groups(playlist_id)):
            abort(404)
        try:
            db.delete_crossover_group(group_id)
        except Exception as e:
            log.exception("crossover group delete failed")
            flash(f"Failed to delete crossover group: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        try:
            row = db.get_playlist(playlist_id)
            if row and row["sort_mode"] == "air_date":
                configs = [service._config_from_row(r) for r in db.list_shows(playlist_id)]
                service._rebuild_playlist_tails(row, configs, op_label="crossover delete group")
        except Exception:
            log.exception("tail rebuild after crossover delete failed")
        flash("Crossover group deleted.", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    @app.route("/playlist/<int:playlist_id>/crossover/link/<int:link_id>/remove", methods=["POST"])
    def crossover_remove_link(playlist_id: int, link_id: int):
        # Verify the link belongs to a group owned by this playlist before deleting
        groups = db.list_crossover_groups(playlist_id)
        if not any(li["id"] == link_id for g in groups for li in g["links"]):
            abort(404)
        try:
            db.remove_crossover_link(link_id)
        except Exception as e:
            log.exception("crossover link remove failed")
            flash(f"Failed to remove episode from group: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        try:
            row = db.get_playlist(playlist_id)
            if row and row["sort_mode"] == "air_date":
                configs = [service._config_from_row(r) for r in db.list_shows(playlist_id)]
                service._rebuild_playlist_tails(row, configs, op_label="crossover remove link")
        except Exception:
            log.exception("tail rebuild after crossover remove failed")
        flash("Episode removed from crossover group.", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    # -- v1.8.0 smart playlist rules ------------------------------------- #

    @app.route("/playlist/<int:playlist_id>/rules/add", methods=["POST"])
    def add_smart_rule(playlist_id: int):
        rule_type = (request.form.get("rule_type") or "").strip()
        operator = (request.form.get("operator") or "include").strip()
        value = (request.form.get("value") or "").strip()
        if not rule_type or not value:
            abort(400)
        try:
            db.add_rule(playlist_id, rule_type, operator, value)
        except Exception as e:
            log.exception("add_rule failed")
            flash(f"Failed to add rule: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        service.sync_playlist(playlist_id, force=True)
        flash("Rule added — playlist synced.", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    @app.route("/playlist/<int:playlist_id>/rules/<int:rule_id>/delete", methods=["POST"])
    def delete_smart_rule(playlist_id: int, rule_id: int):
        try:
            db.remove_rule(rule_id)
        except Exception as e:
            log.exception("remove_rule failed")
            flash(f"Failed to remove rule: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        service.sync_playlist(playlist_id, force=True)
        flash("Rule removed — playlist synced.", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    @app.route("/playlist/<int:playlist_id>/rules/reorder", methods=["POST"])
    def reorder_genre_list(playlist_id: int):
        row = db.get_playlist(playlist_id)
        if not row:
            abort(404)
        full_configs = [service._config_from_row(r) for r in db.list_shows(playlist_id)]
        service._rebuild_playlist_tails(row, full_configs, op_label="genre reorder")
        flash("Playlist rebuilt.", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    @app.route("/playlist/<int:playlist_id>/weight", methods=["POST"])
    def change_weight(playlist_id: int):
        show = (request.form.get("show") or "").strip()
        if not show:
            abort(400)
        try:
            weight = max(1, int(request.form.get("weight", "1")))
        except ValueError:
            abort(400)
        try:
            service.set_show_weight(playlist_id, show, weight)
        except Exception as e:
            log.exception("weight change failed")
            flash(f"Failed to update weight: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        flash(f"Weight updated to {weight}.", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    @app.route("/playlist/<int:playlist_id>/auto_sync", methods=["POST"])
    def change_auto_sync(playlist_id: int):
        enabled = bool(request.form.get("enabled"))
        return _playlist_action(
            playlist_id,
            lambda: service.set_playlist_auto_sync(playlist_id, enabled),
            ok_msg=f"Auto-update {'enabled' if enabled else 'disabled'}.",
            fail_label="Setting change",
        )

    @app.route("/playlist/<int:playlist_id>/unwatched_only", methods=["POST"])
    def change_unwatched_only(playlist_id: int):
        new_value = bool(request.form.get("enabled"))
        return _playlist_action(
            playlist_id,
            lambda: service.set_playlist_unwatched_only(playlist_id, new_value),
            ok_msg=f"Unwatched-only filter {'enabled' if new_value else 'disabled'}.",
            fail_label="Filter change",
        )

    @app.route("/playlist/<int:playlist_id>/pruning", methods=["POST"])
    def change_pruning(playlist_id: int):
        enabled = bool(request.form.get("enabled"))

        def _apply():
            db.set_pruning_enabled(playlist_id, enabled)
            # Turning pruning off forgets the pruned set so franchise items
            # (which sync rebuilds from the definition) come back on next sync.
            if not enabled:
                db.clear_pruned_items(playlist_id)

        return _playlist_action(
            playlist_id,
            _apply,
            ok_msg=f"Pruning {'enabled' if enabled else 'disabled'}.",
            fail_label="Setting change",
        )

    @app.route("/playlist/<int:playlist_id>/reorder", methods=["POST"])
    def reorder(playlist_id: int):
        ordered = request.form.getlist("order")
        if not ordered:
            abort(400)
        return _playlist_action(
            playlist_id,
            lambda: service.reorder_shows(playlist_id, ordered),
            ok_msg="Order updated.",
            fail_label="Reorder",
        )

    @app.route("/playlist/<int:playlist_id>/rename", methods=["POST"])
    def rename_playlist(playlist_id: int):
        new_name = (request.form.get("name") or "").strip()
        return _playlist_action(
            playlist_id,
            lambda: service.rename_managed_playlist(playlist_id, new_name),
            ok_msg=lambda failed: (
                "Playlist renamed, but the rename failed on: "
                + ", ".join(b.title() for b in failed)
                + ". The server-side playlist keeps its old name there."
                if failed else "Playlist renamed."
            ),
            fail_label="Rename",
        )

    @app.route("/playlist/<int:playlist_id>/delete", methods=["POST"])
    def delete_playlist(playlist_id: int):
        try:
            failed = service.delete_managed_playlist(playlist_id)
        except Exception as e:
            log.exception("delete failed")
            flash(f"Failed to delete playlist: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        if failed:
            names = ", ".join(b.title() for b in failed)
            flash(
                f"Removed from Linearr, but the playlist could NOT be deleted on: "
                f"{names}. Check that backend's credentials have permission to "
                f"delete items (e.g. the Emby API key / user must allow deletion).",
                "error",
            )
        else:
            flash("Playlist deleted.", "ok")
        # The local row is deleted even when a backend failed, so always
        # clean up any uploaded card art for this playlist id.
        import os as _os, glob as _glob
        try:
            card_dir = _os.path.join(_os.path.dirname(db.DB_PATH) or ".", "card_art")
            for f in _glob.glob(_os.path.join(card_dir, f"{playlist_id}.*")):
                _os.remove(f)
        except Exception:
            pass
        return redirect(url_for("index"))

    @app.route("/playlist/<int:playlist_id>/sync", methods=["POST"])
    def sync_now(playlist_id: int):
        return _playlist_action(
            playlist_id,
            lambda: service.sync_playlist(playlist_id, force=True),
            ok_msg=lambda r: (
                "Already up to date." if r == (0, 0)
                else "Order updated to match current air dates / settings." if r == (0, -1)
                else f"Synced: +{r[0]} added, -{r[1]} removed."
            ),
            fail_label="Sync",
            changed=lambda r: r != (0, 0),
        )

    @app.route("/playlist/<int:playlist_id>/prune", methods=["POST"])
    def prune_now(playlist_id: int):
        return _playlist_action(
            playlist_id,
            lambda: service.prune_playlist(playlist_id),
            ok_msg=lambda r: f"Removed {r} watched item(s).",
            fail_label="Prune",
            changed=lambda r: r > 0,
        )

    @app.route("/playlist/<int:playlist_id>/refresh-metadata", methods=["POST"])
    def refresh_metadata(playlist_id: int):
        def _msg(result):
            msg = f"Metadata refresh queued for {result['ok']} show(s)."
            if result["errors"]:
                msg += f" {result['errors']} failed."
            return msg

        return _playlist_action(
            playlist_id,
            lambda: service.refresh_playlist_metadata(playlist_id),
            ok_msg=_msg,
            fail_label="Refresh",
            changed=False,  # queued server-side; page content doesn't change
        )

    @app.route("/playlist/<int:playlist_id>/shows/<show_rating_key>/link", methods=["POST"])
    def link_show_backend(playlist_id: int, show_rating_key: str):
        backend = request.form.get("backend", "")
        link_to_key = (request.form.get("link_to_key") or "").strip()
        if not backend or not link_to_key:
            abort(400)
        try:
            service.link_show_backend(playlist_id, show_rating_key, backend, link_to_key)
        except Exception as e:
            log.exception("manual link failed")
            flash(f"Failed to link show: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        flash(f"Linked show to {backend.capitalize()} ({link_to_key}). Rebuilding…", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    # -- v3.6.0 card artwork -------------------------------------------- #

    @app.route("/card-art/<int:playlist_id>")
    def serve_card_art(playlist_id: int):
        import os as _os
        row = db.get_playlist(playlist_id)
        if not row:
            abort(404)
        fname = dict(row).get("card_poster_file")
        if not fname:
            abort(404)
        fpath = _os.path.join(_os.path.dirname(db.DB_PATH) or ".", "card_art", fname)
        if not _os.path.isfile(fpath):
            abort(404)
        ext = _os.path.splitext(fname)[1].lower()
        mimes = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
        return send_file(fpath, mimetype=mimes.get(ext, "image/png"), max_age=3600)

    @app.route("/playlist/<int:playlist_id>/card-art", methods=["POST"])
    def card_art(playlist_id: int):
        import os as _os
        row = db.get_playlist(playlist_id)
        if not row:
            abort(404)
        mode = (request.form.get("mode") or "auto").strip()
        if mode not in ("auto", "pick", "custom"):
            abort(400)

        card_dir = _os.path.join(_os.path.dirname(db.DB_PATH) or ".", "card_art")
        existing_file = dict(row).get("card_poster_file")

        if mode == "pick":
            keys = request.form.getlist("poster_key")[:5]
            if not keys:
                flash("Pick at least one poster.", "error")
                return redirect(url_for("view_playlist", playlist_id=playlist_id))
            posters = service.resolve_card_posters(playlist_id, keys)
            if not posters:
                flash("Couldn't resolve any posters for that selection.", "error")
                return redirect(url_for("view_playlist", playlist_id=playlist_id))
            db.set_card_art(playlist_id, "pick", _json.dumps(keys), _json.dumps(posters), existing_file)
            flash("Card artwork updated.", "ok")

        elif mode == "custom":
            file = request.files.get("image")
            if not file or not file.filename:
                if not existing_file:
                    flash("Select an image file.", "error")
                    return redirect(url_for("view_playlist", playlist_id=playlist_id))
                db.set_card_art(playlist_id, "custom", None, None, existing_file)
            else:
                data = file.read()
                if len(data) > 5 * 1024 * 1024:
                    flash("Image must be PNG, JPEG, or WebP (max 5 MB).", "error")
                    return redirect(url_for("view_playlist", playlist_id=playlist_id))
                ext = _sniff_image(data)
                if not ext:
                    flash("Image must be PNG, JPEG, or WebP (max 5 MB).", "error")
                    return redirect(url_for("view_playlist", playlist_id=playlist_id))
                _os.makedirs(card_dir, exist_ok=True)
                # Remove any previous file for this playlist with a different extension
                if existing_file and existing_file != f"{playlist_id}.{ext}":
                    try:
                        _os.remove(_os.path.join(card_dir, existing_file))
                    except OSError:
                        pass
                fname = f"{playlist_id}.{ext}"
                with open(_os.path.join(card_dir, fname), "wb") as f:
                    f.write(data)
                db.set_card_art(playlist_id, "custom", None, None, fname)
            flash("Card artwork updated.", "ok")

        else:  # auto
            db.set_card_art(playlist_id, "auto", None, None, existing_file)
            flash("Card artwork set to automatic.", "ok")

        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    # -- v2.0.0 settings page ------------------------------------------- #

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        if request.method == "POST":
            action = request.form.get("action")

            if action == "regenerate":
                db.set_setting("api_key", secrets.token_urlsafe(32))
                flash("API key regenerated.", "ok")

            elif action == "tmdb_key_save":
                key = (request.form.get("tmdb_key") or "").strip()
                db.set_setting("tmdb_api_key", key)
                flash("TMDB API key saved." if key else "TMDB API key cleared.", "ok")

            elif action == "webhook_add":
                url = (request.form.get("webhook_url") or "").strip()
                label = (request.form.get("webhook_label") or "").strip()
                if url:
                    db.add_webhook(url, label)
                    flash("Webhook added.", "ok")
                else:
                    flash("URL is required.", "error")

            elif action == "webhook_delete":
                wid = request.form.get("webhook_id")
                if wid and wid.isdigit():
                    db.delete_webhook(int(wid))
                    flash("Webhook removed.", "ok")

            elif action == "webhook_test":
                url = (request.form.get("webhook_url") or "").strip()
                if url:
                    ok, msg = _webhooks.fire_test(url)
                    flash(msg, "ok" if ok else "error")
                else:
                    flash("No URL provided.", "error")

            elif action == "auth_save":
                username = (request.form.get("auth_username") or "").strip()
                password = (request.form.get("auth_password") or "")
                if auth.auth_enabled():
                    # Already on: update username always; password only if given.
                    auth.set_username(username)
                    if password.strip():
                        auth.change_password(password)
                        flash("Login updated.", "ok")
                    else:
                        flash("Username updated.", "ok")
                else:
                    # Enabling for the first time needs a password.
                    if not password.strip():
                        flash("Set a password to enable login.", "error")
                    else:
                        auth.set_credentials(username, password)
                        session["authed"] = True  # don't lock the current user out
                        session.permanent = True
                        flash("Login enabled.", "ok")

            elif action == "auth_disable":
                auth.disable_auth()
                flash("Login disabled — the UI is open again.", "ok")

            elif action == "manual_link_add":
                # Each picked select carries "id||title" so we get a label
                # without re-listing every backend's shows.
                pairs: list[tuple[str, str]] = []
                label = None
                for be in available_backends():
                    raw = (request.form.get(f"link_{be}") or "").strip()
                    if not raw:
                        continue
                    iid, _, title = raw.partition("||")
                    if iid:
                        pairs.append((be, iid))
                        if label is None and title:
                            label = title
                if len(pairs) >= 2:
                    db.link_shows_same(pairs, label=label)
                    flash("Show match added — it now applies everywhere.", "ok")
                else:
                    flash("Pick the matching show on at least two backends.", "error")
                return redirect(url_for("settings", match=1))

            elif action == "manual_link_delete":
                gk = (request.form.get("group_key") or "").strip()
                if gk:
                    db.remove_manual_show_link_group(gk)
                    flash("Show match removed.", "ok")

            elif action == "backends_save":
                from media_client import BACKEND_SETTING_ENV
                for _key in BACKEND_SETTING_ENV:
                    db.set_setting(_key, (request.form.get(_key) or "").strip())
                # New creds take effect immediately: drop cached clients so the
                # next get_client() rebuilds with the saved values.
                get_client.cache_clear()
                flash("Backend settings saved.", "ok")

            elif action == "backend_test":
                # Persist whatever's on screen first, so Test reflects the
                # current form (Save-and-test in one click).
                from media_client import BACKEND_SETTING_ENV
                for _key in BACKEND_SETTING_ENV:
                    db.set_setting(_key, (request.form.get(_key) or "").strip())
                tb = (request.form.get("test_backend") or "").strip()
                get_client.cache_clear()
                if tb not in available_backends():
                    flash(f"{tb.title()} is not fully configured — fill in and Save first.", "error")
                else:
                    try:
                        client = get_client(tb)
                        n_tv = len(client.list_tv_sections())
                        n_movie = len(client.list_movie_sections())
                        parts = []
                        if n_tv:
                            parts.append(f"{n_tv} TV")
                        if n_movie:
                            parts.append(f"{n_movie} movie")
                        detail = (" (" + " + ".join(parts) + " "
                                  + ("library" if (n_tv + n_movie) == 1 else "libraries")
                                  + ")") if parts else ""
                        flash(f"{tb.title()} OK — reachable{detail}", "ok")
                    except Exception as exc:
                        flash(f"{tb.title()} connection failed: {exc}", "error")

            return redirect(url_for("settings"))

        api_key = db.get_setting("api_key") or "(not set)"
        webhooks_list = db.list_webhooks()
        tmdb_key = db.get_setting("tmdb_api_key") or ""
        from media_client import BACKEND_SETTING_ENV, backend_setting
        backend_conf = {k: (backend_setting(k) or "") for k in BACKEND_SETTING_ENV}
        backend_env_only = {
            k: (bool(os.environ.get(env)) and not db.get_setting(k))
            for k, env in BACKEND_SETTING_ENV.items()
        }
        # Per-backend show lists for the "add a match" form — only built when
        # explicitly requested (?match=1), since list_all_shows() hits each
        # backend and would otherwise slow/hang every Settings load.
        link_backend_shows: dict[str, list[dict]] = {}
        if request.args.get("match"):
            for be in available_backends():
                try:
                    shows = get_client(be).list_all_shows()
                    link_backend_shows[be] = [
                        {
                            "id": str(s.rating_key),
                            "title": s.title + (f" ({s.year})" if s.year else ""),
                        }
                        for s in sorted(shows, key=lambda x: (x.title or "").lower())
                    ]
                except Exception:
                    log.warning("match-options: list_all_shows failed on %s", be, exc_info=True)
                    link_backend_shows[be] = []
        return render_template(
            "settings.html",
            api_key=api_key,
            webhooks=webhooks_list,
            tmdb_key=tmdb_key,
            backend_conf=backend_conf,
            backend_env_only=backend_env_only,
            configured_backends=available_backends(),
            manual_links=db.list_manual_show_links(),
            link_backend_shows=link_backend_shows,
            auth_enabled=auth.auth_enabled(),
            auth_username=auth.get_username(),
            auth_env_reset=bool(os.environ.get("LINEARR_AUTH_PASSWORD", "").strip()),
        )

    # ── REST API v1 ──────────────────────────────────────────────────── #

    @app.route("/api/v1/playlists", methods=["GET"])
    @_api_key_required
    def api_list_playlists():
        views = service.list_playlist_views()
        return jsonify([
            {
                "id":            v.id,
                "name":          v.name,
                "sort_mode":     v.sort_mode,
                "backend":       v.backend,
                "playlist_type": v.playlist_type,
                "auto_sync":     bool(v.auto_sync),
                "unwatched_only": bool(v.unwatched_only),
                "shows_count":   len(v.shows),
            }
            for v in views
        ])

    @app.route("/api/v1/playlists/<int:playlist_id>", methods=["GET"])
    @_api_key_required
    def api_get_playlist(playlist_id: int):
        view = service.get_playlist_view(playlist_id)
        if not view:
            return jsonify({"error": "Not found"}), 404
        rules = db.list_rules(playlist_id) if view.playlist_type == "genre" else []
        return jsonify({
            "id":            view.id,
            "name":          view.name,
            "sort_mode":     view.sort_mode,
            "backend":       view.backend,
            "playlist_type": view.playlist_type,
            "auto_sync":     bool(view.auto_sync),
            "unwatched_only": bool(view.unwatched_only),
            "genre_filter":  view.genre_filter,
            "rule_mode":     view.rule_mode,
            "shows": [
                {
                    "title":            s.get("show_title", s.get("title", "")),
                    "start_season":     s.get("start_season", 1),
                    "end_season":       s.get("end_season"),
                    "include_specials": bool(s.get("include_specials", False)),
                    "include_movies":   bool(s.get("include_movies", False)),
                    "weight":           s.get("weight", 1),
                }
                for s in view.shows
            ],
            "rules": [
                {
                    "id":        r["id"],
                    "rule_type": r["rule_type"],
                    "operator":  r["operator"],
                    "value":     r["value"],
                }
                for r in rules
            ],
        })

    @app.route("/api/v1/playlists/<int:playlist_id>/sync", methods=["POST"])
    @_api_key_required
    def api_sync_playlist(playlist_id: int):
        row = db.get_playlist(playlist_id)
        if not row:
            return jsonify({"error": "Not found"}), 404
        try:
            added, removed = service.sync_playlist(playlist_id, force=True)
        except Exception as exc:
            log.exception("api sync failed for playlist %d", playlist_id)
            return jsonify({"error": str(exc)}), 500
        return jsonify({"added": added, "removed": max(removed, 0)})

    @app.route("/api/v1/backends", methods=["GET"])
    @_api_key_required
    def api_backends():
        result = {}
        for backend in available_backends():
            info: dict = {"configured": True}
            try:
                client = get_client(backend)
                client.list_tv_sections()
                info["healthy"] = True
            except Exception as exc:
                info["healthy"] = False
                info["error"] = str(exc)
            url_key = {"plex": "PLEX_URL", "jellyfin": "JELLYFIN_URL", "emby": "EMBY_URL"}.get(backend, "")
            info["url"] = os.environ.get(url_key, "")
            result[backend] = info
        return jsonify(result)

    @app.route("/api/v1/genres", methods=["GET"])
    @_api_key_required
    def api_genres_v1():
        result: dict[str, list[str]] = {}
        for backend in available_backends():
            cached = db.get_genre_cache(backend)
            if cached is not None:
                result[backend] = cached
            else:
                try:
                    genres = get_client(backend).list_all_genres()
                    db.set_genre_cache(backend, genres)
                    result[backend] = genres
                except Exception:
                    result[backend] = []
        return jsonify(result)

    @app.route("/api/v1/playlists/<int:playlist_id>/stats", methods=["GET"])
    @_api_key_required
    def api_playlist_stats(playlist_id: int):
        view = service.get_playlist_view(playlist_id)
        if not view:
            return jsonify({"error": "Not found"}), 404
        if not view.last_stats:
            return jsonify({"error": "No stats yet — run a sync first"}), 404
        return jsonify(view.last_stats)

    return app


if __name__ == "__main__":
    import signal
    app = create_app()
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_PORT", "5005"))
    threads = int(os.environ.get("WEB_THREADS", "8"))

    # `docker stop` sends SIGTERM; re-raise as KeyboardInterrupt — the same
    # clean-shutdown path SIGINT/Ctrl-C already triggers (issue #5, v3.0.15).
    def _graceful_sigterm(_signum, _frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, _graceful_sigterm)

    try:
        from waitress import serve
    except ImportError:
        # Dev convenience only (e.g. a venv that predates v3.5.0). The Docker
        # image always has waitress via requirements.txt.
        log.warning("waitress not installed — falling back to the Flask dev server.")
        app.run(host=host, port=port, debug=False, use_reloader=False)
    else:
        log.info(
            "Linearr v%s listening on %s:%s (waitress, %d threads)",
            __version__, host, port, threads,
        )
        try:
            serve(app, host=host, port=port, threads=threads, ident="Linearr")
        except KeyboardInterrupt:
            pass
