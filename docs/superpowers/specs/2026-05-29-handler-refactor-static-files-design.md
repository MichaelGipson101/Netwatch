# Design: Handler Refactor + Static File Split

**Date:** 2026-05-29  
**Status:** Approved

## Problem

Two structural issues are limiting the long-term maintainability of Netwatch:

1. `make_handler()` in `monitor.py` is an 815-line closure containing a nested `Handler` class that mixes routing, auth checking, business logic, and HTTP response writing. There are zero tests for any HTTP route — the highest-risk untested code in the project.

2. `dashboard.html` is 6,141 lines of interleaved HTML, CSS, and JavaScript in a single file. The JS is ~4,200 lines in one `<script>` block, making navigation and targeted edits increasingly painful.

## Goals

- Make route handler logic unit-testable without spinning up an HTTP server
- Split `dashboard.html` into focused, navigable source files served directly as static assets
- No behaviour change — all existing functionality works identically after both changes
- No new Python files; no ES module system; no build step

---

## Part A: `monitor.py` Handler Refactor + Tests

### Architecture

Each route's business logic is extracted into a private module-level function with an explicit parameter signature. The `make_handler` closure and `Handler` class stay in place but become a thin dispatcher.

**Handler function contract:**
```python
def _h_get_status(host_manager, settings, incident_log, inventory_db) -> tuple[int, dict]:
    return 200, build_api_payload(...)

def _h_post_wake(body, host_manager) -> tuple[int, dict]:
    mac = body.get('mac', '').strip()
    if not mac:
        return 400, {'error': 'mac required'}
    send_wol_packet(mac)
    return 200, {'ok': True}
```

- Functions are named `_h_<method>_<route>` (e.g. `_h_get_status`, `_h_post_wake`, `_h_post_hosts`)
- Each receives only the specific dependencies it needs — no `self`, no closure
- Returns `(status_code: int, body: dict)` for JSON responses
- Lives at module level in `monitor.py`, above `make_handler`

### Handler class after refactor

`Handler.do_GET` and `Handler.do_POST` become dispatchers:

```python
def do_GET(self):
    if self.path == '/api/status':
        if not self._require_auth(): return
        code, body = _h_get_status(host_manager, settings, incident_log, inventory_db)
        self._send_json(code, body)
        return
    ...
```

All helper methods (`_require_auth`, `_send_json`, `_read_json_body`, `_set_session_cookie`, etc.) remain on `Handler` unchanged.

### Binary responses stay inline

Two routes return binary content: `POST /api/backup` (tarball) and `GET /api/inventory-export` (xlsx). These do not fit the `(int, dict)` contract and stay inline in `Handler` to avoid over-engineering a separate response type.

### Routes to extract

**GET routes (~11):** `/api/status`, `/api/ai-config`, `/api/hosts`, `/api/pi-health`, `/api/auth/status`, `/api/auth/users`, `/api/inventory`, `/api/topology`, `/api/connections`, `/api/discover`

**POST routes (~12):** `/api/auth/setup`, `/api/auth/login`, `/api/auth/logout`, `/api/inventory` (create/update), `/api/inventory-import`, `/api/auth/users`, `/api/auth/password`, `/api/discover`, `/api/detect-mac`, `/api/wake`, `/api/hosts`

### Tests

Added to `tests/test_netwatch.py`. Tests call `_h_*` functions directly with lightweight mock objects — no HTTP server required.

**Test focus areas:**
- Input validation: missing fields, malformed data, out-of-range values
- Auth guard: routes that require auth/admin return correct error when called without
- Response shape: correct keys present in success responses
- Error cases: not-found hosts, bad MACs, duplicate users

**Estimated additions:** ~30 new test functions

---

## Part B: `dashboard.html` → Static Files

### New file layout

```
netwatch/
  monitor.py
  dashboard.html        ← ~200 lines: HTML structure only, <link> and <script src> tags
  static/
    main.css            ← all CSS (~1,210 lines, two existing <style> blocks merged)
    utils.js            ← escapeHtml, _renderMarkdown, _truncateModelName, shared helpers
    core.js             ← refresh loop, host cards, drawer, theme toggle, main DOMContentLoaded init
    topology.js         ← D3 force graph, force simulation, topology tab rendering
    inventory.js        ← inventory table, connections panel, import/export, discovery UI
    auth.js             ← login modal, session management, auth state UI
    ai-panel.js         ← AI chat IIFE (already self-contained at lines 5774–6138)
```

### Load order

Files are loaded in this order in `dashboard.html`:

```html
<link rel="stylesheet" href="/static/main.css">
...
<script src="/static/utils.js"></script>
<script src="/static/core.js"></script>
<script src="/static/topology.js"></script>
<script src="/static/inventory.js"></script>
<script src="/static/auth.js"></script>
<script src="/static/ai-panel.js"></script>
```

All files share global scope (no ES module system). No `import`/`export` changes are needed. Functions defined in earlier files are available to later files.

### Static file serving in `monitor.py`

An allowlist dict in `monitor.py` maps filenames to content types. Only listed filenames are served — path traversal is structurally impossible:

```python
_STATIC_FILES = {
    'main.css':      'text/css; charset=utf-8',
    'utils.js':      'application/javascript; charset=utf-8',
    'core.js':       'application/javascript; charset=utf-8',
    'topology.js':   'application/javascript; charset=utf-8',
    'inventory.js':  'application/javascript; charset=utf-8',
    'auth.js':       'application/javascript; charset=utf-8',
    'ai-panel.js':   'application/javascript; charset=utf-8',
}
```

Route in `do_GET`:
```python
if self.path.startswith('/static/'):
    fname = self.path[8:]  # strip '/static/'
    if fname in _STATIC_FILES:
        base_dir = os.path.dirname(os.path.abspath(config_path))  # config_path is in closure
        path = os.path.join(base_dir, 'static', fname)
        with open(path, 'rb') as f:
            body = f.read()
        self.send_response(200)
        self.send_header('Content-Type', _STATIC_FILES[fname])
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)
        return
    self._send_json(404, {'error': 'not found'})
    return
```

No auth required for static assets (same as `dashboard.html` itself).

Static files are read from disk on each request (no in-memory caching). This differs from `dashboard.html`, which is read once at startup into memory — the new static files intentionally use disk reads so that editing a JS file and reloading the browser is immediately visible without restarting the server.

### Splitting strategy for JS

The JS block (lines 1949–6138) splits cleanly because:
- Most code is global-scope functions, not wrapped in an IIFE
- The AI panel (lines 5774–6138) is already a self-contained IIFE — cleanest file to extract first
- The theme init IIFE (lines 1950–1953) moves into `core.js`

Splitting proceeds from most self-contained to most entangled: `ai-panel.js` → `utils.js` → `topology.js` → `inventory.js` → `auth.js` → `core.js`.

---

## Implementation Order

1. Extract handler functions in `monitor.py` (Part A refactor)
2. Write handler unit tests (Part A tests)
3. Create `static/` directory and split JS/CSS files (Part B)
4. Add static-serving routes to `monitor.py` (Part B monitor changes)
5. Update `dashboard.html` to reference static assets (Part B HTML)
6. Smoke test: start server, verify all tabs/features work

## Out of Scope

- No changes to `HistoryDB`, `InventoryDB`, `AuthManager`, `HostManager`, or any monitoring logic
- No ES module refactor
- No frontend framework introduction
- No change to deployment workflow beyond the addition of the `static/` directory
