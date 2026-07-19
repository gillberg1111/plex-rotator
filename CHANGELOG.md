# Changelog

All notable changes to Linearr. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [3.8.0] - 2026-07-18

### Added

- **Rename playlists** (requested in
  [#15](https://github.com/gillberg1111/linearr/issues/15)). Click the ✎
  button next to the playlist title on its detail page, type a new name, and
  Linearr renames the playlist locally **and on every backend it lives on**
  (Plex, Jellyfin, and Emby all supported). Duplicate and empty names are
  rejected with a clear message; if a backend can't be reached, the local
  rename still applies and the failing backend is called out so you can re-run
  the rename once it's reachable (or fix the name server-side by hand).

## [3.7.2] - 2026-06-12

### Changed

- **Playlist order page size is now per-playlist.** The "Show current playlist
  order" list defaults to 25 per page and remembers the size you last chose
  for that specific playlist (persisted across browser restarts), instead of
  sharing one global setting across every playlist.

## [3.7.1] - 2026-06-12

### Changed

- **Playlist order view polish.** The collapsible section is now labelled
  "Show current playlist order" with a chevron (▸/▾) instead of a +/–, since a
  "+" next to a reveal toggle read like the "Add" action in the add-show
  section below it. The order list now paginates (25 / 50 / 100 / 250 / All
  per page, default 50) like the builder preview, so long playlists (e.g. a
  500-episode Law & Order) no longer dump every row at once. Both the order
  view and the "Add another show" picker get proper vertical spacing so they
  no longer butt up against the Crossover and Maintenance sections.

## [3.7.0] - 2026-06-12

### Added

- **Live "Current playlist order" view.** Show and Genre playlist pages gain a
  collapsible section that loads the playlist's actual, current order straight
  from your media server — numbered, with show + episode titles, air dates,
  and watched items dimmed. Fix some metadata, hit Sync, expand the section,
  and verify the new order without opening Plex/Jellyfin/Emby. Playlists on
  multiple backends get a per-backend toggle.

### Changed

- **Playlist page layout.** The "Add another show" picker (often hundreds of
  poster tiles) no longer pushes Stats, Card artwork, and Maintenance below
  the fold: it moved to the bottom of the page and is collapsed behind an
  expandable "Add another show — N available" header.

### Fixed

- **Sync now reorders playlists.** Sync applied changes as a membership diff —
  remove what left, append what's new at the end — so it could never MOVE
  existing items. If you fixed missing air dates (e.g. via Refresh metadata)
  on an Air Date playlist, Sync reported "Already up to date." and the order
  on the server never changed; recreating the playlist was the only fix.
  Sync now detects order changes and rebuilds the playlist in the exact
  computed order (atomically on Jellyfin), preserving the watched head
  buffer. New episodes that belong mid-list are spliced into place instead
  of appended at the end, and a pure reorder reports "Order updated to match
  current air dates / settings."

## [3.6.0] - 2026-06-12

### Added

- **Card artwork control.** Every playlist detail page gains a "Card artwork"
  section that controls the home-page card art: **Automatic** (current
  behaviour — first 5 member posters, or the franchise poster strip),
  **Pick posters** (choose exactly which 1–5 members appear, in the order you
  pick them — e.g. keep crossover-only shows like Chicago Fire off a
  Law & Order card), or **Custom image** (upload a PNG/JPEG/WebP up to 5 MB
  as the full card art). Uploads are validated by content, stored alongside
  the database, and removed when the playlist is deleted.

### Fixed

- **Pruning toggle now renders its real state.** The playlist page coalesced
  a disabled (`0`) pruning setting back to "On" when building the view, so
  the pill always showed On even though the setting (and the prune behaviour)
  had changed. Display-only bug; pruning itself always honoured the toggle.
- **Franchise cards stuck on a single poster.** Two cooperating defects:
  franchise playlists created from the picker never stored the up-to-5 poster
  strip (only the Maker path did), and the startup backfill could permanently
  freeze a strip at 1 poster if TMDB was unreachable during one boot (its
  "already done" check treated any non-empty strip as complete). The strip is
  now resolved at fetch time and at playlist creation, and the backfill
  retries 1-poster strips on multi-item franchises until a real strip
  resolves. Affected playlists (e.g. James Bond, John Wick) heal on the next
  container start — no action needed.

## [3.5.0] - 2026-06-11

### Added

- **Toast notifications + no-reload actions.** Sync now / Prune / Refresh
  metadata and every playlist toggle (sort, filter, auto-update, pruning,
  block size, reshuffle) now post in the background: the button shows a
  spinner, the result arrives as a frosted-glass toast, and the page only
  refreshes when something actually changed. A long sync no longer freezes
  the tab on a blank page. Pure progressive enhancement — with JavaScript
  disabled every form still works exactly as before (new `static/linearr.js`).
- **Home-page search & type filters.** A filter input plus All / Show / Genre /
  Franchise pills above the playlist grid (shown when you have 2+ playlists),
  and each card now shows when the playlist last synced ("synced 2h ago").
- **First-run welcome.** A fresh install with no media server configured now
  shows a guided "Connect a server" setup card instead of a bare empty state.
- **`WEB_THREADS`** env var — worker threads for the new built-in server
  (default 8).

### Changed

- **Production web server.** Linearr now serves itself with **waitress**
  instead of Flask's development server (single process + worker threads, so
  scheduled jobs still run exactly once). `docker stop` shutdown behavior is
  unchanged.
- **Self-hosted font.** The Inter font is now bundled
  (`static/fonts/InterVariable.woff2`) instead of loaded from Google Fonts —
  no third-party requests, works on offline LANs, faster first paint.
- Subtle cross-fade between page navigations on browsers that support the
  View Transitions API.

### Security

- **Cross-site form posts are now blocked.** Mutating requests whose
  `Origin`/`Referer` header points at a different site are rejected with 403
  — closing the classic CSRF-against-LAN-service hole (a malicious webpage
  firing blind POSTs at `http://<lan-ip>:5005/...` through your browser),
  which matters because the web-UI login is optional. Non-browser clients
  (curl, scripts) and the keyed `/api/v1/` REST API are unaffected. If a
  reverse proxy rewrites your `Host` header, set
  `LINEARR_DISABLE_ORIGIN_CHECK=1` (see README troubleshooting).

### Internal

- ~10 copy-pasted POST handlers consolidated into one `_playlist_action`
  helper that answers either a classic redirect or JSON depending on the
  caller; new `db.set_pruning_enabled()`; 9 per-backend DB setters now
  delegate to two column-allow-listed helpers. 29 new test checks (634 total).

## [3.4.0] - 2026-06-10

### Added

- **Installable as a home-screen app (PWA).** Saving Linearr to your home screen
  on iPhone or Android now shows the Linearr logo and opens it full-screen. Added
  a web app manifest (`manifest.webmanifest`) with 192/512 icons for Android, an
  `apple-touch-icon` for iOS, and the standard standalone-display / theme-color
  meta tags (on both the app and the login page). No config needed.

## [3.3.6] - 2026-06-09

### Internal

- **Test hardening for the v3.3.5 franchise-pruning fix (no behavior change).**
  Extracted the prune decision into a testable `_franchise_prune_keys()` helper
  and added end-to-end coverage: multi-sweep convergence (watched items stay
  removed), the `WATCHED_KEEP` buffer sliding correctly as more is watched,
  mixed movie/episode playlists, per-backend isolation, and FK-cascade cleanup
  of the pruned set when a playlist is deleted. Also verified the on-disk upgrade
  (a pre-v3.3.5 database gains the `pruned_items` table on boot) and a clean app
  start. 605 tests.

## [3.3.5] - 2026-06-09

### Fixed

- **Franchise pruning on Emby now actually sticks (definitive fix).** Franchise
  sync rebuilds the playlist from the static definition, so to keep watched
  items out it needs to know what's watched. The previous approach asked Emby
  for play counts in bulk (`get_view_counts`) — but Emby returns empty watch
  data for that id-filtered query no matter the endpoint, so sync thought
  nothing was watched and re-added every item the prune sweep had just removed.
  Linearr now **persists what it prunes** (a per-playlist "pruned set", detected
  from the live playlist — which *does* report watch state — via the same path
  the manual prune uses) and franchise sync subtracts that set from the rebuild.
  No more dependence on Emby's bulk watch-state API. Turning pruning off for a
  playlist forgets its pruned set, so items return on the next sync.

## [3.3.4] - 2026-06-09

### Fixed

- **Franchise pruning on Emby actually sticks now (watched episodes stay
  removed).** The bulk watch-state lookup (`get_view_counts`) queried Emby's
  plain `/Items?userId=` endpoint, where Emby can return empty `UserData` — so
  every play count read as 0, franchise sync concluded nothing was watched, and
  re-added the items the prune sweep had just removed (which is why pruning
  "only worked manually": the manual prune reads watch state through a
  user-scoped endpoint that works). `get_view_counts` now uses the user-scoped
  `/Users/{id}/Items` path, matching the read paths that already worked. Added a
  per-sync diagnostic log (`Franchise prune 'X' on emby: N items, W watched, K
  kept`) so this is verifiable in the logs.

## [3.3.3] - 2026-06-09

### Security

- **Session signing key is now strong by default (closes a login-bypass).** The
  optional web-UI login (v3.3.0) makes the signed session cookie an auth
  boundary, but `FLASK_SECRET` fell back to a publicly-known default
  (`dev-secret-change-me`) when unset — so anyone could forge an authenticated
  cookie and bypass the login. Linearr now auto-generates a strong random secret
  and persists it (in the same store as the API key) when `FLASK_SECRET` isn't
  set; an explicit `FLASK_SECRET` still takes precedence. No action needed.
  **Note:** any session signed with the old default is invalidated, so you'll log
  in once after upgrading (and only if you had login enabled).

## [3.3.2] - 2026-06-09

### Fixed

- **Login guard now also covers the internal API endpoints.** When web-UI login
  is enabled, the guard previously exempted *all* `/api/` paths — including the
  unauthenticated internal endpoints the UI uses (`/api/genres`, `/api/preview`,
  `/api/episodes`, `/api/franchise/preview`). Only the keyed REST API at
  `/api/v1/` should bypass the session; the internal endpoints now require a
  login (the UI's own JS already sends the session cookie, so nothing in the app
  breaks). No effect when login is off.

## [3.3.1] - 2026-06-09

### Changed

- **Playlist cards keep a uniform height.** The show-title list on each card is
  now clamped to two lines with an ellipsis, so a genre playlist matching many
  shows no longer makes its card tower over the others. The full list is still
  on the playlist's own page.

## [3.3.0] - 2026-06-09

### Added

- **Optional web-UI login (username + password).** Off by default — set a
  password under **Settings → Login & Security** to enable it. A single admin
  user; the password is stored hashed (never plaintext), the session is a signed
  cookie, and failed logins are rate-limited. Every page is protected except the
  login page and static assets; the REST API at `/api/v1/` keeps its own
  independent key auth. LAN-only and reverse-proxy/SSO setups can simply leave it
  off (no behavior change on upgrade).
- **No-email password reset.** Forgot the password? Set `LINEARR_AUTH_PASSWORD`
  (and optionally `LINEARR_AUTH_USERNAME`) in your environment and restart — the
  credentials are reset from the env on boot. Clear the variable again afterward;
  the hashed password persists in the database. Settings shows a reminder while
  the variable is set.

## [3.2.9] - 2026-06-09

### Fixed

- **Franchise pruning now actually removes watched episodes on Jellyfin/Emby.**
  The bulk view-count lookup used by franchise sync-time pruning
  (`get_view_counts`) didn't pass `enableUserData=true`, so Jellyfin/Emby
  returned empty `UserData` and every play count read as 0 — pruning concluded
  nothing was watched and re-added watched episodes on the next sync. (Manual
  "Prune watched now" worked because it reads watch state through a different
  call that already set the flag, which is why Show/rotation playlists pruned
  fine but franchise didn't.) Added the flag to both clients' `get_view_counts`,
  matching every other view-count read path. `WATCHED_KEEP=0` now correctly
  leaves zero watched episodes in franchise playlists.

## [3.2.8] - 2026-06-09

### Added

- **Create cross-backend show matches directly in Settings.** v3.2.7 could only
  record a match via the "Link manually…" banner on a playlist — but that banner
  only appears when a row is missing a backend, so shows you'd already linked
  (or any already-matched pair) had no way to create the persistent record.
  **Settings → Manual show matches → "+ Add a match"** now loads your per-backend
  show lists and lets you pick the same show on each backend and link them in one
  place. (Show lists load only when you click "+ Add a match", so the Settings
  page stays fast.)

## [3.2.7] - 2026-06-07

### Added

- **Persistent cross-backend "same show" matches.** When a show has a different
  title on one backend and no shared provider IDs (e.g. Emby has no metadata and
  names it differently), automatic matching can't connect them, so it appears as
  two separate shows in genre builds, the picker, and playlists. Using "Link
  manually…" on a playlist now records a **global** same-show link, and the
  matching layer honors it **everywhere** — genre builds, the By Show picker, and
  sync all treat the linked entries as one show from then on (not just in the one
  playlist where you linked them). Manage/undo these under **Settings → Manual
  show matches**.

## [3.2.6] - 2026-06-07

### Fixed

- **"Link manually…" now merges duplicate show entries instead of leaving two.**
  When a show has a different title on one backend and no shared provider id
  (e.g. Emby names it differently and can't fetch metadata), genre/manual
  discovery creates two separate rows for the same show. Manually linking used
  to just write the id onto one row, leaving both listed. The link now detects
  when another row already owns the linked backend id, folds that row's
  remaining backend ids onto the linked row, and removes the redundant
  duplicate — so the show collapses to a single entry covering every backend.
  (To clean up an existing double-linked playlist, Remove or Exclude the
  duplicate row, then Sync now.)

## [3.2.5] - 2026-06-07

### Changed

- **Dropdowns now match the site's frosted-glass styling.** The "Link manually…"
  show picker (in the missing-on warning banner) looked like an unstyled native
  control; it's now a frosted-glass select with the blue accent caret, hover/focus
  ring, and a dark option list. The same treatment is applied to every other
  dropdown in the app (smart-rule type/value pickers, crossover show picker,
  preview page-size menu). The "Link manually…" toggle is now a rounded
  warning-accent chip, and the link form sits in a small frosted panel.

## [3.2.4] - 2026-06-07

### Fixed

- **Genre / smart-rule matched-show posters no longer break when a show's
  artwork comes from a non-primary backend.** Genre membership differs per
  backend (a show can be tagged a genre on Jellyfin/Emby but not Plex), so a
  matched show's thumb may originate on a backend other than the primary. The
  preview was hardcoding the thumb backend to `primary_backend(...)`, so a
  Jellyfin/Emby thumb got requested as Plex and 404'd (broken-image icon).
  `ShowConfig` now carries the thumb's actual source backend through genre/smart
  resolution and the preview uses it.
- **`/thumb` now falls back to the other configured non-Plex backend on a
  miss.** Jellyfin and Emby item ids are indistinguishable 32-char GUIDs, so the
  backend inference for a bare-GUID thumb ref (used by saved playlist pages and
  home-page cards, which don't pass an explicit backend) could guess wrong and
  404. The proxy now tries the other non-Plex backend before giving up — fixing
  broken posters on existing playlists with no re-sync needed.

## [3.2.3] - 2026-06-07

### Fixed

- **Franchise home-page cards now show the up-to-5 poster strip** like show and
  genre cards (1 poster → full width, 2 → 50/50, 3 → thirds, 4 → quarters, 5 →
  fifths, capped at 5). Franchise cards previously rendered a single cover, so a
  multi-movie/show franchise (e.g. Mission Impossible) showed only one poster and
  franchises without a single resolvable cover (e.g. DCU) showed none. The strip
  is built from up to 5 distinct TMDB posters across the franchise's items
  (de-duplicated, so a single-show franchise still fills the area with one).
  Posters are resolved and stored on save and via the startup backfill — which
  now also fills the new strip for pre-existing franchises (restart to apply).

### Changed

- New Genre playlist: the empty-state hint now says *Click "Match shows"* to
  match the actual button label (was "Preview matches").

## [3.2.2] - 2026-06-07

### Fixed

- **Existing franchise playlists now get home-page cover art too.** v3.2.1 only
  persisted a `poster_url` for franchise playlists saved *after* the fix, so
  franchises created earlier (user-built / forked ones in particular) stayed
  blank. A one-shot startup backfill now resolves and stores a poster for any
  franchise definition missing one — a TMDB poster from its items, falling back
  to the bundled static poster by key / origin key. Runs once on app start;
  restart to apply.

## [3.2.1] - 2026-06-07

### Fixed

- **The Pruning toggle now shows on franchise playlist pages.** v3.2.0 enabled
  franchise pruning but the On/Off toggle lived in the non-franchise branch of
  the detail template, so franchise pages only got the "Prune watched now"
  button with no way to enable the background sweep. The toggle now renders for
  franchise playlists (both bundled and custom) without changing the show/genre
  layout.
- **Custom (Build-Your-Own) franchise playlists now get cover art on the home
  page.** User-built and forked franchise definitions never persisted a
  `poster_url`, so their home-page card was blank. Saving a franchise via the
  Maker now resolves and stores a representative TMDB poster on the definition.
- **Home-page franchise cards degrade gracefully when the cover image can't
  load.** A blocked or 404'd poster URL now falls back to a letter tile instead
  of a broken-image icon (same fallback the show/genre cards already use). This
  only touches the franchise card image — the `/thumb` show-poster path is
  unchanged.

## [3.2.0] - 2026-06-07

### Added

- **Watched-media pruning now works for Franchise and Genre playlists.** Pruning
  (keep the last `WATCHED_KEEP` watched as a buffer, remove older watched items)
  previously only stuck on rotation/manual playlists with *unwatched-only* on.
  It is now **idempotent** for every playlist type and every setting combination —
  a single sync converges to `[recent watched buffer] + [unwatched future]` and
  stays there:
  - **Franchise:** the background sweep no longer skips franchise playlists. When
    pruning is enabled, sync reads live library watch state (new bulk
    `MediaClient.get_view_counts()` on Plex / Jellyfin / Emby) and excludes
    watched-beyond-buffer items before rebuilding, so they aren't re-added on the
    next sweep. The **"Prune watched now"** button is now available on franchise
    playlists too.
  - **Genre / manual:** the tail rebuild trims the watched head buffer to the last
    `WATCHED_KEEP` watched items and excludes watched episodes from the future
    tail, so pruning sticks even when *unwatched-only* is **off**.
- **Franchise pruning stays opt-in (off by default).** Existing and new franchise
  playlists keep `pruning_enabled=0` — nothing is removed until you flip the
  Pruning toggle on. No migration, no behavior change on upgrade.

### Notes

- Genre playlists default to pruning **on**, so genre playlists with
  *unwatched-only* off will now actually remove watched-beyond-buffer episodes on
  the next sweep (previously the toggle was effectively inert in that
  combination). Turn Pruning off on a genre playlist to keep every watched
  episode.

## [3.1.2] - 2026-06-06

### Fixed

- **Mobile: the "Linearr" wordmark is back in the top bar.** v3.1.1's fix for the
  hidden Settings button over-corrected and dropped the wordmark too; the
  compacted buttons leave room for icon + "Linearr" + both actions.
- **Mobile: the franchise create bar no longer clips the playlist-name field off
  the screen.** The commit card now wraps on phones — the name input gets a
  full-width row and the Edit / Create Playlist buttons sit below it (applies to
  every create bar, not just franchises).

## [3.1.1] - 2026-06-06

### Fixed

- **Franchise Maker rows overflowed on narrow screens.** The third action button
  ("+ Add all eps") squeezed show titles down to "B.." and made season buttons
  wrap mid-word. Result rows and season rows now wrap their action buttons to a
  new line and keep the title readable.
- **Franchise Maker: "+ Add all eps" now marks the episodes as added
  immediately** (✓ on each row) instead of only after the next search.
- **Settings button was hidden on mobile.** A rule hid every non-primary topbar
  button below 480px, which removed the only one — Settings. The topbar now keeps
  Settings visible on phones (the wordmark drops and the buttons compact so both
  fit).

## [3.1.0] - 2026-06-06

### Added

- **Franchise Playlist Maker: "+ Add all eps".** New buttons on each season row
  and each TV search result add *every* episode of that season (or whole series)
  as individual, drag-to-reorder rows — instead of a single season/series item
  that can't be interleaved. Built for custom interleaved orders across
  overlapping shows (e.g. Band of Brothers → The Pacific → Masters of the Air).
  Series-level adds in season/episode order and skips specials; both skip
  episodes already in the list.

### Fixed

- **Franchise Maker: the seasons/episodes browse no longer collapses after each
  add.** Adding a movie, season, episode, or whole series re-ran the TMDB search,
  wiping the expanded browse and forcing you to re-navigate for every addition.
  Adds now update the clicked button in place and leave the browse open, so you
  can add item after item without extra clicks.

## [3.0.15] - 2026-06-06

### Fixed

- **Genre matched-show posters didn't load on an Emby server that uses numeric
  item ids.** Linearr's legacy "a numeric id means Plex" shortcut stamped a
  phantom `plex_rating_key` onto Emby/Jellyfin shows whose ids happen to be
  numeric, so the genre preview asked for the poster from Plex
  (`/thumb?...&b=plex`) and 404'd when Plex isn't configured. The numeric→Plex
  fallback now only applies when the show has no Jellyfin or Emby id. (#5)
- **Container hung on `docker stop`.** The dev server only handled SIGINT, so
  Docker's SIGTERM was ignored until the stop-timeout forced a SIGKILL. SIGTERM
  is now handled and shuts down through the same clean path as SIGINT. (#5)

## [3.0.14] - 2026-06-06

### Fixed

- **Franchise preview reported "0 of N in your library" on a single-backend
  Emby (or Jellyfin) install** — even though creating the playlist matched the
  items fine. The franchise picker's preview JavaScript fell back to querying
  the `plex` backend when the multi-backend "Push to" picker wasn't rendered
  (single-backend installs use a hidden field instead), so the preview checked a
  backend the user doesn't have. It now reads the hidden backend field. (#5)
- **Posters didn't load on the configure and detail pages for an Emby-only
  install.** The `/thumb` proxy inferred the backend from the image reference
  and a bare GUID fell back to a hardcoded `jellyfin`, which 404s when Jellyfin
  isn't configured. It now falls back to the first configured non-Plex backend,
  and the configure page passes the source backend explicitly. (#5)

## [3.0.13] - 2026-06-06

### Fixed

- **"By Show" playlists couldn't be created on a single Emby- or Jellyfin-only
  install** — every selected series resolved to "0 episodes" even though the
  shows were listed and selectable. Single-backend installs don't build the
  cross-backend aggregated list, and the picked item id (a hex GUID) was only
  mirrored into the per-backend id slot for numeric Plex keys — so
  `id_for(emby/jellyfin)` was `None` and every episode/preview/create path
  filtered the show out. The picked rating_key is now mirrored into the sole
  configured backend's id slot. (Plex-only and multi-backend installs were
  unaffected.) (#5)
- **Franchise preview falsely reported "0 of N items in your library" when a
  backend timed out.** If the franchise match-cache build failed (e.g. Emby
  unreachable for the preview), the preview silently treated every item as "not
  in library" **and cached that failure** for 60s — while the actual playlist
  creation rebuilt the cache and matched fine ("finds them after"). The preview
  cache no longer caches failures (the next attempt retries) and now surfaces a
  clear "Couldn't reach Emby…" note above the results instead of a misleading
  zero count. (#5)

## [3.0.12] - 2026-06-05

### Changed

- **Unreachable backends now show a clear banner instead of an empty picker.**
  When a configured backend can't be listed (e.g. Emby times out because the
  container can't route to it), the "New Playlist → By Show" picker, the
  configure step, the add-shows picker, and the genre builder previously failed
  silently and rendered nothing — leaving users to conclude Linearr "wasn't
  detecting their libraries." These flows now surface a per-backend warning
  (e.g. *"Couldn't reach Emby — the connection timed out or was refused. Check
  its URL in Settings and that the server is reachable from the Linearr
  container."*) while still listing whatever the reachable backends returned.
  `/api/genres` returns per-backend errors and the genre picker shows an inline
  note. (#5)

## [3.0.11] - 2026-06-05

### Fixed

- **Crash on startup after upgrading: `NOT NULL constraint failed:
  managed_playlists_new.block_size`.** `init_db()`'s one-time `managed_playlists`
  rebuild — the migration that relaxes the `backend` CHECK from an enum to a CSV
  set — copied rows with `INSERT INTO managed_playlists_new SELECT *`. That copy
  is positional, but a database's physical column order depends on its upgrade
  history: columns added by `ALTER TABLE` are appended last, so `emby_playlist_id`
  (5th in the rebuilt table) and the v2.x columns sit elsewhere on disk. The
  ordinal mismatch shifted a NULL-able value (e.g. `shuffle_seed`) into a NOT
  NULL column and aborted startup. The rebuild now copies by explicit column
  name, so every value lands in its own column regardless of on-disk order.
  Reported and fixed by @andrebrait. (#8)

## [3.0.10] - 2026-06-05

### Fixed

- **Jellyfin/Emby: large playlists no longer fail with HTTP 414 (URI Too
  Long).** Syncing a genre playlist with many shows (e.g. 30+ anime series)
  sent every episode id in a single request's query string, overrunning the
  server/reverse-proxy URI limit. `add_items_to_playlist`,
  `remove_items_from_playlist` (both backends), and Emby's `create_playlist`
  now chunk ids into batches of 100 per request.
- **Jellyfin "By Show": shows no longer wrongly report "0 regular seasons
  available / This show can't be added."** On newer Jellyfin (10.11.x) the
  single-item fetch (`GET /Items/{id}`) returned HTTP 400; because the configure
  page fetched the show summary and its seasons in one combined try-block, that
  error silently discarded the (otherwise valid) season list. `_fetch_item` now
  uses the version-stable list form (`GET /Items?Ids=…`) on both backends, and
  the configure page gathers seasons, summary, and associated movies
  independently — a show stays addable whenever its seasons are fetchable, with
  a title fallback when the summary is unavailable.

## [3.0.9] - 2026-05-30

### Changed

- **Cross-backend show matching now uses TVDB + TMDB + IMDB on all three
  backends.** Linking the same show across Plex/Jellyfin/Emby (for multi-backend
  show & genre playlists), the heal-on-sync pass, and the picker/genre
  deduplication previously matched Plex/Jellyfin by TVDB only and Emby by title
  alone. They now match on ANY shared provider id (TVDB/TMDB/IMDB) before
  falling back to title+year, so libraries scraped with different metadata
  agents still link up — and Emby gets ID matching it never had.
- **"Test connection" now reports movie libraries too**, e.g. `Emby OK —
  reachable (3 TV + 4 movie libraries)`. New `list_movie_sections()` on each
  client.

### Added

- **Main-page notice when no TMDB API key is set**, explaining that a free TMDB
  key improves franchise library matching, with links to get a key and to the
  Settings page.

## [3.0.8] - 2026-05-30

### Added

- **TMDB → TVDB/IMDB fallback matching for franchises.** Bundled franchises are
  TMDB-keyed, so libraries scraped with a TVDB- or IMDB-only metadata agent (no
  TMDB ids) previously matched only by fuzzy title+year and often showed items
  as "not in library". Linearr now resolves each franchise item's TMDB id to its
  TVDB/IMDB id via TMDB's `external_ids` endpoint (cached per process) and
  matches against the library by those ids:
  - movies: TMDB id → IMDB id → library `movie_by_imdb`
  - shows/episodes: show TMDB id → TVDB or IMDB id → library `show_by_tvdb` /
    `show_by_imdb`
  `ShowSummary`/`MovieSummary` now carry `imdb_id` (populated by all three
  backends). Requires a TMDB API key (the same one the Franchise Maker uses);
  without it, matching falls back to title+year as before. The franchise
  library-cache diagnostic log now also reports IMDB coverage.

## [3.0.7] - 2026-05-30

### Added

- Diagnostic log line when building the franchise library cache, e.g.
  `Franchise lib cache [emby]: 412 movies (0 w/ tmdb), 150 shows (150 w/ tvdb,
  0 w/ tmdb)`. Franchise lists are TMDB-keyed, so a library scraped with a
  TVDB-only metadata agent (no TMDB ids) matches only by fuzzy title+year and
  items can show as "not in library" even though they're present. This line
  makes the cause visible (0 movies = the resolved user can't see the movie
  libraries; N movies but 0 w/ tmdb = TVDB-only scrape).

## [3.0.6] - 2026-05-30

### Changed

- **Fast-fail on an unreachable backend.** Jellyfin/Emby HTTP calls now use a
  split `(connect, read)` timeout with a **5-second connect timeout** (read
  stays 30s). An unreachable server (wrong URL, server down, firewall) now
  fails in ~5s instead of hanging the request for 30s. The show picker already
  skips a failing backend and surfaces a flash, so a down backend no longer
  stalls the page — it just isn't listed until reachable.

## [3.0.5] - 2026-05-29

### Changed

- New Playlist type picker (By Show / By Genre / By Franchise) is now centered
  on the page and uses the frosted-glass card treatment to match the franchise
  picker and settings cards.
- README: added genre / franchise / build-your-own screenshots.

## [3.0.4] - 2026-05-29

### Fixed

- **Emby UI parity** — several user-facing strings/labels still said only
  "Plex/Jellyfin":
  - Refresh-metadata hint on the playlist page now reads "Plex/Jellyfin/Emby".
  - Genre builder's matched-shows "On …" line now lists Emby (any combination).
  - Configure exclusion note and the unwatched-only hint are now
    backend-agnostic ("all configured backends" / "on the backend").
- Missing-side warning on the playlist page was gated on `',' in backend` and
  could mislabel — e.g. a `plex,emby` playlist showing "Not on Jellyfin" even
  though Jellyfin isn't targeted. It now checks actual backend membership, so it
  only warns about backends the playlist targets.

## [3.0.3] - 2026-05-29

### Fixed

- **Emby (and any backend) playlists created via the Show/Genre builder were
  never deletable** — the real root cause behind "Playlist deleted" while the
  playlist stayed on the server. `create_managed_playlist` created the playlist
  on each backend but only persisted the new id for Plex and Jellyfin; the
  **Emby id was dropped**, so `managed_playlists.emby_playlist_id` stayed NULL
  and the delete loop skipped Emby entirely (reporting "no failures"). Now
  persists `emby_playlist_id` too. (Franchise playlists already stored it, which
  is why only Show/Genre playlists were affected.)
- Show/Genre **preview crash** (`Unknown backend: <function primary_backend>`):
  two `service.preview_playlist(...)` calls passed the `primary_backend`
  function object instead of the resolved backend string. Now pass the computed
  `primary_be`.

## [3.0.2] - 2026-05-29

### Fixed

- Emby playlists silently not deleting (reported as "Playlist deleted" while the
  playlist stayed on Emby). The existence/Type pre-check in `delete_playlist`
  and `playlist_exists` queried `/Items?Ids=&userId=<resolved user>`; when the
  playlist was owned by a different user than the lazily-resolved one (e.g. a
  different "first admin" after a restart, or an `EMBY_USERNAME`), the lookup
  returned empty and `delete_playlist` took its "already gone" no-op path — no
  DELETE, no error, false success. Both checks are now **owner-agnostic** (no
  `userId` filter), and `delete_playlist` logs the lookup result and the DELETE
  status code for diagnosis.

## [3.0.1] - 2026-05-29

### Fixed

- Playlist deletion no longer fails silently. `delete_managed_playlist` was
  gated on `playlist_exists()` and swallowed any backend error with a generic
  warning while still removing the local row — so a delete that failed on a
  backend (e.g. an Emby credential without item-deletion permission) looked
  successful in Linearr while the playlist lingered on the server. It now calls
  each backend's `delete_playlist` directly (each already no-ops when the
  playlist is gone), logs the real exception, and the UI reports exactly which
  backend(s) the deletion failed on.

### Changed

- Updated the project banner image.

## [3.0.0] - 2026-05-29

### Added

- **Emby as a third backend.** Emby now appears everywhere Plex and Jellyfin
  do — a playlist can target any one, any pair, or all three. New
  `emby_client.py` (standalone `EmbyClient`, ~mirrors the Jellyfin client since
  Jellyfin was forked from Emby) with **API-key auth** via the `X-Emby-Token`
  header. Gated like the others: Emby is only ever contacted when `EMBY_URL`
  **and** `EMBY_API_KEY` are set. Emby carries its own DELETE safety guard —
  the "Linearr never deletes your media" guarantee now covers all three
  backends.
- **CSV-set backend model.** `managed_playlists.backend` now stores a
  comma-separated set in canonical order (e.g. `plex,jellyfin,emby`) instead of
  a 3-value enum, so all 7 combinations fall out naturally. `'both'` is kept as
  a legacy alias for `plex,jellyfin`. New helpers in `media_client.py`:
  `ALL_BACKENDS`, `parse_backend_set`, `format_backend_set`, `primary_backend`.
- **Per-backend checkbox picker.** The "Push to" control on the configure,
  genre, franchise, and Maker pages is now a checkbox per configured backend
  (select any subset), replacing the old Both/Plex/Jellyfin radio.
- **Deterministic franchise cover art.** Franchise playlists now get a real
  TMDB poster uploaded on every backend (`MediaClient.set_playlist_image`)
  rather than relying on the media server's inconsistent auto-composite —
  applied on create, Maker-save, and forced sync (so existing playlists
  backfill a cover via **Sync Now**).
- **Frosted-glass posters on the New Franchise picker cards.** Each card shows
  a representative TMDB poster behind a frosted layer (label stays crisp).
  Auto-discovered Chronolists lists resolve and store a poster at fetch time,
  so new cards get art automatically.
- **Backend credentials in Settings.** Plex / Jellyfin / Emby connection fields
  are now editable on the Settings page (with a per-backend "Test connection"
  button), so a backend can be added or changed later without editing env vars
  or recreating the container. Credentials read DB-value-then-env-fallback via
  `media_client.backend_setting()`; saving clears the `get_client` cache so new
  creds take effect immediately. Env vars still work unchanged as the fallback.

### Database

- `managed_playlists.emby_playlist_id`; `playlist_shows.emby_show_item_id` +
  `emby_movie_item_ids`; `franchise_match_state.emby_found` + `emby_item_id`;
  `franchise_definitions.poster_url` (introspection migrations).
- The `backend` column's value-list `CHECK` is relaxed (table rebuild on
  existing DBs) so set values like `plex,emby` are accepted;
  `db._validate_backend()` replaces the old enum membership check and accepts
  any set of `{plex, jellyfin, emby}` plus the legacy `both`.

### Fixed

- Emby library listing hit Jellyfin's `/UserViews` (404 on Emby) — switched to
  `/Users/{id}/Views`. This was why nothing matched in Emby.
- Emby `user_id` is now resolved lazily via a property, so it's populated
  before a request's path/params are built (previously `/Users/None/Views`).
- Emby playlist creation sent a JSON body (Emby 500 "Unrecognized Guid
  format") — now uses query params. This had blocked all Emby playlist creation.
- Emby playlist deletion was a silent no-op: `delete_playlist` and
  `playlist_exists` checked `GET /Playlists/{id}` (which 404s on Emby), so the
  delete early-returned "already gone" and the DELETE used a lowercase `ids`
  param Emby ignores. Both now verify via `/Items?Ids=` (confirming
  `Type == "Playlist"` before the destructive call) and DELETE with `Ids`.
- Franchise preview 500 when Emby was selected (the cache error-fallback was
  missing the `show_tmdb` key).
- "Invalid backend: plex,jellyfin" when creating a franchise playlist
  (validation ran against the old enum instead of `_validate_backend`).
- `_match_franchise_to_library` returned a 3-tuple for empty definitions while
  callers unpack 4 values.
- Franchise preview over-reported library matches (it checked only whether the
  *show* existed) — it now resolves the actual episode/movie, matching what the
  build produces.
- Index playlist-card poster strip was pinned to 1/5 width per show — now
  auto-fits the column count (1–5) and shows up to 5 posters.
- Configure-page preview sat at "0 episodes" until the first change — it now
  fetches once on load.
- Genre "Push to" picker was missing Emby; the pruning help text referenced the
  `WATCHED_KEEP` variable name literally.

### Tests

- 384 total (Emby backend-set parsing, primary-backend, Emby DELETE safety
  guard, `_validate_backend` set acceptance, franchise empty-match 4-tuple,
  `set_playlist_image` presence).

## [2.5.0] - 2026-05-28

### Added

- **Chronolists auto-discovery.** The weekly franchise refresh now diffs the
  live Chronolists index against the bundled registry and handles two cases
  automatically — no git push required for either:
  - **Auto-migrate** (Scenario A): if Chronolists adds a list whose id
    normalises to a key Linearr already ships under Trakt or local (e.g.
    `james-bond` → `james_bond`), that franchise definition's source switches
    to Chronolists, its items are re-downloaded, and any existing playlists
    re-sync automatically.
  - **Auto-discover** (Scenario B): if Chronolists adds a brand-new list with
    no matching registry key, it is stored with `auto_discovered=1` and a new
    card appears in the New Franchise Playlist picker — sorted alphabetically
    after the 23 bundled cards, before "Build Your Own".
- `_normalize_cl_key(cl_id)` helper — maps Chronolists hyphenated ids to
  underscore registry keys.
- `_discover_new_chronolists_franchises(cl_index)` — core discovery/migration
  logic called at the tail of `refresh_franchise_definitions`.
- `_merged_franchise_list()` in `app.py` — merges the static registry with DB
  source overrides (Scenario A) and auto-discovered entries (Scenario B); used
  by the New Franchise Playlist picker.

### Database

- `franchise_definitions.auto_discovered` column added (`INTEGER DEFAULT 0`).
  Set to `1` on INSERT for auto-discovered entries; never overwritten on UPDATE
  so migrations preserve the original flag. Introspection migration for
  existing DBs.
- `db.list_auto_discovered_franchise_definitions()` — returns definitions with
  `auto_discovered = 1`, ordered by name.

### Tests

- 346/346 (was 326): `_normalize_cl_key` mappings, known Chronolists id count,
  `auto_discovered` flag preserved on UPDATE, `list_auto_discovered` filter,
  `_merged_franchise_list` source override, auto-discovered append, no
  duplication.

## [2.4.0] - 2026-05-28

### Added

- **Chronolists as a franchise source.** Franchise watch orders can now be
  sourced from [Chronolists](https://chronolists.com) alongside Trakt. New
  `chronolists_client.py` (read-only public JSON API, no key required;
  override base URL with `CHRONOLISTS_BASE_URL`). Change detection uses
  Chronolists' own list hash from the index endpoint, so a refresh is a single
  cheap request unless a list actually changed.
- **Expanded franchise registry — 23 bundled franchises** (was 17). Added
  Harry Potter, One Chicago, Battlestar Galactica, The Walking Dead,
  Underworld, and split X-Men into two timelines (Timeline A / Timeline B).
  16 franchises now pull from Chronolists; DCU, Jurassic Park, MonsterVerse,
  John Wick, Alien & Predator, Conjuring Universe, and James Bond remain on
  their existing Trakt/local sources.
- **TMDB-based show matching for franchise episodes.** `ShowSummary` gained a
  `tmdb_id` field (populated by both Plex and Jellyfin); the franchise matcher
  resolves a show by TVDB → TMDB → title+year, so Chronolists' TMDB-only data
  resolves against the library without TVDB ids.
- **`FRANCHISE_REFRESH_DAYS`** env var (default `7`) controls how often
  franchise definitions refresh from upstream. Set `30` for ~monthly.
- **Automatic source migration.** Existing franchise playlists whose bundled
  definition moved from Trakt to Chronolists are upgraded and re-synced
  automatically (one-shot job at startup + on each refresh cycle).

### Changed

- New Franchise Playlist picker credits Chronolists in the source line
  (Trakt retained). Edit on a Chronolists-sourced card opens the Maker
  pre-loaded from Chronolists with fork-on-edit semantics.

### Database

- `franchise_items.show_tmdb_id` and `franchise_definitions.chronolists_id`
  columns added (introspection migrations; existing DBs upgrade in place).
- `VALID_FRANCHISE_SOURCES` now includes `chronolists`.

### Tests

- 326/326 (was 269): Chronolists parser, TMDB show resolution, registry
  integrity, `ShowSummary.tmdb_id` default, source-enum coverage.

## [2.3.0] - 2026-05-28

### Added

- **Franchise Playlists.** A third playlist creation path alongside
  By Show and By Genre. Build a chronologically ordered playlist of movies,
  full series, individual seasons, and individual episodes — mixed in any
  order. Items not yet in your library are tracked in the playlist
  definition and added automatically on the next sync once you add them
  to Plex/Jellyfin. 17 pre-baked franchises ship with the app sourced from
  curated community Trakt.tv lists: MCU, Star Wars, DCEU, DCU (James
  Gunn's new universe), Arrowverse, Star Trek, Stargate, Doctor Who, Buffy
  & Angel, X-Men, Mission: Impossible, Jurassic Park, MonsterVerse, John
  Wick, Alien & Predator, Conjuring Universe, James Bond. Each pre-baked
  franchise can be overridden with a custom Trakt list URL per-playlist,
  or fully customized via the Franchise Playlist Maker (see below).
- **Franchise Playlist Maker** (`/franchise-maker`). In-app visual builder
  for creating fully custom franchise watch orders. Search TMDB for movies
  and TV shows, browse seasons and episodes inline, add items individually
  or with **+ Add Series** to add all seasons of a show at once. Drag to
  reorder. **Import from Trakt URL** populates the editor with any public
  Trakt list as a starting point. Save creates the playlist on
  Plex/Jellyfin and the franchise definition persists in the DB for future
  edits.
- **Edit & Restore for franchise playlists.** Every franchise playlist
  has an **Edit franchise** button on its detail page that opens the
  Maker pre-loaded with the current items. Pre-baked franchises that get
  edited are forked into a user-owned copy — the bundled list stays
  untouched. A **Restore default** button on forked playlists reverts to
  the original bundled list.
- **Trakt API client.** Read-only access to public Trakt.tv lists.
  Bundled `TRAKT_CLIENT_ID` so users don't have to configure anything;
  override via env var for self-hosters who prefer their own Trakt app
  registration. Automatic pagination handles lists of any size.
  Five-minute cache on fetched lists keeps the franchise picker snappy.
- **TMDB API client.** Powers the Franchise Playlist Maker. Supports both
  v3 API Key and v4 Read Access Token formats. Configured via the
  Settings page (`/settings`); also reads `TMDB_API_KEY` env var as a
  fallback. The Maker shows a clear prompt linking to Settings if no key
  is configured.
- **Per-playlist pruning toggle.** Every playlist now has a Pruning
  on/off toggle on the configure page (default on for show/genre, off
  for franchise). Disabling pruning keeps every episode in the playlist
  regardless of watch state.
- **Playlist type badges on the home page.** Each playlist card now
  carries a small **Show**, **Genre**, or **Franchise** tag next to its
  backend badge so the playlist type is obvious at a glance.
- **Smarter playlist card stats.** The home page now reports `X shows`,
  `X movies`, and `X episodes` separately, and only shows non-zero
  counts. A franchise playlist made of three movies shows "3 movies"
  instead of "0 shows · 3 episodes".

### Changed

- **New tagline.** The home page and project description now read:
  *"The missing show sequencer for Plex and Jellyfin. Shows, genres,
  franchises — Automated. Sequenced. Yours."* — reflecting the expanded
  scope of the app.
- **Frosted-glass styling pass.** The Maintenance card, franchise item
  lists, franchise picker cards, Maker panels, and the topbar Settings
  button all use a translucent dark background with backdrop-blur. Hover
  states lift slightly with a stronger shadow.
- **Maintenance section layout.** Buttons in the Maintenance card now
  stack vertically with consistent widths and centered text. For
  franchise playlists, irrelevant buttons (Refresh metadata, Prune
  watched) are hidden so the section is clean.
- **Replaced "Custom" card on the franchise picker** with **"Build Your
  Own"** linking directly to the Maker. Custom Trakt URLs are still
  supported — paste a URL inside the Maker's Trakt import field.
- **Status labels in the franchise preview.** Items now show **In
  library** (green), **Plex only** / **Jellyfin only** (amber), or
  **Not in library** (red) instead of a single in/out badge.

### Fixed

- **Trakt API pagination bug.** `fetch_list_items` was only reading the
  first 100 items of any Trakt list, silently truncating large
  franchises (Doctor Who's 1,092 episodes, Arrowverse's 808 episodes,
  etc.). Now follows pagination headers to fetch every page.
- **`sqlite3.Row.get()` regression in `sync_franchise_playlist`.**
  Several `row.get(...)` calls would silently fail because `sqlite3.Row`
  doesn't implement `.get()`. Replaced with `_row_get(...)` helper or
  `dict(row).get(...)`. Pre-existing bug since v2.2.0 development.
- **Franchise preview cross-backend matching.** Previewing a "both"
  playlist always failed to find anything because `get_client("both")`
  raised, leaving the lookup dicts empty. Now iterates over each
  configured backend.
- **UNIQUE constraint on duplicate edit-and-save.** Editing the same
  pre-baked franchise twice in a row (without renaming) would crash on
  insert. Maker now auto-disambiguates the imported name on load and
  the service layer pre-checks name uniqueness with a clear error
  message.

### Notes

- Caching: 60-second in-memory cache for per-backend library lookups and
  5-minute cache for fetched Trakt lists keep the franchise picker fast
  even with a large Plex library.

## [2.1.0] - 2026-05-27

### Added

- **Outbound webhooks.** Linearr can now POST a JSON event payload to one or
  more configured URLs when a playlist is created, synced with changes, or
  deleted. Webhook URLs are managed on the Settings page with an optional
  label per URL and a "Send test" button to verify delivery. Delivery runs
  in a background thread — a failing endpoint is logged but never blocks a
  sync or any other operation. Events: `playlist.created`, `playlist.synced`
  (only fires when episodes are actually added or removed), `playlist.deleted`,
  and `test`. New file: `webhooks.py`. New DB table: `webhooks (id, url, label)`.

### Fixed

- **Settings page card styling.** `.card` class had no CSS definition since
  v2.0.0; settings sections were unstyled. Added proper background, border,
  and padding.

## [2.0.8] - 2026-05-27

### Changed

- **Playlist stats simplified.** The watched-episode progress bar and
  watched/total fraction have been removed. Because Linearr prunes watched
  episodes down to the configured `WATCHED_KEEP` buffer after every sync,
  the watched count was always artificially low and the percentage was
  meaningless. The Stats section now shows only the current episode count
  in the playlist — a number that is always accurate and useful.

## [2.0.7] - 2026-05-27

### Added

- **Editable playlist name on configure page.** The "Playlist name" field now
  appears as the first row on the configure page (new-playlist flow), pre-filled
  from the show picker. No need to go back if you forgot to set a name or want
  to change it before hitting Create.

### Fixed

- **Auto-name blocked by browser validation.** The `required` attribute on the
  show picker name input caused the browser to show "Please fill out this field"
  before the auto-name JS could run. Removed `required`; the JS submit handler
  and the server-side route both auto-generate "Linearr 001" (first unused
  increment) when the field is blank.

## [2.0.6] - 2026-05-27

### Fixed

- **Auto-name on proceed.** Submitting the show picker or genre builder without
  entering a playlist name now auto-generates "Linearr 001" (incrementing to
  the first unused number) instead of sending a blank name to the server.
- **Genre page "Create Playlist" button permanently disabled.** The button was
  server-rendered as `disabled` when no name was present, with no client-side
  logic to re-enable it. A new JS block watches the name input and dynamically
  toggles `disabled` based on whether a name has been typed and whether a
  preview has been run. The `prev_name` condition was removed from the
  server-side disabled check (the `matched_shows` guard alone is sufficient
  for the initial state before any preview).

## [2.0.5] - 2026-05-27

### Changed

- **Commit card redesign.** The "Create Playlist" / "Configure →" / "Add to
  Playlist" action is now a frosted-glass card (`rgba(14,15,19,0.78)` +
  `backdrop-filter: blur(14px)`) matching the topbar treatment, right-aligned
  with equal inner padding (`1.2rem` all sides) and a matching right margin so
  button-to-card-edge equals card-to-section-edge. A blue glow
  (`box-shadow: 0 0 18px rgba(77,150,255,0.38)`) highlights the primary button
  inside the card. Sticky positioning is isolated in a transparent `.commit-bar`
  wrapper so the frosted glass `.commit-card` inner element sizes naturally to
  its content (avoids sticky+backdrop-filter browser compositing bug). On mobile
  the card is right-aligned with a tighter `0.75rem` margin and the button uses
  a pill shape (`border-radius: 999px`).
- **Disabled button style.** Buttons with the `disabled` attribute render at
  `opacity: 0.42` — the "Configure →" card on the show picker dims until a
  show is selected, giving clear affordance that the action is not yet available.
- **Linearr logo on create buttons.** A 16px favicon icon prefixes "Create
  Playlist" on the configure and genre playlist pages.

## [2.0.4] - 2026-05-27

### Fixed

- **Mobile commit button pill shape.** On phones the "Create Playlist" /
  "Configure →" button is now `min(100%, 320px)` wide with `border-radius:
  999px` — a centered pill shape rather than edge-to-edge full-width. Text
  is centered within the pill.

## [2.0.3] - 2026-05-27

### Fixed

- **Commit bar visual treatment.** The "Create Playlist" / "Configure →" button
  now sits in a clearly defined action zone: centered (was right-flush),
  with a `border-top` separator and a solid `var(--bg)` background so content
  no longer bleeds through when the bar is sticky at the bottom of the
  viewport. On mobile the button remains full-width; on desktop it sizes to
  its content and is centered.

## [2.0.2] - 2026-05-27

### Changed

- **Playlist type picker.** "+ New Playlist" now opens an interstitial page
  (`/new`) with two large cards — "By Show" and "By Genre" — before routing
  to the respective creation flow. The separate "+ Genre" topbar button is
  removed; both playlist types are now discovered through a single entry
  point. Back-navigation on the show picker and configure page updated.
- **"Configure →" floating button on show picker.** "Next: configure →" moved
  from the sticky toolbar into a sticky commit-bar at the bottom (consistent
  with the configure page), disabled until at least one show is selected.
  The toolbar now holds only the name input, filter, counter, and clear button.

### Fixed

- **Desktop pill background full-width.** `.pill-toggle` inside a column-flex
  `.sort-mode-content` (which has `flex: 1`) stretched to the full section
  width. Added `align-self: flex-start` to the global `.pill-toggle` rule so
  it always sizes to its content.

## [2.0.1] - 2026-05-27

### Fixed

- **Mobile: huge input boxes on show picker** — `flex: 1 1 220px` in a
  column-flex `.builder-toolbar` grew single-line text inputs to viewport
  height on phones. Overridden to `flex: 0 0 auto; height: auto` in 480px
  block; applies to both Playlist name and Filter shows inputs.
- **Mobile: Episode Order pills broken layout** — `pill-toggle { flex-wrap:
  wrap }` at ≤768px caused the 5-pill group to spill across 2–3 rows, each
  pill losing the outer container's connected border-radius styling. Changed
  to `flex-wrap: nowrap; overflow-x: auto` so pills scroll horizontally in
  a single connected row. Applied to all `pill-toggle` variants including
  `pill-toggle-5`.
- **Mobile: date + title overlap in episode preview** — `.preview-list-with-
  date li` had a higher-specificity 5-column grid that wasn't collapsed by
  the 768px media-query override for `.preview-list li`. Added explicit
  `preview-list-with-date li` collapse to 2 columns and mapped `.pl-date`
  to `grid-column: 2` (stacks below show/se, above title).
- **Mobile: commit bar button text not centered** — `.commit-bar .btn {
  width: 100% }` made the button span full-width but left text left-aligned.
  Added `justify-content: center`.
- **Capitalization:** "Create playlist" → "Create Playlist" on configure
  and genre-playlist pages; "+ New playlist" → "+ New Playlist" in topbar.

## [2.0.0] - 2026-05-27

### Added

- **REST API** (`/api/v1/`). JSON endpoints for external integrations: list
  playlists, get playlist detail (with shows + rules), trigger manual sync,
  list configured backends with health checks, genre cache, and playlist
  stats. Authenticated via `Authorization: Bearer <key>` or `?api_key=`.
  API key auto-generated on first boot, persisted in `managed_settings`,
  overridable via `LINEARR_API_KEY` env var. Settings page at `/settings`
  shows the key with a regenerate button.
- **Playlist analytics.** After each sync, watched/total episode counts are
  collected from the first available backend and stored as JSON on
  `managed_playlists.last_stats`. Index page cards show a progress bar.
  Playlist detail gets a Stats section. A `/api/v1/playlists/{id}/stats`
  endpoint exposes the raw JSON.
- **Mobile-first CSS redesign.** Breakpoints at 768px (tablet) and 480px
  (phone). Topbar actions hide non-primary buttons on small screens. Poster
  grid drops to 3→2 columns. Config card stack goes single-column on phone.
  Builder toolbar stacks vertically. Commit button goes full-width. Genre
  preview section max-width removed on phone.
- **Settings page** (`/settings`) to view and regenerate the API key.
- **`MediaClient.list_tv_sections()`** ABC method (lightweight health probe)
  implemented in Plex and Jellyfin clients.
- **`MediaClient.list_playlist_episodes()`** ABC method for stats collection.

### Database (additive only)

- `managed_settings` table (key/value store for API key)
- `managed_playlists.last_stats` TEXT column (JSON analytics blob)
- New helpers: `db.get_setting`, `db.set_setting`, `db.update_playlist_stats`

### Changed

- **`sync_playlist`** now collects and persists analytics stats after each
  tail rebuild.
- **`PlaylistView`** gained `last_stats` field (parsed from JSON on load).
- **`templates/base.html`** topbar nav wrapped in `<nav class="topbar-actions">`
  for mobile CSS targeting.

### Tests

- **187 passing** (up from 175). 12 new: 9 REST API (auth, list, 404, query
  param auth, backends health) + 3 analytics (stats store/retrieve, default
  None field).

### Files touched

`app.py` · `media_client.py` · `plex_client.py` · `jellyfin_client.py` ·
`db.py` · `service.py` · `templates/settings.html` · `templates/base.html` ·
`templates/index.html` · `templates/playlist.html` · `static/style.css` ·
`tests.py` · `CHANGELOG.md` · `README.md` · `CLAUDE.md`.

## [1.8.4] - 2026-05-27

### Fixed

- **Smart rules value input still too narrow** — `input.rules-add-value-text`
  uses element+class specificity so it wins over `.number-input { width: 5rem }`
  (defined later in the CSS file). Added `text-align: left`.
- **Episode order preview: Air Date** now shows illustrative `YYYY-MM-DD`
  dates (shows staggered by 3 days, episodes weekly) so chronological
  interleaving is visible. Note: "Illustrative dates — actual playlist uses
  real air dates from your library."
- **Episode order preview count** raised to 25 (was 15).

## [1.8.3] - 2026-05-27

### Added / Fixed

- **Episode order preview for all modes** — Weighted, Air Date, and Shuffle
  now produce episode lists (15 items) with a mode note.
- **Per-show weight fields in genre playlist creation** — Weight inputs appear
  per-show when Weighted mode is active; live-update the preview; submitted at
  create time via `create_genre_playlist(weights=)` and smart-rules path.
- **Smart rules value input** widened to 320 px.

## [1.8.2] - 2026-05-27

### Fixed

- **Genre playlist JS crash after "Preview Matches"** — `var showTitles`
  declared after first use (`applySortMode()`) → TypeError killed IIFE; genre
  fetch stuck at "Loading genres…", all options unresponsive. Fixed by moving
  `showTitles` init before `applySortMode()` and adding `!showTitles` guard.
- **Matched shows preview capped at 50** for performance.
- **Episode exclusion `scheduleUpdate is not defined`** — `scheduleUpdate` was
  IIFE-local; moved to outer script scope.
- **Smart rules inputs widened** from 150 px to 220 px.

## [1.8.1] - 2026-05-27

### Fixed

- Genre filter input CSS stretch; options bar vertical alignment; episode order
  description always below pills (genre + configure pages); block size +
  weighted controls inline in episode order card; smart rules inline add-row
  (no `prompt()` dialogs); simulated episode order preview panel in genre
  playlist builder; Preview button works before name is set (`formnovalidate`);
  Jellyfin episode exclusion (`_ensure_authenticated()` before param
  construction, `resp.ok` guard); smart rule add/delete calls
  `sync_playlist(force=True)`.

## [1.8.0] - 2026-05-26

### Added

- **Smart playlist rules** — new `rule_mode='rules'` for genre playlists, driven
  by `playlist_rules` DB rows instead of the `genre_filter` CSV. Rule types:
  `year_min`, `year_max`, `status`, `content_rating`, `season_min`,
  `season_max`, `rating_min`, plus genre include rules. Non-genre rules combine
  with AND logic; genre includes are OR'd at query time. `rule_mode='genre'`
  preserves the v1.4–v1.7 behaviour for existing playlists.
- **New ShowSummary fields:** `status`, `content_rating`, `season_count`,
  `community_rating` — populated by both PlexClient (`show.status`,
  `show.contentRating`, `show.childCount`, `show.audienceRating`) and
  JellyfinClient (`item.Status`, `item.OfficialRating`, `item.ChildCount`,
  `item.CommunityRating`). No extra network round-trips — they ride on the
  existing `list_all_shows()` / `list_shows_by_genres()` responses.
- **`service._apply_rules(shows, rules)`** filters a list of `ShowSummary`
  objects against a rule set. Null-absent field values are permissive (they
  pass through rather than being filtered out).
- **`service._resolve_smart_shows(rules, target_backends)`** queries backends
  with genre-include rules and applies non-genre rules as post-filters. Shares
  the `_dedup_show_summaries_to_configs()` dedup helper with
  `_resolve_genre_shows` (extracted to eliminate duplication).
- **New routes:** `POST /playlist/<id>/rules/add`,
  `POST /playlist/<id>/rules/<rule_id>/delete`.
- **Sync now respects `rule_mode`:** `_genre_sync_discover` dispatches to
  `_resolve_smart_shows` when `rule_mode='rules'`, or the legacy
  `_resolve_genre_shows` for `rule_mode='genre'`.

### Changed

- **`_resolve_genre_shows`** dedup logic extracted to shared
  `_dedup_show_summaries_to_configs()` so both genre and smart-rule paths
  use the same title+year→TVDB matching.
- **`_list_series_via_items`** in `JellyfinClient` now requests additional
  fields (`ChildCount`, `CommunityRating`, `OfficialRating`, `Status`) to
  populate the new `ShowSummary` attributes.

### Database (additive only)

- `playlist_rules` table (id, playlist_id FK, rule_type, operator, value)
- `managed_playlists.rule_mode TEXT NOT NULL DEFAULT 'genre'`
- New helpers: `db.list_rules`, `db.add_rule`, `db.remove_rule`,
  `db.set_rule_mode`

### Tests

- **175 passing** (up from 149). 26 new: 6 genre cache + 20 covering
  `_apply_rules` (year, status, season count, rating, combined, content
  rating).

### Files touched

`media_client.py` · `plex_client.py` · `jellyfin_client.py` · `db.py` ·
`service.py` · `app.py` · `tests.py` · `CHANGELOG.md` · `README.md` ·
`CLAUDE.md`.

## [1.7.0] - 2026-05-26

### Added

- **Genre picker UI** replaces the freeform genres text input. All genre tags
  from your configured media servers are loaded via `/api/genres` and displayed
  as selectable pills. A filter input narrows the list; selected genres appear
  as removable chips. Genres are cached in the DB (7-day TTL) and refreshed
  weekly by the scheduler, plus on first startup.
- **`MediaClient.list_all_genres()`** abstract method implemented in both
  `PlexClient` (`section.listChoices("genre")`) and `JellyfinClient`
  (`GET /Genres?IncludeItemTypes=Series`). Returns sorted genre names across
  all TV libraries.
- **`/api/genres`** JSON endpoint returning cached genre lists keyed by backend
  name (`{"plex": [...], "jellyfin": [...]}`). Falls back to live fetch on
  cache miss.
- **Genre cache tables** (`genre_cache` + `genre_cache_meta`) with 7-day TTL
  and `db.get_genre_cache` / `db.set_genre_cache` helpers.
- **`scheduler._refresh_genre_cache()`** job: fires once at startup (date
  trigger) and weekly thereafter to keep genre lists current.

### Fixed

- **Thumb poster cache reduced from 24 h to 1 h.** After a metadata refresh,
  new poster images reach the browser within the hour instead of potentially
  waiting a day.
- **Exclusion picker backend/ID mismatch.** The per-episode exclusion widget
  on the configure page was sending `show_rating_key` (the DB primary key) to
  the episodes API even when the show only existed on the other backend.
  Fixed to resolve the correct backend and ID for the show's actual side.
  Also now shows a cross-backend note for 'both' playlists.
- **Genre preview shows poster images.** `matched_shows` dicts now carry
  `thumb` and `thumb_backend` fields so the genre preview renders posters
  instead of empty placeholders.
- **Genre preview section constrained** to max-width 560px (no controls column),
  preventing it from stretching full page width.
- **"Click Preview first" hint relocated** from inside the commit bar to a
  separate `<p>` above it, visible only before the first preview.

### Changed

- **"Push to", "Filter", and "Auto-update" consolidated** into a single compact
  options bar in both `new_genre.html` and `configure.html`. No more three
  separate full-width bars with hints.
- **`new_genre.html` full rewrite** with the genre pill picker, consolidated
  options, and clean commit bar.

### Tests

- **149 passing** (up from 143). 6 new tests covering genre cache store,
  retrieve, expiry, overwrite, empty-list, and backend isolation.

### Files touched

`app.py` · `media_client.py` · `plex_client.py` · `jellyfin_client.py` ·
`db.py` · `scheduler.py` · `templates/new_genre.html` ·
`templates/configure.html` · `static/style.css` · `tests.py` ·
`CHANGELOG.md` · `README.md` · `CLAUDE.md`.

## [1.6.3] - 2026-05-26

### Fixed

- **Title normalization now strips trailing disambiguation suffixes** that
  Plex appends but Jellyfin often omits — country codes like `(US)`, `(UK)`,
  `(AU)` and premiere years like `(2018)`, in any combination or order.
  Examples fixed automatically on the next sync:
  - "Whose Line Is It Anyway? (US)" (Plex) ↔ "Whose Line Is It Anyway?"
    (Jellyfin)
  - "Yellowstone (2018)" (Plex) ↔ "Yellowstone" (Jellyfin)
  - Any similar suffixed-vs-plain mismatch where the TVDB ID alone didn't
    resolve it.
  Year disagreement (when both sides report different years) still
  distinguishes genuinely different shows (e.g. US vs UK versions).
  No action needed — existing mismatches auto-heal on the next background
  sync or "Sync now".

### Tests

- **5 new tests** for `normalize_title` suffix stripping and updated
  `titles_match` cases to match the new behavior (143 total, all passing).

### Files touched

`media_client.py` · `tests.py` · `app.py` · `CHANGELOG.md` · `README.md` ·
`CLAUDE.md`.

## [1.6.2] - 2026-05-26

### Changed

- **Sort mode pill order** updated to Rotation → Blocks → Weighted → Air Date
  → Shuffle across all three templates (`playlist.html`, `configure.html`,
  `new_genre.html`). Blocks is a direct extension of Rotation (same concept,
  N episodes per cycle instead of 1); Weighted adds per-show asymmetry on top;
  Air Date and Shuffle break from the rotation paradigm entirely. The new order
  reflects increasing conceptual distance from plain round-robin.
- **Banner image** updated.

### Files touched

`app.py` · `templates/playlist.html` · `templates/configure.html` ·
`templates/new_genre.html` · `images/banner.png` · `CHANGELOG.md` ·
`README.md` · `CLAUDE.md`.

## [1.6.1] - 2026-05-26

Bug-fix and polish release.

### Fixed

- **Genre preview layout broken.** `.rotation-row` is a CSS grid with
  `60px 1fr auto` columns. The preview only rendered `.rotation-info` with
  no leading `.rotation-poster` div, so the text landed in the 60px slot
  and the row stretched incorrectly. Fixed by adding an empty placeholder
  poster div.
- **TVDB IDs always empty on Plex.** `PlexClient.list_all_shows()` and
  `list_shows_by_genres()` called `section.all()` / `section.search()`
  without `includeGuids=1`. Plex's abbreviated list response omits `<Guid>`
  elements unless that parameter is present, so `show.guids` was always
  `[]` and `_tvdb_id_from_guids()` always returned `None`. TVDB-based
  cross-backend matching (introduced in v1.6.0) was therefore entirely
  inert — title-mismatched shows like "Yellowstone (2018)" on Plex vs
  "Yellowstone" on Jellyfin were never auto-linked.
- **Picker and genre deduplication missed TVDB-only matches.** Both
  `_aggregated_shows` (show picker) and `_resolve_genre_shows` (genre
  playlist creation) deduplicated solely by normalized title+year. Shows
  with different titles on each backend appeared as two separate "Plex only"
  / "Jellyfin only" entries. Both functions now fall back to TVDB ID
  equality after title+year fails to match.
- **Auto-update hint showed literal `PRUNE_INTERVAL_MINUTES`** instead of
  the configured value. Fixed to `{{ "10" }}`, matching the pattern already
  used in the page description above it.
- **Genre placeholder suggested "Sci-Fi"** — an input that returns zero
  results on Plex, which tags the genre as "Science Fiction". Changed to
  "Science Fiction, Drama".

### Added

- **Genre hint** explaining that genre names must match exactly what the
  media server uses, with the Plex "Science Fiction" vs "Sci-Fi" example.
- **Version footer** on every page (`v1.6.1`). Defined as `__version__` in
  `app.py`, injected via context processor, rendered in `base.html`.

### Files touched

`app.py` · `plex_client.py` · `service.py` · `templates/new_genre.html` ·
`templates/base.html` · `static/style.css` · `CHANGELOG.md` · `README.md`.

## [1.6.0] - 2026-05-26

### Added

- **Block-size and Weight fields in genre playlist creation.** `new_genre.html`
  now shows a block-size input when "Blocks" is selected and a hint row when
  "Weighted" is selected — matching the existing `configure.html` behaviour.
  JS `applyMode()` toggles visibility on pill change. `new_genre_action` reads
  and forwards `block_size` to `service.create_genre_playlist`.
- **Refresh metadata button** on the playlist detail page (maintenance section,
  alongside Sync Now). Sends `POST /playlist/<id>/refresh-metadata`. On Plex
  calls `show.refresh()` (PUT `/library/metadata/{id}/refresh`); on Jellyfin
  calls `POST /Items/{id}/Refresh` with `FullRefresh` mode. Both backends are
  fire-and-forget — flash message says "queued". Excluded shows are skipped.
  New `MediaClient.refresh_show_metadata(rating_key)` abstract method
  implemented in both `PlexClient` and `JellyfinClient`.
- **TVDB ID cross-backend matching.** `ShowSummary` gains `tvdb_id: str | None`.
  Plex populates it via `show.guids` (`"tvdb://12345"` prefix); Jellyfin via
  `ProviderIds.Tvdb` (added `ProviderIds` to the `GET /Items` field list). At
  add-time (`_enrich_configs_with_matches`) and heal-on-sync
  (`_heal_missing_ids`), TVDB ID is tried first and title+year is the fallback.
  Fixes cases like "Stargirl" ↔ "DC's Stargirl" that title normalization
  could not bridge.
- **Manual show-link UI.** When a show appears in the warning banner as "not
  on Jellyfin / Plex", a `▸ Link manually…` disclosure widget offers a
  `<select>` of all shows on the missing backend. Submitting writes
  `plex_show_item_id` or `jellyfin_show_item_id` directly on the playlist row
  via `service.link_show_backend()` and triggers a tail rebuild. New route:
  `POST /playlist/<id>/shows/<key>/link`. `all_shows` (the full aggregated
  list, not the filtered-available list) is passed to the playlist template as
  the picker source. `missing_on` entries now carry `rating_key` for URL
  construction.

### Files touched

`media_client.py` · `plex_client.py` · `jellyfin_client.py` · `service.py` ·
`app.py` · `templates/new_genre.html` · `templates/playlist.html` ·
`CHANGELOG.md` · `CLAUDE.md`.

## [1.5.2] - 2026-05-26

Bug-fix release from a post-v1.5.1 code review.

### Fixed

- **Drag-to-reorder permanently broken on genre playlists with excluded
  shows.** `reorder_shows` validated the submitted key set against *all* rows
  including soft-deleted (`is_excluded=1`) ones, so any excluded show caused
  a key-mismatch error. Filter now targets active shows only.
- **Block-size and weight fields always visible regardless of sort mode.**
  `.weight-control { display: flex }` in `style.css` overrode the HTML
  `hidden` attribute, making both fields visible on every configure-page load.
  Fixed with `[hidden] { display: none !important }` global reset rule.
- **Add-show preview wrong for rotation_blocks / shuffle_chronological
  playlists.** The add-configure preview call omitted `block_size` and
  `shuffle_seed`, so blocks-mode playlists always previewed as round-robin
  (block_size=1) and shuffle playlists used a non-deterministic seed. Now
  passes both from `view.block_size` / `view.shuffle_seed`.
- **Crossover group/link routes lacked playlist-ownership checks.** The
  `crossover_add_link`, `crossover_delete_group`, and `crossover_remove_link`
  routes operated on raw IDs without verifying they belonged to the playlist
  in the URL, allowing accidental or crafted requests to corrupt groups on
  other playlists. All three now 404 on a mismatch.
- **Crossover link items displayed raw Plex/Jellyfin internal IDs** instead
  of the show title, making the crossover groups UI unreadable. `get_playlist_view`
  now annotates each link dict with `show_title` (with fallback to the raw key
  for shows removed from the playlist since the group was created).
- **Add-show mode label in configure page incorrectly showed "Rotation"** for
  rotation_blocks, rotation_weighted, and shuffle_chronological playlists. Now
  maps all five sort modes correctly.
- **Dead code in `preview_playlist`:** `or not configs` inside the
  per-element list comprehension was always `False` when `configs` is
  non-empty. Removed; the real empty-result fallback two lines below was
  already correct.

### Files touched

`app.py` · `service.py` · `static/style.css` · `templates/configure.html` ·
`templates/playlist.html` · `CHANGELOG.md`.

## [1.5.1] - 2026-05-26

Bug-fix release addressing issues found during post-v1.3.0 testing.

### Fixed

- **False "Plex Only" / "Jellyfin Only" badges in the show picker.** The
  cross-backend dedup key in `_aggregated_shows()` and `_resolve_genre_shows()`
  used `year or 0`, turning `None` into `0`. When one backend carried a year and
  the other didn't, the show appeared as two separate entries with one-backend
  badges. Both functions now match `titles_match()` semantics: year only
  disambiguates when both sides carry a non-None year that differs.
- **Unselecting a show in the picker sent it to the bottom of the grid.** The
  picker now records each tile's original DOM position at init and inserts
  unchecked tiles back at their correct spot instead of appending.
- **Shuffle hint text reworded** from "A stable seed is generated at create
  time" to "The shuffle order is fixed until you hit Reshuffle on the playlist
  page."
- **Pill order normalized** across all three templates: Rotation, Air Date,
  Blocks, Weighted, Shuffle (was: Rotation, Blocks, Weighted, Air Date, Shuffle
  on two pages).
- **Number input browser spinners removed** via CSS for weight and block-size
  fields.
- **Backend badge background opacity increased** for better readability on
  poster thumbnails.

### Files touched

`app.py` · `service.py` · `static/picker.js` · `static/style.css` ·
`templates/configure.html` · `templates/new_genre.html` ·
`templates/playlist.html` · `CHANGELOG.md`.

## [1.5.0] - 2026-05-26

Manual crossover grouping for air_date mode. When title-based Part N detection
misses a crossover (e.g. "Buffy — Fool for Love" / "Angel — Darla"), users can
explicitly link specific episodes across shows and set their play order.

### Added

- **Crossover groups** — two new tables (`crossover_groups` + `crossover_links`)
  let users manually link specific episodes across different shows. Within the
  same air date, grouped episodes sort before non-grouped ones and play in
  the user-defined `sort_index` order.
- **Sort key integration.** `rotation.air_date_sequence()` gains an optional
  `crossover_map` parameter. The sort key inserts `(0, group_id, sort_idx)`
  between `air_date` and `part_number`, so manually grouped episodes take
  priority over title-based Part N detection on the same day.
- **Crossover groups UI section** on the playlist detail page (air_date mode
  only). Create groups, add episodes by show + season + episode number, remove
  episodes, and delete groups. Each action rebuilds the playlist tail on every
  enabled backend.
- **New routes:** `POST /playlist/<id>/crossover/create`,
  `/crossover/<group_id>/add`, `/crossover/<group_id>/delete`,
  `/crossover/link/<link_id>/remove`.
- **`PlaylistView.crossover_groups`** field populated from
  `db.list_crossover_groups()` — available in the playlist template as
  `playlist.crossover_groups`.

### Changed

- **`rotation.compose()`** and **`rotation.rebuild_tail()`** gain a
  `crossover_map` kwarg (only used by air_date mode, ignored by others).
- **`service._rebuild_tail_on()`** and **`service._rebuild_playlist_tails()`**
  build and pass `crossover_map` when the sort mode is `air_date`.
- **`service.get_playlist_view()`** populates the new `crossover_groups`
  field.

### Database (additive only)

- New table `crossover_groups` (id, playlist_id FK, label, sort_index)
- New table `crossover_links` (id, group_id FK, show_rating_key, season,
  episode, sort_index)
- New helpers: `db.list_crossover_groups`, `db.create_crossover_group`,
  `db.delete_crossover_group`, `db.add_crossover_link`,
  `db.remove_crossover_link`

### Tests

- 136 passing (up from 131). New: 5 covering crossover_map sort key
  (grouped-before-non-grouped, group_id ordering, part_number fallback),
  compose passthrough, and rebuild_tail passthrough.

### Files touched

`rotation.py` · `service.py` · `db.py` · `app.py` ·
`templates/playlist.html` · `static/style.css` · `tests.py` ·
`CHANGELOG.md`.

## [1.4.0] - 2026-05-26

Dynamic genre playlists. Instead of picking shows one-by-one, you now choose
a genre (or several) and Linearr builds the playlist from every matching show
in your library. Background sync re-queries your library and auto-adds new
shows that match. Exclude individual shows and they stay excluded across syncs.

### Added

- **Dynamic genre playlists** — new playlist type alongside manual. Created via
  the **+ Genre** button in the topbar. Input genre names (comma-separated,
  e.g. "Sci-Fi, Drama"), preview matches live before creating.
- **Genre auto-discovery on sync.** Every background sweep re-queries the
  configured backends for genre-matching shows. New matches are auto-added
  (with default settings: Season 1+, no specials). Excluded shows stay
  excluded — the `is_excluded` flag persists across syncs.
- **Per-show exclude button** on the playlist rotation list (genre playlists
  only). Removes the show from rotation but keeps it in the DB so future
  syncs don't re-add it. An **Excluded shows** section below the rotation
  list offers a **Re-include** button per show.
- **`MediaClient.list_shows_by_genres(genres)`** — new ABC method.
  - **Plex:** `section.search(genre=...)` per TV section, deduplicated by
    ratingKey.
  - **Jellyfin:** shared `_list_series_via_items(extra_params)` with
    `genres="pipe|delimited"` query param for multi-genre OR matching.
- **`service._resolve_genre_shows(genres, target_backends)`** — queries each
  backend, deduplicates across backends via title+year, fills in cross-backend
  IDs for 'both' setups.
- **`service._genre_sync_discover(playlist_id, row, configs)`** — called
  during every sync for genre playlists. Compares discovered shows against
  existing (including excluded) and adds only genuinely new ones.
- **`service.set_show_excluded(playlist_id, rk, excluded)`** — soft-delete
  or re-include a single show, then rebuilds tails.
- **New routes:** `GET/POST /new/genre` (genre playlist creation + preview),
  `POST /playlist/<id>/exclude`, `POST /playlist/<id>/include`.
- **New template:** `templates/new_genre.html` — genre name input, genre
  comma-separated input, backend picker, sort mode pill, unwatched-only
  toggle, auto-update toggle, preview section, create button.

### Changed

- **`service.PlaylistView`** gains `playlist_type` (default `"manual"`),
  `genre_filter` (nullable CSV string), and `excluded_shows` (list of
  soft-deleted show dicts).
- **`service.get_playlist_view`** splits `list_shows` rows into active
  (`is_excluded=0`) and excluded (`is_excluded=1`).
- **`service._rebuild_playlist_tails`** filters out `is_excluded=True`
  configs so excluded shows never contribute episodes.
- **`templates/playlist.html`** subtitle shows a **Genre** badge with the
  genre filter when `playlist_type == 'genre'`. Exclude button per row on
  genre playlists. New "Excluded shows" section with re-include buttons.
- **`templates/base.html`** topbar gains a **+ Genre** button alongside
  **+ New playlist**.

### Database (additive only)

- `managed_playlists.playlist_type TEXT NOT NULL DEFAULT 'manual'
  CHECK(playlist_type IN ('manual','genre'))`
- `managed_playlists.genre_filter TEXT` (comma-separated genre names)
- `playlist_shows.is_excluded INTEGER NOT NULL DEFAULT 0`
- `db.VALID_PLAYLIST_TYPES` tuple constant
- New helpers: `db.set_genre_filter`, `db.set_show_excluded`

### Tests

- 131 passing (up from 117). New: 14 covering `_parse_genre_csv` parse +
  whitespace tolerance, `ShowConfig.is_excluded` default and explicit,
  `PlaylistView` genre-field defaults, `VALID_PLAYLIST_TYPES` shape, genre
  CSV round-trip, `weight + is_excluded` field independence.

### Files touched

`media_client.py` · `plex_client.py` · `jellyfin_client.py` · `db.py` ·
`service.py` · `app.py` · `templates/new_genre.html` ·
`templates/base.html` · `templates/playlist.html` · `static/style.css` ·
`tests.py` · `CHANGELOG.md`.

## [1.3.0] - 2026-05-26 - 2026-05-26

Three new sequencing modes alongside Rotation and Air Date. Existing
playlists keep their current mode and behave identically — the new modes
are opt-in per playlist.

### Added

- **Weighted Rotation** (`sort_mode = 'rotation_weighted'`). Per-show
  weight on `playlist_shows.weight` (default 1). New
  `rotation.interleave_weighted(shows_episodes, weights)` takes
  `weights[i]` episodes from show *i* per cycle, falling back to
  partial-take when a show is short. Solves the "The Simpsons drowns out
  Firefly" problem. Edit weights inline on the playlist detail page when
  the playlist is in this mode.
- **Block Scheduling** (`sort_mode = 'rotation_blocks'`). A single
  playlist-wide `block_size` integer on `managed_playlists` (default 1 =
  current rotation behavior). New `rotation.interleave_blocks(...)`. Pill
  toggle on the playlist detail page reveals a block-size adjuster when
  this mode is active.
- **Intelligent Shuffle** (`sort_mode = 'shuffle_chronological'`). New
  `rotation.shuffle_chronological(shows_episodes, seed)` produces a
  random sequence where each show's episodes stay in chronological order
  and same-show consecutive plays are avoided when possible (with a
  forced-fallback when one show vastly outnumbers the rest). A seed is
  auto-generated at creation; users can hit **Reshuffle** on the playlist
  detail page to regenerate it.
- **`rotation.VALID_SORT_MODES`** constant — a tuple of every supported
  value, used as the single source of truth for validation in
  `db.set_sort_mode`, `service.create_managed_playlist`,
  `service.set_playlist_sort_mode`, and `app.change_sort_mode`.
- **New service helpers**: `set_playlist_block_size`,
  `reshuffle_playlist` (regenerates seed + rebuilds tails),
  `set_show_weight`. Each rebuilds tails on every enabled backend after
  updating the relevant value.
- **New routes**: `POST /playlist/<id>/block_size`,
  `POST /playlist/<id>/reshuffle`, `POST /playlist/<id>/weight`.

### Changed

- **`rotation.compose()` and `rotation.rebuild_tail()`** gained keyword-
  only args `weights`, `block_size`, `shuffle_seed`. Backward compat:
  unused args are ignored on old modes. For rotation_* modes the
  remainder-per-show approach is preserved; for air_date and shuffle the
  full canonical sequence is composed once, then kept items are dropped
  (preserves the original randomized/dated order).
- **`service._rebuild_tail_on` and `_rebuild_playlist_tails`** thread the
  new params through. Both default values from the row when callers
  don't override.
- **Configure page sort-mode pill** expanded from 2 options to 5 (with
  `.pill-toggle-5` modifier that wraps on narrow screens). Reactive
  hint text and conditional visibility for the block-size input and
  per-show weight steppers.
- **Playlist detail subtitle** shows the new mode label and block size
  when relevant.

### Database (additive only)

- `playlist_shows.weight INTEGER NOT NULL DEFAULT 1`
- `managed_playlists.block_size INTEGER NOT NULL DEFAULT 1`
- `managed_playlists.shuffle_seed INTEGER` (nullable)

### Tests

- 117 passing (up from 98). New: 18 covering the three sequencing modes
  — depletion-fallback for weighted, exact block patterns for blocks,
  per-show chronological order preservation + no-consecutive-when-
  possible + forced-fallback for shuffle, dispatch table, `rebuild_tail`
  in each new mode, and `VALID_SORT_MODES` shape.

### Files touched

`rotation.py` · `service.py` · `db.py` · `app.py` ·
`templates/configure.html` · `templates/playlist.html` · `static/style.css` ·
`tests.py`.

## [1.2.0] - 2026-05-26

Quick-wins release based on a code review. No backend semantics change; new
features are additive and opt-in.

### Added

- **Manual "Sync Now" button** on the playlist detail page. Hits a new
  `POST /playlist/<id>/sync` route that calls `service.sync_playlist(id,
  force=True)`, bypassing the per-playlist `auto_sync` opt-out so a manual
  click always works. Flash reports `(added, removed)` counts.
- **Per-episode exclusions.** Each show's configure card now has an
  *Exclude individual episodes* expander. Lazy-loaded on open via a new
  `GET /api/episodes/<rk>?b=<backend>` endpoint, grouped by season into
  collapsible sections. Uncheck an episode to skip it everywhere this show
  appears in this playlist. Excluded set is filtered out of
  `_episodes_for_config()` before rotation/air-date composition, so it
  applies uniformly across both backends.
- **`service.sync_playlist(playlist_id, force=False)`** signature gained
  the optional `force` kwarg. Scheduler always uses `force=False`;
  user-initiated Sync Now uses `force=True`.

### Changed

- **`_rebuild_playlist_tails` helper** consolidates the 5 near-identical
  per-backend loops in `service.py` (`add_shows_to_playlist`,
  `reorder_shows`, `set_playlist_sort_mode`, `set_playlist_unwatched_only`,
  `sync_playlist`). Pure refactor — zero behavior change. `sort_mode` and
  `unwatched_only` can be passed as kwargs by callers that have just
  written a new value to the DB but haven't refetched the row.

### Database (additive only)

- `playlist_shows.excluded_episode_keys TEXT NOT NULL DEFAULT ''` — CSV of
  `S:E` pairs (e.g. `"1:1,3:14"`). New helper `db.set_excluded_episodes()`.

### Tests

- 87 → 98 passing. New: CSV parse/serialize round-trips, malformed-input
  tolerance, default-empty behavior, sorted output stability.

### Files touched

`service.py` · `db.py` · `app.py` · `templates/playlist.html` ·
`templates/configure.html` · `static/style.css` · `tests.py`.

## [1.1.0] - 2026-05-24

**Linearr is now for Plex *and* Jellyfin.** Each managed playlist can target
Plex, Jellyfin, or both (mirrored to each server). Single-backend installs
look and behave exactly like v1.0.7 — the dual-backend UI only appears when
both backends are configured.

### Added

- **Jellyfin support.** New `jellyfin_client.py` implements the full
  `MediaClient` contract against the Jellyfin REST API. Authenticated via
  username + password against `POST /Users/AuthenticateByName` (API keys are
  broken on the playlist endpoints we need — see Jellyfin issue #15600).
  Configure with `JELLYFIN_URL` / `JELLYFIN_USERNAME` / `JELLYFIN_PASSWORD`.
- **Triple-pill backend picker** on the configure page when both backends
  are configured: `Push to: Both / Plex / Jellyfin`. Hidden when only one
  backend is available.
- **Cross-backend show matching** at add-time: shows added to a "Both"
  playlist are matched on the other side by title + year. The matched
  Plex/Jellyfin IDs are persisted alongside the show row.
- **Heal-on-sync.** For "Both" playlists, every sync re-attempts matching
  for shows missing an ID on one side — so adding a show to your
  previously-empty Jellyfin library auto-resolves on the next sweep without
  any manual intervention.
- **Missing-side warning banner** on the configure and playlist pages,
  listing shows that aren't on every targeted backend. Informational, never
  blocking — you can deliberately add a show to a "Both" playlist that only
  exists on one server today and expect to add it to the other later.
- **Backend badges** on playlist cards (index page), playlist detail
  header, and individual show rows when applicable.
- **`_aggregated_shows()`** in `app.py` lists shows across every configured
  backend and dedupes via title+year normalization, so the show picker is
  one unified list with per-backend overlays.
- **41 new unit tests** in `tests.py` (now 87/87 passing):
  - 18 covering the Jellyfin DELETE safety guard against every dangerous
    endpoint category (`/Items`, `/Library/VirtualFolders`, `/Users/{id}`,
    image deletes, recordings, plugins, etc.).
  - 4 verifying the Plex `Episode/Show/Season/Movie.delete()` monkey-patch
    is actually applied on import (defense-in-depth verification).
  - 15 covering the title-match helper across normalization, year
    disambiguation, None handling.
  - 8 covering the service-layer dispatch (`ShowConfig.id_for`,
    `_backends_for`, `_find_match`, etc.).

### Changed

- **Architecture: `MediaClient` interface.** Every backend operation now
  goes through the abstract base class in `media_client.py`. Plex's existing
  module-level functions remain as thin shims for back-compat with external
  callers; internally everything uses the interface. This is what makes
  dual-backend possible.
- **Tail-rebuild deduplication.** The 6 near-identical 15-line tail-rebuild
  blocks across `add_shows_to_playlist`, `reorder_shows`,
  `set_playlist_sort_mode`, `set_playlist_unwatched_only`, `sync_playlist`,
  and `add_shows_to_playlist` are collapsed into a single
  `_rebuild_tail_on()` primitive. ~80 lines of duplication eliminated.
  Per-backend dispatch loops over the new primitive.
- **Jellyfin's native atomic playlist replace.** `JellyfinClient`
  overrides `replace_playlist_items()` with a single `POST /Playlists/{id}`
  (UpdatePlaylistDto.Ids) — Jellyfin clears `LinkedChildren` then re-adds
  in one transaction. No PlaylistItemId bookkeeping, no partial-state
  window. (Plex still uses the existing incremental remove/add.)
- **`PlexClient` lazy connection.** The PlexServer instance is created on
  first API call instead of at `PlexClient.__init__`. If Plex is briefly
  unreachable when the app starts, the app still boots and degrades
  gracefully per request — instead of hard-crashing during init.
- **DB schema (additive, no data movement):**
  - `managed_playlists.backend` (`plex` | `jellyfin` | `both`, default `plex`)
    with CHECK constraint enforcement on new DBs and helper-layer
    validation on migrated ones.
  - `managed_playlists.jellyfin_playlist_id` (nullable)
  - `playlist_shows.plex_show_item_id` and `jellyfin_show_item_id` (both
    nullable; one-time backfill copies `show_rating_key` into
    `plex_show_item_id` for legacy rows since they were all Plex-originated)
  - `playlist_shows.jellyfin_movie_item_ids` (nullable, comma-separated)

### Safety

- **HTTP-layer DELETE safety guard** in `jellyfin_client.py` deny-by-default
  for every outbound DELETE; only `/Playlists/{id}/Items` (removing items
  from a playlist) is allow-listed. The intentional `delete_playlist()`
  bypass is the single audited route that hits `DELETE /Items?ids=X` —
  it first verifies the target is a playlist via `GET /Playlists/{id}`,
  then sets a one-shot bypass flag. Every other DELETE raises
  `JellyfinSafetyError` before the request goes out.
- **Plex safety guard unchanged.** The module-level monkey-patch of
  `Episode.delete` / `Show.delete` / `Season.delete` / `Movie.delete`
  remains, now also verified by unit tests.

### Single-backend behavior

Plex-only installs see no UI change. The picker is hidden, backend badges
are hidden, the warning banner only fires if you somehow have a "both" row
in the DB. Every operation runs exactly once with the Plex client — the
dispatch loop short-circuits to a single iteration. Tail-rebuild semantics
on Plex are byte-for-byte identical to v1.0.7.

### Fixed

- **Empty `FLASK_SECRET=` no longer crashes session-using routes.** Previously
  an empty-string value (set in `.env` but blank) was passed through to Flask
  instead of falling back to the dev default — every redirect that called
  `flash()` 500'd, including the success page after creating a playlist
  (Plex/Jellyfin playlists were created, but the user saw an error). Now any
  empty/unset value falls back to the dev secret.

### Migration

Migrating from v1.0.7: no manual steps. On first boot, `db.init_db()`
runs the additive ALTER TABLEs and backfills `plex_show_item_id` from
`show_rating_key` for every legacy row.

### Testing notes

`tests.py` is stdlib-only, no Plex or Jellyfin connection needed. Integration
testing against real servers is a manual step before tagging.

## [1.0.7] - 2026-05-24

### Added
- **Acknowledgments section** in the README disclosing that Linearr was built
  collaboratively with Claude Code (Anthropic's AI coding assistant).
  Architecture, testing, deployment, and maintenance are mine.

## [1.0.6] - 2026-05-24

### Fixed
- **Unraid Docker tab icon**: switched the CA template's `<Icon>` (and the
  `ca_profile.xml` icon) from SVG to the 256×256 PNG. Unraid's Apps tab
  renders SVG fine, but the Docker tab's installed-container list uses a
  different rendering path and was leaving the SVG icon blank. PNG works in
  both views.

## [1.0.5] - 2026-05-24

### Changed
- **Repository restructured for Unraid Community Applications submission:**
  - `unraid-template.xml` moved to `templates/linearr.xml` (CA's required
    folder layout).
  - `ca_profile.xml` added at the repo root with repository-wide metadata
    (description, support links, donation).
  - CA Icon now points at the SVG variant in `images/`.

## [1.0.4] - 2026-05-24

### Added
- **Screenshots in README** — four UI screenshots (landing, show picker,
  selected tray, configure page) with library posters blurred for privacy.
  Linked from the table of contents.

## [1.0.3] - 2026-05-24

### Changed
- **Buy Me a Coffee button** is now a styled in-app link sized to match the
  other primary buttons (was the larger BMC JS widget). Same destination,
  same blue, just smaller and visually consistent with the rest of the UI.

## [1.0.2] - 2026-05-24

First stable release. Adds the auto-sync background loop and per-playlist
Auto-update toggle, plus visual identity (logo, favicon, banner, blue
accent palette) and the project's settled name (Linearr).

### Added
- **Auto-sync**: the existing background sweep now also splices newly-aired
  episodes and new seasons (within each show's configured range) into managed
  playlists; episodes deleted from Plex drop out. Already-played portion is
  preserved. Controlled by the new `AUTO_SYNC` env var (default `true`).
- **Per-playlist Auto-update toggle**: in addition to the global env var,
  each playlist has its own **Auto-update: Enabled / Disabled** pill on the
  playlist detail page. Disabled playlists are skipped by the scheduler —
  useful for "locked" curated playlists you don't want changing on their own.
- **Auto-update choice on configure page**: when creating a new playlist,
  there's now an **Auto-update** toggle alongside Sort and Filter so you can
  set the initial state at creation rather than create-then-toggle.
- **Buy Me a Coffee** support button in the landing-page footer (only on
  the playlists landing page — not on the configure or detail flows so it
  doesn't intrude while you're working).
- **Logo + visual identity**: banner, favicon (16/32/64), and Unraid
  template icon. Topbar brand mark swapped from a CSS gradient placeholder
  to the real logo image.
- **Tagline**: *"The missing show sequencer for Plex. Automated round-robin
  rotation and chronological crossover alignment for your episodes (and
  their movies)."* — appears on the README, Dockerfile image label, Unraid
  Overview/Description, and the app's landing page subtitle.

### Changed
- **Accent color** swapped from Plex orange (`#e5a00d`) to a balanced blue
  (`#4d96ff`, `rgb(77, 150, 255)`) across the entire UI — buttons, pills,
  toggles, brand mark, ambient page glow. Buy Me a Coffee button matches.
  This is a deliberate visual distinction from Plex's own brand (Plex
  trademark concerns).
- **Project rename**: codebase renamed from *Plex Rotator* → briefly
  *Plaitarr* → **Linearr**. Container image now publishes to
  `ghcr.io/gillberg1111/linearr`. GitHub auto-redirects old repo URLs.

### Fixed
- **Season-range validation**: the configure page now prevents picking an
  "End at" season earlier than "Start from". When you change the start
  season, end-season cards below it are hidden and any invalid prior
  selection auto-resets to **All**. Server-side validation also rejects
  end &lt; start as a safety net.

## [0.1.0] - 2026-05-23

First public release as **Linearr** (initially named *Plex Rotator*; renamed
to avoid trademark concerns with the Plex name and adopt the community
`*arr` naming convention — "linearr" plays on "linear TV", the broadcast-era
term for scheduled programming, which the app emulates). Published to
ghcr.io and tested on Unraid.

### Added
- **Sort modes**: each playlist can switch between **Rotation** (round-robin) and
  **Air Date** (chronological across shows). Switch any time; the future portion
  of the playlist rebuilds, watched portion stays put.
- **Crossover alignment**: in Air Date mode, multi-part crossovers stay together
  across different shows via `Part 1` / `Pt. 2` / `(N)` title detection.
- **Associated movies**: each show on the configure page detects matching movies
  in your movie libraries by word-boundary title match. Per-show toggle reveals
  a picker with a styled **Select all** button.
- **Unwatched-only filter**: per-playlist toggle excludes episodes you've
  already watched anywhere in Plex.
- **Live AJAX preview**: every config change debounces (600 ms) and updates the
  preview list in place via `/api/preview` — no full page reloads.
- **Picker improvements**: pinned "Selected" tray, filter input, Clear-selection
  button. Selection order = rotation order.
- **Preview pagination**: 10/25/50/100/All with Prev/Next; page-size persists
  across navigations via sessionStorage.
- **Reorder rotation**: up/down arrows on the playlist page.
- **Reorder-aware splicing**: add/remove/reorder all preserve already-played
  episodes and rebuild only the future portion.

### Safety
- Runtime guard installs at import: `Episode.delete`, `Show.delete`,
  `Season.delete`, `Movie.delete` raise `RuntimeError` instead of doing anything.
  Only `Playlist.delete` / `Playlist.removeItem` are allowed (pure metadata).

### Tests
- `tests.py` has 31 self-contained unit tests for the rotation/sort/prune logic
  in `rotation.py`. No Plex or network needed.
