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
