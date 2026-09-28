import pytest

from netwatch import pollers as P
from netwatch.attention import AlertLedger
from netwatch.storage import HistoryDB


@pytest.fixture
def hdb(tmp_path):
    db = HistoryDB(str(tmp_path / "t.db"))
    yield db
    db.close()


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(P, "_send_alert_async", lambda settings, title, message, **kw: calls.append((title, message, kw)))
    return calls


POOL_BAD = [{"name": "tank", "status": "DEGRADED", "last_scrub": None}]
POOL_OK = [{"name": "tank", "status": "ONLINE", "last_scrub": None}]


def _nas(ledger):
    return P.NASPoller(None, alert_settings={}, ledger=ledger)


def test_nas_alert_does_not_renotify_after_a_restart(hdb, sent):
    led = AlertLedger(hdb)
    _nas(led)._check_alerts(POOL_BAD, [], [])
    assert len(sent) == 1
    _nas(led)._check_alerts(POOL_BAD, [], [])          # "restarted" poller, same ledger
    assert len(sent) == 1
    assert [r["title"] for r in led.active()] == ['Pool "tank" is DEGRADED']
    assert led.active()[0]["severity"] == "critical" and led.active()[0]["detail"] == "TrueNAS"


def test_nas_condition_that_resolved_while_down_is_cleared_on_first_pass(hdb, sent):
    led = AlertLedger(hdb)
    _nas(led)._check_alerts(POOL_BAD, [], [{"id": "a1", "message": "disk failing"}])
    assert led.active_ids("nas") == {"pool_health_tank", "truenas_alert_a1"}
    _nas(led)._check_alerts(POOL_OK, [], [])            # healed during downtime
    assert led.active_ids("nas") == set()


def test_nas_pass_clears_a_removed_pool_via_reconciliation(hdb, sent):
    led = AlertLedger(hdb)
    _nas(led)._check_alerts(POOL_BAD, [], [])
    _nas(led)._check_alerts([], [], [])                 # pool no longer reported at all
    assert led.active_ids("nas") == set()


def test_nas_without_a_ledger_behaves_as_before(sent):
    p = P.NASPoller(None, alert_settings={})
    p._check_alerts(POOL_BAD, [], [])
    p._check_alerts(POOL_BAD, [], [])
    assert len(sent) == 1 and p._alert_state["pool_health_tank"] is True
    p._check_alerts(POOL_OK, [], [])
    assert p._alert_state["pool_health_tank"] is False


def test_ups_alert_does_not_renotify_after_a_restart_and_maps_severity(hdb, sent):
    led = AlertLedger(hdb)
    P.UPSPoller(None, alert_settings={}, ledger=led)._check_alerts("OB LB RB")
    assert len(sent) == 3
    P.UPSPoller(None, alert_settings={}, ledger=led)._check_alerts("OB LB RB")
    assert len(sent) == 3
    sev = {r["condition_id"]: r["severity"] for r in led.active("ups")}
    assert sev == {"ups-on-battery": "warning", "ups-low-battery": "critical",
                   "ups-replace-battery": "info"}
    P.UPSPoller(None, alert_settings={}, ledger=led)._check_alerts("OL")
    assert led.active("ups") == []


def test_pbs_alert_does_not_renotify_after_a_restart(hdb, sent):
    led = AlertLedger(hdb)
    bad = [{"type": "ct", "vmid": 120, "status": "failed", "last_backup_time": None}]
    mk = lambda: P.PBSPoller(None, alert_settings={}, ledger=led)
    mk()._check_alerts(bad)
    mk()._check_alerts(bad)
    assert len(sent) == 1
    mk()._check_alerts([])                              # backup group gone -> reconciled
    assert led.active("pbs") == []


def _node(guests, status="online"):
    return {"name": "pve", "status": status, "guests": guests}


def test_proxmox_stopped_guest_alert_persists_and_is_not_reconciled_away(hdb, sent):
    led = AlertLedger(hdb)
    mk = lambda: P.ProxmoxPoller(None, alert_settings={}, ledger=led)
    running = [_node([{"vmid": 100, "name": "web", "status": "running"}])]
    stopped = [_node([{"vmid": 100, "name": "web", "status": "stopped"}])]
    mk()._check_alerts(stopped, running)                # running -> stopped: fires
    assert len(sent) == 1 and led.active_ids("proxmox") == {"stop:100"}
    # restart; the guest simply stays stopped (no edge): the alert must stay active
    mk()._check_alerts(stopped, stopped)
    mk()._check_alerts(stopped, stopped)
    assert len(sent) == 1 and led.active_ids("proxmox") == {"stop:100"}
    # guest deleted: the stop alert goes away
    mk()._check_alerts([_node([])], stopped)
    assert led.active_ids("proxmox") == set()


def test_proxmox_node_and_pause_conditions_are_level_triggered(hdb, sent):
    led = AlertLedger(hdb)
    mk = lambda: P.ProxmoxPoller(None, alert_settings={}, ledger=led)
    mk()._check_alerts([_node([{"vmid": 7, "name": "x", "status": "paused"}], status="offline")], [])
    assert led.active_ids("proxmox") == {"node:pve", "pause:7"}
    assert {r["condition_id"]: r["severity"] for r in led.active("proxmox")}["node:pve"] == "critical"
    mk()._check_alerts([_node([{"vmid": 7, "name": "x", "status": "running"}])], [])
    assert led.active_ids("proxmox") == set()


# ── Proxmox: partial reads must not reconcile away edge-triggered / level rows ──

def _seed_stop_and_pause(led):
    led.fire("stop:100", "proxmox", "warning", "t", "Proxmox", now=1)
    led.fire("pause:7", "proxmox", "warning", "t", "Proxmox", now=1)


def test_proxmox_unread_guest_list_keeps_stop_and_pause_rows(hdb, sent):
    led = AlertLedger(hdb)
    _seed_stop_and_pause(led)
    node = {"name": "pve", "status": "online", "guests": [], "guests_ok": False}
    P.ProxmoxPoller(None, alert_settings={}, ledger=led)._check_alerts([node], [])
    assert led.active_ids("proxmox") == {"stop:100", "pause:7"}


def test_proxmox_offline_node_keeps_stop_and_pause_rows(hdb, sent):
    led = AlertLedger(hdb)
    _seed_stop_and_pause(led)
    node = {"name": "pve", "status": "offline", "guests": []}
    P.ProxmoxPoller(None, alert_settings={}, ledger=led)._check_alerts([node], [])
    assert led.active_ids("proxmox") == {"stop:100", "pause:7", "node:pve"}


def test_proxmox_fully_read_pass_clears_genuinely_absent_guests(hdb, sent):
    led = AlertLedger(hdb)
    _seed_stop_and_pause(led)
    mk = lambda: P.ProxmoxPoller(None, alert_settings={}, ledger=led)
    mk()._check_alerts([{"name": "pve", "status": "online", "guests": [], "guests_ok": False}], [])
    assert led.active_ids("proxmox") == {"stop:100", "pause:7"}
    mk()._check_alerts([{"name": "pve", "status": "online", "guests": [], "guests_ok": True}], [])
    assert led.active_ids("proxmox") == set()


def test_proxmox_empty_node_list_keeps_stop_and_pause_rows(hdb, sent):
    led = AlertLedger(hdb)
    _seed_stop_and_pause(led)
    P.ProxmoxPoller(None, alert_settings={}, ledger=led)._check_alerts([], [])
    assert led.active_ids("proxmox") == {"stop:100", "pause:7"}


# ── failure / plumbing coverage ──────────────────────────────────────────────

def test_a_pass_that_raises_midway_leaves_untouched_ledger_rows_intact(hdb, sent, monkeypatch):
    led = AlertLedger(hdb)
    led.fire("truenas_alert_old", "nas", "warning", "t", "TrueNAS", now=1)
    p = _nas(led)
    real = p._fire_alert

    def boom(cid, *a, **kw):
        if cid.startswith("replication_"):
            raise RuntimeError("boom")
        return real(cid, *a, **kw)

    monkeypatch.setattr(p, "_fire_alert", boom)
    tasks = [{"id": 1, "name": "r", "enabled": True, "last_state": "ERROR", "last_run": None}]
    with pytest.raises(RuntimeError, match="boom"):
        p._check_alerts(POOL_BAD, tasks, [])
    # end_pass never ran, so the untouched row is still active (and the earlier fire persisted)
    assert led.active_ids("nas") == {"truenas_alert_old", "pool_health_tank"}


def test_mark_notified_is_set_after_a_successful_send(hdb, monkeypatch):
    def send(settings, title, message, **kw):
        kw["on_success"]()
    monkeypatch.setattr(P, "_send_alert_async", send)
    led = AlertLedger(hdb)
    _nas(led)._check_alerts(POOL_BAD, [], [])
    assert led.active()[0]["notified_at"] is not None


def test_failed_send_leaves_notified_at_unset(hdb, sent):
    led = AlertLedger(hdb)
    _nas(led)._check_alerts(POOL_BAD, [], [])           # `sent` never calls on_success
    assert led.active()[0]["notified_at"] is None


class _RaisingLedger:
    def fire(self, *a, **kw):
        raise RuntimeError("db locked")

    clear = mark_notified = active_ids = fire


def test_poller_still_sends_first_time_ntfy_when_the_ledger_raises(sent):
    p = _nas(_RaisingLedger())
    p._check_alerts(POOL_BAD, [], [])
    assert len(sent) == 1
    p._check_alerts(POOL_BAD, [], [])                   # in-memory dedupe still holds
    assert len(sent) == 1


def test_prune_loop_prunes_the_alert_ledger(monkeypatch):
    import threading
    from unittest.mock import MagicMock
    from netwatch import storage

    stop = threading.Event()
    sleeps = {"n": 0}

    def fake_sleep(s):                                           # skip the ~60s boot delay
        sleeps["n"] += 1
        if sleeps["n"] > 30:                                     # a regression fails fast, not hangs
            stop.set()
    monkeypatch.setattr(storage.time, "sleep", fake_sleep)
    ledger = MagicMock()
    ledger.prune.side_effect = lambda: stop.set() or 0           # stop right after the first prune
    storage._prune_loop(MagicMock(), stop, None, ledger)
    ledger.prune.assert_called_once_with()
