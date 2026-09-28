import pytest

from netwatch import http_handlers as H
from netwatch.storage import HistoryDB

NOW = 1_000_000


@pytest.fixture
def hdb(tmp_path):
    db = HistoryDB(str(tmp_path / "t.db"))
    yield db
    db.close()


@pytest.fixture(autouse=True)
def _clear_heartbeat_cache():
    H._HEARTBEAT_CACHE.clear()
    yield
    H._HEARTBEAT_CACHE.clear()


class _HM:
    def __init__(self, ips):
        self._ips = ips

    def list_hosts(self):
        return [type("H", (), {"ip": ip})() for ip in self._ips]


def _ping(db, ip, ts, up):
    db.conn.execute("INSERT INTO pings(host_ip, timestamp, is_up, latency_ms) VALUES (?,?,?,?)",
                    (ip, ts, 1 if up else 0, 1.0 if up else None))


def test_heartbeat_query_buckets_all_four_states(hdb):
    # hours=1, buckets=4 -> 900s buckets; at NOW the window is [997200, 1000800)
    for ts in (997300, 997400):
        _ping(hdb, "10.0.0.1", ts, True)          # bucket 0: all up
    for ts in (998200, 998300):
        _ping(hdb, "10.0.0.1", ts, False)         # bucket 1: all down
    _ping(hdb, "10.0.0.1", 999100, True)          # bucket 2: mixed
    _ping(hdb, "10.0.0.1", 999200, False)
    #                                               bucket 3: no data
    _ping(hdb, "10.0.0.1", 900000, False)         # outside the window: ignored
    assert hdb.heartbeat(997200, 900, 4) == {"10.0.0.1": [1, 0, 2, None]}


def test_heartbeat_handler_shape_defaults_and_unmonitored_hosts_omitted(hdb):
    _ping(hdb, "10.0.0.1", NOW - 10, True)
    _ping(hdb, "10.9.9.9", NOW - 10, True)         # not monitored -> omitted
    status, p = H._h_get_heartbeat(hdb, _HM(["10.0.0.1", "10.0.0.2"]), "", now=NOW)
    assert status == 200
    assert p["bucket_seconds"] == 1800 and len(p["hosts"]["10.0.0.1"]) == 48
    assert set(p["hosts"]) == {"10.0.0.1", "10.0.0.2"}
    assert p["hosts"]["10.0.0.1"][-1] == 1         # the current (partial) bucket holds the ping
    assert p["hosts"]["10.0.0.2"] == [None] * 48   # monitored but silent
    assert p["start"] + 1800 * 48 > NOW >= p["start"]


def test_heartbeat_handler_clamps_and_survives_garbage_params(hdb):
    _, p = H._h_get_heartbeat(hdb, _HM(["10.0.0.1"]), "hours=9999&buckets=99999", now=NOW)
    assert len(p["hosts"]["10.0.0.1"]) == 96 and p["bucket_seconds"] == 72 * 3600 // 96
    _, p = H._h_get_heartbeat(hdb, _HM(["10.0.0.1"]), "hours=-3&buckets=0", now=NOW)
    assert len(p["hosts"]["10.0.0.1"]) == 1
    _, p = H._h_get_heartbeat(hdb, _HM(["10.0.0.1"]), "hours=abc&buckets=%00", now=NOW)
    assert len(p["hosts"]["10.0.0.1"]) == 48


def test_heartbeat_handler_caches_for_sixty_seconds(hdb):
    hm = _HM(["10.0.0.1"])
    _, first = H._h_get_heartbeat(hdb, hm, "", now=NOW)
    _ping(hdb, "10.0.0.1", NOW - 5, True)
    _, again = H._h_get_heartbeat(hdb, hm, "", now=NOW + 30)
    assert again is first                          # served from cache
    _, later = H._h_get_heartbeat(hdb, hm, "", now=NOW + 61)
    assert later is not first and later["hosts"]["10.0.0.1"][-1] == 1


def test_heartbeat_handler_without_db_or_hosts_never_errors():
    assert H._h_get_heartbeat(None, None, "", now=NOW)[1]["hosts"] == {}
    _, p = H._h_get_heartbeat(None, _HM(["10.0.0.1"]), "", now=NOW)
    assert p["hosts"]["10.0.0.1"] == [None] * 48


def test_heartbeat_cache_is_bounded(hdb):
    hm = _HM(["10.0.0.1"])
    for buckets in range(1, 60):
        H._h_get_heartbeat(hdb, hm, f"buckets={buckets}", now=NOW)
    assert len(H._HEARTBEAT_CACHE) <= 32


import json
import threading
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer

from netwatch.attention import AlertLedger, IPDriftMonitor
from netwatch.auth import AuthManager
from netwatch.hosts import HostState
from netwatch.server import make_handler


class _Inv:
    def __init__(self, records=(), conns=(), pending=0):
        self._r, self._c = list(records), list(conns)
        self.suggestions = type("S", (), {"count_pending": staticmethod(lambda: pending)})()

    def list_all(self):
        return self._r

    def list_all_connections(self):
        return self._c


def _host(name, ip, up, mac=""):
    h = HostState(name=name, ip=ip, group="g", interval=30, specs={"mac": mac} if mac else {})
    h.last_checked = datetime.now()
    h.history.append(up)
    return h


class _HMgr:
    def __init__(self, hosts):
        self._h = hosts

    def list_hosts(self):
        return self._h


def test_attention_handler_groups_a_topology_outage_end_to_end():
    hm = _HMgr([_host("sw", "10.0.0.2", False, "aa:aa:aa:aa:aa:01"),
                _host("ap", "10.0.0.3", False, "aa:aa:aa:aa:aa:02"),
                _host("pi", "10.0.0.4", True)])
    inv = _Inv(records=[{"id": 1, "mac": "aa:aa:aa:aa:aa:01", "ip": "", "system": "sw"},
                        {"id": 2, "mac": "aa:aa:aa:aa:aa:02", "ip": "", "system": "ap"}],
               conns=[{"id": 1, "from_device_id": 2, "to_device_id": 1, "connection_type": "ethernet"}],
               pending=2)
    status, p = H._h_get_attention(hm, inv, None, None, now=1_000_000)
    assert status == 200
    kinds = [i["kind"] for i in p["items"]]
    assert kinds == ["host_down", "connection_suggestions"]
    assert p["items"][0]["affected"] == ["10.0.0.3"]
    assert p["verdict"]["headline"] == "sw is down. 1 host unreachable."


def test_attention_handler_includes_ledger_and_drift(hdb):
    led = AlertLedger(hdb)
    led.fire("pool_health_tank", "nas", "critical", 'Pool "tank" is DEGRADED', "TrueNAS", now=999_000)
    mon = IPDriftMonitor(_HMgr([_host("vf2", "10.0.0.7", False, "aa:bb:cc:dd:ee:01")]), _Inv(),
                         lambda: {"aa:bb:cc:dd:ee:01": {"10.0.0.8"}})
    mon.refresh()
    _, p = H._h_get_attention(_HMgr([_host("vf2", "10.0.0.7", True, "aa:bb:cc:dd:ee:01")]),
                              _Inv(), led, mon, now=1_000_000)
    assert {i["kind"] for i in p["items"]} == {"poller_condition", "ip_drift"}


def test_attention_handler_never_errors_on_missing_pieces():
    status, p = H._h_get_attention(None, None, None, None)
    assert status == 200 and p["items"] == []
    assert p["verdict"]["headline"] == "No hosts are being monitored yet."


def test_attention_handler_survives_inventory_and_ledger_failures():
    class BoomInv:
        suggestions = None

        def list_all(self):
            raise RuntimeError("db")

        def list_all_connections(self):
            raise RuntimeError("db")

    class BoomLedger:
        def active(self):
            raise RuntimeError("db")

    status, p = H._h_get_attention(_HMgr([_host("a", "10.0.0.1", False)]), BoomInv(), BoomLedger(), None)
    assert status == 200 and [i["kind"] for i in p["items"]] == ["host_down"]


def test_attention_handler_never_500s_when_the_host_manager_raises():
    class BoomHM:
        def list_hosts(self):
            raise RuntimeError("hosts")

    status, p = H._h_get_attention(BoomHM(), None, None, None)
    assert status == 200 and p["items"] == []


def test_heartbeat_error_is_not_cached(hdb):
    real = hdb.heartbeat
    calls = {"n": 0}

    def flaky(start, bucket_seconds, n):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return real(start, bucket_seconds, n)

    class Stub:
        heartbeat = staticmethod(flaky)

    hm = _HM(["10.0.0.1"])
    _ping(hdb, "10.0.0.1", NOW - 5, True)
    _, first = H._h_get_heartbeat(Stub(), hm, "", now=NOW)
    assert first["hosts"]["10.0.0.1"] == [None] * 48
    _, second = H._h_get_heartbeat(Stub(), hm, "", now=NOW + 5)
    assert second["hosts"]["10.0.0.1"][-1] == 1


# ── HTTP level: auth on the new routes ──────────────────────────────────────

def _server(auth, **kw):
    handler = make_handler(None, {}, "/dev/null", auth_manager=auth, **kw)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=server.handle_request)
    t.start()
    return server, server.server_address[1], t


def _auth(tmp_path):
    auth = AuthManager(str(tmp_path / "auth.json"))
    auth.create_user("bob", "password123", admin=False)
    return auth


@pytest.mark.parametrize("path", ["/api/attention", "/api/attention?_=123", "/api/heartbeat", "/api/heartbeat?hours=6"])
def test_new_get_routes_require_a_session(tmp_path, path):
    server, port, t = _server(_auth(tmp_path))
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"http://127.0.0.1:{port}{path}")
        assert e.value.code == 401
    finally:
        server.server_close()
        t.join()


def test_attention_route_serves_json_to_a_logged_in_user(tmp_path):
    auth = _auth(tmp_path)
    cookie = auth.make_session_cookie("bob")
    server, port, t = _server(auth)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/attention",
                                     headers={"Cookie": f"nw_session={cookie}"})
        with urllib.request.urlopen(req) as r:
            body = json.loads(r.read())
        assert r.status == 200 and body["verdict"]["level"] == "ok" and body["items"] == []
    finally:
        server.server_close()
        t.join()


def test_heartbeat_route_serves_json_and_passes_the_query(tmp_path, hdb):
    auth = _auth(tmp_path)
    cookie = auth.make_session_cookie("bob")
    server, port, t = _server(auth, history_db=hdb)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/heartbeat?hours=6&buckets=12",
                                     headers={"Cookie": f"nw_session={cookie}"})
        with urllib.request.urlopen(req) as r:
            body = json.loads(r.read())
        assert body["bucket_seconds"] == 1800 and body["hosts"] == {}
    finally:
        server.server_close()
        t.join()


from netwatch.attention import Explainer


class _Auth:
    def __init__(self, key):
        self.lock = threading.Lock()
        self.data = {"openrouter_api_key": key}


def _explain(hm, key="sk-x", settings=None, llm=None, now=1_000_000):
    llm = llm or (lambda k, m, msgs: "It is probably the switch.")
    return H._h_post_attention_explain(hm, _Inv(), None, None, _Auth(key), settings or {},
                                       Explainer(complete=llm), now=now)


def test_explain_handler_explains_current_problems_using_the_configured_model():
    seen = {}

    def llm(k, m, msgs):
        seen["m"], seen["k"] = m, k
        return "It is probably the switch."
    hm = _HMgr([_host("sw", "10.0.0.2", False)])
    s, p = _explain(hm, settings={"ai_model": "meta-llama/llama-3.3-70b-instruct:free"}, llm=llm)
    assert s == 200 and p["explanation"] == "It is probably the switch."
    assert seen == {"m": "meta-llama/llama-3.3-70b-instruct:free", "k": "sk-x"}


def test_explain_handler_falls_back_to_the_free_model_for_an_unlisted_model():
    seen = {}
    _explain(_HMgr([_host("sw", "10.0.0.2", False)]), settings={"ai_model": "evil/model"},
             llm=lambda k, m, msgs: seen.setdefault("m", m) or "x")
    assert seen["m"] == "openrouter/free"


def test_explain_handler_no_key_404_and_nothing_to_explain_200():
    down = _HMgr([_host("sw", "10.0.0.2", False)])
    assert _explain(down, key="")[0] == 404
    s, p = _explain(_HMgr([_host("a", "10.0.0.1", True)]))
    assert s == 200 and "Nothing needs attention" in p["explanation"]


def test_explain_handler_without_an_explainer_is_404():
    assert H._h_post_attention_explain(None, None, None, None, _Auth("k"), {}, None) == (
        404, {"error": "ai_not_configured"})


def test_explain_post_route_requires_a_session_and_csrf(tmp_path):
    auth = _auth(tmp_path)
    server, port, t = _server(auth, explainer=Explainer(complete=lambda k, m, msgs: "x"))
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/attention/explain", data=b"{}",
                                     method="POST")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code in (401, 403)
    finally:
        server.server_close()
        t.join()


def test_explain_post_route_works_for_a_logged_in_user_with_csrf(tmp_path):
    auth = _auth(tmp_path)
    cookie = auth.make_session_cookie("bob")
    token = auth.csrf_token_for_cookie(cookie)
    server, port, t = _server(auth, explainer=Explainer(complete=lambda k, m, msgs: "x"))
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/attention/explain", data=b"{}", method="POST",
            headers={"Cookie": f"nw_session={cookie}", "X-CSRF-Token": token,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            body = json.loads(r.read())
        assert r.status == 200 and "Nothing needs attention" in body["explanation"]   # no hosts -> fixed text
    finally:
        server.server_close()
        t.join()


class _IncLog:
    def __init__(self, events=None, boom=False):
        self._e, self._boom = events or [], boom

    def list_incidents(self):
        if self._boom:
            raise RuntimeError("db")
        return self._e


def test_attention_handler_seeds_down_since_from_the_ongoing_incident():
    hm = _HMgr([_host("jellyfin", "10.0.0.4", False)])          # first_down_at is 0 (never set)
    log = _IncLog([
        {"host_ip": "10.0.0.4", "host_name": "jellyfin", "ongoing": True, "started_ts": 1_000_000 - 3 * 86400},
        {"host_ip": "10.0.0.5", "host_name": "other", "ongoing": False, "started_ts": 5},
    ])
    _, p = H._h_get_attention(hm, None, None, None, now=1_000_000, incident_log=log)
    (it,) = p["items"]
    assert it["since"] == 1_000_000 - 3 * 86400 and it["detail"] == "Down 3 d"


def test_attention_handler_ignores_resolved_incidents_and_survives_a_failing_log():
    hm = _HMgr([_host("jellyfin", "10.0.0.4", False)])
    resolved = _IncLog([{"host_ip": "10.0.0.4", "ongoing": False, "started_ts": 10}])
    _, p = H._h_get_attention(hm, None, None, None, now=1_000_000, incident_log=resolved)
    assert p["items"][0]["since"] is None and p["items"][0]["detail"] == "Down"
    _, p = H._h_get_attention(hm, None, None, None, now=1_000_000, incident_log=_IncLog(boom=True))
    assert p["items"][0]["kind"] == "host_down"                  # degraded, not an error


class _RecordingExplainer:
    def __init__(self):
        self.items = None

    def explain(self, items, api_key, model, now=None):
        self.items = items
        return 200, {"explanation": "ok"}


def test_explain_handler_sees_the_same_seeded_down_since_as_get_attention():
    hm = _HMgr([_host("jellyfin", "10.0.0.4", False)])          # first_down_at is 0 (never set)
    started = 1_000_000 - 3 * 86400
    log = _IncLog([{"host_ip": "10.0.0.4", "host_name": "jellyfin", "ongoing": True,
                    "started_ts": started}])
    ex = _RecordingExplainer()
    s, _ = H._h_post_attention_explain(hm, _Inv(), None, None, _Auth("k"), {}, ex,
                                       now=1_000_000, incident_log=log)
    assert s == 200 and len(ex.items) == 1 and ex.items[0]["since"] == started
    # without incident_log the handler still works and simply has no seeded start
    ex2 = _RecordingExplainer()
    s, _ = H._h_post_attention_explain(hm, _Inv(), None, None, _Auth("k"), {}, ex2, now=1_000_000)
    assert s == 200 and ex2.items[0]["since"] is None
