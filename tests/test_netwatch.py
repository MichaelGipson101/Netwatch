import sqlite3
import os
import io
import json as _json
import threading as _threading
import urllib.request as _urlreq
import urllib.error as _urlerr
from http.server import ThreadingHTTPServer as _THTS
from monitor import _column_exists
from monitor import export_inventory_to_xlsx
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


def test_column_exists_returns_true_for_existing_column():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (a TEXT, b INTEGER)")
    assert _column_exists(conn, "t", "a") is True
    assert _column_exists(conn, "t", "b") is True


def test_column_exists_returns_false_for_missing_column():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (a TEXT)")
    assert _column_exists(conn, "t", "x") is False


def test_column_exists_returns_false_for_unknown_table():
    conn = sqlite3.connect(":memory:")
    assert _column_exists(conn, "nonexistent", "col") is False


def test_real_operationalerror_propagates_on_bad_sql():
    """ALTER TABLE on a non-existent table raises OperationalError — not swallowed."""
    import sqlite3 as _sq
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("ALTER TABLE ghost ADD COLUMN x TEXT")
        assert False, "Should have raised"
    except _sq.OperationalError:
        pass  # expected — real errors propagate


import tempfile, time
from monitor import AuthManager


def _make_auth(tmpdir, db_path=None):
    auth_path = os.path.join(tmpdir, "auth.json")
    return AuthManager(auth_path, db_path=db_path)


def test_failed_attempts_persist_across_restart():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        am1 = _make_auth(tmpdir, db_path=db_path)
        for _ in range(AuthManager.LOCKOUT_AFTER):
            am1.record_failed_attempt("1.2.3.4")
        assert am1.is_locked_out("1.2.3.4")

        # New instance, same DB — lockout must survive
        am2 = _make_auth(tmpdir, db_path=db_path)
        assert am2.is_locked_out("1.2.3.4")


def test_successful_login_clears_persisted_attempts():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        am1 = _make_auth(tmpdir, db_path=db_path)
        for _ in range(AuthManager.LOCKOUT_AFTER):
            am1.record_failed_attempt("10.0.0.1")
        am1.record_successful_login("10.0.0.1")

        am2 = _make_auth(tmpdir, db_path=db_path)
        assert not am2.is_locked_out("10.0.0.1")


def test_no_db_path_works_as_before():
    """AuthManager without db_path still works (in-memory only)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        am = _make_auth(tmpdir, db_path=None)
        for _ in range(AuthManager.LOCKOUT_AFTER):
            am.record_failed_attempt("192.168.1.1")
        assert am.is_locked_out("192.168.1.1")


# ── device_type in /api/status ──────────────────────────────────────────────

from monitor import HistoryDB, InventoryDB, build_api_payload, make_handler


class _FakeHost:
    def __init__(self, ip):
        self.ip = ip
        self.is_up = True
        self.last_checked = None
        self.always_on = True
    def to_dict(self):
        return {"ip": self.ip, "is_up": self.is_up}


class _FakeHostManager:
    def __init__(self, hosts): self._hosts = hosts
    def list_hosts(self): return self._hosts


def _make_idb(tmpdir):
    db_path = os.path.join(tmpdir, "icons_test.db")
    hdb = HistoryDB(db_path)
    idb = InventoryDB(hdb)
    return hdb, idb


def test_get_device_type_map_returns_ip_to_type():
    with tempfile.TemporaryDirectory() as tmpdir:
        hdb, idb = _make_idb(tmpdir)
        with hdb.lock:
            hdb.conn.execute(
                "INSERT INTO inventory (system, ip, device_type, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ("TabletA", "10.0.0.1", "tablet", 0, 0)
            )
            hdb.conn.execute(
                "INSERT INTO inventory (system, ip, device_type, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ("Phone1", "10.0.0.2", "phone", 0, 0)
            )
        result = idb.get_device_type_map()
        assert result == {"10.0.0.1": "tablet", "10.0.0.2": "phone"}
        hdb.close()


def test_get_device_type_map_excludes_null_ip():
    with tempfile.TemporaryDirectory() as tmpdir:
        hdb, idb = _make_idb(tmpdir)
        with hdb.lock:
            hdb.conn.execute(
                "INSERT INTO inventory (system, ip, device_type, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ("NoIP", None, "host", 0, 0)
            )
        result = idb.get_device_type_map()
        assert result == {}
        hdb.close()


def test_build_api_payload_annotates_device_type():
    with tempfile.TemporaryDirectory() as tmpdir:
        hdb, idb = _make_idb(tmpdir)
        with hdb.lock:
            hdb.conn.execute(
                "INSERT INTO inventory (system, ip, device_type, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ("MyTablet", "10.0.0.1", "tablet", 0, 0)
            )
        hm = _FakeHostManager([_FakeHost("10.0.0.1"), _FakeHost("10.0.0.99")])
        payload = build_api_payload(hm, {}, inventory_db=idb)
        hosts = {h["ip"]: h for h in payload["hosts"]}
        assert hosts["10.0.0.1"]["device_type"] == "tablet"
        assert hosts["10.0.0.99"]["device_type"] == "host"  # no record → default
        hdb.close()


def test_build_api_payload_without_inventory_db():
    hm = _FakeHostManager([_FakeHost("10.0.0.1")])
    payload = build_api_payload(hm, {})
    assert payload["hosts"][0]["device_type"] == "host"


def test_export_scope_hosts_filters_to_hosts_only():
    try:
        import openpyxl
    except ImportError:
        return  # skip if openpyxl not installed
    with tempfile.TemporaryDirectory() as tmpdir:
        hdb, idb = _make_idb(tmpdir)
        with hdb.lock:
            hdb.conn.execute(
                "INSERT INTO inventory (system, ip, device_type, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ("Server1", "10.0.0.1", "host", 0, 0)
            )
            hdb.conn.execute(
                "INSERT INTO inventory (system, ip, device_type, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ("VM1", "10.0.0.2", "vm", 0, 0)
            )
        data, filename = export_inventory_to_xlsx(idb, scope='hosts')
        assert data is not None, filename
        assert "hosts" in filename
        wb = openpyxl.load_workbook(io.BytesIO(data))
        assert wb.sheetnames == ["Inventory"]
        ws = wb.active
        # Only 1 data row (host); VM excluded
        assert ws.max_row == 2  # header + 1 host
        assert ws.cell(row=2, column=2).value == "Server1"
        hdb.close()


def test_export_scope_all_creates_one_sheet_per_type():
    try:
        import openpyxl
    except ImportError:
        return
    with tempfile.TemporaryDirectory() as tmpdir:
        hdb, idb = _make_idb(tmpdir)
        with hdb.lock:
            hdb.conn.execute(
                "INSERT INTO inventory (system, ip, device_type, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ("Server1", "10.0.0.1", "host", 0, 0)
            )
            hdb.conn.execute(
                "INSERT INTO inventory (system, ip, device_type, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ("VM1", "10.0.0.2", "vm", 0, 0)
            )
        data, filename = export_inventory_to_xlsx(idb, scope='all')
        assert data is not None, filename
        assert "all" in filename
        wb = openpyxl.load_workbook(io.BytesIO(data))
        assert "Hosts" in wb.sheetnames
        assert "VMs" in wb.sheetnames
        assert "Network" not in wb.sheetnames  # no network records → no sheet
        assert wb["Hosts"].cell(row=2, column=2).value == "Server1"
        assert wb["VMs"].cell(row=2, column=2).value == "VM1"
        hdb.close()


def test_export_scope_defaults_to_hosts():
    try:
        import openpyxl
    except ImportError:
        return
    with tempfile.TemporaryDirectory() as tmpdir:
        hdb, idb = _make_idb(tmpdir)
        with hdb.lock:
            hdb.conn.execute(
                "INSERT INTO inventory (system, ip, device_type, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ("VM1", "10.0.0.2", "vm", 0, 0)
            )
        # No scope arg → defaults to hosts → VM excluded → 0 data rows
        data, filename = export_inventory_to_xlsx(idb)
        assert data is not None
        wb = openpyxl.load_workbook(io.BytesIO(data))
        assert wb.active.max_row == 1  # header only, no host records
        hdb.close()


def _ai_config_server(settings):
    """Spin up a single-request test server for /api/ai-config. Returns (server, port)."""
    hm = _FakeHostManager([])
    handler = make_handler(hm, settings, "/dev/null", auth_manager=None)
    server = _THTS(("127.0.0.1", 0), handler)
    return server, server.server_address[1]


def test_ai_config_returns_key_and_model():
    settings = {"openrouter_api_key": "sk-or-test-123", "ai_model": "deepseek/deepseek-v4-flash:free"}
    server, port = _ai_config_server(settings)
    t = _threading.Thread(target=server.handle_request)
    t.start()
    try:
        with _urlreq.urlopen(f"http://127.0.0.1:{port}/api/ai-config") as r:
            data = _json.loads(r.read())
        assert data["api_key"] == "sk-or-test-123"
        assert data["model"] == "deepseek/deepseek-v4-flash:free"
    finally:
        server.server_close()
        t.join()


def test_ai_config_missing_key_returns_404():
    server, port = _ai_config_server({})
    t = _threading.Thread(target=server.handle_request)
    t.start()
    try:
        try:
            _urlreq.urlopen(f"http://127.0.0.1:{port}/api/ai-config")
            assert False, "Expected 404"
        except _urlerr.HTTPError as e:
            assert e.code == 404
    finally:
        server.server_close()
        t.join()


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
    assert body["error"] == "ai_not_configured"


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


def test_h_get_auth_users_no_auth_manager():
    code, body = _h_get_auth_users(None)
    assert code == 404
    assert "error" in body


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
        assert isinstance(body["items"], list)
        assert len(body["items"]) > 0
        assert any(i["system"] == "Switch1" for i in body["items"])
        assert "id" in body["items"][0]
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
    assert "error" in body


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
    assert body["error"] == "ip required"


# ── _h_post_inventory_create ─────────────────────────────────────────────────

def test_h_post_inventory_create_no_db():
    code, body = _h_post_inventory_create({}, None)
    assert code == 500
    assert "error" in body


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
        assert "error" in body


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
        assert "error" in body


def test_h_post_auth_user_delete_not_found():
    with tempfile.TemporaryDirectory() as tmpdir:
        am = _make_auth(tmpdir)
        code, body = _h_post_auth_user_delete("/api/auth/users/nobody", am)
        assert code == 400
        assert "error" in body
