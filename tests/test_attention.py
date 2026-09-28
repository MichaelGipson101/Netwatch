import threading
from unittest.mock import MagicMock

import pytest

from netwatch.attention import AlertGate, AlertLedger
from netwatch.storage import HistoryDB


@pytest.fixture
def hdb(tmp_path):
    db = HistoryDB(str(tmp_path / "t.db"))
    yield db
    db.close()


def test_first_fire_notifies_and_repeat_does_not(hdb):
    led = AlertLedger(hdb, cooldown_seconds=300)
    assert led.fire("c1", "nas", "warning", "Pool tank is DEGRADED", "TrueNAS", now=1000) is True
    assert led.fire("c1", "nas", "warning", "Pool tank is DEGRADED", "TrueNAS", now=1010) is False
    (row,) = led.active()
    assert row["since"] == 1000 and row["title"] == "Pool tank is DEGRADED"


def test_active_condition_survives_a_restart_without_renotifying(hdb):
    AlertLedger(hdb).fire("c1", "ups", "warning", "UPS on battery", "UPS", now=1000)
    fresh = AlertLedger(hdb)  # simulates a process restart: new object, same database
    assert fresh.fire("c1", "ups", "warning", "UPS on battery", "UPS", now=2000) is False
    assert fresh.active()[0]["since"] == 1000


def test_refire_updates_title_and_detail_but_keeps_since(hdb):
    led = AlertLedger(hdb)
    led.fire("c1", "nas", "warning", "old", "TrueNAS", now=1000)
    led.fire("c1", "nas", "critical", "new", "TrueNAS", now=1100)
    (row,) = led.active()
    assert (row["title"], row["severity"], row["since"]) == ("new", "critical", 1000)


def test_flap_inside_cooldown_reactivates_without_notifying(hdb):
    led = AlertLedger(hdb, cooldown_seconds=300)
    led.fire("c1", "pbs", "warning", "t", "Backups", now=1000)
    assert led.clear("c1", now=1100) is True
    assert led.active() == []
    assert led.fire("c1", "pbs", "warning", "t", "Backups", now=1200) is False  # 100s < 300s
    (row,) = led.active()
    assert row["since"] == 1200  # fresh since, still visible in the attention list


def test_refire_after_cooldown_notifies_again(hdb):
    led = AlertLedger(hdb, cooldown_seconds=300)
    led.fire("c1", "pbs", "warning", "t", "Backups", now=1000)
    led.clear("c1", now=1100)
    assert led.fire("c1", "pbs", "warning", "t", "Backups", now=1500) is True  # 400s >= 300s


def test_cooldown_accepts_callable_none_and_zero(hdb):
    led = AlertLedger(hdb, cooldown_seconds=lambda: 0)
    led.fire("c1", "nas", "warning", "t", "d", now=1000)
    led.clear("c1", now=1001)
    assert led.fire("c1", "nas", "warning", "t", "d", now=1001) is True  # zero disables cooldown
    led2 = AlertLedger(hdb, cooldown_seconds=lambda: None)  # cleared setting -> default 300
    led2.fire("c2", "nas", "warning", "t", "d", now=1000)
    led2.clear("c2", now=1001)
    assert led2.fire("c2", "nas", "warning", "t", "d", now=1100) is False


def test_clear_is_a_noop_for_unknown_or_already_cleared(hdb):
    led = AlertLedger(hdb)
    assert led.clear("nope") is False
    led.fire("c1", "nas", "warning", "t", "d", now=1)
    assert led.clear("c1", now=2) is True
    assert led.clear("c1", now=3) is False


def test_active_orders_by_severity_then_since_and_filters_by_source(hdb):
    led = AlertLedger(hdb)
    led.fire("a", "nas", "warning", "a", "d", now=100)
    led.fire("b", "ups", "critical", "b", "d", now=300)
    led.fire("c", "nas", "critical", "c", "d", now=200)
    led.fire("d", "nas", "info", "d", "d", now=50)
    assert [r["condition_id"] for r in led.active()] == ["c", "b", "a", "d"]
    assert led.active_ids("nas") == {"a", "c", "d"}


def test_prune_removes_only_old_cleared_rows(hdb):
    led = AlertLedger(hdb)
    led.fire("old", "nas", "warning", "t", "d", now=1)
    led.clear("old", now=10)
    led.fire("recent", "nas", "warning", "t", "d", now=1)
    led.clear("recent", now=40 * 86400)
    led.fire("live", "nas", "warning", "t", "d", now=1)
    assert led.prune(older_than_days=30, now=45 * 86400) == 1
    assert led.active_ids("nas") == {"live"}


def test_concurrent_first_fire_notifies_exactly_once(hdb):
    led = AlertLedger(hdb)
    results = []
    threads = [threading.Thread(target=lambda: results.append(
        led.fire("c1", "nas", "warning", "t", "d"))) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1 and len(results) == 8


def test_mark_notified_only_touches_active_rows(hdb):
    led = AlertLedger(hdb)
    led.fire("c1", "nas", "warning", "t", "d", now=1)
    led.mark_notified("c1", now=5)
    assert led.active()[0]["notified_at"] == 5


# ── AlertGate ────────────────────────────────────────────────────────────────

def test_gate_without_ledger_always_says_notify():
    g = AlertGate("nas", "TrueNAS")
    assert g.fire("c1", "warning", "m") is True
    assert g.fire("c1", "warning", "m") is True  # the poller's own _alert_state dedupes
    g.clear("c1")
    g.begin_pass()
    g.end_pass()  # no ledger: no-op, no error
    assert g.active_ids() == set()


def test_gate_with_ledger_passes_label_and_message(hdb):
    led = AlertLedger(hdb)
    g = AlertGate("nas", "TrueNAS", led)
    assert g.fire("c1", "critical", "Pool tank is DEGRADED") is True
    (row,) = led.active()
    assert (row["source"], row["detail"], row["title"]) == ("nas", "TrueNAS", "Pool tank is DEGRADED")
    assert g.active_ids() == {"c1"}


def test_gate_end_pass_clears_untouched_rows_of_its_own_source_only(hdb):
    led = AlertLedger(hdb)
    led.fire("gone", "nas", "warning", "t", "TrueNAS", now=1)
    led.fire("kept", "nas", "warning", "t", "TrueNAS", now=1)
    led.fire("other-source", "ups", "warning", "t", "UPS", now=1)
    g = AlertGate("nas", "TrueNAS", led)
    g.begin_pass()
    g.fire("kept", "warning", "t")
    g.end_pass()
    assert led.active_ids("nas") == {"kept"}
    assert led.active_ids("ups") == {"other-source"}


def test_gate_end_pass_prefix_filter_leaves_other_conditions_alone(hdb):
    led = AlertLedger(hdb)
    led.fire("stop:100", "proxmox", "warning", "t", "Proxmox", now=1)
    led.fire("node:pve", "proxmox", "critical", "t", "Proxmox", now=1)
    g = AlertGate("proxmox", "Proxmox", led)
    g.begin_pass()
    g.end_pass(prefixes=("node:", "pause:"))
    assert led.active_ids("proxmox") == {"stop:100"}  # edge-triggered stop alert untouched


def test_gate_falls_back_to_notify_when_the_ledger_raises():
    bad = MagicMock()
    bad.fire.side_effect = RuntimeError("db locked")
    bad.clear.side_effect = RuntimeError("db locked")
    bad.active_ids.side_effect = RuntimeError("db locked")
    g = AlertGate("nas", "TrueNAS", bad)
    assert g.fire("c1", "warning", "m") is True
    g.clear("c1")
    g.begin_pass()
    g.end_pass()
    assert g.active_ids() == set()


# ── cooldown setting ─────────────────────────────────────────────────────────

def test_alert_cooldown_setting_is_an_int_with_a_range():
    from netwatch.http_handlers import SETTINGS_EDITABLE_KEYS, _SETTINGS_INT_RANGES
    assert SETTINGS_EDITABLE_KEYS["alert_cooldown_seconds"] is int
    assert _SETTINGS_INT_RANGES["alert_cooldown_seconds"] == (0, 86400)


def test_alert_cooldown_setting_rejects_out_of_range(tmp_path):
    from netwatch.http_handlers import _h_post_settings
    am = MagicMock()
    am.data = {}
    am.lock = MagicMock()
    am.lock.__enter__ = MagicMock(return_value=None)
    am.lock.__exit__ = MagicMock(return_value=False)
    am._save = MagicMock()
    status, body = _h_post_settings({"alert_cooldown_seconds": -5}, str(tmp_path / "hosts.yaml"), {},
                                    auth_manager=am)
    assert status == 400 and "alert_cooldown_seconds" in body["error"]
