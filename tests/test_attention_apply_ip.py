"""Update IP action for ip_drift attention items: history migration, hosts.yaml rewrite,
handler ordering/rollback, item data, and the HTTP route."""
import json
import os
import stat
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
import yaml

from netwatch import http_handlers as H
from netwatch.attention import build_attention
from netwatch.auth import AuthManager
from netwatch.hosts import change_host_ip
from netwatch.server import make_handler
from netwatch.storage import HistoryDB, InventoryDB

MAC = "aa:bb:cc:dd:ee:01"
OLD, NEW = "192.168.5.160", "192.168.4.44"
TABLES = ("pings", "ping_daily", "incidents", "maintenance_windows")


@pytest.fixture
def hdb(tmp_path):
    db = HistoryDB(str(tmp_path / "t.db"))
    yield db
    db.close()


def _seed(db, ip, n=1):
    """One (or n) row(s) for `ip` in every history table."""
    for i in range(n):
        db.conn.execute("INSERT INTO pings(host_ip, timestamp, is_up, latency_ms) VALUES (?,?,1,1.0)",
                        (ip, 1000 + i))
        db.conn.execute("INSERT INTO ping_daily(day, host_ip, total, up) VALUES (?,?,10,10)",
                        (f"2026-01-0{i + 1}", ip))
        db.conn.execute("INSERT INTO incidents(host_ip, host_name, host_group, started) VALUES (?,?,?,?)",
                        (ip, "vf2", "g", 500 + i))
        db.conn.execute("INSERT INTO maintenance_windows(host_ip, host_name, started_at, expires_at) "
                        "VALUES (?,?,?,?)", (ip, "vf2", 1, 2))


def _count(db, table, ip):
    return db.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE host_ip = ?", (ip,)).fetchone()[0]


def _only(db, table, one):
    """Insert a single row into `table` for ip `one`."""
    if table == "pings":
        db.conn.execute("INSERT INTO pings(host_ip, timestamp, is_up) VALUES (?,1,1)", (one,))
    elif table == "ping_daily":
        db.conn.execute("INSERT INTO ping_daily(day, host_ip, total, up) VALUES ('2026-01-01',?,1,1)", (one,))
    elif table == "incidents":
        db.conn.execute("INSERT INTO incidents(host_ip, host_name, host_group, started) VALUES (?,'x','g',1)",
                        (one,))
    else:
        db.conn.execute("INSERT INTO maintenance_windows(host_ip, host_name, started_at, expires_at) "
                        "VALUES (?,'x',1,2)", (one,))


# ── HistoryDB.migrate_host_ip ───────────────────────────────────────────────

def test_migrate_moves_all_four_tables_and_returns_exact_counts(hdb):
    _seed(hdb, OLD, n=3)
    _seed(hdb, "10.9.9.9", n=2)                        # bystander
    res = hdb.migrate_host_ip(OLD, NEW)
    assert res == {"pings": 3, "ping_daily": 3, "incidents": 3, "maintenance_windows": 3}
    for t in TABLES:
        assert _count(hdb, t, OLD) == 0
    assert (_count(hdb, "pings", NEW), _count(hdb, "ping_daily", NEW),
            _count(hdb, "incidents", NEW), _count(hdb, "maintenance_windows", NEW)) == (3, 3, 3, 3)
    for t in TABLES:
        assert _count(hdb, t, "10.9.9.9") == 2         # untouched


@pytest.mark.parametrize("table", TABLES)
def test_migrate_is_skipped_when_target_has_any_history(hdb, table):
    _seed(hdb, OLD, n=2)
    _only(hdb, table, NEW)
    assert hdb.migrate_host_ip(OLD, NEW) is None
    for t in TABLES:
        assert _count(hdb, t, OLD) == 2                # nothing moved
        assert _count(hdb, t, NEW) == (1 if t == table else 0)


def test_migrate_flushes_buffered_pings_first(hdb):
    hdb.record_ping(OLD, True, 1.5)                    # buffered, not flushed
    res = hdb.migrate_host_ip(OLD, NEW)
    assert res["pings"] == 1
    hdb.flush_pings()
    assert _count(hdb, "pings", NEW) == 1 and _count(hdb, "pings", OLD) == 0


def test_migrate_force_bypasses_the_target_check(hdb):
    _seed(hdb, OLD)
    _only(hdb, "incidents", NEW)
    res = hdb.migrate_host_ip(OLD, NEW, force=True)
    assert res["incidents"] == 1 and _count(hdb, "incidents", NEW) == 2


def test_migrate_rolls_back_everything_on_failure(hdb):
    _seed(hdb, OLD)
    # A conflicting ping_daily row for the same day makes the 2nd UPDATE fail after pings moved.
    hdb.conn.execute("INSERT INTO ping_daily(day, host_ip, total, up) VALUES ('2026-01-01',?,1,1)", (NEW,))
    with pytest.raises(Exception):
        hdb.migrate_host_ip(OLD, NEW, force=True)
    for t in TABLES:
        assert _count(hdb, t, OLD) == 1                # old IP intact, incl. the already-updated pings
    assert _count(hdb, "pings", NEW) == 0
    assert hdb.conn.in_transaction is False


# ── change_host_ip ──────────────────────────────────────────────────────────

def _write_hosts(tmp_path):
    p = tmp_path / "hosts.yaml"
    cfg = {"settings": {"default_interval": 30},
           "hosts": [
               {"name": "pi", "ip": "10.0.0.2", "group": "Core", "interval": 15},
               {"name": "vf2", "ip": OLD, "group": "Lab", "always_on": False, "notes": "hi",
                "specs": {"mac": MAC}, "services": [{"name": "ssh", "port": 22}]},
               {"name": "nas", "ip": "10.0.0.9"},
           ]}
    p.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return str(p), cfg


def test_change_host_ip_changes_exactly_one_entry_and_keeps_the_rest(tmp_path):
    path, cfg = _write_hosts(tmp_path)
    ok, err, hosts = change_host_ip(path, OLD, NEW)
    assert (ok, err) == (True, None)
    on_disk = yaml.safe_load(open(path))
    expected = json.loads(json.dumps(cfg))
    expected["hosts"][1]["ip"] = NEW
    assert on_disk == expected                          # settings, order, every other field
    assert [h["name"] for h in on_disk["hosts"]] == ["pi", "vf2", "nas"]
    assert hosts == on_disk["hosts"]


def test_change_host_ip_creates_a_backup_and_keeps_0600(tmp_path):
    path, _ = _write_hosts(tmp_path)
    change_host_ip(path, OLD, NEW)
    backups = os.listdir(tmp_path / "backups")
    assert len(backups) == 1 and backups[0].startswith("hosts-")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_change_host_ip_host_not_found(tmp_path):
    path, _ = _write_hosts(tmp_path)
    before = open(path).read()
    assert change_host_ip(path, "1.2.3.4", NEW) == (False, "host_not_found", None)
    assert open(path).read() == before


def test_change_host_ip_ip_in_use(tmp_path):
    path, _ = _write_hosts(tmp_path)
    before = open(path).read()
    assert change_host_ip(path, OLD, "10.0.0.9") == (False, "ip_in_use", None)
    assert open(path).read() == before


def test_change_host_ip_invalid_result_returns_the_validation_error_and_leaves_file(tmp_path):
    path, _ = _write_hosts(tmp_path)
    before = open(path).read()
    ok, err, hosts = change_host_ip(path, OLD, "not an ip!")
    assert ok is False and hosts is None and "not a valid IP" in err
    assert open(path).read() == before
    assert not os.path.exists(tmp_path / "backups")


# ── handler ─────────────────────────────────────────────────────────────────

class _HM:
    def __init__(self, boom=False):
        self.calls, self.boom = [], boom

    def reload_from_config(self, hosts, interval):
        self.calls.append((hosts, interval))
        if self.boom:
            raise RuntimeError("reload")


class _Drift:
    def __init__(self, entries=None, boom=False):
        self.entries = entries if entries is not None else [
            {"mac": MAC, "name": "vf2", "monitored_ip": OLD, "seen_ip": NEW}]
        self.refreshed, self.boom = 0, boom

    def get(self):
        return list(self.entries)

    def refresh(self):
        self.refreshed += 1
        if self.boom:
            raise RuntimeError("neighbors")
        return list(self.entries)


@pytest.fixture
def env(tmp_path, hdb):
    path, cfg = _write_hosts(tmp_path)
    inv = InventoryDB(hdb)
    rec_id, err = inv.create({"system": "vf2", "mac": MAC, "ip": OLD, "device_type": "host"})
    assert err is None
    _seed(hdb, OLD, n=2)
    return type("E", (), dict(path=path, hdb=hdb, inv=inv, hm=_HM(), drift=_Drift(),
                              settings={"default_interval": 45}))


def _body(**kw):
    d = {"mac": MAC, "from_ip": OLD, "to_ip": NEW}
    d.update(kw)
    return d


def _call(env, data=None, **over):
    args = dict(config_path=env.path, host_manager=env.hm, history_db=env.hdb, inventory_db=env.inv,
                drift_monitor=env.drift, settings=env.settings)
    args.update(over)
    return H._h_post_attention_apply_ip(_body() if data is None else data, args["config_path"],
                                        args["host_manager"], args["history_db"], args["inventory_db"],
                                        args["drift_monitor"], args["settings"])


def _inv_ip(env):
    return env.inv.find_by_mac(MAC)["ip"]


def test_handler_happy_path_end_to_end(env):
    status, body = _call(env)
    assert status == 200
    assert body == {"ok": True, "from": OLD, "to": NEW,
                    "history": {"pings": 2, "ping_daily": 2, "incidents": 2, "maintenance_windows": 2},
                    "inventory_updated": True}
    for t in TABLES:
        assert _count(env.hdb, t, OLD) == 0 and _count(env.hdb, t, NEW) == 2
    on_disk = yaml.safe_load(open(env.path))
    assert [h["ip"] for h in on_disk["hosts"]] == ["10.0.0.2", NEW, "10.0.0.9"]
    assert len(env.hm.calls) == 1
    assert env.hm.calls[0] == (on_disk["hosts"], 45)
    assert _inv_ip(env) == NEW
    assert env.drift.refreshed == 2                        # before the match, and after applying


@pytest.mark.parametrize("data", [
    {}, {"mac": MAC, "from_ip": OLD}, {"mac": MAC, "to_ip": NEW}, {"from_ip": OLD, "to_ip": NEW},
    _body(mac=5), _body(from_ip=None), _body(to_ip=["x"]), None, "str",
])
def test_handler_400_on_missing_or_non_string_fields(env, data):
    args = (data, env.path, env.hm, env.hdb, env.inv, env.drift, env.settings)
    assert H._h_post_attention_apply_ip(*args) == (400, {"error": "mac, from_ip and to_ip are required"})
    assert env.hm.calls == [] and _count(env.hdb, "pings", OLD) == 2


@pytest.mark.parametrize("over", [
    {"from_ip": "nope"}, {"to_ip": "nope"}, {"to_ip": "999.1.1.1"}, {"from_ip": "10.0.0.1/24"},
    {"to_ip": OLD}, {"from_ip": "host.local"},
])
def test_handler_400_invalid_ip(env, over):
    assert _call(env, _body(**over)) == (400, {"error": "invalid ip"})
    assert _count(env.hdb, "pings", OLD) == 2


def test_handler_503_without_drift_monitor(env):
    assert _call(env, drift_monitor=None) == (503, {"error": "drift monitor not available"})
    assert _count(env.hdb, "pings", OLD) == 2


@pytest.mark.parametrize("over", [
    {"mac": "aa:bb:cc:dd:ee:99"}, {"from_ip": "192.168.5.161"}, {"to_ip": "192.168.4.45"},
])
def test_handler_409_drift_changed_when_request_does_not_match_a_current_drift(env, over):
    before = open(env.path).read()
    assert _call(env, _body(**over)) == (409, {"error": "drift_changed"})
    assert open(env.path).read() == before
    assert _count(env.hdb, "pings", OLD) == 2 and env.hm.calls == [] and env.drift.refreshed == 1


def test_handler_normalizes_the_request_mac(env):
    status, _ = _call(env, _body(mac="AA-BB-CC-DD-EE-01"))
    assert status == 200


def test_handler_404_host_not_found(env):
    env.drift.entries[0]["monitored_ip"] = "10.5.5.5"
    status, body = _call(env, _body(from_ip="10.5.5.5"))
    assert (status, body) == (404, {"error": "host_not_found"})


def test_handler_409_ip_in_use_and_history_stays_put(env):
    env.drift.entries[0]["seen_ip"] = "10.0.0.9"
    assert _call(env, _body(to_ip="10.0.0.9")) == (409, {"error": "ip_in_use"})
    assert _count(env.hdb, "pings", OLD) == 2
    assert yaml.safe_load(open(env.path))["hosts"][1]["ip"] == OLD
    assert env.hm.calls == []


def test_handler_other_change_error_maps_to_400(env, monkeypatch):
    monkeypatch.setattr(H, "change_host_ip", lambda *a: (False, "Duplicate IP: x", None))
    assert _call(env) == (400, {"error": "Duplicate IP: x"})
    assert _count(env.hdb, "pings", OLD) == 2 and _count(env.hdb, "pings", NEW) == 0


def test_handler_rolls_back_history_when_change_host_ip_fails_after_migration(env, monkeypatch):
    seen = {}

    def failing(path, old, new):
        seen["moved"] = _count(env.hdb, "pings", NEW)      # history already moved (step a before b)
        return False, "host_not_found", None
    monkeypatch.setattr(H, "change_host_ip", failing)
    assert _call(env) == (404, {"error": "host_not_found"})
    assert seen["moved"] == 2
    for t in TABLES:
        assert _count(env.hdb, t, OLD) == 2 and _count(env.hdb, t, NEW) == 0
    assert env.hm.calls == [] and env.drift.refreshed == 1


def test_handler_rolls_back_history_and_yaml_when_reload_raises(env):
    env.hm.boom = True
    before = yaml.safe_load(open(env.path))
    assert _call(env) == (500, {"error": "reload failed"})
    assert yaml.safe_load(open(env.path)) == before        # yaml restored
    for t in TABLES:
        assert _count(env.hdb, t, OLD) == 2 and _count(env.hdb, t, NEW) == 0
    assert _inv_ip(env) == OLD                              # inventory not touched
    assert env.drift.refreshed == 1


def test_handler_history_skipped_when_target_has_history_still_succeeds(env):
    _only(env.hdb, "incidents", NEW)
    status, body = _call(env)
    assert status == 200 and body["history"] is None
    assert _count(env.hdb, "pings", OLD) == 2               # not mixed
    assert yaml.safe_load(open(env.path))["hosts"][1]["ip"] == NEW


def test_handler_does_not_touch_an_inventory_record_with_a_different_ip(env):
    rec = env.inv.find_by_mac(MAC)
    env.inv.update(rec["id"], {"ip": "10.7.7.7"})
    status, body = _call(env)
    assert status == 200 and body["inventory_updated"] is False
    assert _inv_ip(env) == "10.7.7.7"


def test_handler_fills_an_empty_inventory_ip(env):
    rec = env.inv.find_by_mac(MAC)
    env.inv.update(rec["id"], {"ip": ""})
    assert _call(env)[1]["inventory_updated"] is True
    assert _inv_ip(env) == NEW


def test_handler_inventory_failure_does_not_fail_the_request(env):
    class Boom:
        def find_by_mac(self, mac):
            raise RuntimeError("db")
    status, body = _call(env, inventory_db=Boom())
    assert status == 200 and body["inventory_updated"] is False


def test_handler_inventory_update_not_ok_reports_false(env):
    class Inv:
        def find_by_mac(self, mac):
            return {"id": 1, "ip": OLD}

        def update(self, i, d):
            return False, "nope"
    assert _call(env, inventory_db=Inv())[1]["inventory_updated"] is False


def test_handler_drift_refresh_failure_does_not_fail_the_request(env):
    env.drift.boom = True
    assert _call(env)[0] == 200


def test_handler_works_without_history_host_manager_or_inventory(env):
    status, body = _call(env, history_db=None, host_manager=None, inventory_db=None)
    assert status == 200
    assert body["history"] is None and body["inventory_updated"] is False
    assert yaml.safe_load(open(env.path))["hosts"][1]["ip"] == NEW


def test_handler_unexpected_exception_is_500_apply_failed(env):
    class Boom:
        def migrate_host_ip(self, *a, **k):
            raise RuntimeError("disk")
    assert _call(env, history_db=Boom()) == (500, {"error": "apply failed"})


def test_handler_refreshes_the_drift_before_matching_so_a_returned_device_is_refused(env):
    class Returned(_Drift):                    # the device came back: refresh() finds no drift,
        def refresh(self):                     # but the cached get() is still the stale entry
            self.refreshed += 1
            return []
    env.drift = Returned()
    before = open(env.path).read()
    assert _call(env) == (409, {"error": "drift_changed"})
    assert env.drift.refreshed == 1
    assert open(env.path).read() == before and _count(env.hdb, "pings", OLD) == 2 and env.hm.calls == []


def test_handler_falls_back_to_the_cached_drift_when_the_pre_match_refresh_fails(env):
    env.drift.boom = True
    assert _call(env)[0] == 200


def test_handler_warns_when_the_yaml_restore_fails_after_a_reload_failure(env, monkeypatch, caplog):
    real = H.change_host_ip
    calls = []

    def flaky(path, old, new):
        calls.append((old, new))
        return real(path, old, new) if len(calls) == 1 else (False, "disk full", None)
    monkeypatch.setattr(H, "change_host_ip", flaky)
    env.hm.boom = True
    with caplog.at_level("WARNING"):
        assert _call(env) == (500, {"error": "reload failed"})
    assert calls == [(OLD, NEW), (NEW, OLD)]
    assert any("disk full" in r.getMessage() for r in caplog.records)


class _YamlDrift:
    """Drift derived from hosts.yaml like the real monitor: present while a host still has OLD."""

    def __init__(self, path):
        self.path = path

    def _cur(self):
        hosts = yaml.safe_load(open(self.path))["hosts"]
        return ([{"mac": MAC, "name": "vf2", "monitored_ip": OLD, "seen_ip": NEW}]
                if any(h["ip"] == OLD for h in hosts) else [])

    def get(self):
        return self._cur()

    def refresh(self):
        return self._cur()


def test_concurrent_applies_for_the_same_drift_are_serialised(env, monkeypatch):
    """Forced interleaving A.migrate, B.migrate, B.change, A.change. Unserialised, B's migrate is
    skipped (target already has rows), B changes the yaml, A's change fails host_not_found and
    A's rollback moves the history back to OLD while hosts.yaml points at NEW."""
    a_migrated, b_changed = threading.Event(), threading.Event()
    real_change = H.change_host_ip
    real_mig = env.hdb.migrate_host_ip

    def change(path, o, n):
        if threading.current_thread().name == "A":
            b_changed.wait(1.0)
        r = real_change(path, o, n)
        if threading.current_thread().name == "B":
            b_changed.set()
        return r

    def mig(o, n, force=False):
        if threading.current_thread().name == "B" and not force:
            a_migrated.wait(1.0)
        r = real_mig(o, n, force=force)
        if threading.current_thread().name == "A" and not force:
            a_migrated.set()
        return r
    monkeypatch.setattr(H, "change_host_ip", change)
    monkeypatch.setattr(env.hdb, "migrate_host_ip", mig)
    drift = _YamlDrift(env.path)
    res = {}

    def run(name):
        res[name] = _call(env, drift_monitor=drift)
    threads = [threading.Thread(target=run, args=(n,), name=n) for n in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    statuses = sorted(r[0] for r in res.values())
    assert statuses == [200, 409], res
    loser = next(r for r in res.values() if r[0] != 200)
    assert loser == (409, {"error": "drift_changed"})
    assert yaml.safe_load(open(env.path))["hosts"][1]["ip"] == NEW
    for t in TABLES:
        assert _count(env.hdb, t, NEW) == 2 and _count(env.hdb, t, OLD) == 0, t


# ── item data ───────────────────────────────────────────────────────────────

def test_drift_item_carries_data_and_other_kinds_do_not():
    p = build_attention([], [], {}, [{"condition_id": "c", "source": "nas", "severity": "warning",
                                       "title": "t", "detail": "d", "since": 1}], 2,
                        [{"mac": MAC, "name": "vf2", "monitored_ip": OLD, "seen_ip": NEW}], now=1000.0)
    by_kind = {i["kind"]: i for i in p["items"]}
    assert by_kind["ip_drift"]["data"] == {"mac": MAC, "from_ip": OLD, "to_ip": NEW}
    assert "data" not in by_kind["poller_condition"] and "data" not in by_kind["connection_suggestions"]


# ── HTTP route ──────────────────────────────────────────────────────────────

def _server(auth, env):
    handler = make_handler(env.hm, env.settings, env.path, auth_manager=auth, inventory_db=env.inv,
                           history_db=env.hdb, drift_monitor=env.drift)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=server.handle_request)
    t.start()
    return server, server.server_address[1], t


def _post(port, cookie=None, token=None, body=None):
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = f"nw_session={cookie}"
    if token:
        headers["X-CSRF-Token"] = token
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/attention/apply-ip",
                                 data=json.dumps(body or _body()).encode(), method="POST", headers=headers)
    return urllib.request.urlopen(req)


def _run(tmp_path, env, user, csrf=True, session=True):
    auth = AuthManager(str(tmp_path / "auth.json"))
    auth.create_user("root", "password123", admin=True)
    auth.create_user("bob", "password123")
    cookie = auth.make_session_cookie(user) if session else None
    server, port, t = _server(auth, env)
    try:
        return _post(port, cookie, auth.csrf_token_for_cookie(cookie) if (csrf and cookie) else None)
    finally:
        server.server_close()
        t.join()


def test_route_401_without_a_session(tmp_path, env):
    with pytest.raises(urllib.error.HTTPError) as e:
        _run(tmp_path, env, "root", session=False)
    assert e.value.code == 401
    assert env.hm.calls == []


def test_route_403_for_a_non_admin(tmp_path, env):
    with pytest.raises(urllib.error.HTTPError) as e:
        _run(tmp_path, env, "bob")
    assert e.value.code == 403 and json.loads(e.value.read()) == {"error": "admin_required"}
    assert env.hm.calls == []


def test_route_403_for_an_admin_without_csrf(tmp_path, env):
    with pytest.raises(urllib.error.HTTPError) as e:
        _run(tmp_path, env, "root", csrf=False)
    assert e.value.code == 403 and json.loads(e.value.read()) == {"error": "csrf_required"}
    assert env.hm.calls == []
    assert _count(env.hdb, "pings", OLD) == 2


def test_route_200_for_an_admin_with_csrf(tmp_path, env):
    with _run(tmp_path, env, "root") as r:
        assert r.status == 200
        body = json.loads(r.read())
    assert body["ok"] is True and body["to"] == NEW and body["inventory_updated"] is True
    assert len(env.hm.calls) == 1 and env.drift.refreshed == 2
