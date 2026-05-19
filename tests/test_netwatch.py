import sqlite3
import os
import io
from monitor import _column_exists
from monitor import export_inventory_to_xlsx


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

from monitor import HistoryDB, InventoryDB, build_api_payload


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
