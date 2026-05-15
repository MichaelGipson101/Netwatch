import sqlite3
import os
from monitor import _column_exists


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
