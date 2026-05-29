# Handler Refactor + Static File Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract HTTP route business logic into testable `_h_*` functions in `monitor.py`, add ~30 unit tests, and split `dashboard.html`'s 4200-line JS block into focused static files served by `monitor.py`.

**Architecture:** Part A — each route handler becomes a private module-level function returning `(int, dict)`; the `Handler` class becomes a thin dispatcher. Part B — JS/CSS moves to a `static/` directory; `monitor.py` serves them via an allowlist route; `dashboard.html` becomes a ~200-line HTML template. All 6 JS files are extracted in one atomic operation to avoid producing malformed HTML.

**Tech Stack:** Python stdlib only (no new deps). No JS module system — all files share global scope, loaded in order.

---

## File map

**Modified:**
- `monitor.py` — add `_h_*` functions above `make_handler`; update `do_GET`/`do_POST`; add `_STATIC_FILES` + static route
- `dashboard.html` — strip to HTML-only template; replace `<style>` with `<link>`; replace `<script>` block with `<script src>` tags
- `tests/test_netwatch.py` — add ~30 new unit tests calling `_h_*` directly

**Created:**
- `static/main.css` — merged CSS from two `<style>` blocks in `dashboard.html`
- `static/utils.js` — shared utility functions (`escapeHtml`, `fmtLatency`, Pi health helpers, etc.)
- `static/core.js` — refresh loop, host cards, drawer, editor, discover
- `static/topology.js` — D3 force graph
- `static/inventory.js` — inventory table, connections, import/export
- `static/auth.js` — auth state, login/logout/setup, backup download
- `static/ai-panel.js` — AI chat IIFE

---

## Task 1: Extract GET route handler functions

**Files:**
- Modify: `monitor.py`

These functions belong at module level, just above `make_handler`. They take explicit deps, return `(status_code, body_dict)`.

- [ ] **Step 1: Insert `_h_*` GET functions into `monitor.py` above `make_handler`**

Find the line containing `def make_handler(` and insert the following block immediately above it:

```python
# ── Route handler functions (module-level; testable without HTTP) ─────────────

def _h_get_status(host_manager, settings, incident_log, inventory_db) -> tuple:
    return 200, build_api_payload(host_manager, settings, incident_log, inventory_db)


def _h_get_ai_config(settings: dict) -> tuple:
    api_key = settings.get("openrouter_api_key", "")
    if not api_key.strip():
        return 404, {"error": "ai_not_configured"}
    return 200, {
        "api_key": api_key,
        "model": settings.get("ai_model", "openrouter/free"),
    }


def _h_get_hosts(config_path: str) -> tuple:
    try:
        cfg = load_yaml(config_path) or {}
        return 200, {"hosts": cfg.get("hosts", [])}
    except Exception as e:
        return 500, {"error": f"Could not read config: {e}"}


def _h_get_pi_health() -> tuple:
    try:
        return 200, read_pi_health()
    except Exception as e:
        logging.exception("Error reading Pi health")
        return 500, {"error": str(e)}


def _h_get_auth_status(auth_manager, current_user_fn) -> tuple:
    user, is_admin = current_user_fn() if auth_manager else (None, False)
    return 200, {
        "logged_in":      bool(user),
        "username":       user,
        "admin":          is_admin,
        "setup_required": bool(auth_manager and not auth_manager.has_users),
    }


def _h_get_auth_users(auth_manager) -> tuple:
    return 200, {"users": auth_manager.list_users()}


def _h_get_inventory(inventory_db, host_manager) -> tuple:
    try:
        items = inventory_db.list_all() if inventory_db else []
        host_map = {}
        if host_manager:
            for h in [h.to_dict() for h in host_manager.list_hosts()]:
                mac = (h.get("specs", {}) or {}).get("mac")
                if mac:
                    key = InventoryDB.normalize_mac(mac)
                    if key:
                        host_map[key] = {
                            "name":       h.get("name"),
                            "ip":         h.get("ip"),
                            "is_up":      h.get("is_up"),
                            "status":     h.get("status"),
                            "uptime_pct": h.get("uptime_pct"),
                        }
        for item in items:
            m = InventoryDB.normalize_mac(item.get("mac"))
            item["linked_host"] = host_map.get(m) if m else None
        return 200, {"items": items}
    except Exception as e:
        logging.exception("inventory list error")
        return 500, {"error": str(e)}


def _h_get_inventory_record(path: str, inventory_db, host_manager) -> tuple:
    try:
        inv_id = int(path.split("/")[-1])
    except ValueError:
        return 400, {"error": "invalid id"}
    if not inventory_db:
        return 500, {"error": "inventory not available"}
    rec = inventory_db.get(inv_id)
    if not rec:
        return 404, {"error": "not found"}
    m = InventoryDB.normalize_mac(rec.get("mac"))
    rec["linked_host"] = None
    if m and host_manager:
        for h in [hh.to_dict() for hh in host_manager.list_hosts()]:
            h_mac = (h.get("specs", {}) or {}).get("mac")
            if InventoryDB.normalize_mac(h_mac) == m:
                rec["linked_host"] = {
                    "name":       h.get("name"),
                    "ip":         h.get("ip"),
                    "is_up":      h.get("is_up"),
                    "status":     h.get("status"),
                    "uptime_pct": h.get("uptime_pct"),
                }
                break
    return 200, rec


def _h_get_topology(inventory_db, host_manager) -> tuple:
    if not inventory_db:
        return 500, {"error": "inventory not available"}
    try:
        return 200, build_topology_payload(inventory_db, host_manager)
    except Exception as e:
        logging.exception("topology fetch error")
        return 500, {"error": str(e)}


def _h_get_connections(inventory_db) -> tuple:
    if not inventory_db:
        return 500, {"error": "inventory not available"}
    try:
        return 200, {"items": inventory_db.list_all_connections()}
    except Exception as e:
        logging.exception("connections list error")
        return 500, {"error": str(e)}


def _h_get_connections_for_device(path: str, inventory_db) -> tuple:
    if not inventory_db:
        return 500, {"error": "inventory not available"}
    try:
        inv_id = int(path.split("/")[-2])
    except (ValueError, IndexError):
        return 400, {"error": "invalid id"}
    try:
        return 200, {"items": inventory_db.list_connections_for_device(inv_id)}
    except Exception as e:
        logging.exception("connections fetch error")
        return 500, {"error": str(e)}


def _h_get_discover(config_path: str) -> tuple:
    try:
        state = get_discovery_state()
        cfg = load_yaml(config_path) or {}
        known_ips = {h.get("ip") for h in cfg.get("hosts", []) if isinstance(h, dict)}
        state["results"] = [
            {**r, "already_monitored": r["ip"] in known_ips}
            for r in state.get("results", [])
        ]
        return 200, state
    except Exception as e:
        logging.exception("Error reading discovery state")
        return 500, {"error": str(e)}
```

- [ ] **Step 2: Replace `do_GET` body in `Handler` with the dispatcher**

Replace the entire `do_GET` method body (starting from `if self.path in ("/", "/index.html"):` through the final `self.send_response(404)`) with:

```python
        def do_GET(self):
            if self.path in ("/", "/index.html"):
                body = dashboard_html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith('/static/'):
                fname = self.path[8:]
                if fname not in _STATIC_FILES:
                    self._send_json(404, {'error': 'not found'})
                    return
                base_dir = os.path.dirname(os.path.abspath(config_path))
                fpath = os.path.join(base_dir, 'static', fname)
                try:
                    with open(fpath, 'rb') as f:
                        body = f.read()
                    self.send_response(200)
                    self.send_header('Content-Type', _STATIC_FILES[fname])
                    self.send_header('Content-Length', len(body))
                    self.end_headers()
                    self.wfile.write(body)
                except FileNotFoundError:
                    self._send_json(404, {'error': f'static file not found: {fname}'})
                return
            if self.path == "/api/status":
                if not self._require_auth(): return
                self._send_json(*_h_get_status(host_manager, settings, incident_log, inventory_db))
                return
            if self.path == "/api/ai-config":
                if not self._require_auth(): return
                self._send_json(*_h_get_ai_config(settings))
                return
            if self.path == "/api/hosts":
                if not self._require_auth(): return
                self._send_json(*_h_get_hosts(config_path))
                return
            if self.path == "/api/pi-health":
                if not self._require_auth(): return
                self._send_json(*_h_get_pi_health())
                return
            if self.path == "/api/auth/status":
                self._send_json(*_h_get_auth_status(auth_manager, self._current_user))
                return
            if self.path == "/api/auth/users":
                if not self._require_auth(admin_only=True): return
                self._send_json(*_h_get_auth_users(auth_manager))
                return
            if self.path == "/api/inventory-export" or self.path.startswith("/api/inventory-export?"):
                if not self._require_auth(admin_only=True): return
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"}); return
                try:
                    from urllib.parse import urlparse as _up, parse_qs as _pqs
                    _scope = _pqs(_up(self.path).query).get('scope', ['hosts'])[0]
                    if _scope not in ('hosts', 'all'):
                        _scope = 'hosts'
                    data, result = export_inventory_to_xlsx(inventory_db, scope=_scope)
                    if data is None:
                        self._send_json(500, {"error": result}); return
                    self.send_response(200)
                    self.send_header("Content-Type",
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Content-Disposition", f'attachment; filename="{result}"')
                    self.end_headers()
                    self.wfile.write(data)
                    logging.info(f"Inventory export: {result} ({len(data)} bytes)")
                except Exception as e:
                    logging.exception("inventory export error")
                    self._send_json(500, {"error": str(e)})
                return
            if self.path == "/api/inventory":
                if not self._require_auth(): return
                self._send_json(*_h_get_inventory(inventory_db, host_manager))
                return
            if self.path == "/api/topology":
                if not self._require_auth(): return
                self._send_json(*_h_get_topology(inventory_db, host_manager))
                return
            if self.path == "/api/connections":
                if not self._require_auth(): return
                self._send_json(*_h_get_connections(inventory_db))
                return
            if (self.path.startswith("/api/inventory/") and self.path.endswith("/connections")):
                if not self._require_auth(): return
                self._send_json(*_h_get_connections_for_device(self.path, inventory_db))
                return
            if self.path.startswith("/api/inventory/") and self.path != "/api/inventory/":
                if not self._require_auth(): return
                self._send_json(*_h_get_inventory_record(self.path, inventory_db, host_manager))
                return
            if self.path == "/api/discover":
                if not self._require_auth(): return
                self._send_json(*_h_get_discover(config_path))
                return
            self.send_response(404)
            self.end_headers()
```

Note: the `/static/` route is included here. Add `_STATIC_FILES` as a module-level constant (just above the `_h_*` block):

```python
_STATIC_FILES = {
    'main.css':    'text/css; charset=utf-8',
    'utils.js':    'application/javascript; charset=utf-8',
    'core.js':     'application/javascript; charset=utf-8',
    'topology.js': 'application/javascript; charset=utf-8',
    'inventory.js':'application/javascript; charset=utf-8',
    'auth.js':     'application/javascript; charset=utf-8',
    'ai-panel.js': 'application/javascript; charset=utf-8',
}
```

- [ ] **Step 3: Run existing tests**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/ -v 2>&1 | tail -20
```

Expected: all 15 existing tests pass.

- [ ] **Step 4: Commit**

```bash
git add monitor.py
git commit -m "refactor: extract GET route handlers, add _STATIC_FILES + /static/ serving"
```

---

## Task 2: Write GET route unit tests

**Files:**
- Modify: `tests/test_netwatch.py`

- [ ] **Step 1: Add imports to the top of `tests/test_netwatch.py`**

After the existing imports block, add:

```python
from monitor import (
    _h_get_ai_config, _h_get_hosts,
    _h_get_auth_status, _h_get_auth_users,
    _h_get_inventory, _h_get_inventory_record,
    _h_get_topology, _h_get_connections, _h_get_connections_for_device,
    _h_get_discover,
)
```

- [ ] **Step 2: Add GET route tests at the end of `tests/test_netwatch.py`**

```python
# ── _h_get_ai_config ─────────────────────────────────────────────────────────

def test_h_get_ai_config_returns_key_and_model():
    code, body = _h_get_ai_config({"openrouter_api_key": "sk-test", "ai_model": "gpt-4"})
    assert code == 200
    assert body["api_key"] == "sk-test"
    assert body["model"] == "gpt-4"


def test_h_get_ai_config_missing_key_returns_404():
    code, body = _h_get_ai_config({})
    assert code == 404
    assert body["error"] == "ai_not_configured"


def test_h_get_ai_config_blank_key_returns_404():
    code, body = _h_get_ai_config({"openrouter_api_key": "   "})
    assert code == 404


def test_h_get_ai_config_default_model():
    code, body = _h_get_ai_config({"openrouter_api_key": "sk-x"})
    assert code == 200
    assert body["model"] == "openrouter/free"


# ── _h_get_hosts ─────────────────────────────────────────────────────────────

def test_h_get_hosts_returns_host_list():
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = os.path.join(tmpdir, "hosts.yaml")
        with open(cfg_path, "w") as f:
            f.write("hosts:\n  - name: router\n    ip: 192.168.1.1\n    group: Lab\n")
        code, body = _h_get_hosts(cfg_path)
        assert code == 200
        assert body["hosts"][0]["name"] == "router"


def test_h_get_hosts_missing_file_returns_500():
    code, body = _h_get_hosts("/nonexistent/path/hosts.yaml")
    assert code == 500
    assert "error" in body


# ── _h_get_auth_status ───────────────────────────────────────────────────────

def test_h_get_auth_status_no_auth_manager():
    code, body = _h_get_auth_status(None, lambda: (None, False))
    assert code == 200
    assert body["logged_in"] is False
    assert body["setup_required"] is False


def test_h_get_auth_status_logged_in():
    class FakeAM:
        has_users = True
    code, body = _h_get_auth_status(FakeAM(), lambda: ("alice", True))
    assert code == 200
    assert body["logged_in"] is True
    assert body["username"] == "alice"
    assert body["admin"] is True


def test_h_get_auth_status_setup_required():
    class FakeAM:
        has_users = False
    code, body = _h_get_auth_status(FakeAM(), lambda: (None, False))
    assert code == 200
    assert body["setup_required"] is True


# ── _h_get_auth_users ────────────────────────────────────────────────────────

def test_h_get_auth_users_returns_list():
    class FakeAM:
        def list_users(self): return [{"username": "alice", "admin": True}]
    code, body = _h_get_auth_users(FakeAM())
    assert code == 200
    assert body["users"][0]["username"] == "alice"


# ── _h_get_inventory ─────────────────────────────────────────────────────────

def test_h_get_inventory_no_db_returns_empty():
    code, body = _h_get_inventory(None, None)
    assert code == 200
    assert body["items"] == []


def test_h_get_inventory_returns_items():
    with tempfile.TemporaryDirectory() as tmpdir:
        hdb, idb = _make_idb(tmpdir)
        with hdb.lock:
            hdb.conn.execute(
                "INSERT INTO inventory (system, ip, device_type, created_at, updated_at)"
                " VALUES (?,?,?,?,?)", ("Switch1", "10.0.0.5", "network", 0, 0)
            )
            hdb.conn.commit()
        code, body = _h_get_inventory(idb, None)
        assert code == 200
        assert any(i["system"] == "Switch1" for i in body["items"])
        hdb.close()


# ── _h_get_inventory_record ──────────────────────────────────────────────────

def test_h_get_inventory_record_not_found():
    with tempfile.TemporaryDirectory() as tmpdir:
        hdb, idb = _make_idb(tmpdir)
        code, body = _h_get_inventory_record("/api/inventory/9999", idb, None)
        assert code == 404
        hdb.close()


def test_h_get_inventory_record_bad_id():
    with tempfile.TemporaryDirectory() as tmpdir:
        hdb, idb = _make_idb(tmpdir)
        code, body = _h_get_inventory_record("/api/inventory/notanint", idb, None)
        assert code == 400
        hdb.close()


def test_h_get_inventory_record_no_db():
    code, body = _h_get_inventory_record("/api/inventory/1", None, None)
    assert code == 500


# ── _h_get_topology ──────────────────────────────────────────────────────────

def test_h_get_topology_no_db():
    code, body = _h_get_topology(None, None)
    assert code == 500
    assert "error" in body


# ── _h_get_connections ───────────────────────────────────────────────────────

def test_h_get_connections_no_db():
    code, body = _h_get_connections(None)
    assert code == 500


def test_h_get_connections_returns_list():
    with tempfile.TemporaryDirectory() as tmpdir:
        hdb, idb = _make_idb(tmpdir)
        code, body = _h_get_connections(idb)
        assert code == 200
        assert "items" in body
        hdb.close()


# ── _h_get_connections_for_device ────────────────────────────────────────────

def test_h_get_connections_for_device_bad_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        hdb, idb = _make_idb(tmpdir)
        code, body = _h_get_connections_for_device("/api/inventory/notanint/connections", idb)
        assert code == 400
        hdb.close()
```

- [ ] **Step 3: Run tests**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/ -v -k "h_get" 2>&1 | tail -30
```

Expected: all new `h_get` tests pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_netwatch.py
git commit -m "test: add unit tests for extracted GET route handlers"
```

---

## Task 3: Extract POST route handler functions

**Files:**
- Modify: `monitor.py`

**Routes that stay inline** (need cookies or multipart): `auth/setup`, `auth/login`, `auth/logout`, `inventory-import`, `backup`.

- [ ] **Step 1: Append POST handler functions to the `_h_*` block above `make_handler`**

```python
def _h_post_inventory_create(body: dict, inventory_db) -> tuple:
    if not inventory_db:
        return 500, {"error": "inventory not available"}
    inv_id, err = inventory_db.create(body)
    if err:
        return 400, {"error": err}
    return 200, {"ok": True, "id": inv_id}


def _h_post_inventory_update(path: str, body: dict, inventory_db) -> tuple:
    if not inventory_db:
        return 500, {"error": "inventory not available"}
    try:
        inv_id = int(path.split("/")[-1])
    except ValueError:
        return 400, {"error": "invalid id"}
    ok, err = inventory_db.update(inv_id, body)
    if not ok:
        return 400, {"error": err}
    return 200, {"ok": True}


def _h_post_inventory_delete(path: str, inventory_db) -> tuple:
    if not inventory_db:
        return 500, {"error": "inventory not available"}
    try:
        inv_id = int(path.split("/")[-2])
    except (ValueError, IndexError):
        return 400, {"error": "invalid id"}
    ok, err = inventory_db.delete(inv_id)
    if not ok:
        return 404, {"error": err}
    return 200, {"ok": True}


def _h_post_connection_create(path: str, body: dict, inventory_db) -> tuple:
    if not inventory_db:
        return 500, {"error": "inventory not available"}
    try:
        inv_id = int(path.split("/")[-2])
    except (ValueError, IndexError):
        return 400, {"error": "invalid id"}
    body = dict(body)
    body["from_device_id"] = inv_id
    new_id, err = inventory_db.create_connection(body)
    if err:
        return 400, {"error": err}
    return 200, {"ok": True, "id": new_id}


def _h_post_connection_update(path: str, body: dict, inventory_db) -> tuple:
    if not inventory_db:
        return 500, {"error": "inventory not available"}
    try:
        conn_id = int(path.split("/")[-1])
    except ValueError:
        return 400, {"error": "invalid id"}
    ok, err = inventory_db.update_connection(conn_id, body)
    if not ok:
        return 400, {"error": err}
    return 200, {"ok": True}


def _h_post_connection_delete(path: str, inventory_db) -> tuple:
    if not inventory_db:
        return 500, {"error": "inventory not available"}
    try:
        conn_id = int(path.split("/")[-2])
    except (ValueError, IndexError):
        return 400, {"error": "invalid id"}
    ok, err = inventory_db.delete_connection(conn_id)
    if not ok:
        return 404, {"error": err}
    return 200, {"ok": True}


def _h_post_discover() -> tuple:
    try:
        started, msg = start_discovery_scan()
        if started:
            return 200, {"ok": True, "message": msg}
        return 400, {"error": msg}
    except Exception as e:
        logging.exception("Error starting discovery scan")
        return 500, {"error": str(e)}


def _h_post_detect_mac(body: dict) -> tuple:
    ip = (body.get("ip") or "").strip()
    if not ip:
        return 400, {"error": "ip required"}
    try:
        mac = _detect_mac_for_ip(ip)
        if mac:
            return 200, {"ok": True, "mac": mac}
        return 404, {"error": "not in ARP cache (host may be offline or not yet pinged)"}
    except Exception as e:
        logging.exception("detect-mac error")
        return 500, {"error": str(e)}


def _h_post_wake(body: dict, host_manager, inventory_db) -> tuple:
    target_ip = body.get("ip", "").strip()
    if not target_ip:
        return 400, {"error": "ip is required"}
    target_host = next((h for h in host_manager.list_hosts() if h.ip == target_ip), None)
    if not target_host:
        return 404, {"error": "Host not found"}
    mac = (target_host.specs or {}).get("mac", "")
    if not mac and inventory_db:
        try:
            for rec in inventory_db.list_all():
                if rec.get("ip") == target_ip and rec.get("mac"):
                    mac = rec["mac"]
                    logging.info("WoL: using MAC from inventory record %s for %s",
                                 rec.get("id"), target_ip)
                    break
        except Exception as e:
            logging.warning("WoL inventory MAC lookup failed: %s", e)
    if not mac:
        return 400, {"error": "No MAC address configured for this host (in hosts.yaml or inventory)"}
    ok, err = send_wol_packet(mac)
    if ok:
        return 200, {"ok": True, "message": f"Magic packet sent to {mac}"}
    return 500, {"error": err or "Failed to send magic packet"}


def _h_post_hosts(body: dict, config_path: str, host_manager, settings: dict) -> tuple:
    new_hosts = body.get("hosts", [])
    if not isinstance(new_hosts, list):
        return 400, {"error": "'hosts' must be a list"}
    ok, err = validate_hosts_config({"hosts": new_hosts})
    if not ok:
        return 400, {"error": err}
    try:
        save_hosts_config(config_path, new_hosts)
        logging.info(f"hosts.yaml updated via web: {len(new_hosts)} hosts")
        host_manager.reload_from_config(new_hosts, settings.get("default_interval", 30))
        return 200, {"ok": True, "count": len(new_hosts)}
    except Exception as e:
        logging.exception("Error saving hosts")
        return 500, {"error": str(e)}


def _h_post_auth_users(body: dict, auth_manager) -> tuple:
    username = body.get("username", "")
    password = body.get("password", "")
    is_admin = bool(body.get("admin", False))
    ok, err = auth_manager.create_user(username, password, admin=is_admin)
    if not ok:
        return 400, {"error": err}
    return 200, {"ok": True}


def _h_post_auth_password(body: dict, user: str, auth_manager) -> tuple:
    current = body.get("current", "")
    new_pw = body.get("new", "")
    if not auth_manager.verify_password(user, current):
        return 401, {"error": "current password is incorrect"}
    ok, err = auth_manager.change_password(user, new_pw)
    if not ok:
        return 400, {"error": err}
    return 200, {"ok": True}


def _h_post_auth_user_delete(path: str, auth_manager) -> tuple:
    username = path[len("/api/auth/users/"):]
    if not username:
        return 400, {"error": "username required"}
    ok, err = auth_manager.delete_user(username)
    if not ok:
        return 400, {"error": err}
    return 200, {"ok": True}
```

- [ ] **Step 2: Replace `do_POST` body in `Handler` with the dispatcher**

Replace the entire `do_POST` body (from `if self.path == "/api/auth/setup":` through the final `self.send_response(404)`) with:

```python
        def do_POST(self):
            # Auth routes that set/clear cookies stay inline
            if self.path == "/api/auth/setup":
                if not auth_manager:
                    self._send_json(400, {"error": "auth disabled"}); return
                client_ip = self._client_ip()
                if client_ip not in ("127.0.0.1", "::1", "localhost"):
                    self._send_json(403, {
                        "error": "setup must be performed from localhost",
                        "message": "SSH to the Pi and run: curl -X POST http://localhost:8080/api/auth/setup -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"...\"}'"
                    }); return
                if auth_manager.has_users:
                    self._send_json(400, {"error": "setup already complete"}); return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length).decode()
                    data = json.loads(body)
                    username = data.get("username", "")
                    password = data.get("password", "")
                    ok, err = auth_manager.create_user(username, password, admin=True)
                    if not ok:
                        self._send_json(400, {"error": err}); return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self._set_session_cookie(username)
                    self.end_headers()
                    self.wfile.write(json.dumps({"ok": True, "username": username}).encode())
                except json.JSONDecodeError:
                    self._send_json(400, {"error": "invalid JSON"})
                except Exception as e:
                    logging.exception("setup error")
                    self._send_json(500, {"error": str(e)})
                return

            if self.path == "/api/auth/login":
                if not auth_manager:
                    self._send_json(400, {"error": "auth disabled"}); return
                ip = self._client_ip()
                if auth_manager.is_locked_out(ip):
                    self._send_json(429, {"error": "too many failed attempts, try again in 15 minutes"}); return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length).decode()
                    data = json.loads(body)
                    username = data.get("username", "")
                    password = data.get("password", "")
                    if auth_manager.verify_password(username, password):
                        auth_manager.record_successful_login(ip)
                        is_admin = auth_manager.is_admin(username.strip().lower())
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self._set_session_cookie(username.strip().lower())
                        self.end_headers()
                        self.wfile.write(json.dumps({
                            "ok": True,
                            "username": username.strip().lower(),
                            "admin": is_admin,
                        }).encode())
                    else:
                        auth_manager.record_failed_attempt(ip)
                        self._send_json(401, {"error": "invalid username or password"})
                except json.JSONDecodeError:
                    self._send_json(400, {"error": "invalid JSON"})
                except Exception as e:
                    logging.exception("login error")
                    self._send_json(500, {"error": str(e)})
                return

            if self.path == "/api/auth/logout":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self._clear_session_cookie()
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
                return

            if self.path == "/api/inventory":
                if not self._require_auth(): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_inventory_create(data, inventory_db))
                return

            if self.path.startswith("/api/inventory/") and self.path.endswith("/delete"):
                if not self._require_auth(): return
                self._send_json(*_h_post_inventory_delete(self.path, inventory_db))
                return

            if (self.path.startswith("/api/inventory/") and self.path.endswith("/connections")):
                if not self._require_auth(): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_connection_create(self.path, data, inventory_db))
                return

            if (self.path.startswith("/api/connections/") and self.path.endswith("/delete")):
                if not self._require_auth(): return
                self._send_json(*_h_post_connection_delete(self.path, inventory_db))
                return

            if self.path.startswith("/api/connections/"):
                if not self._require_auth(): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_connection_update(self.path, data, inventory_db))
                return

            if self.path.startswith("/api/inventory/"):
                if not self._require_auth(): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_inventory_update(self.path, data, inventory_db))
                return

            if self.path == "/api/inventory-import":
                if not self._require_auth(): return
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"}); return
                try:
                    ctype = self.headers.get("Content-Type", "")
                    length = int(self.headers.get("Content-Length", 0))
                    if length > 10 * 1024 * 1024:
                        self._send_json(400, {"error": "file too large (10MB max)"}); return
                    body = self.rfile.read(length)
                    if "multipart/form-data" not in ctype:
                        self._send_json(400, {"error": "expected multipart/form-data"}); return
                    boundary = None
                    for part in ctype.split(";"):
                        part = part.strip()
                        if part.startswith("boundary="):
                            boundary = part[9:].strip('"')
                    if not boundary:
                        self._send_json(400, {"error": "missing boundary"}); return
                    delimiter = ("--" + boundary).encode()
                    parts = body.split(delimiter)
                    file_bytes = None
                    mode = "add"
                    for p in parts:
                        if not p or p == b"--" or p.strip() in (b"--\r\n", b""):
                            continue
                        sep = p.find(b"\r\n\r\n")
                        if sep < 0: continue
                        headers_blob = p[:sep].decode("latin-1", errors="replace")
                        content = p[sep + 4:]
                        if content.endswith(b"\r\n"): content = content[:-2]
                        name = None
                        for hline in headers_blob.split("\r\n"):
                            if hline.lower().startswith("content-disposition"):
                                for piece in hline.split(";"):
                                    piece = piece.strip()
                                    if piece.startswith("name="):
                                        name = piece[5:].strip('"')
                        if name == "file": file_bytes = content
                        elif name == "mode":
                            try:
                                mode = content.decode().strip()
                                if mode not in ("add", "replace"): mode = "add"
                            except Exception: mode = "add"
                    if file_bytes is None:
                        self._send_json(400, {"error": "no file uploaded"}); return
                    added, skipped, errors = import_inventory_from_xlsx(inventory_db, file_bytes, mode=mode)
                    self._send_json(200, {"ok": True, "added": added, "skipped": skipped,
                                          "errors": errors[:20], "mode": mode})
                except Exception as e:
                    logging.exception("inventory import error")
                    self._send_json(500, {"error": str(e)})
                return

            if self.path == "/api/backup":
                if not self._require_auth(admin_only=True): return
                try:
                    auth_path_local = auth_manager.path if auth_manager else None
                    data, filename, manifest = create_backup_tarball(config_path, auth_path_local)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/gzip")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                    self.send_header("X-Netwatch-Backup-Version", str(manifest["manifest_version"]))
                    self.send_header("X-Netwatch-Source", manifest["source_hostname"])
                    self.end_headers()
                    self.wfile.write(data)
                    logging.info(f"Backup downloaded: {filename} ({len(data)} bytes)")
                except Exception as e:
                    logging.exception("Backup failed")
                    self._send_json(500, {"error": f"backup failed: {e}"})
                return

            if self.path == "/api/auth/users":
                if not self._require_auth(admin_only=True): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_auth_users(data, auth_manager))
                return

            if self.path == "/api/auth/password":
                if not self._require_auth(): return
                user, _ = self._current_user()
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_auth_password(data, user, auth_manager))
                return

            if self.path.startswith("/api/auth/users/"):
                if not self._require_auth(admin_only=True): return
                self._send_json(*_h_post_auth_user_delete(self.path, auth_manager))
                return

            if self.path == "/api/discover":
                if not self._require_auth(): return
                self._send_json(*_h_post_discover())
                return

            if self.path == "/api/detect-mac":
                if not self._require_auth(): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_detect_mac(data))
                return

            if self.path == "/api/wake":
                if not self._require_auth(): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_wake(data, host_manager, inventory_db))
                return

            if self.path == "/api/hosts":
                if not self._require_auth(): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_hosts(data, config_path, host_manager, settings))
                return

            self.send_response(404)
            self.end_headers()
```

- [ ] **Step 3: Run full test suite**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/ -v 2>&1 | tail -20
```

Expected: all 15 existing tests pass.

- [ ] **Step 4: Commit**

```bash
git add monitor.py
git commit -m "refactor: extract POST route handler functions from make_handler closure"
```

---

## Task 4: Write POST route unit tests

**Files:**
- Modify: `tests/test_netwatch.py`

- [ ] **Step 1: Extend the import block added in Task 2**

Replace the existing `from monitor import (...)` block with:

```python
from monitor import (
    _h_get_ai_config, _h_get_hosts,
    _h_get_auth_status, _h_get_auth_users,
    _h_get_inventory, _h_get_inventory_record,
    _h_get_topology, _h_get_connections, _h_get_connections_for_device,
    _h_get_discover,
    _h_post_inventory_create, _h_post_inventory_update, _h_post_inventory_delete,
    _h_post_connection_create, _h_post_connection_update, _h_post_connection_delete,
    _h_post_detect_mac, _h_post_wake, _h_post_hosts,
    _h_post_auth_users, _h_post_auth_password, _h_post_auth_user_delete,
)
```

- [ ] **Step 2: Add POST route tests at end of `tests/test_netwatch.py`**

```python
# ── _h_post_wake ─────────────────────────────────────────────────────────────

class _FakeHostWithMac:
    def __init__(self, ip, mac=None):
        self.ip = ip
        self.specs = {"mac": mac} if mac else {}
    def to_dict(self): return {"ip": self.ip}


class _FakeHMWake:
    def __init__(self, hosts): self._hosts = hosts
    def list_hosts(self): return self._hosts


def test_h_post_wake_missing_ip():
    hm = _FakeHMWake([])
    code, body = _h_post_wake({}, hm, None)
    assert code == 400
    assert body["error"] == "ip is required"


def test_h_post_wake_host_not_found():
    hm = _FakeHMWake([_FakeHostWithMac("10.0.0.1")])
    code, body = _h_post_wake({"ip": "10.0.0.99"}, hm, None)
    assert code == 404


def test_h_post_wake_no_mac():
    hm = _FakeHMWake([_FakeHostWithMac("10.0.0.1")])
    code, body = _h_post_wake({"ip": "10.0.0.1"}, hm, None)
    assert code == 400
    assert "MAC" in body["error"]


# ── _h_post_detect_mac ───────────────────────────────────────────────────────

def test_h_post_detect_mac_missing_ip():
    code, body = _h_post_detect_mac({})
    assert code == 400
    assert body["error"] == "ip required"


def test_h_post_detect_mac_blank_ip():
    code, body = _h_post_detect_mac({"ip": "  "})
    assert code == 400


# ── _h_post_inventory_create ─────────────────────────────────────────────────

def test_h_post_inventory_create_no_db():
    code, body = _h_post_inventory_create({}, None)
    assert code == 500


def test_h_post_inventory_create_success():
    with tempfile.TemporaryDirectory() as tmpdir:
        hdb, idb = _make_idb(tmpdir)
        code, body = _h_post_inventory_create(
            {"system": "TestBox", "device_type": "host"}, idb
        )
        assert code == 200
        assert body["ok"] is True
        assert isinstance(body["id"], int)
        hdb.close()


# ── _h_post_inventory_delete ─────────────────────────────────────────────────

def test_h_post_inventory_delete_not_found():
    with tempfile.TemporaryDirectory() as tmpdir:
        hdb, idb = _make_idb(tmpdir)
        code, body = _h_post_inventory_delete("/api/inventory/9999/delete", idb)
        assert code == 404
        hdb.close()


def test_h_post_inventory_delete_bad_id():
    with tempfile.TemporaryDirectory() as tmpdir:
        hdb, idb = _make_idb(tmpdir)
        code, body = _h_post_inventory_delete("/api/inventory/abc/delete", idb)
        assert code == 400
        hdb.close()


# ── _h_post_hosts ────────────────────────────────────────────────────────────

def test_h_post_hosts_not_a_list():
    hm = _FakeHostManager([])
    code, body = _h_post_hosts({"hosts": "not-a-list"}, "/dev/null", hm, {})
    assert code == 400
    assert "'hosts' must be a list" in body["error"]


def test_h_post_hosts_invalid_host():
    hm = _FakeHostManager([])
    code, body = _h_post_hosts(
        {"hosts": [{"name": "noip", "group": "Lab"}]}, "/dev/null", hm, {}
    )
    assert code == 400
    assert "error" in body


# ── _h_post_auth_users ───────────────────────────────────────────────────────

def test_h_post_auth_users_creates_user():
    with tempfile.TemporaryDirectory() as tmpdir:
        am = _make_auth(tmpdir)
        code, body = _h_post_auth_users(
            {"username": "bob", "password": "Str0ng!pass", "admin": False}, am
        )
        assert code == 200
        assert body["ok"] is True
        assert any(u["username"] == "bob" for u in am.list_users())


def test_h_post_auth_users_duplicate():
    with tempfile.TemporaryDirectory() as tmpdir:
        am = _make_auth(tmpdir)
        am.create_user("alice", "Str0ng!pass", admin=True)
        code, body = _h_post_auth_users({"username": "alice", "password": "other"}, am)
        assert code == 400
        assert "error" in body


# ── _h_post_auth_password ────────────────────────────────────────────────────

def test_h_post_auth_password_wrong_current():
    with tempfile.TemporaryDirectory() as tmpdir:
        am = _make_auth(tmpdir)
        am.create_user("carol", "Str0ng!pass", admin=False)
        code, body = _h_post_auth_password(
            {"current": "wrongpassword", "new": "newpass"}, "carol", am
        )
        assert code == 401


def test_h_post_auth_password_success():
    with tempfile.TemporaryDirectory() as tmpdir:
        am = _make_auth(tmpdir)
        am.create_user("dave", "Str0ng!pass", admin=False)
        code, body = _h_post_auth_password(
            {"current": "Str0ng!pass", "new": "NewStr0ng!pass"}, "dave", am
        )
        assert code == 200
        assert body["ok"] is True


# ── _h_post_auth_user_delete ─────────────────────────────────────────────────

def test_h_post_auth_user_delete_empty_username():
    with tempfile.TemporaryDirectory() as tmpdir:
        am = _make_auth(tmpdir)
        code, body = _h_post_auth_user_delete("/api/auth/users/", am)
        assert code == 400


def test_h_post_auth_user_delete_not_found():
    with tempfile.TemporaryDirectory() as tmpdir:
        am = _make_auth(tmpdir)
        code, body = _h_post_auth_user_delete("/api/auth/users/nobody", am)
        assert code == 400
        assert "error" in body
```

- [ ] **Step 3: Run all tests**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/ -v 2>&1 | tail -40
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add tests/test_netwatch.py
git commit -m "test: add unit tests for extracted POST route handlers"
```

---

## Task 5: Extract `static/main.css`

**Files:**
- Create: `static/main.css`
- Modify: `dashboard.html`

The CSS lives in two `<style>` blocks: lines ~9–1220 and ~1221–1236 (open/close tags included).

- [ ] **Step 1: Create `static/` directory and extract CSS**

```bash
mkdir -p /home/mgipson/netwatch/static

python3 - <<'EOF'
with open('/home/mgipson/netwatch/dashboard.html', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Find both <style> and </style> boundaries
style_ranges = []
i = 0
while i < len(lines):
    if lines[i].strip() == '<style>':
        start = i
        j = i + 1
        while j < len(lines) and lines[j].strip() != '</style>':
            j += 1
        style_ranges.append((start, j))  # j is the </style> line
        i = j + 1
    else:
        i += 1

print(f"Found {len(style_ranges)} <style> blocks")
assert len(style_ranges) == 2, f"Expected 2, got {len(style_ranges)}"

# Write CSS content (between the tags, not the tags themselves)
css_parts = []
for (open_line, close_line) in style_ranges:
    css_parts.append(''.join(lines[open_line+1:close_line]))

with open('/home/mgipson/netwatch/static/main.css', 'w', encoding='utf-8') as f:
    f.write('\n'.join(css_parts))
print(f"Wrote static/main.css: {sum(len(p.splitlines()) for p in css_parts)} lines")

# Replace both <style> blocks in dashboard.html with a single <link>
# Remove from first <style> open tag through last </style> close tag
first_open  = style_ranges[0][0]
last_close  = style_ranges[-1][1]
new_lines = (lines[:first_open]
             + ['  <link rel="stylesheet" href="/static/main.css">\n']
             + lines[last_close+1:])
with open('/home/mgipson/netwatch/dashboard.html', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
print(f"dashboard.html: replaced style blocks with <link>")
EOF
```

- [ ] **Step 2: Verify**

```bash
wc -l /home/mgipson/netwatch/static/main.css
grep -c "<style>" /home/mgipson/netwatch/dashboard.html
# Expected: main.css ~1226 lines, <style> count = 0
```

- [ ] **Step 3: Quick server test**

```bash
cd /home/mgipson/netwatch && python monitor.py --no-tui &
sleep 2
curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/static/main.css
# Expected: 200
curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/
# Expected: 200
kill %1
```

- [ ] **Step 4: Commit**

```bash
git add static/main.css dashboard.html
git commit -m "refactor: extract CSS to static/main.css"
```

---

## Task 6: Split all JavaScript into static files (atomic operation)

The JS lives in one `<script>` block in `dashboard.html`. All 6 files are extracted in one Python script to avoid producing intermediate malformed HTML. The script uses landmark comments to find split points, then reconstructs `dashboard.html` with `<script src>` tags.

**Landmark comments and what they mark:**
- `// =============================================================` — start of D3 topology section
- `function renderTopology(` — end of topology section (first occurrence after the landmark)
- `// ── Auth state ──` — start of auth section
- `// ── Inventory state ──` — start of inventory section
- `// ── AI Chat Bubble` — start of AI panel IIFE

**Utility functions extracted from core into `utils.js`** (pulled by name before writing core.js):
`deviceIcon`, `escapeHtml`, `fmtLatency`, `uptimeColor`, `durationStr`, `lastSeenStr`, `sortHosts`, `_bytesHuman`, `_uptimeHuman`, `_tempColor`, `_pctColor`, `_pctClass`, `_loadColor`, `_loadClass`, `ipValid`, `macValid`, `setStatus`

**Dead code removed:** The first `openEditor` definition (the one without auth checks, identifiable by its body starting with `try {` and having no `_authState` reference) is stripped from core.js.

**Files:**
- Create: `static/utils.js`, `static/core.js`, `static/topology.js`, `static/auth.js`, `static/inventory.js`, `static/ai-panel.js`
- Modify: `dashboard.html`

- [ ] **Step 1: Run the JS split script**

```bash
python3 - <<'SPLITEOF'
import re

with open('/home/mgipson/netwatch/dashboard.html', 'r', encoding='utf-8') as f:
    content = f.read()

# ── Locate the single <script> block ──────────────────────────────────────────
script_open_tag  = '<script>\n'
script_close_tag = '</script>'
# Use index/rindex to get the only script block (there should be exactly one)
so = content.index(script_open_tag)
sc = content.rindex(script_close_tag)
js = content[so + len(script_open_tag):sc]
print(f"Script block: {len(js.splitlines())} lines")

# ── Split by landmarks ────────────────────────────────────────────────────────
TOPO_MARKER  = '// ============================================================='
RENDER_TOPO  = 'function renderTopology('
AUTH_MARKER  = '// ── Auth state ──'
INV_MARKER   = '// ── Inventory state ──'
AI_MARKER    = '// ── AI Chat Bubble'

topo_start = js.index(TOPO_MARKER)
topo_end   = js.index(RENDER_TOPO, topo_start)
auth_start = js.index(AUTH_MARKER)
inv_start  = js.index(INV_MARKER)
ai_start   = js.index(AI_MARKER)

pre_topo  = js[:topo_start]
topo_js   = js[topo_start:topo_end]
core_mid  = js[topo_end:auth_start]
auth_js   = js[auth_start:inv_start]
inv_js    = js[inv_start:ai_start]
ai_js     = js[ai_start:]

# ── Extract utility functions from pre_topo + core_mid ────────────────────────
UTILS = [
    'deviceIcon', 'escapeHtml', 'fmtLatency', 'uptimeColor',
    'durationStr', 'lastSeenStr', 'sortHosts',
    '_bytesHuman', '_uptimeHuman', '_tempColor', '_pctColor',
    '_pctClass', '_loadColor', '_loadClass',
    'ipValid', 'macValid', 'setStatus',
]

def extract_fn(text, name):
    """Find 'function name(' at line start, return (fn_body, remaining_text).
    Uses brace counting to find the closing }."""
    pattern = re.compile(
        r'^(?:async )?function ' + re.escape(name) + r'\b',
        re.MULTILINE
    )
    m = pattern.search(text)
    if not m:
        return '', text
    start = m.start()
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                fn_body = text[start:i+1]
                remaining = text[:start] + text[i+1:]
                return fn_body, remaining
        i += 1
    return '', text  # unclosed — shouldn't happen

core = pre_topo + core_mid
utils_parts = []
for name in UTILS:
    fn_text, core = extract_fn(core, name)
    if fn_text:
        utils_parts.append(fn_text)
        print(f"  extracted {name} ({len(fn_text.splitlines())} lines)")
    else:
        print(f"  WARNING: {name} not found")

# ── Remove dead openEditor (the one without _authState checks) ─────────────────
# The dead one's body starts with 'try {' and has no _authState reference.
# Find both openEditor definitions and remove the shorter/dead one.
dead_pattern = re.compile(
    r'^async function openEditor\(\)\s*\{\s*\n\s*try \{',
    re.MULTILINE
)
m = dead_pattern.search(core)
if m:
    _, core = extract_fn(core, 'openEditor')
    print("  removed dead openEditor")
else:
    print("  WARNING: dead openEditor not found by pattern — check manually")

# ── Write output files ────────────────────────────────────────────────────────
STATIC = '/home/mgipson/netwatch/static'

files = {
    'utils.js':    '\n\n'.join(utils_parts) + '\n',
    'core.js':     core.strip() + '\n',
    'topology.js': topo_js.strip() + '\n',
    'auth.js':     auth_js.strip() + '\n',
    'inventory.js':inv_js.strip() + '\n',
    'ai-panel.js': ai_js.strip() + '\n',
}

for fname, body in files.items():
    path = f'{STATIC}/{fname}'
    with open(path, 'w', encoding='utf-8') as f:
        f.write(body)
    print(f"  {fname}: {len(body.splitlines())} lines")

# ── Reconstruct dashboard.html ────────────────────────────────────────────────
script_tags = (
    '<script src="/static/utils.js"></script>\n'
    '<script src="/static/core.js"></script>\n'
    '<script src="/static/topology.js"></script>\n'
    '<script src="/static/auth.js"></script>\n'
    '<script src="/static/inventory.js"></script>\n'
    '<script src="/static/ai-panel.js"></script>\n'
)

before = content[:so]
after  = content[sc + len(script_close_tag):]
new_content = before + script_tags + after

with open('/home/mgipson/netwatch/dashboard.html', 'w', encoding='utf-8') as f:
    f.write(new_content)
print(f"dashboard.html: {len(new_content.splitlines())} lines")
print("Done!")
SPLITEOF
```

- [ ] **Step 2: Verify no inline JS or CSS remains in `dashboard.html`**

```bash
grep -c "<script>" /home/mgipson/netwatch/dashboard.html
# Expected: 0
grep -c "<style>" /home/mgipson/netwatch/dashboard.html
# Expected: 0
grep -c 'script src' /home/mgipson/netwatch/dashboard.html
# Expected: 6
wc -l /home/mgipson/netwatch/dashboard.html
# Expected: ~200
```

- [ ] **Step 3: Verify static file line counts are plausible**

```bash
wc -l /home/mgipson/netwatch/static/*.js /home/mgipson/netwatch/static/*.css
```

Expected approximate ranges:
- `main.css`: 1200–1230 lines
- `utils.js`: 80–120 lines
- `core.js`: 1000–1300 lines
- `topology.js`: 850–950 lines
- `auth.js`: 250–320 lines
- `inventory.js`: 1350–1450 lines
- `ai-panel.js`: 350–380 lines

- [ ] **Step 4: Check dead `openEditor` is gone**

```bash
grep -n "async function openEditor" /home/mgipson/netwatch/static/core.js
# Expected: 0 matches (dead one removed)
grep -n "async function openEditor" /home/mgipson/netwatch/static/auth.js
# Expected: 1 match (the auth-aware version)
```

- [ ] **Step 5: Commit**

```bash
git add static/ dashboard.html
git commit -m "refactor: split dashboard.html JS into static/utils.js, core.js, topology.js, auth.js, inventory.js, ai-panel.js"
```

---

## Task 7: Full smoke test

- [ ] **Step 1: Start the server**

```bash
cd /home/mgipson/netwatch && python monitor.py --no-tui &
sleep 2
```

- [ ] **Step 2: Verify all static assets return 200**

```bash
for f in main.css utils.js core.js topology.js auth.js inventory.js ai-panel.js; do
  code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/static/$f)
  echo "$f -> $code"
done
```

Expected: all return `200`.

- [ ] **Step 3: Verify dashboard loads and references static files**

```bash
curl -s http://localhost:8080/ | grep "script src"
# Expected: 6 lines with /static/ references
```

- [ ] **Step 4: Verify unknown static path returns 404**

```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/static/../../monitor.py
# Expected: 404 (allowlist prevents traversal)
```

- [ ] **Step 5: Run full test suite**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/ -v 2>&1 | tail -25
```

Expected: all tests pass.

- [ ] **Step 6: Kill server**

```bash
kill %1
```

- [ ] **Step 7: Deploy to Pi and do manual browser test**

```bash
# On the Pi:
systemctl --user restart netwatch
```

Open `http://192.168.6.90:8080` and verify:
- [ ] Host cards load and show live status
- [ ] Topology tab renders the D3 force graph
- [ ] Inventory tab shows records
- [ ] AI chat bubble opens and streams a response
- [ ] Login/logout cycle works
- [ ] Backup download produces a `.tar.gz`

- [ ] **Step 8: Commit any fixups**

```bash
git add -p
git commit -m "fix: [describe any smoke test fixups]"
```
