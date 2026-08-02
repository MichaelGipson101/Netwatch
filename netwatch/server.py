"""HTTP server: static-file map, request handler factory, and the
threading server loop. `make_handler()` builds a `BaseHTTPRequestHandler`
subclass with `do_GET`/`do_POST` implemented as long if/elif chains over
`self.path`; each branch calls a `_h_get_*`/`_h_post_*` handler function
from netwatch.http_handlers with whatever state it needs as explicit
arguments."""

import os
import json
import logging
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from netwatch.auth import parse_cookies
from netwatch.storage import export_inventory_to_xlsx, import_inventory_from_xlsx, create_backup_tarball
from netwatch.http_handlers import (
    _h_get_status, _h_get_backup_status,
    _h_get_inventory_backup_status, _h_get_ai_config,
    _h_get_ai_usage, _h_post_ai_chat,
    _h_get_settings, _h_post_settings, _h_post_nas_ignore_alert,
    _h_post_nas_unignore_alert, _h_post_nas_acknowledge_alert, _h_post_system_restart,
    _h_get_hosts, _h_get_pi_health, _h_get_nas, _h_get_proxmox, _h_get_pbs,
    _h_get_power, _h_get_ups, _h_post_proxmox_action, _h_get_auth_status, _h_get_auth_users,
    _h_get_inventory, _h_get_inventory_record, _h_get_topology, _h_get_connections,
    _h_get_connections_for_device, _h_get_discover, _h_post_brief, _h_get_briefs,
    _h_get_history, _h_post_inventory_create, _h_post_inventory_update,
    _h_post_inventory_delete, _h_post_connection_create, _h_post_connection_update,
    _h_post_connection_delete, _h_post_discover, _h_post_detect_mac, _h_post_wake,
    _h_post_hosts, _h_post_auth_users, _h_post_auth_password, _h_post_auth_user_delete,
    _h_get_quicklinks, _h_post_quicklinks_create, _h_post_quicklinks_update,
    _h_post_quicklinks_delete, _h_post_quicklinks_move,
    _h_post_maintenance_start, _h_post_maintenance_clear, _h_post_maintenance_quickstart,
)


_STATIC_FILES = {
    'main.css':    'text/css; charset=utf-8',
    'fonts.css':   'text/css; charset=utf-8',
    'utils.js':    'application/javascript; charset=utf-8',
    'overview.js': 'application/javascript; charset=utf-8',
    'core.js':     'application/javascript; charset=utf-8',
    'topology.js': 'application/javascript; charset=utf-8',
    'inventory.js':'application/javascript; charset=utf-8',
    'auth.js':     'application/javascript; charset=utf-8',
    'ai-panel.js':  'application/javascript; charset=utf-8',
    'nas.js':       'application/javascript; charset=utf-8',
    'proxmox.js':   'application/javascript; charset=utf-8',
    'settings.js':  'application/javascript; charset=utf-8',
    'd3.v7.min.js':    'application/javascript; charset=utf-8',
    'dmsans-300.woff2':'font/woff2',
    'dmsans-400.woff2':'font/woff2',
    'dmsans-500.woff2':'font/woff2',
    'dmsans-600.woff2':'font/woff2',
    'dmmono-400.woff2':'font/woff2',
    'dmmono-500.woff2':'font/woff2',
    'favicon.svg':     'image/svg+xml',
    'favicon-alert.svg':'image/svg+xml',
    'manifest.json':   'application/manifest+json',
    'icon-192.png':    'image/png',
    'icon-512.png':    'image/png',
    'apple-touch-icon.png':'image/png',
    'mira-avatar.png': 'image/png',
    'quicklinks.js':'application/javascript; charset=utf-8',
}


def make_handler(host_manager, settings, config_path, incident_log=None, auth_manager=None, inventory_db=None, dashboard_html="", history_db=None, nas_poller=None, proxmox_poller=None, ha_poller=None, pbs_poller=None, ups_poller=None, static_dir=None, quicklinks_db=None):
    static_dir = static_dir or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args): pass

        def _client_ip(self):
            # Just use the direct connection IP - no proxy support for now
            return self.client_address[0] if self.client_address else "unknown"

        def _session_cookie_value(self):
            cookies = parse_cookies(self.headers.get("Cookie", ""))
            return cookies.get("nw_session", "")

        def _current_user(self):
            """Returns (username, is_admin) or (None, False) if not logged in."""
            if not auth_manager:
                return None, False
            return auth_manager.verify_session_cookie(self._session_cookie_value())

        def _require_auth(self, admin_only=False):
            """Returns True if request is authorised, else writes an error
            response and returns False. If no users exist yet, returns False
            with a 'setup_required' response so the frontend can prompt for
            first-run setup. POST requests additionally require a valid
            X-CSRF-Token header matching the session cookie."""
            if not auth_manager:
                return True  # auth disabled entirely
            if not auth_manager.has_users:
                self._send_json(401, {"error": "setup_required",
                                      "message": "No users configured yet. Set up the first admin user."})
                return False
            user, is_admin = self._current_user()
            if not user:
                self._send_json(401, {"error": "auth_required"})
                return False
            if admin_only and not is_admin:
                self._send_json(403, {"error": "admin_required"})
                return False
            if self.command == "POST":
                expected = auth_manager.csrf_token_for_cookie(self._session_cookie_value())
                provided = self.headers.get("X-CSRF-Token", "")
                if not provided or not hmac.compare_digest(expected, provided):
                    self._send_json(403, {"error": "csrf_required"})
                    return False
            return True

        def _set_session_cookie(self, username):
            cookie = auth_manager.make_session_cookie(username)
            max_age = auth_manager.SESSION_DAYS * 86400
            self.send_header("Set-Cookie",
                f"nw_session={cookie}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict")
            return cookie

        def _clear_session_cookie(self):
            self.send_header("Set-Cookie",
                "nw_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")


        def _read_json_body(self, max_bytes=1024 * 1024):
            """Read a JSON request body with size cap. Returns (data, error_response).

            On success: (parsed_dict, None).
            On error: (None, True) and an HTTP error response has been written.
            """
            try:
                length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                self._send_json(400, {"error": "invalid Content-Length"})
                return None, True
            if length > max_bytes:
                self._send_json(413, {"error": f"request body too large (max {max_bytes} bytes)"})
                return None, True
            try:
                body = self.rfile.read(length).decode()
                data = json.loads(body)
            except json.JSONDecodeError:
                self._send_json(400, {"error": "invalid JSON"})
                return None, True
            except (UnicodeDecodeError, ValueError):
                self._send_json(400, {"error": "invalid request body"})
                return None, True
            if not isinstance(data, dict):
                self._send_json(400, {"error": "expected a JSON object"})
                return None, True
            return data, None

        def _send_json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                body = dashboard_html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", len(body))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith('/static/'):
                fname = self.path[8:].split('?')[0]
                if fname not in _STATIC_FILES:
                    self._send_json(404, {'error': 'not found'})
                    return
                fpath = os.path.join(static_dir, fname)
                try:
                    with open(fpath, 'rb') as f:
                        body = f.read()
                    self.send_response(200)
                    self.send_header('Content-Type', _STATIC_FILES[fname])
                    self.send_header('Content-Length', len(body))
                    # URLs carry ?v={VERSION}, so a day of caching is safe
                    self.send_header('Cache-Control', 'public, max-age=86400')
                    self.end_headers()
                    self.wfile.write(body)
                except FileNotFoundError:
                    self._send_json(404, {'error': f'static file not found: {fname}'})
                return
            if self.path == "/api/status":
                if not self._require_auth(): return
                self._send_json(*_h_get_status(host_manager, settings, incident_log, inventory_db))
                return
            if self.path == "/api/backup-status":
                if not self._require_auth(admin_only=True): return
                self._send_json(*_h_get_backup_status())
                return
            if self.path == "/api/inventory-backup-status":
                if not self._require_auth(admin_only=True): return
                self._send_json(*_h_get_inventory_backup_status())
                return
            if self.path == "/api/quicklinks":
                if not self._require_auth(): return
                self._send_json(*_h_get_quicklinks(quicklinks_db))
                return
            if self.path == "/api/ai-config":
                if not self._require_auth(): return
                self._send_json(*_h_get_ai_config(settings, auth_manager))
                return
            if self.path == "/api/ai/usage":
                if not self._require_auth(): return
                self._send_json(*_h_get_ai_usage(auth_manager))
                return
            if self.path == "/api/settings":
                if not self._require_auth(admin_only=True): return
                self._send_json(*_h_get_settings(settings, auth_manager))
                return
            if self.path == "/api/hosts":
                if not self._require_auth(): return
                self._send_json(*_h_get_hosts(config_path))
                return
            if self.path == "/api/pi-health":
                if not self._require_auth(): return
                self._send_json(*_h_get_pi_health())
                return
            if self.path == "/api/nas" or self.path.startswith("/api/nas?"):
                if not self._require_auth(): return
                from urllib.parse import urlparse as _up_nas, parse_qs as _pqs_nas
                force = _pqs_nas(_up_nas(self.path).query).get("refresh", ["0"])[0] == "1"
                self._send_json(*_h_get_nas(nas_poller, force=force))
                return
            if self.path == "/api/proxmox" or self.path.startswith("/api/proxmox?"):
                if not self._require_auth(): return
                from urllib.parse import urlparse as _up_pve, parse_qs as _pqs_pve
                force = _pqs_pve(_up_pve(self.path).query).get("refresh", ["0"])[0] == "1"
                self._send_json(*_h_get_proxmox(proxmox_poller, force=force))
                return
            if self.path == "/api/pbs" or self.path.startswith("/api/pbs?"):
                if not self._require_auth(): return
                from urllib.parse import urlparse as _up_pbs, parse_qs as _pqs_pbs
                force = _pqs_pbs(_up_pbs(self.path).query).get("refresh", ["0"])[0] == "1"
                self._send_json(*_h_get_pbs(pbs_poller, force=force))
                return
            if self.path == "/api/power" or self.path.startswith("/api/power?"):
                if not self._require_auth(): return
                from urllib.parse import urlparse as _up_ha, parse_qs as _pqs_ha
                force = _pqs_ha(_up_ha(self.path).query).get("force", ["0"])[0] == "1"
                self._send_json(*_h_get_power(ha_poller, history_db, force=force))
                return
            if self.path == "/api/ups" or self.path.startswith("/api/ups?"):
                if not self._require_auth(): return
                self._send_json(*_h_get_ups(ups_poller))
                return
            if self.path == "/api/auth/status":
                self._send_json(*_h_get_auth_status(auth_manager, self._current_user, self._session_cookie_value()))
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
            if self.path.startswith("/api/history"):
                if not self._require_auth(): return
                self._send_json(*_h_get_history(self.path, history_db))
                return
            if self.path == "/api/brief":
                if not self._require_auth(): return
                self._send_json(*_h_get_briefs(history_db))
                return
            self.send_response(404)
            self.end_headers()

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
                    cookie = self._set_session_cookie(username)
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "ok": True,
                        "username": username,
                        "csrf_token": auth_manager.csrf_token_for_cookie(cookie),
                    }).encode())
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
                data, err = self._read_json_body()
                if err: return
                try:
                    username = data.get("username", "")
                    password = data.get("password", "")
                    if auth_manager.verify_password(username, password):
                        auth_manager.record_successful_login(ip)
                        is_admin = auth_manager.is_admin(username.strip().lower())
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        cookie = self._set_session_cookie(username.strip().lower())
                        self.end_headers()
                        self.wfile.write(json.dumps({
                            "ok": True,
                            "username": username.strip().lower(),
                            "admin": is_admin,
                            "csrf_token": auth_manager.csrf_token_for_cookie(cookie),
                        }).encode())
                    else:
                        auth_manager.record_failed_attempt(ip)
                        self._send_json(401, {"error": "invalid username or password"})
                except Exception as e:
                    logging.exception("login error")
                    self._send_json(500, {"error": str(e)})
                return

            if self.path == "/api/auth/logout":
                if not self._require_auth(): return
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

            if self.path == "/api/quicklinks":
                if not self._require_auth(admin_only=True): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_quicklinks_create(data, quicklinks_db))
                return

            if self.path.startswith("/api/quicklinks/") and self.path.endswith("/delete"):
                if not self._require_auth(admin_only=True): return
                self._send_json(*_h_post_quicklinks_delete(self.path, quicklinks_db))
                return

            if self.path.startswith("/api/quicklinks/") and self.path.endswith("/move"):
                if not self._require_auth(admin_only=True): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_quicklinks_move(self.path, data, quicklinks_db))
                return

            if self.path.startswith("/api/quicklinks/"):
                if not self._require_auth(admin_only=True): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_quicklinks_update(self.path, data, quicklinks_db))
                return

            if self.path == "/api/inventory-import":
                if not self._require_auth(admin_only=True): return
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

            if self.path == "/api/maintenance/start":
                if not self._require_auth(admin_only=True): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_maintenance_start(data, host_manager, history_db))
                return

            if self.path == "/api/maintenance/clear":
                if not self._require_auth(admin_only=True): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_maintenance_clear(data, host_manager, history_db))
                return

            if self.path == "/api/maintenance/quick-start" or self.path.startswith("/api/maintenance/quick-start?"):
                from urllib.parse import urlparse as _up_maint, parse_qs as _pqs_maint
                q = _pqs_maint(_up_maint(self.path).query)
                data = {"ip": q.get("ip", [""])[0], "token": q.get("token", [""])[0]}
                self._send_json(*_h_post_maintenance_quickstart(data, host_manager, history_db, auth_manager))
                return

            if self.path == "/api/proxmox/action":
                if not self._require_auth(admin_only=True): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_proxmox_action(data, proxmox_poller, auth_manager))
                return

            if self.path == "/api/ai/chat":
                if not self._require_auth(): return
                data, err = self._read_json_body()
                if err: return
                _h_post_ai_chat(self, data, auth_manager)
                return

            if self.path == "/api/hosts":
                if not self._require_auth(admin_only=True): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_hosts(data, config_path, host_manager, settings))
                return

            if self.path == "/api/nas/ignore-alert":
                if not self._require_auth(admin_only=True): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_nas_ignore_alert(data, config_path, settings, auth_manager))
                return

            if self.path == "/api/nas/unignore-alert":
                if not self._require_auth(admin_only=True): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_nas_unignore_alert(data, config_path, settings, auth_manager))
                return

            if self.path == "/api/nas/acknowledge-alert":
                if not self._require_auth(admin_only=True): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_nas_acknowledge_alert(data, nas_poller))
                return

            if self.path == "/api/system/restart":
                if not self._require_auth(admin_only=True): return
                self._send_json(200, {"ok": True})
                _h_post_system_restart(history_db, auth_manager)
                return

            if self.path == "/api/settings":
                if not self._require_auth(admin_only=True): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_settings(data, config_path, settings, auth_manager))
                return

            if self.path == "/api/brief":
                if not self._require_auth(): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_brief(history_db, data))
                return

            self.send_response(404)
            self.end_headers()

    return Handler


def start_web_server(host_manager, settings, config_path, port, stop_event, incident_log=None, auth_manager=None, inventory_db=None, dashboard_html="", history_db=None, nas_poller=None, proxmox_poller=None, ha_poller=None, pbs_poller=None, ups_poller=None, static_dir=None, quicklinks_db=None):
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(host_manager, settings, config_path, incident_log, auth_manager, inventory_db, dashboard_html, history_db, nas_poller=nas_poller, proxmox_poller=proxmox_poller, ha_poller=ha_poller, pbs_poller=pbs_poller, ups_poller=ups_poller, static_dir=static_dir, quicklinks_db=quicklinks_db))
    server.timeout = 1
    logging.info(f"Web dashboard: http://0.0.0.0:{port}")
    while not stop_event.is_set():
        server.handle_request()
    server.server_close()
