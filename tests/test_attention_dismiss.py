import json
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from netwatch import http_handlers as H
from netwatch.attention import AlertGate, AlertLedger, build_attention
from netwatch.auth import AuthManager
from netwatch.server import make_handler
from netwatch.storage import HistoryDB


@pytest.fixture
def hdb(tmp_path):
    db = HistoryDB(str(tmp_path / "t.db"))
    yield db
    db.close()


def _fire(led, cid="pbs-backup-ct-304", now=1000):
    return led.fire(cid, "pbs", "warning", "No recent backup for CT 304", "Backups", now=now)


def test_dismiss_hides_the_row_and_counts_it(hdb):
    led = AlertLedger(hdb)
    _fire(led)
    assert led.dismiss("pbs-backup-ct-304", now=1100) is True
    assert led.active() == []
    assert led.dismissed_count() == 1
    assert [r["condition_id"] for r in led.active(include_dismissed=True)] == ["pbs-backup-ct-304"]


def test_dismiss_is_false_for_unknown_cleared_or_already_dismissed(hdb):
    led = AlertLedger(hdb)
    assert led.dismiss("nope") is False
    _fire(led)
    assert led.dismiss("pbs-backup-ct-304") is True
    assert led.dismiss("pbs-backup-ct-304") is False           # already dismissed
    led.clear("pbs-backup-ct-304", now=1200)
    assert led.dismiss("pbs-backup-ct-304") is False           # cleared rows cannot be dismissed
    assert led.dismissed_count() == 0                          # cleared rows are not "dismissed"


def test_refire_of_an_active_dismissed_row_stays_dismissed_and_does_not_notify(hdb):
    led = AlertLedger(hdb)
    _fire(led)
    led.dismiss("pbs-backup-ct-304")
    assert _fire(led, now=1300) is False
    assert led.active() == [] and led.dismissed_count() == 1


def test_recurrence_after_clear_is_a_fresh_undismissed_alert(hdb):
    led = AlertLedger(hdb, cooldown_seconds=0)
    _fire(led)
    led.dismiss("pbs-backup-ct-304")
    led.clear("pbs-backup-ct-304", now=1200)
    assert _fire(led, now=1300) is True
    (row,) = led.active()
    assert row["since"] == 1300 and led.dismissed_count() == 0


def test_restore_all_returns_the_count_and_unhides_rows(hdb):
    led = AlertLedger(hdb)
    _fire(led, "a")
    _fire(led, "b")
    _fire(led, "c")
    led.dismiss("a")
    led.dismiss("b")
    assert led.restore_all() == 2
    assert {r["condition_id"] for r in led.active()} == {"a", "b", "c"}
    assert led.restore_all() == 0


def test_gate_still_sees_and_clears_dismissed_rows(hdb):
    led = AlertLedger(hdb)
    g = AlertGate("pbs", "Backups", led)
    g.fire("x", "warning", "m")
    led.dismiss("x")
    assert g.active_ids() == {"x"}                   # the poller must still be able to clear it
    g.begin_pass()
    g.end_pass()                                     # untouched this pass -> cleared
    assert led.active_ids("pbs") == set() and led.dismissed_count() == 0


def test_dismissed_rows_are_excluded_from_the_verdict_and_counted():
    p = build_attention([], [], {}, [], 0, [], now=1000.0, dismissed=3)
    assert p["verdict"]["counts"]["dismissed"] == 3
    assert build_attention([], [], {}, [], 0, [], now=1000.0)["verdict"]["counts"]["dismissed"] == 0


def test_alert_state_column_is_added_to_an_existing_database(tmp_path):
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE alert_state (condition_id TEXT PRIMARY KEY, source TEXT NOT NULL,
            severity TEXT NOT NULL, title TEXT NOT NULL, detail TEXT NOT NULL,
            since INTEGER NOT NULL, notified_at INTEGER, cleared_at INTEGER);
        INSERT INTO alert_state VALUES ('old', 'nas', 'warning', 't', 'd', 5, 6, NULL);
    """)
    con.commit()
    con.close()
    db = HistoryDB(path)
    try:
        cols = [r[1] for r in db.conn.execute("PRAGMA table_info(alert_state)")]
        assert "dismissed_at" in cols
        led = AlertLedger(db)
        assert [r["condition_id"] for r in led.active()] == ["old"]     # existing row preserved
        assert led.dismiss("old") is True
    finally:
        db.close()


# ── handler ─────────────────────────────────────────────────────────────────

def test_handler_dismisses_one_item(hdb):
    led = AlertLedger(hdb)
    _fire(led)
    assert H._h_post_attention_dismiss({"id": "alert:pbs-backup-ct-304"}, led) == (200, {"ok": True})
    assert led.active() == []


@pytest.mark.parametrize("data", [
    {}, {"id": None}, {"id": 5}, {"id": ""}, {"id": "alert:"}, {"id": "host_down:10.0.0.1"},
    {"id": "pbs-backup-ct-304"}, {"id": "alert:" + "x" * 300}, {"restore_all": "yes"},
])
def test_handler_rejects_bad_ids_with_400(hdb, data):
    status, body = H._h_post_attention_dismiss(data, AlertLedger(hdb))
    assert status == 400 and "error" in body


def test_handler_404_when_nothing_matches_and_503_without_a_ledger(hdb):
    assert H._h_post_attention_dismiss({"id": "alert:nope"}, AlertLedger(hdb)) == (404, {"error": "not_found"})
    assert H._h_post_attention_dismiss({"id": "alert:x"}, None)[0] == 503
    assert H._h_post_attention_dismiss({"restore_all": True}, None)[0] == 503


def test_handler_restore_all(hdb):
    led = AlertLedger(hdb)
    _fire(led, "a")
    _fire(led, "b")
    led.dismiss("a")
    assert H._h_post_attention_dismiss({"restore_all": True}, led) == (200, {"ok": True, "restored": 1})


def test_handler_survives_a_ledger_that_raises():
    class Boom:
        def dismiss(self, cid):
            raise RuntimeError("db")

        def restore_all(self):
            raise RuntimeError("db")
    assert H._h_post_attention_dismiss({"id": "alert:x"}, Boom())[0] == 500
    assert H._h_post_attention_dismiss({"restore_all": True}, Boom())[0] == 500


def test_attention_handler_hides_dismissed_items_and_reports_the_count(hdb):
    led = AlertLedger(hdb)
    _fire(led, "a")
    _fire(led, "b")
    led.dismiss("a")
    _, p = H._h_get_attention(None, None, led, None, now=2000)
    assert [i["id"] for i in p["items"]] == ["alert:b"]
    assert p["verdict"]["counts"]["dismissed"] == 1


# ── HTTP level ──────────────────────────────────────────────────────────────

def _server(auth, ledger):
    handler = make_handler(None, {}, "/dev/null", auth_manager=auth, ledger=ledger)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=server.handle_request)
    t.start()
    return server, server.server_address[1], t


def _post(port, cookie=None, token=None, body=b'{"restore_all": true}'):
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = f"nw_session={cookie}"
    if token:
        headers["X-CSRF-Token"] = token
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/attention/dismiss", data=body,
                                 method="POST", headers=headers)
    return urllib.request.urlopen(req)


def test_dismiss_route_needs_a_session(tmp_path, hdb):
    auth = AuthManager(str(tmp_path / "auth.json"))
    auth.create_user("root", "password123", admin=True)
    server, port, t = _server(auth, AlertLedger(hdb))
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(port)
        assert e.value.code in (401, 403)
    finally:
        server.server_close()
        t.join()


def test_dismiss_route_is_admin_only(tmp_path, hdb):
    auth = AuthManager(str(tmp_path / "auth.json"))
    auth.create_user("root", "password123", admin=True)
    auth.create_user("bob", "password123")
    cookie = auth.make_session_cookie("bob")
    server, port, t = _server(auth, AlertLedger(hdb))
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(port, cookie, auth.csrf_token_for_cookie(cookie))
        assert e.value.code == 403
    finally:
        server.server_close()
        t.join()


def test_dismiss_route_works_for_an_admin_with_csrf(tmp_path, hdb):
    auth = AuthManager(str(tmp_path / "auth.json"))
    auth.create_user("root", "password123", admin=True)
    cookie = auth.make_session_cookie("root")
    led = AlertLedger(hdb)
    _fire(led)
    server, port, t = _server(auth, led)
    try:
        with _post(port, cookie, auth.csrf_token_for_cookie(cookie),
                   body=json.dumps({"id": "alert:pbs-backup-ct-304"}).encode()) as r:
            assert r.status == 200 and json.loads(r.read()) == {"ok": True}
        assert led.active() == []
    finally:
        server.server_close()
        t.join()


def test_dismiss_route_without_csrf_is_403(tmp_path, hdb):
    auth = AuthManager(str(tmp_path / "auth.json"))
    auth.create_user("root", "password123", admin=True)
    cookie = auth.make_session_cookie("root")
    server, port, t = _server(auth, AlertLedger(hdb))
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(port, cookie, None)
        assert e.value.code == 403
    finally:
        server.server_close()
        t.join()
