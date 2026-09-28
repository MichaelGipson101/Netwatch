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


# ── per-thread pass tracking ─────────────────────────────────────────────────

def test_overlapping_passes_on_two_threads_do_not_clear_each_others_rows(hdb):
    led = AlertLedger(hdb)
    g = AlertGate("nas", "TrueNAS", led)
    fired = threading.Event()
    second_began = threading.Event()
    errors = []

    def first():
        try:
            g.begin_pass()
            g.fire("kept", "warning", "m")
            fired.set()
            assert second_began.wait(5)      # the other pass begins between our fire and end_pass
            g.end_pass()
        except Exception as e:               # surface assertion failures from the thread
            errors.append(e)

    def second():
        try:
            assert fired.wait(5)
            g.begin_pass()
            second_began.set()
        except Exception as e:
            errors.append(e)

    t1, t2 = threading.Thread(target=first), threading.Thread(target=second)
    t1.start(); t2.start(); t1.join(10); t2.join(10)
    assert not errors
    assert led.active_ids("nas") == {"kept"}


def test_end_pass_without_begin_pass_on_this_thread_is_a_noop(hdb):
    led = AlertLedger(hdb)
    led.fire("orphan", "nas", "warning", "t", "TrueNAS", now=1)
    g = AlertGate("nas", "TrueNAS", led)
    g.fire("also", "warning", "m")           # outside a pass: harmless, tracks nothing
    g.end_pass()
    assert led.active_ids("nas") == {"orphan", "also"}


def test_end_pass_consumes_the_pass_so_a_second_end_pass_is_a_noop(hdb):
    led = AlertLedger(hdb)
    g = AlertGate("nas", "TrueNAS", led)
    g.begin_pass()
    g.end_pass()
    led.fire("late", "nas", "warning", "t", "TrueNAS", now=1)
    g.end_pass()
    assert led.active_ids("nas") == {"late"}


from datetime import datetime, timedelta

from netwatch.attention import (build_attention, check_ip_drift, fmt_duration, host_facts)
from netwatch.hosts import HostState
from netwatch.network import parse_neighbors

NOW = 1_000_000.0


def F(name, ip, *, up=True, mac="", always_on=True, checked=True, maint=False, down_since=0.0):
    return {"name": name, "ip": ip, "mac": mac, "always_on": always_on, "is_up": up,
            "checked": checked, "in_maintenance": maint, "first_down_at": down_since}


def R(id_, ip="", mac="", system=None):
    return {"id": id_, "ip": ip, "mac": mac, "system": system or f"dev{id_}"}


def build(facts, records=(), parents=None, rows=(), pending=0, drift=()):
    return build_attention(list(facts), list(records), parents or {}, list(rows), pending, list(drift), now=NOW)


def items_of(payload, kind):
    return [i for i in payload["items"] if i["kind"] == kind]


def test_fmt_duration():
    assert fmt_duration(5) == "under a minute"
    assert fmt_duration(12 * 60) == "12 min"
    assert fmt_duration(3 * 3600 + 5) == "3 h"
    assert fmt_duration(47 * 3600) == "47 h"
    assert fmt_duration(5 * 86400) == "5 d"
    assert fmt_duration(-10) == "under a minute"


def test_empty_install_headline():
    p = build([])
    assert p["verdict"]["level"] == "ok"
    assert p["verdict"]["headline"] == "No hosts are being monitored yet."
    assert p["items"] == [] and p["verdict"]["counts"]["hosts_total"] == 0


def test_all_up_is_ok():
    p = build([F("a", "10.0.0.1"), F("b", "10.0.0.2")])
    assert p["verdict"]["level"] == "ok" and p["verdict"]["headline"] == "Everything looks good."
    assert p["verdict"]["counts"] == {"hosts_total": 2, "hosts_up": 2, "hosts_down": 0,
                                      "affected": 0, "maintenance": 0, "dismissed": 0}


def test_single_down_host_without_inventory_is_its_own_root():
    p = build([F("ZeroPi", "10.0.0.9", up=False, down_since=NOW - 720)])
    (it,) = items_of(p, "host_down")
    assert it["id"] == "host_down:10.0.0.9" and it["title"] == "ZeroPi is down"
    assert it["severity"] == "critical" and it["root_ip"] == "10.0.0.9" and it["affected"] == []
    assert it["detail"] == "Down 12 min" and it["since"] == NOW - 720
    assert it["link"] == {"page": "monitor", "subview": "hosts", "params": {"host": "10.0.0.9"}}
    assert p["verdict"]["level"] == "down" and p["verdict"]["headline"] == "ZeroPi is down."


def test_chain_under_one_down_root_groups_into_one_item():
    facts = [F("sw", "10.0.0.2", up=False, mac="aa:aa:aa:aa:aa:01", down_since=NOW - 600),
             F("ap", "10.0.0.3", up=False, mac="aa:aa:aa:aa:aa:02"),
             F("nas", "10.0.0.4", up=False, mac="aa:aa:aa:aa:aa:03"),
             F("pi", "10.0.0.5", up=True, mac="aa:aa:aa:aa:aa:04")]
    records = [R(1, mac="aa:aa:aa:aa:aa:01"), R(2, mac="aa:aa:aa:aa:aa:02"),
               R(3, mac="aa:aa:aa:aa:aa:03"), R(4, mac="aa:aa:aa:aa:aa:04")]
    parents = {1: None, 2: 1, 3: 2, 4: 1}          # nas -> ap -> sw; pi -> sw (pi is up)
    p = build(facts, records, parents)
    (it,) = items_of(p, "host_down")
    assert it["root_ip"] == "10.0.0.2" and it["affected"] == ["10.0.0.3", "10.0.0.4"]
    assert it["detail"] == "Down 10 min · root cause of 2 host alerts"
    assert p["verdict"]["headline"] == "sw is down. 2 hosts unreachable."
    assert p["verdict"]["counts"]["hosts_down"] == 3 and p["verdict"]["counts"]["affected"] == 2


def test_single_affected_host_is_singular():
    facts = [F("sw", "10.0.0.2", up=False, mac="aa:aa:aa:aa:aa:01"),
             F("ap", "10.0.0.3", up=False, mac="aa:aa:aa:aa:aa:02")]
    p = build(facts, [R(1, mac="aa:aa:aa:aa:aa:01"), R(2, mac="aa:aa:aa:aa:aa:02")], {1: None, 2: 1})
    assert p["verdict"]["headline"] == "sw is down. 1 host unreachable."
    assert items_of(p, "host_down")[0]["detail"].endswith("root cause of 1 host alert")


def test_two_independent_roots_make_two_items_and_a_count_headline():
    facts = [F("a", "10.0.0.2", up=False, mac="aa:aa:aa:aa:aa:01", down_since=NOW - 100),
             F("b", "10.0.0.3", up=False, mac="aa:aa:aa:aa:aa:02", down_since=NOW - 900)]
    p = build(facts, [R(1, mac="aa:aa:aa:aa:aa:01"), R(2, mac="aa:aa:aa:aa:aa:02")], {1: None, 2: None})
    assert [i["root_ip"] for i in items_of(p, "host_down")] == ["10.0.0.3", "10.0.0.2"]  # longest first
    assert p["verdict"]["headline"] == "2 problems need attention. b is down."


def test_child_of_an_up_parent_is_its_own_root():
    facts = [F("sw", "10.0.0.2", up=True, mac="aa:aa:aa:aa:aa:01"),
             F("ap", "10.0.0.3", up=False, mac="aa:aa:aa:aa:aa:02")]
    p = build(facts, [R(1, mac="aa:aa:aa:aa:aa:01"), R(2, mac="aa:aa:aa:aa:aa:02")], {1: None, 2: 1})
    (it,) = items_of(p, "host_down")
    assert it["root_ip"] == "10.0.0.3" and it["affected"] == []


def test_inventory_link_falls_back_to_ip_when_there_is_no_mac():
    facts = [F("sw", "10.0.0.2", up=False), F("ap", "10.0.0.3", up=False)]
    p = build(facts, [R(1, ip="10.0.0.2"), R(2, ip="10.0.0.3")], {1: None, 2: 1})
    assert items_of(p, "host_down")[0]["affected"] == ["10.0.0.3"]


def test_cyclic_parent_data_terminates():
    facts = [F("a", "10.0.0.2", up=False, mac="aa:aa:aa:aa:aa:01"),
             F("b", "10.0.0.3", up=False, mac="aa:aa:aa:aa:aa:02")]
    p = build(facts, [R(1, mac="aa:aa:aa:aa:aa:01"), R(2, mac="aa:aa:aa:aa:aa:02")], {1: 2, 2: 1})
    assert len(items_of(p, "host_down")) >= 1     # no hang, no exception


def test_dangling_parent_id_is_ignored():
    facts = [F("a", "10.0.0.2", up=False, mac="aa:aa:aa:aa:aa:01")]
    p = build(facts, [R(1, mac="aa:aa:aa:aa:aa:01")], {1: 999})
    assert items_of(p, "host_down")[0]["root_ip"] == "10.0.0.2"


def test_maintenance_idle_and_unchecked_hosts_are_not_problems():
    facts = [F("m", "10.0.0.2", up=False, maint=True),
             F("idle", "10.0.0.3", up=False, always_on=False),
             F("new", "10.0.0.4", up=False, checked=False)]
    p = build(facts)
    assert p["items"] == [] and p["verdict"]["level"] == "ok"
    assert p["verdict"]["counts"]["maintenance"] == 1


def test_ledger_rows_become_poller_items_with_links():
    rows = [{"condition_id": "pool_health_tank", "source": "nas", "severity": "critical",
             "title": 'Pool "tank" is DEGRADED', "detail": "TrueNAS", "since": NOW - 3600, "notified_at": 1},
            {"condition_id": "ups-on-battery", "source": "ups", "severity": "warning",
             "title": "UPS is running on battery power", "detail": "UPS", "since": NOW - 60, "notified_at": 1}]
    p = build([F("a", "10.0.0.1")], rows=rows)
    nas, ups = items_of(p, "poller_condition")
    assert nas["id"] == "alert:pool_health_tank" and nas["detail"] == "TrueNAS · 1 h"
    assert nas["link"] == {"page": "infra", "subview": "truenas", "params": {}}
    assert ups["link"] is None
    assert p["verdict"]["level"] == "down"       # a critical item
    assert p["verdict"]["headline"] == '2 problems need attention. Pool "tank" is DEGRADED.'


def test_warning_only_is_warn_and_single_problem_headline_is_its_title():
    rows = [{"condition_id": "ups-on-battery", "source": "ups", "severity": "warning",
             "title": "UPS is running on battery power", "detail": "UPS", "since": NOW - 5, "notified_at": None}]
    p = build([F("a", "10.0.0.1")], rows=rows)
    assert p["verdict"]["level"] == "warn"
    assert p["verdict"]["headline"] == "UPS is running on battery power."


def test_info_items_do_not_change_the_verdict():
    p = build([F("a", "10.0.0.1")], pending=3,
              drift=[{"mac": "aa:aa:aa:aa:aa:01", "name": "vf2", "monitored_ip": "10.0.0.7", "seen_ip": "10.0.0.8"}],
              records=[R(5, mac="aa:aa:aa:aa:aa:01")])
    assert p["verdict"]["level"] == "ok" and p["verdict"]["headline"] == "Everything looks good."
    (sug,) = items_of(p, "connection_suggestions")
    assert sug["title"] == "3 connection suggestions" and sug["severity"] == "info"
    assert sug["link"] == {"page": "lab", "subview": "connections", "params": {}}
    (dr,) = items_of(p, "ip_drift")
    assert dr["id"] == "ip_drift:aa:aa:aa:aa:aa:01" and dr["title"] == "vf2 moved to 10.0.0.8"
    assert dr["detail"] == "Monitored at 10.0.0.7"
    assert dr["link"] == {"page": "lab", "subview": "inventory", "params": {"inv": 5}}


def test_singular_suggestion_and_drift_without_inventory_record_has_no_link():
    p = build([F("a", "10.0.0.1")], pending=1,
              drift=[{"mac": "aa:aa:aa:aa:aa:09", "name": "x", "monitored_ip": "1.1.1.1", "seen_ip": "1.1.1.2"}])
    assert items_of(p, "connection_suggestions")[0]["title"] == "1 connection suggestion"
    assert items_of(p, "ip_drift")[0]["link"] is None


def test_items_sort_by_severity_then_longest_running_first():
    rows = [{"condition_id": "w", "source": "nas", "severity": "warning", "title": "w", "detail": "TrueNAS",
             "since": NOW - 10, "notified_at": None}]
    facts = [F("d", "10.0.0.2", up=False, down_since=NOW - 50)]
    p = build(facts, rows=rows, pending=2)
    assert [i["kind"] for i in p["items"]] == ["host_down", "poller_condition", "connection_suggestions"]


def test_host_down_with_unknown_since_has_null_since_and_still_a_detail():
    p = build([F("a", "10.0.0.2", up=False, down_since=0.0)])
    (it,) = items_of(p, "host_down")
    assert it["since"] is None and it["detail"] == "Down"


def test_unchecked_child_of_a_down_root_is_not_counted_as_affected():
    facts = [F("sw", "10.0.0.2", up=False, mac="aa:aa:aa:aa:aa:01", down_since=NOW - 60),
             F("ap", "10.0.0.3", up=False, mac="aa:aa:aa:aa:aa:02", checked=False)]
    recs = [R(1, mac="aa:aa:aa:aa:aa:01"), R(2, mac="aa:aa:aa:aa:aa:02")]
    p = build(facts, recs, parents={2: 1})
    (it,) = items_of(p, "host_down")
    assert it["affected"] == [] and p["verdict"]["counts"]["affected"] == 0
    assert p["verdict"]["headline"] == "sw is down."


# ── host_facts ───────────────────────────────────────────────────────────────

def test_host_facts_maps_hoststate_fields():
    h = HostState(name="a", ip="10.0.0.1", group="g", interval=30,
                  specs={"mac": "AA-BB-CC-DD-EE-FF"})
    h.history.append(False)
    h.last_checked = datetime.now()
    h.first_down_at = 123.0
    h.maintenance_until = datetime.now() + timedelta(hours=1)
    (f,) = host_facts([h])
    assert f == {"name": "a", "ip": "10.0.0.1", "mac": "aa:bb:cc:dd:ee:ff", "always_on": True,
                 "is_up": False, "checked": True, "in_maintenance": True, "first_down_at": 123.0}


def test_host_facts_without_specs_or_checks():
    (f,) = host_facts([HostState(name="a", ip="10.0.0.1", group="g", interval=30)])
    assert f["mac"] == "" and f["checked"] is False and f["is_up"] is False
    assert f["in_maintenance"] is False and f["first_down_at"] == 0.0


# ── IP drift ─────────────────────────────────────────────────────────────────

MAC1 = "aa:bb:cc:dd:ee:01"


def test_drift_detected_when_mac_seen_only_at_another_ip():
    d = check_ip_drift([F("vf2", "10.0.0.7", up=False, mac=MAC1)], [], {MAC1: {"10.0.0.8"}})
    assert d == [{"mac": MAC1, "name": "vf2", "monitored_ip": "10.0.0.7", "seen_ip": "10.0.0.8"}]


def test_drift_is_detected_across_subnets():
    d = check_ip_drift([F("ap", "192.168.5.160", up=False, mac=MAC1)], [], {MAC1: {"192.168.4.44"}})
    assert d and d[0]["seen_ip"] == "192.168.4.44"


def test_no_drift_for_an_up_host_even_if_its_mac_is_seen_only_elsewhere():
    assert check_ip_drift([F("ts", "100.64.0.7", up=True, mac=MAC1)], [], {MAC1: {"10.0.0.8"}}) == []


def test_no_drift_for_an_unchecked_host():
    assert check_ip_drift([F("a", "10.0.0.7", up=False, checked=False, mac=MAC1)], [],
                          {MAC1: {"10.0.0.8"}}) == []


def test_no_drift_when_monitored_ip_is_among_the_seen_ips():
    assert check_ip_drift([F("pve", "10.0.0.7", up=False, mac=MAC1)], [], {MAC1: {"10.0.0.7", "10.0.0.8"}}) == []


def test_no_drift_when_mac_absent_from_neighbor_table_or_host_has_no_mac():
    assert check_ip_drift([F("a", "10.0.0.7", up=False, mac=MAC1)], [], {}) == []
    assert check_ip_drift([F("a", "10.0.0.7", up=False)], [], {MAC1: {"10.0.0.8"}}) == []


def test_drift_uses_the_inventory_mac_when_the_host_has_none():
    d = check_ip_drift([F("a", "10.0.0.7", up=False)], [R(1, ip="10.0.0.7", mac=MAC1.upper())], {MAC1: {"10.0.0.8"}})
    assert d and d[0]["seen_ip"] == "10.0.0.8"


def test_drift_skips_hosts_in_maintenance():
    assert check_ip_drift([F("a", "10.0.0.7", up=False, mac=MAC1, maint=True)], [], {MAC1: {"10.0.0.8"}}) == []


# ── ip neigh parsing ─────────────────────────────────────────────────────────

def test_parse_neighbors_handles_states_extra_tokens_and_garbage():
    text = "\n".join([
        "192.168.4.1 dev eth0 lladdr aa:bb:cc:dd:ee:01 REACHABLE",
        "192.168.4.2 dev eth0 lladdr AA:BB:CC:DD:EE:02 router STALE",
        "192.168.4.3 dev eth0  FAILED",
        "192.168.4.4 dev eth0 lladdr aa:bb:cc:dd:ee:04 INCOMPLETE",
        "192.168.4.5 dev eth0 lladdr aa:bb:cc:dd:ee:01 DELAY",
        "fe80::1 dev eth0 lladdr aa:bb:cc:dd:ee:09 REACHABLE",
        "garbage line",
        "",
    ])
    assert parse_neighbors(text) == {
        "aa:bb:cc:dd:ee:01": {"192.168.4.1", "192.168.4.5"},
        "aa:bb:cc:dd:ee:02": {"192.168.4.2"},
    }


def test_read_neighbors_returns_empty_when_ip_is_unavailable(monkeypatch):
    import subprocess
    from netwatch import network

    def boom(*a, **k):
        raise FileNotFoundError("ip")
    monkeypatch.setattr(subprocess, "run", boom)
    assert network.read_neighbors() == {}


from netwatch.attention import IPDriftMonitor


class _HostMgr:
    def __init__(self, hosts):
        self._hosts = hosts

    def list_hosts(self):
        return self._hosts


class _Inv:
    def __init__(self, records=(), conns=(), pending=0):
        self._r, self._c = list(records), list(conns)
        self.suggestions = type("S", (), {"count_pending": staticmethod(lambda: pending)})()

    def list_all(self):
        return self._r

    def list_all_connections(self):
        return self._c


def _hs(name, ip, mac, up=False):
    h = HostState(name=name, ip=ip, group="g", interval=30, specs={"mac": mac})
    h.last_checked = datetime.now()
    h.history.append(up)
    return h


def test_drift_monitor_refresh_and_get_return_a_copy():
    mon = IPDriftMonitor(_HostMgr([_hs("vf2", "10.0.0.7", MAC1)]), _Inv(), lambda: {MAC1: {"10.0.0.8"}})
    assert mon.get() == []                          # nothing before the first pass
    d = mon.refresh()
    assert d[0]["seen_ip"] == "10.0.0.8"
    got = mon.get()
    got.clear()
    assert len(mon.get()) == 1                      # callers cannot mutate the stored list


def test_drift_monitor_survives_a_failing_reader():
    def boom():
        raise OSError("no ip")
    mon = IPDriftMonitor(_HostMgr([_hs("a", "10.0.0.7", MAC1)]), None, boom)
    with pytest.raises(OSError):
        mon.refresh()                               # refresh itself propagates...
    assert mon.get() == []                          # ...and leaves the previous result alone


import json

from netwatch.attention import Explainer, build_explain_messages, explain_key, openrouter_complete

ITEM_A = {"id": "host_down:10.0.0.2", "kind": "host_down", "severity": "critical", "title": "sw is down",
          "detail": "Down 10 min · root cause of 2 host alerts", "since": NOW - 600,
          "affected": ["10.0.0.3"], "root_ip": "10.0.0.2", "link": None}
ITEM_B = {"id": "alert:x", "kind": "poller_condition", "severity": "warning", "title": "UPS on battery",
          "detail": "UPS · 1 min", "since": NOW - 60, "affected": [], "root_ip": None, "link": None}
ITEM_INFO = {"id": "connection_suggestions", "kind": "connection_suggestions", "severity": "info",
             "title": "3 connection suggestions", "detail": "Review in Lab", "since": None,
             "affected": [], "root_ip": None, "link": None}


class _Llm:
    def __init__(self, text="  Probably the switch.  "):
        self.calls, self.text = [], text

    def __call__(self, api_key, model, messages):
        self.calls.append((api_key, model, messages))
        if isinstance(self.text, Exception):
            raise self.text
        return self.text


def test_explain_key_changes_with_the_set_and_since_but_not_order():
    assert explain_key([ITEM_A, ITEM_B]) == explain_key([ITEM_B, ITEM_A])
    assert explain_key([ITEM_A]) != explain_key([ITEM_A, ITEM_B])
    assert explain_key([ITEM_A]) != explain_key([{**ITEM_A, "since": NOW - 5}])


def test_prompt_contains_items_and_no_secrets():
    msgs = build_explain_messages([ITEM_A, ITEM_B], NOW)
    assert msgs[0]["role"] == "system" and msgs[1]["role"] == "user"
    body = msgs[1]["content"]
    assert "sw is down" in body and "10.0.0.3" in body and "UPS on battery" in body
    assert "[critical]" in body and "for 10 min" in body
    assert "sk-secret" not in json.dumps(msgs)


def test_explain_calls_upstream_once_then_serves_the_cache():
    llm = _Llm()
    ex = Explainer(complete=llm)
    s, p = ex.explain([ITEM_A, ITEM_INFO], "sk-secret", "openrouter/free", now=1000)
    assert s == 200 and p["explanation"] == "Probably the switch." and p["cached"] is False
    s, p = ex.explain([ITEM_A, ITEM_INFO], "sk-secret", "openrouter/free", now=1005)
    assert s == 200 and p["cached"] is True and len(llm.calls) == 1
    assert llm.calls[0][0] == "sk-secret" and llm.calls[0][1] == "openrouter/free"
    assert "3 connection suggestions" not in llm.calls[0][2][1]["content"]   # info items are not sent


def test_explain_regenerates_when_the_set_changes_but_respects_the_rate_limit():
    llm = _Llm()
    ex = Explainer(complete=llm)
    ex.explain([ITEM_A], "k", "m", now=1000)
    s, p = ex.explain([ITEM_A, ITEM_B], "k", "m", now=1010)     # changed, but inside 30s
    assert s == 200 and p["cached"] is True and p["stale"] is True and len(llm.calls) == 1
    s, p = ex.explain([ITEM_A, ITEM_B], "k", "m", now=1031)     # outside the window
    assert s == 200 and p["cached"] is False and len(llm.calls) == 2


def test_explain_rate_limited_with_nothing_cached_is_429():
    ex = Explainer(complete=_Llm(RuntimeError("upstream down")))
    assert ex.explain([ITEM_A], "k", "m", now=1000)[0] == 502   # a failed call still starts the window
    s, p = ex.explain([ITEM_A], "k", "m", now=1005)
    assert s == 429 and p["error"] == "rate_limited" and p["retry_after"] >= 1


def test_explain_no_key_is_404_and_no_problems_is_fixed_text_without_a_call():
    llm = _Llm()
    ex = Explainer(complete=llm)
    assert ex.explain([ITEM_A], "  ", "m", now=1) == (404, {"error": "ai_not_configured"})
    s, p = ex.explain([ITEM_INFO], "k", "m", now=1)
    assert s == 200 and "Nothing needs attention" in p["explanation"] and not llm.calls
    s, p = ex.explain([], "", "m", now=1)                       # no items: fixed text even without a key
    assert s == 200 and not llm.calls


def test_explain_upstream_failure_is_502_and_leaks_nothing():
    ex = Explainer(complete=_Llm(RuntimeError("connect to https://openrouter.ai failed key=sk-secret")))
    s, p = ex.explain([ITEM_A], "sk-secret", "m", now=1)
    assert s == 502 and "sk-secret" not in json.dumps(p) and "openrouter" not in json.dumps(p)
    s, p = Explainer(complete=_Llm("   ")).explain([ITEM_A], "k", "m", now=1)
    assert s == 502 and p["error"] == "empty explanation"


def test_openrouter_complete_posts_a_non_streaming_request(monkeypatch):
    import urllib.request
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "Hello"}}]}).encode()

    def fake_urlopen(req, timeout=None):
        seen["req"], seen["timeout"] = req, timeout
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = openrouter_complete("sk-x", "openrouter/free", [{"role": "user", "content": "hi"}])
    assert out == "Hello"
    req = seen["req"]
    body = json.loads(req.data)
    assert body["model"] == "openrouter/free" and "stream" not in body and body["max_tokens"] == 300
    assert req.get_header("Authorization") == "Bearer sk-x"
    assert req.full_url == "https://openrouter.ai/api/v1/chat/completions" and seen["timeout"] == 45


def test_host_facts_seeds_first_down_at_from_the_open_incident():
    h = HostState(name="a", ip="10.0.0.1", group="g", interval=30)
    h.first_down_at = 5000.0                            # set after a restart (later than the incident)
    (f,) = host_facts([h], since_by_ip={"10.0.0.1": 1000.0})
    assert f["first_down_at"] == 1000.0                 # the earlier stored start wins
    h.first_down_at = 0.0
    (f,) = host_facts([h], since_by_ip={"10.0.0.1": 1000.0})
    assert f["first_down_at"] == 1000.0                 # seeds when the host has none
    h.first_down_at = 800.0
    (f,) = host_facts([h], since_by_ip={"10.0.0.1": 1000.0})
    assert f["first_down_at"] == 800.0                  # an earlier in-memory value is kept
    (f,) = host_facts([h], since_by_ip={"10.9.9.9": 1.0})
    assert f["first_down_at"] == 800.0                  # other hosts' incidents are ignored
    (f,) = host_facts([h])
    assert f["first_down_at"] == 800.0                  # no seed at all: unchanged
