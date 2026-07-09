# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Netwatch is a single-process homelab monitor: continuous ICMP ping monitoring, a SQLite-backed
inventory CMDB, a D3.js network topology graph, Proxmox/TrueNAS dashboards, and an AI assistant
("Mira") proxied through OpenRouter — served from the `netwatch/` Python package (~5900 lines
across 9 modules) with no framework. `monitor.py` at the repo root is a thin entrypoint shim
(`from netwatch.__main__ import main`). It's designed to run unattended on a Raspberry Pi via
systemd. Stdlib + PyYAML + openpyxl only; no pip-installed web framework.

## Commands

```bash
# Run dev server (headless, no curses TUI — almost always what you want while developing)
python3 monitor.py --no-tui --port 8080

# Run the TUI (curses) instead of headless mode — omit --no-tui
python3 monitor.py --port 8080

# Run the full test suite
pytest tests/

# Run a single test
pytest tests/test_netwatch.py::test_column_exists_returns_true_for_existing_column

# Run tests matching a pattern
pytest tests/ -k "inventory"

# Restore from a backup tarball (exits after restoring, does not start the server)
python3 monitor.py --restore <tarball> [--force]
```

There's no build/lint step — it's plain Python served directly. `static/*.js`/`*.css` are
read off disk at request time (cached client-side via `?v=` query strings), but
`dashboard.html` is loaded **once at startup** (`_load_dashboard_html`, in
`netwatch/__main__.py`) — restart the server to see HTML changes. New files under `static/`
must also be added to the `_STATIC_FILES` allowlist in `netwatch/server.py` or they 404.

First run requires creating an admin account (no auth configured = dashboard shows setup wizard):

```bash
curl -X POST http://localhost:8080/api/auth/setup \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"yourpassword"}'
```

## Architecture

**Everything lives in the `netwatch/` package.** `monitor.py` at the repo root is now just a
thin entrypoint shim (`from netwatch.__main__ import main`) kept for backwards-compatible
invocation (`python3 monitor.py ...`) — it has no logic of its own. The package is organized
as one module per subsystem rather than the old single-file layout — when working on a feature,
go to the relevant module below (or search for the class / `_h_*` handler function; module
boundaries mostly follow the subsystem list that used to be "in file order" in the monolith).

Major subsystems, by module:
- `netwatch/hosts.py` — `HostState` / `HostManager` / `poll_host` (per-host ping loop, status
  state machine: up/down/idle/degraded/pending, one thread per host) and `IncidentLog` (records
  status transitions with duration, persisted via `HistoryDB`).
- `netwatch/auth.py` — `AuthManager`: session-cookie auth, brute-force lockout (persisted in
  SQLite so it survives restarts), CSRF tokens issued at login/setup.
- `netwatch/storage.py` — `HistoryDB`: SQLite layer for ping history, daily uptime rollups,
  incidents, lockouts. Pings are batched and flushed every 30s (`_flush_loop`); old data is
  pruned daily (`_prune_loop`). Schema migrations are ad hoc via `_column_exists()` +
  `ALTER TABLE` guards, not a migration framework — when adding a column, follow that pattern.
  Also `InventoryDB`: the CMDB, 9 device types (host/VM/network/UPS/disk/peripheral/tablet/
  phone/printer) each with type-specific JSON-ish fields, plus arbitrary connection edges
  between records. Backed by the same SQLite file as `HistoryDB`. Supports XLSX import/export
  (`export_inventory_to_xlsx` / `import_inventory_from_xlsx`) and nmap-based discovery.
- `netwatch/network.py` — `send_ntfy_alert` / ntfy plumbing, `send_wol_packet` / MAC detection
  helpers (Wake-on-LAN with broadcast-address auto-detection), nmap-based discovery
  (`start_discovery_scan` / `get_discovery_state`), and Pi host-health reads (`read_pi_health`).
- `netwatch/pollers.py` — `NASPoller` / `ProxmoxPoller` / `PBSPoller` / `HAPoller`: background
  pollers (TrueNAS every 900s, Proxmox every 60s) that hit those APIs directly and drive ntfy
  alerting via `netwatch.network.send_ntfy_alert` on state change. `NASPoller` also fetches
  TrueNAS's own `/api/v2.0/alert/list`, filters to WARNING+ via `_filter_alerts()` (excluding any
  klass in the user-configurable `truenas_ignored_alert_klasses` setting), and fires/clears ntfy
  alerts keyed by TrueNAS's own alert `id` — see `_check_alerts`. Scrub-schedule math
  (`_next_cron_run`) accounts for both the pool's `threshold` (TrueNAS's minimum days between
  actual runs, separate from the cron's check frequency) and the NAS's configured timezone
  (`/api/v2.0/system/info`'s `timezone` field, via `zoneinfo`) — don't assume the cron's
  hour/minute fields are UTC.
- `netwatch/http_handlers.py` — `build_topology_payload` / `build_api_payload` (assemble the
  JSON the frontend polls; topology payload merges live host status onto inventory records +
  connection edges) plus every `_h_get_*`/`_h_post_*` handler function, each taking whatever
  state it needs as explicit arguments (these are unit-testable in isolation — see how
  `tests/test_netwatch.py` imports them directly). When adding an endpoint, add a `_h_*`
  function here and wire it up as a branch in `netwatch/server.py`'s `do_GET`/`do_POST`,
  following the existing naming convention.
- `netwatch/server.py` — **HTTP layer**: `make_handler()` builds a `BaseHTTPRequestHandler`
  subclass with `do_GET`/`do_POST` implemented as long if/elif chains over `self.path` (no
  routing library/decorator table), dispatching to the `_h_*` handlers in `http_handlers.py`.
  Also owns `start_web_server()` and the `_STATIC_FILES` allowlist.
- `netwatch/tui.py` — `draw_tui`, `_init_tui_colors`, `_tui_status_role`: curses-based terminal
  view, used when `--no-tui` is *not* passed. Falls back gracefully (256-color → 8-color →
  monochrome) and catches `curses.error` so an unsupported terminal doesn't crash the process —
  it prints a hint to use `--no-tui` instead.
- `netwatch/__main__.py` — `main()`: wires everything together: loads `hosts.yaml`, starts
  `HistoryDB`/prune/flush threads, starts pollers conditionally (only if Proxmox/TrueNAS are
  configured), starts the web server in a thread, then runs either the TUI or a headless sleep
  loop. SIGTERM is routed through `KeyboardInterrupt` so shutdown flushes buffered pings and
  checkpoints the WAL. Also owns `_load_dashboard_html`.
  **Important:** `settings` (the dict loaded from `hosts.yaml`) is passed by reference into
  `HostManager`, `NASPoller`, `ProxmoxPoller`, *and* the web server's handler closure — all as
  the *same* dict object, not copies, even though they now live in different modules.
  `_h_post_settings` relies on this: it mutates `settings` in place so a change saved via the
  API is immediately visible to every poller without a restart. If you ever find yourself
  writing `{**settings, ...}` to build a "local" settings dict for one consumer, stop — that
  silently reintroduces the exact bug fixed in `00480af` (settings changes appearing to save but
  not taking effect until the next restart). See `tests/test_netwatch.py`'s settings
  dict-identity test for a regression check that spans module boundaries.

**Frontend**: `dashboard.html` is the shell, loaded fresh from disk per request (not templated).
`static/*.js` are separate vanilla-JS modules per dashboard area (`core.js`, `topology.js`,
`inventory.js`, `proxmox.js`, `nas.js`, `ai-panel.js`, `settings.js`, `auth.js`, `utils.js`) —
no bundler, no framework, no CDN dependencies (D3 and fonts are vendored under `static/` so the
dashboard works on an isolated LAN).

**Every new dashboard feature must be checked at mobile widths (320–390px), not just desktop.**
The dashboard is used from phones, not just at a desk. Follow the existing responsive
conventions rather than inventing new ones: wide data tables get wrapped in
`.pve-table-scroll` with an explicit `min-width` on the `<table>` at `@media (max-width: 600px)`
so they scroll horizontally instead of squishing columns (see `.pve-guest-table`,
`.pbs-backup-table`); fixed-width label/value pairs in flex rows need their own narrow
breakpoint (`@media (max-width: 380px)`) shrinking widths/font-size rather than letting content
overflow (see `.pbs-ds-name`/`.pbs-ds-val`). Verify with the browser's device toolbar or by
checking `element.scrollWidth` vs `clientWidth` at 320px — don't assume desktop layout math
holds at phone widths.

**Bump `VERSION` (in `netwatch/__init__.py`) after every major task completion.** Static assets are
cache-busted via `?v={{VERSION}}` in `dashboard.html`; if the version string doesn't change,
browsers may keep serving stale cached JS/CSS after an edit, even though the server is reading
the new file from disk. Bumping requires a service restart (`systemctl restart netwatch`) to take
effect, since `{{VERSION}}` is substituted from the Python constant at request time.

**Data files** (gitignored, live next to `monitor.py`, not in a subdirectory):
`hosts.yaml` (ping targets + settings, copy from `hosts.yaml.example`), `auth.json` (users +
Proxmox/TrueNAS/OpenRouter/ntfy credentials), `netwatch.db`/`-shm`/`-wal` (SQLite: history,
inventory, lockouts), `monitor.log` (rotating, 10MB × 3).

## Security model worth knowing before touching auth/API code

- Every route requires a session cookie except `/api/auth/status` and `/api/auth/setup`
  (`_require_auth()`, with an `admin_only` variant for settings/users/backup endpoints).
- All mutating (`POST`) requests must carry an `X-CSRF-Token` header matching the token tied to
  the session cookie (`csrf_token_for_cookie`); the token is handed back in the
  login/setup response body. This is checked centrally before dispatching to `do_POST` branches.
- Sessions are invalidated on user deletion and password change.
- The OpenRouter API key never reaches the browser — Mira's chat/usage calls proxy through
  `/api/ai/chat` and `/api/ai/usage` server-side.
- Proxmox TLS verification is on by default; `proxmox_verify_ssl: false` or `proxmox_ca_cert`
  in `hosts.yaml` are the escape hatches for self-signed certs. Note: Proxmox's own cluster CA
  commonly lacks the X.509 Key Usage extension, which OpenSSL 3.x's strict policy rejects even
  with the right CA pinned — `_make_ssl_ctx()` clears `ssl.VERIFY_X509_STRICT` specifically (and
  only) when `proxmox_ca_cert` is set, so a correctly-pinned self-managed CA still validates.

## Testing notes

`tests/test_netwatch.py` imports internals directly from the `netwatch` submodules (handler
functions, classes, even private helpers like `netwatch.storage._column_exists`) rather than
hitting the HTTP layer for most cases — follow that pattern for new tests: call the `_h_*`
handler function with constructed state/mocks rather than spinning up a real server, unless the
test specifically targets HTTP plumbing (a few tests do use `ThreadingHTTPServer` directly for
that, and one locates `monitor.py` itself via `import monitor` to exercise the entrypoint shim).
`tests/conftest.py` just puts the repo root on `sys.path` so both `netwatch` and `monitor` are
importable.
