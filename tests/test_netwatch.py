import sqlite3
import os
import re
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


# ── Transition-only logging ──────────────────────────────────────────────────

def test_should_log_transition_first_ping():
    from monitor import _should_log_transition
    assert _should_log_transition(None, True) is True
    assert _should_log_transition(None, False) is True


def test_should_log_transition_state_change():
    from monitor import _should_log_transition
    assert _should_log_transition(True, False) is True
    assert _should_log_transition(False, True) is True


def test_should_log_transition_steady_state():
    from monitor import _should_log_transition
    assert _should_log_transition(True, True) is False
    assert _should_log_transition(False, False) is False


# ── WAL cap + prune without VACUUM ───────────────────────────────────────────

def test_journal_size_limit_pragma_set(tmp_path):
    from monitor import HistoryDB
    hdb = HistoryDB(str(tmp_path / "t.db"))
    assert hdb.conn.execute("PRAGMA journal_size_limit").fetchone()[0] == 16777216
    hdb.close()


def test_prune_deletes_old_keeps_recent_no_vacuum(tmp_path):
    from monitor import HistoryDB
    hdb = HistoryDB(str(tmp_path / "t.db"), retention_days=7)
    now = int(time.time())
    hdb.conn.execute("INSERT INTO pings (host_ip, timestamp, is_up, latency_ms) VALUES (?,?,?,?)",
                     ("10.0.0.1", now - 8 * 86400, 1, 1.0))
    hdb.conn.execute("INSERT INTO pings (host_ip, timestamp, is_up, latency_ms) VALUES (?,?,?,?)",
                     ("10.0.0.1", now, 1, 1.0))
    deleted, _ = hdb.prune()
    assert deleted == 1
    assert hdb.conn.execute("SELECT COUNT(*) FROM pings").fetchone()[0] == 1
    hdb.close()


# ── Batched ping inserts ─────────────────────────────────────────────────────

def test_record_ping_buffers_until_flush(tmp_path):
    from monitor import HistoryDB
    db_path = str(tmp_path / "t.db")
    hdb = HistoryDB(db_path)
    hdb.record_ping("10.0.0.1", True, 5.0)
    other = sqlite3.connect(db_path)
    assert other.execute("SELECT COUNT(*) FROM pings").fetchone()[0] == 0
    hdb.flush_pings()
    assert other.execute("SELECT COUNT(*) FROM pings").fetchone()[0] == 1
    other.close(); hdb.close()


def test_buffer_threshold_autoflush(tmp_path):
    from monitor import HistoryDB
    db_path = str(tmp_path / "t.db")
    hdb = HistoryDB(db_path)
    hdb.FLUSH_MAX = 3
    for _ in range(3):
        hdb.record_ping("10.0.0.1", True, 1.0)
    other = sqlite3.connect(db_path)
    assert other.execute("SELECT COUNT(*) FROM pings").fetchone()[0] == 3
    other.close(); hdb.close()


def test_recent_pings_sees_buffered_rows(tmp_path):
    from monitor import HistoryDB
    hdb = HistoryDB(str(tmp_path / "t.db"))
    hdb.record_ping("10.0.0.1", True, 5.0)
    hdb.record_ping("10.0.0.1", False, None)
    assert hdb.recent_pings("10.0.0.1", limit=10) == [(True, 5.0), (False, None)]
    hdb.close()


def test_close_flushes_buffer(tmp_path):
    from monitor import HistoryDB
    db_path = str(tmp_path / "t.db")
    hdb = HistoryDB(db_path)
    hdb.record_ping("10.0.0.1", True, 5.0)
    hdb.close()
    other = sqlite3.connect(db_path)
    assert other.execute("SELECT COUNT(*) FROM pings").fetchone()[0] == 1
    other.close()


# ── Session invalidation ─────────────────────────────────────────────────────

def test_deleted_user_cookie_rejected():
    with tempfile.TemporaryDirectory() as td:
        auth = _make_auth(td)
        auth.create_user("alice", "password123", admin=True)
        auth.create_user("bob", "password123")
        cookie = auth.make_session_cookie("bob")
        assert auth.verify_session_cookie(cookie)[0] == "bob"
        auth.delete_user("bob")
        assert auth.verify_session_cookie(cookie) == (None, False)


def test_password_change_invalidates_old_sessions():
    with tempfile.TemporaryDirectory() as td:
        auth = _make_auth(td)
        auth.create_user("alice", "password123", admin=True)
        old_cookie = auth.make_session_cookie("alice")
        assert auth.verify_session_cookie(old_cookie)[0] == "alice"
        auth.change_password("alice", "newpassword456")
        assert auth.verify_session_cookie(old_cookie) == (None, False)
        new_cookie = auth.make_session_cookie("alice")
        assert auth.verify_session_cookie(new_cookie) == ("alice", True)


def test_legacy_two_part_cookie_rejected():
    import hmac as _hmac, hashlib as _hashlib, base64 as _b64
    with tempfile.TemporaryDirectory() as td:
        auth = _make_auth(td)
        auth.create_user("alice", "password123", admin=True)
        payload = f"alice|{int(time.time()) + 3600}"  # old format: no generation
        secret = auth.data["secret_key"].encode()
        sig = _hmac.new(secret, payload.encode(), _hashlib.sha256).hexdigest()
        token = _b64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        assert auth.verify_session_cookie(f"{token}.{sig}") == (None, False)


# ── /api/status settings allowlist ───────────────────────────────────────────

def test_api_payload_settings_allowlist():
    from monitor import build_api_payload

    class _FakeHM:
        def list_hosts(self):
            return []

    settings = {"default_interval": 15, "openrouter_api_key": "sk-secret",
                "ntfy_topic": "secret-topic", "refresh_rate": 5}
    payload = build_api_payload(_FakeHM(), settings)
    assert payload["settings"] == {"default_interval": 15, "refresh_rate": 5}
    assert "openrouter_api_key" not in payload["settings"]


# ── Config write hardening ───────────────────────────────────────────────────

def test_save_hosts_config_sets_0600(tmp_path):
    from monitor import save_hosts_config
    path = str(tmp_path / "hosts.yaml")
    save_hosts_config(path, [{"name": "a", "ip": "10.0.0.1"}])
    assert os.stat(path).st_mode & 0o777 == 0o600


def _auth_test_server(auth):
    """One-request test server with auth enabled. Returns (server, port, thread)."""
    handler = make_handler(None, {}, "/dev/null", auth_manager=auth)
    server = _THTS(("127.0.0.1", 0), handler)
    t = _threading.Thread(target=server.handle_request)
    t.start()
    return server, server.server_address[1], t


def test_post_hosts_requires_admin(tmp_path):
    auth = AuthManager(str(tmp_path / "auth.json"))
    auth.create_user("root", "password123", admin=True)
    auth.create_user("bob", "password123")
    server, port, t = _auth_test_server(auth)
    try:
        cookie = auth.make_session_cookie("bob")
        req = _urlreq.Request(f"http://127.0.0.1:{port}/api/hosts", data=b'{"hosts": []}',
                              method="POST", headers={"Cookie": f"nw_session={cookie}"})
        try:
            _urlreq.urlopen(req)
            assert False, "expected 403"
        except _urlerr.HTTPError as e:
            assert e.code == 403
    finally:
        server.server_close()
        t.join()


def test_non_dict_json_body_rejected(tmp_path):
    auth = AuthManager(str(tmp_path / "auth.json"))
    auth.create_user("root", "password123", admin=True)
    server, port, t = _auth_test_server(auth)
    try:
        cookie = auth.make_session_cookie("root")
        req = _urlreq.Request(f"http://127.0.0.1:{port}/api/hosts", data=b'[1,2,3]',
                              method="POST", headers={"Cookie": f"nw_session={cookie}"})
        try:
            _urlreq.urlopen(req)
            assert False, "expected 400"
        except _urlerr.HTTPError as e:
            assert e.code == 400
    finally:
        server.server_close()
        t.join()


# ── /api/history: bucketed latency series ────────────────────────────────────

def test_history_series_buckets_and_aggregates(tmp_path):
    from monitor import HistoryDB
    hdb = HistoryDB(str(tmp_path / "t.db"))
    now = int(time.time())
    rows = [("10.0.0.1", now - 30, 1, 10.0), ("10.0.0.1", now - 20, 1, 20.0),
            ("10.0.0.1", now - 10, 0, None), ("10.0.0.2", now - 10, 1, 99.0)]
    hdb.conn.executemany("INSERT INTO pings (host_ip, timestamp, is_up, latency_ms) VALUES (?,?,?,?)", rows)
    res = hdb.history_series("10.0.0.1", hours=1)
    assert res["bucket_seconds"] == 60  # 3600s / 180 points -> min clamp 60
    assert sum(p["n"] for p in res["points"]) == 3
    assert max(p["max"] for p in res["points"] if p["max"] is not None) == 20.0
    assert min(p["min"] for p in res["points"] if p["min"] is not None) == 10.0
    hdb.close()


def test_history_series_clamps_hours(tmp_path):
    from monitor import HistoryDB
    hdb = HistoryDB(str(tmp_path / "t.db"))
    assert hdb.history_series("10.0.0.1", hours=9999)["points"] == []
    hdb.close()


def test_h_get_history_requires_ip(tmp_path):
    from monitor import HistoryDB, _h_get_history
    hdb = HistoryDB(str(tmp_path / "t.db"))
    code, body = _h_get_history("/api/history", hdb)
    assert code == 400
    code, body = _h_get_history("/api/history?ip=10.0.0.1&hours=abc", hdb)
    assert code == 400
    hdb.close()


def test_h_get_history_returns_points(tmp_path):
    from monitor import HistoryDB, _h_get_history
    hdb = HistoryDB(str(tmp_path / "t.db"))
    hdb.record_ping("10.0.0.1", True, 12.0)
    code, body = _h_get_history("/api/history?ip=10.0.0.1&hours=1", hdb)
    assert code == 200
    assert body["ip"] == "10.0.0.1"
    assert sum(p["n"] for p in body["points"]) == 1
    hdb.close()


# ── Daily uptime rollups ─────────────────────────────────────────────────────

def _insert_ping(hdb, ip, ts, up, lat):
    hdb.conn.execute("INSERT INTO pings (host_ip, timestamp, is_up, latency_ms) VALUES (?,?,?,?)",
                     (ip, ts, 1 if up else 0, lat))


def test_rollup_aggregates_complete_days_only(tmp_path):
    from monitor import HistoryDB
    hdb = HistoryDB(str(tmp_path / "t.db"))
    now = int(time.time())
    _insert_ping(hdb, "10.0.0.1", now - 86400, True, 10.0)   # yesterday
    _insert_ping(hdb, "10.0.0.1", now - 86400 + 60, False, None)
    _insert_ping(hdb, "10.0.0.1", now, True, 5.0)            # today: excluded
    hdb.rollup_days()
    rows = hdb.conn.execute("SELECT day, total, up, latency_avg FROM ping_daily").fetchall()
    assert len(rows) == 1
    assert rows[0][1] == 2 and rows[0][2] == 1 and rows[0][3] == 10.0
    hdb.close()


def test_rollup_is_idempotent(tmp_path):
    from monitor import HistoryDB
    hdb = HistoryDB(str(tmp_path / "t.db"))
    _insert_ping(hdb, "10.0.0.1", int(time.time()) - 86400, True, 10.0)
    hdb.rollup_days(); hdb.rollup_days()
    assert hdb.conn.execute("SELECT COUNT(*) FROM ping_daily").fetchone()[0] == 1
    hdb.close()


def test_daily_history_survives_prune(tmp_path):
    from monitor import HistoryDB
    hdb = HistoryDB(str(tmp_path / "t.db"), retention_days=7)
    old = int(time.time()) - 8 * 86400
    _insert_ping(hdb, "10.0.0.1", old, True, 10.0)
    hdb.rollup_days()
    hdb.prune()  # deletes the raw ping
    assert hdb.conn.execute("SELECT COUNT(*) FROM pings").fetchone()[0] == 0
    daily = hdb.daily_history("10.0.0.1", days=30)
    assert len(daily) == 1 and daily[0]["uptime_pct"] == 100.0
    hdb.close()


# ── Static asset cache busting ───────────────────────────────────────────────

def test_dashboard_html_version_substitution(tmp_path):
    from monitor import _load_dashboard_html, VERSION
    (tmp_path / "dashboard.html").write_text('<script src="/static/core.js?v={{VERSION}}"></script>')
    out = _load_dashboard_html(str(tmp_path))
    assert "{{VERSION}}" not in out
    assert f"?v={VERSION}" in out


# ── static asset self-hosting ────────────────────────────────────────────

def test_static_whitelist_includes_vendored_assets():
    from monitor import _STATIC_FILES
    expected = {
        'd3.v7.min.js': 'application/javascript',
        'fonts.css': 'text/css',
        'dmsans-300.woff2': 'font/woff2', 'dmsans-400.woff2': 'font/woff2',
        'dmsans-500.woff2': 'font/woff2', 'dmsans-600.woff2': 'font/woff2',
        'dmmono-400.woff2': 'font/woff2', 'dmmono-500.woff2': 'font/woff2',
    }
    for fname, mime in expected.items():
        assert fname in _STATIC_FILES, fname
        assert _STATIC_FILES[fname].startswith(mime), fname

def test_vendored_asset_files_exist_on_disk():
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'static'))
    for fname in ['d3.v7.min.js', 'fonts.css', 'dmsans-400.woff2', 'dmmono-400.woff2']:
        assert os.path.exists(os.path.join(base, fname)), fname
