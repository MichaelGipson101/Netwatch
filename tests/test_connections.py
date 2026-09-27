"""Tests for the Netwatch 4.0 connections rework (plan 1: foundation)."""
import json
import os
import tempfile

import pytest

from netwatch.connections import (
    orient_edge, default_connection_type, infer_network_role, network_role,
    normalize_port, resolve_ports, validate_parent_port, fingerprint, type_rank,
    lint_edge, plan_connections_migration, migration_drift_key,
)
from netwatch.storage import write_pre_migration_backup


def rec(id_, device_type="host", system=None, role=None, **props):
    """Build an inventory-shaped record dict for pure-function tests."""
    return {"id": id_, "system": system or f"dev{id_}", "device_type": device_type,
            "role": role, "properties": dict(props)}


# ── orientation ─────────────────────────────────────────────────────────────

def test_orient_vm_is_child_of_host_regardless_of_argument_order():
    vm, host = rec(1, "vm"), rec(2, "host")
    assert orient_edge(vm, host) == (vm, host, False)
    assert orient_edge(host, vm) == (vm, host, False)


def test_orient_host_is_child_of_network():
    host, sw = rec(1, "host"), rec(2, "network", network_role="switch")
    assert orient_edge(sw, host) == (host, sw, False)


def test_orient_power_edge_makes_ups_parent_even_of_network_gear():
    sw, ups = rec(1, "network", network_role="switch"), rec(2, "ups")
    assert orient_edge(ups, sw, "power") == (sw, ups, False)
    # Without the power type, plain rank wins: the switch outranks the UPS.
    assert orient_edge(ups, sw, "ethernet") == (ups, sw, False)


def test_orient_network_tie_broken_by_role():
    gw = rec(1, "network", network_role="gateway")
    sw = rec(2, "network", network_role="switch")
    ap = rec(3, "network", network_role="ap")
    assert orient_edge(gw, sw) == (sw, gw, False)
    assert orient_edge(sw, ap) == (ap, sw, False)


def test_orient_equal_rank_without_tiebreak_is_ambiguous_and_keeps_order():
    a, b = rec(1, "host"), rec(2, "host")
    assert orient_edge(a, b) == (a, b, True)
    n1, n2 = rec(3, "network"), rec(4, "network")  # both role "other"
    assert orient_edge(n1, n2) == (n1, n2, True)


def test_type_rank_defaults_unknown_and_missing_types_to_host():
    assert type_rank({"device_type": None}) == type_rank(rec(1, "host"))
    assert type_rank({"device_type": "toaster"}) == type_rank(rec(1, "host"))


# ── default type ────────────────────────────────────────────────────────────

def test_default_type_vm_on_host_is_virtual():
    assert default_connection_type(rec(1, "vm"), rec(2, "host")) == "virtual"


def test_default_type_client_on_ap_is_wifi_and_on_switch_is_ethernet():
    ap = rec(2, "network", network_role="ap")
    sw = rec(3, "network", network_role="switch")
    assert default_connection_type(rec(1, "phone"), ap) == "wifi"
    assert default_connection_type(rec(1, "host"), sw) == "ethernet"


# ── network roles ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("system,role,expected", [
    ("Eero Pro 6E — Gateway", "Primary Router & Gateway", "gateway"),
    ("Ubiquiti Unifi USW Pro Max 16 PoE", "Primary Network Switch", "switch"),
    ("TP Link 24-port Managed Switch", "Main Network Switch", "switch"),
    ("Eero Pro 6E — Basement AP", "Access Point", "ap"),
    ("Mystery box", None, "other"),
])
def test_infer_network_role_from_free_text(system, role, expected):
    assert infer_network_role(rec(1, "network", system=system, role=role)) == expected


def test_network_role_reads_property_and_rejects_unknown_values():
    assert network_role(rec(1, "network", network_role="Switch")) == "switch"
    assert network_role(rec(1, "network", network_role="core-router")) == "other"
    assert network_role(rec(1, "network")) == "other"


# ── ports ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (" 8 ", "8"), ("08", "8"), (8, "8"), ("SFP+ 1", "SFP+ 1"), ("", None), (None, None), ("  ", None),
    ("²", "²"),
])
def test_normalize_port(raw, expected):
    assert normalize_port(raw) == expected


def test_resolve_ports_from_port_count():
    ports = resolve_ports(rec(1, "network", port_count=3))
    assert [p["name"] for p in ports] == ["1", "2", "3"]
    assert set(ports[0]) >= {"name", "up", "speed_mbps", "poe"}


def test_resolve_ports_prefers_live_ports_and_copies_them():
    live = [{"name": "Port 1", "up": True, "speed_mbps": 1000, "poe": None}]
    ports = resolve_ports(rec(1, "network", port_count=16), live)
    assert ports == live
    ports[0]["name"] = "mutated"
    assert live[0]["name"] == "Port 1"


@pytest.mark.parametrize("count", [None, "", "abc", 0, -2, 100000])
def test_resolve_ports_without_usable_count_is_free_text(count):
    r = rec(1, "network") if count is None else rec(1, "network", port_count=count)
    assert resolve_ports(r) is None


def test_validate_parent_port():
    sw = rec(1, "network", system="USW", port_count=16)
    ports = resolve_ports(sw)
    assert validate_parent_port(sw, "8", ports) is None
    assert validate_parent_port(sw, " 08", ports) is None
    assert validate_parent_port(sw, None, ports) is None
    assert "not a port on USW" in validate_parent_port(sw, "etho0", ports)
    assert validate_parent_port(sw, "anything", None) is None  # free text


def test_fingerprint_is_stable_and_order_independent():
    assert fingerprint({"a": 1, "b": [1, 2]}) == fingerprint({"b": [1, 2], "a": 1})
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})
    assert len(fingerprint({})) == 16


# ── schema + oriented listing (Task 2) ──────────────────────────────────────

from netwatch.storage import HistoryDB, InventoryDB, _column_exists


def make_idb(tmpdir):
    hdb = HistoryDB(os.path.join(tmpdir, "conn_test.db"))
    return hdb, InventoryDB(hdb)


def add_device(idb, system, device_type="host", **props):
    new_id, err = idb.create({"system": system, "device_type": device_type,
                              "properties": props or None})
    assert err is None, err
    return new_id


def insert_raw_edge(idb, from_id, to_id, from_port=None, to_port=None, ctype="ethernet"):
    """Insert an edge exactly as stored, bypassing orientation (simulates
    pre-4.0 data)."""
    with idb.lock:
        cur = idb.conn.execute(
            "INSERT INTO inventory_connections (from_device_id, to_device_id, from_port, "
            "to_port, connection_type, created_at) VALUES (?, ?, ?, ?, ?, 0)",
            (from_id, to_id, from_port, to_port, ctype))
        return cur.lastrowid


def test_schema_adds_connection_columns_and_meta_table():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        for col in ("source", "external_key", "last_seen", "updated_at"):
            assert _column_exists(idb.conn, "inventory_connections", col)
        assert idb.conn.execute("SELECT COUNT(*) FROM schema_meta").fetchone()[0] == 0
        assert idb.live_port_provider is None
        hdb.close()


def test_schema_upgrade_is_idempotent_on_reopen():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        hdb.close()
        hdb2, idb2 = make_idb(d)  # second open must not raise "duplicate column"
        assert _column_exists(idb2.conn, "inventory_connections", "source")
        hdb2.close()


def test_list_all_connections_includes_aliases_names_and_source():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        host = add_device(idb, "ProDesk1", "host")
        sw = add_device(idb, "USW", "network", port_count=16)
        eid = insert_raw_edge(idb, host, sw, "eth0", "8")
        [row] = idb.list_all_connections()
        assert row["id"] == eid
        assert (row["child_id"], row["parent_id"]) == (host, sw)
        assert (row["child_port"], row["parent_port"]) == ("eth0", "8")
        assert (row["child_name"], row["parent_name"]) == ("ProDesk1", "USW")
        assert (row["child_type"], row["parent_type"]) == ("host", "network")
        assert row["source"] == "manual"
        assert idb.get_connection(eid)["parent_name"] == "USW"
        assert idb.get_connection(99999) is None
        hdb.close()


def test_listing_skips_dangling_edges():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        a = add_device(idb, "A")
        with idb.lock:
            idb.conn.execute("PRAGMA foreign_keys = OFF")
        insert_raw_edge(idb, a, 424242)  # parent never existed
        with idb.lock:
            idb.conn.execute("PRAGMA foreign_keys = ON")
        assert idb.list_all_connections() == []
        assert idb.list_connections_for_device(a) == []
        hdb.close()


def test_list_connections_for_device_keeps_direction_field():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        vm = add_device(idb, "jellyfin", "vm")
        host = add_device(idb, "EliteDesk", "host")
        insert_raw_edge(idb, vm, host, ctype="virtual")
        assert idb.list_connections_for_device(vm)[0]["direction"] == "out"
        assert idb.list_connections_for_device(host)[0]["direction"] == "in"
        hdb.close()


DAY = 86400


def test_suggestion_insert_get_list_count():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        s = idb.suggestions
        sid = s.upsert("drift", "migration", "drift:x", {"msg": "hi"}, "fp1", now=100)
        row = s.get(sid)
        assert row["payload"] == {"msg": "hi"}
        assert (row["status"], row["first_seen"], row["last_seen"]) == ("pending", 100, 100)
        assert [r["id"] for r in s.list()] == [sid]
        assert s.count_pending() == 1
        assert s.get(99999) is None
        hdb.close()


def test_dismissed_stays_dismissed_at_same_fingerprint_and_reopens_on_change():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        s = idb.suggestions
        sid = s.upsert("edge", "unifi", "edge:a", {"port": "7"}, "fp1", now=100)
        assert s.set_status(sid, "dismissed", now=110)
        s.upsert("edge", "unifi", "edge:a", {"port": "7"}, "fp1", now=200)
        row = s.get(sid)
        assert (row["status"], row["last_seen"], row["decided_at"]) == ("dismissed", 200, 110)
        s.upsert("edge", "unifi", "edge:a", {"port": "9"}, "fp2", now=300)
        row = s.get(sid)
        assert (row["status"], row["payload"], row["decided_at"]) == ("pending", {"port": "9"}, None)
        hdb.close()


def test_resolve_only_touches_pending_and_resolved_rows_reopen_when_seen_again():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        s = idb.suggestions
        a = s.upsert("drift", "migration", "k:a", {}, "fp", now=1)
        b = s.upsert("drift", "migration", "k:b", {}, "fp", now=1)
        s.set_status(b, "dismissed", now=2)
        assert s.resolve("k:a", now=3) is True
        assert s.resolve("k:b", now=3) is False
        assert s.resolve("k:missing", now=3) is False
        assert s.get(a)["status"] == "resolved"
        assert s.get(b)["status"] == "dismissed"
        s.upsert("drift", "migration", "k:a", {}, "fp", now=4)
        assert s.get(a)["status"] == "pending"
        hdb.close()


def test_prune_keeps_pending_and_still_observed_dismissals():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        s = idb.suggestions
        now = 1000 * DAY
        old = now - 100 * DAY
        pending = s.upsert("edge", "unifi", "k:pending", {}, "fp", now=old)
        gone = s.upsert("edge", "unifi", "k:gone", {}, "fp", now=old)
        s.set_status(gone, "dismissed", now=old)
        watched = s.upsert("edge", "unifi", "k:watched", {}, "fp", now=old)
        s.set_status(watched, "dismissed", now=old)
        s.upsert("edge", "unifi", "k:watched", {}, "fp", now=now - DAY)  # still observed
        assert s.prune_decided(now=now) == 1
        assert s.get(gone) is None
        assert s.get(pending) is not None
        assert s.get(watched)["status"] == "dismissed"
        hdb.close()


def _home_lab(idb):
    """A slice of the real lab, stored the way pre-4.0 Netwatch stored it:
    wifi edges in both directions, 'WAN' as a port, a VM edge drawn
    host -> vm, a duplicate switch port, and a host<->host 'other' edge."""
    ids = {
        "eero": add_device(idb, "Eero Pro 6E — Gateway", "network", port_count=2),
        "basement": add_device(idb, "Eero Pro 6E — Basement AP", "network", port_count=2),
        "usw": add_device(idb, "Ubiquiti Unifi USW Pro Max 16 PoE", "network", port_count=16),
        "prodesk": add_device(idb, "HP Prodesk 405 G6 Mini", "host"),
        "desktop": add_device(idb, "Custom Desktop PC", "host"),
        "xps": add_device(idb, "Dell XPS 17 9700", "host"),
        "pi4": add_device(idb, "Raspberry Pi 4B", "host"),
        "owui": add_device(idb, "OpenWebUI", "vm"),
        "nas": add_device(idb, "Custom NAS", "host"),
    }
    with idb.lock:
        idb.conn.execute("UPDATE inventory SET role = 'Primary Router & Gateway' WHERE id = ?", (ids["eero"],))
        idb.conn.execute("UPDATE inventory SET role = 'Access Point' WHERE id = ?", (ids["basement"],))
        idb.conn.execute("UPDATE inventory SET role = 'Primary Network Switch' WHERE id = ?", (ids["usw"],))
    e = {
        "eero_usw": insert_raw_edge(idb, ids["eero"], ids["usw"], "eth0", "13"),        # reversed
        "prodesk_usw": insert_raw_edge(idb, ids["prodesk"], ids["usw"], "etho0", "8"),  # correct
        "xps_wifi": insert_raw_edge(idb, ids["eero"], ids["xps"], "WAN", None, "wifi"),  # reversed + WAN
        "pi4_wifi": insert_raw_edge(idb, ids["pi4"], ids["eero"], None, None, "wifi"),  # correct
        "basement": insert_raw_edge(idb, ids["basement"], ids["eero"], None, None, "other"),  # correct via role
        "desktop_owui": insert_raw_edge(idb, ids["desktop"], ids["owui"], None, None, "other"),  # reversed
        "nas_dup": insert_raw_edge(idb, ids["nas"], ids["usw"], "eth0", "8"),           # duplicate port 8
        "host_host": insert_raw_edge(idb, ids["desktop"], ids["nas"], None, None, "other"),  # ambiguous
        "bad_port": insert_raw_edge(idb, ids["pi4"], ids["usw"], None, "99"),            # out of range
    }
    return ids, e


def test_lint_edge_codes():
    sw = rec(1, "network", system="USW", port_count=16)
    host = rec(2, "host")
    ports = resolve_ports(sw)
    assert lint_edge({"to_port": "8", "connection_type": "ethernet"}, host, sw, ports) == []
    assert lint_edge({"to_port": "99", "connection_type": "ethernet"}, host, sw, ports) == ["bad_parent_port"]
    assert lint_edge({"to_port": "WAN", "connection_type": "wifi"}, host, sw, ports) == ["port_on_wifi"]
    assert lint_edge({"to_port": None, "connection_type": "wifi"}, host, sw, ports) == []


def test_plan_migration_on_home_lab():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        ids, e = _home_lab(idb)
        records = {r["id"]: r for r in idb.list_all()}
        plan = plan_connections_migration(records, idb.list_all_connections())
        assert plan["network_roles"] == {ids["eero"]: "gateway", ids["basement"]: "ap", ids["usw"]: "switch"}
        assert plan["swaps"] == {e["eero_usw"], e["xps_wifi"], e["desktop_owui"]}
        drift = {key: payload for key, payload in plan["drift"]}
        # eero_usw: after re-orientation the USW is the child and the eero
        # parent port is "eth0", which is not one of the eero's 2 ports.
        assert set(drift) == {
            migration_drift_key(e["eero_usw"]), migration_drift_key(e["xps_wifi"]), migration_drift_key(e["prodesk_usw"]),
            migration_drift_key(e["nas_dup"]), migration_drift_key(e["host_host"]),
            migration_drift_key(e["bad_port"]),
        }
        xps = drift[migration_drift_key(e["xps_wifi"])]
        assert xps["issues"] == ["port_on_wifi"]
        assert (xps["child_id"], xps["parent_id"], xps["parent_port"]) == (ids["xps"], ids["eero"], "WAN")
        assert drift[migration_drift_key(e["nas_dup"])]["issues"] == ["duplicate_parent_port"]
        assert drift[migration_drift_key(e["host_host"])]["issues"] == ["ambiguous_direction"]
        assert drift[migration_drift_key(e["bad_port"])]["issues"] == ["bad_parent_port"]
        assert drift[migration_drift_key(e["eero_usw"])]["issues"] == ["bad_parent_port"]
        assert "99" in drift[migration_drift_key(e["bad_port"])]["message"]
        hdb.close()


def test_plan_migration_keeps_existing_network_role():
    records = {1: rec(1, "network", system="Some Switch", network_role="gateway")}
    assert plan_connections_migration(records, [])["network_roles"] == {}


def test_migrate_applies_plan_backs_up_first_and_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        ids, e = _home_lab(idb)
        calls = []
        assert idb.connections_v2_ready() is False
        ok, msg = idb.migrate_connections_v2(backup_fn=lambda: calls.append("backup"), now=500)
        assert ok and calls == ["backup"]
        assert idb.connections_v2_ready() is True

        eero_usw = idb.get_connection(e["eero_usw"])
        assert (eero_usw["child_id"], eero_usw["parent_id"]) == (ids["usw"], ids["eero"])
        assert (eero_usw["child_port"], eero_usw["parent_port"]) == ("13", "eth0")
        xps = idb.get_connection(e["xps_wifi"])
        assert (xps["child_id"], xps["parent_id"], xps["parent_port"]) == (ids["xps"], ids["eero"], "WAN")
        assert idb.get(ids["eero"])["properties"]["network_role"] == "gateway"
        assert idb.get(ids["eero"])["properties"]["port_count"] == 2  # other props kept
        assert all(c["source"] == "manual" and c["updated_at"] == 500
                   for c in idb.list_all_connections())
        assert idb.suggestions.count_pending() == 6

        # Second run: no backup, no changes.
        before = idb.list_all_connections()
        ok2, msg2 = idb.migrate_connections_v2(backup_fn=lambda: calls.append("again"), now=900)
        assert ok2 and msg2 == "already migrated" and calls == ["backup"]
        assert idb.list_all_connections() == before
        hdb.close()


def test_migrate_aborts_without_changes_when_backup_fails():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        ids, e = _home_lab(idb)
        def boom():
            raise OSError("disk full")
        ok, msg = idb.migrate_connections_v2(backup_fn=boom)
        assert ok is False and msg == "backup failed"
        assert idb.connections_v2_ready() is False
        c = idb.get_connection(e["eero_usw"])
        assert (c["child_id"], c["parent_id"]) == (ids["eero"], ids["usw"])  # untouched
        assert idb.suggestions.count_pending() == 0
        hdb.close()


def test_migrate_skips_dangling_edges():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        a = add_device(idb, "A")
        with idb.lock:
            idb.conn.execute("PRAGMA foreign_keys = OFF")
        insert_raw_edge(idb, 424242, a)
        with idb.lock:
            idb.conn.execute("PRAGMA foreign_keys = ON")
        ok, _ = idb.migrate_connections_v2()
        assert ok and idb.connections_v2_ready()
        hdb.close()


def test_write_pre_migration_backup_writes_private_tarball(tmp_path):
    config = tmp_path / "hosts.yaml"
    config.write_text("hosts: []\n")
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    path = write_pre_migration_backup(str(config), str(auth), "connections-v2")
    assert os.path.dirname(path) == str(tmp_path / "backups")
    assert os.path.basename(path).startswith("pre-connections-v2-")
    assert path.endswith(".tar.gz")
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_migrate_rolls_back_and_reports_failure_on_mid_transaction_error(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        ids, e = _home_lab(idb)

        def boom(*args, **kwargs):
            raise RuntimeError("suggestions store exploded")
        monkeypatch.setattr(idb.suggestions, "upsert_locked", boom)

        ok, msg = idb.migrate_connections_v2(now=500)
        assert (ok, msg) == (False, "migration failed")
        assert idb.connections_v2_ready() is False

        c = idb.get_connection(e["eero_usw"])
        assert (c["child_id"], c["parent_id"]) == (ids["eero"], ids["usw"])  # untouched
        assert "network_role" not in idb.get(ids["eero"])["properties"]
        hdb.close()


def test_migrate_does_not_overwrite_malformed_properties_blob():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        eero = add_device(idb, "Eero Pro 6E — Gateway", "network", port_count=2)
        with idb.lock:
            idb.conn.execute(
                "UPDATE inventory SET role = 'Primary Router & Gateway', properties = ? WHERE id = ?",
                ("{not json", eero))
        ok, msg = idb.migrate_connections_v2()
        assert ok is True
        with idb.lock:
            raw = idb.conn.execute(
                "SELECT properties FROM inventory WHERE id = ?", (eero,)).fetchone()[0]
        assert raw == "{not json"
        hdb.close()


# ── Task 5: order-agnostic create, preview, ports, validated update, drift re-lint ──

def _lab_ready(d):
    hdb, idb = make_idb(d)
    ids, e = _home_lab(idb)
    ok, _ = idb.migrate_connections_v2()
    assert ok
    return hdb, idb, ids, e


def test_preview_orients_and_lists_ports_with_occupancy():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = _lab_ready(d)
        p, err = idb.preview_connection(ids["usw"], ids["desktop"])
        assert err is None
        assert (p["child_id"], p["parent_id"], p["ambiguous"]) == (ids["desktop"], ids["usw"], False)
        assert p["default_type"] == "ethernet"
        port8 = next(x for x in p["ports"] if x["name"] == "8")
        assert {o["device_id"] for o in port8["occupants"]} == {ids["prodesk"], ids["nas"]}
        assert idb.preview_connection(ids["usw"], ids["usw"])[1] == "cannot connect a device to itself"
        assert idb.preview_connection(ids["usw"], 99999)[1] == "one or both devices do not exist"
        hdb.close()


def test_quick_add_orients_validates_ports_and_warns_on_taken_port():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = _lab_ready(d)
        new_id, warnings, err = idb.quick_add_connection(
            {"a_id": ids["usw"], "b_id": ids["desktop"], "parent_port": " 015 "}, now=700)
        assert err is None and warnings == []
        c = idb.get_connection(new_id)
        assert (c["child_id"], c["parent_id"], c["parent_port"]) == (ids["desktop"], ids["usw"], "15")
        assert (c["connection_type"], c["source"], c["updated_at"]) == ("ethernet", "manual", 700)

        _, warnings, err = idb.quick_add_connection(
            {"a_id": ids["pi4"], "b_id": ids["usw"], "parent_port": 15})
        assert err is None and warnings == ["port_in_use"]

        _, _, err = idb.quick_add_connection(
            {"a_id": ids["pi4"], "b_id": ids["usw"], "parent_port": "etho0"})
        assert "not a port on" in err
        assert idb.quick_add_connection({"a_id": "x", "b_id": ids["usw"]})[2] == "a_id and b_id required"
        hdb.close()


def test_quick_add_default_type_and_swap():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = _lab_ready(d)
        vm_edge, _, err = idb.quick_add_connection({"a_id": ids["nas"], "b_id": ids["owui"]})
        c = idb.get_connection(vm_edge)
        assert (c["child_id"], c["parent_id"], c["connection_type"]) == (ids["owui"], ids["nas"], "virtual")

        # host <-> host is ambiguous: kept as given unless swap is passed.
        plain, _, _ = idb.quick_add_connection({"a_id": ids["pi4"], "b_id": ids["xps"], "connection_type": "usb"})
        swapped, _, _ = idb.quick_add_connection({"a_id": ids["pi4"], "b_id": ids["xps"], "connection_type": "usb", "swap": True})
        assert idb.get_connection(plain)["child_id"] == ids["pi4"]
        assert idb.get_connection(swapped)["child_id"] == ids["xps"]
        hdb.close()


def test_legacy_create_from_parent_drawer_is_stored_oriented():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = _lab_ready(d)
        # Today's drawer: opened on the SWITCH, "connect to" the desktop,
        # from_port = switch side, to_port = desktop side.
        new_id, err = idb.create_connection({
            "from_device_id": ids["usw"], "to_device_id": ids["desktop"],
            "from_port": "4", "to_port": "eth0", "connection_type": "ethernet"})
        assert err is None
        c = idb.get_connection(new_id)
        assert (c["child_id"], c["parent_id"]) == (ids["desktop"], ids["usw"])
        assert (c["child_port"], c["parent_port"]) == ("eth0", "4")
        hdb.close()


def test_update_validates_port_converts_sourced_to_manual_and_404s():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = _lab_ready(d)
        with idb.lock:
            idb.conn.execute("UPDATE inventory_connections SET source = 'unifi' WHERE id = ?", (e["prodesk_usw"],))
        ok, err, warnings = idb.update_connection(e["prodesk_usw"], {"parent_port": "SFP+ 9"})
        assert not ok and "not a port on" in err
        ok, err, warnings = idb.update_connection(e["prodesk_usw"], {"parent_port": "6"}, now=800)
        assert ok and err is None and warnings == []
        c = idb.get_connection(e["prodesk_usw"])
        assert (c["parent_port"], c["source"], c["updated_at"]) == ("6", "manual", 800)
        assert idb.update_connection(99999, {"notes": "x"})[1] == "connection not found"
        assert idb.update_connection(e["prodesk_usw"], {})[1] == "no fields to update"
        hdb.close()


def test_fixing_one_duplicate_resolves_both_drifts_and_delete_resolves_its_own():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = _lab_ready(d)
        s = idb.suggestions
        keys = {migration_drift_key(e["prodesk_usw"]), migration_drift_key(e["nas_dup"])}
        assert keys <= {r["subject_key"] for r in s.list()}
        ok, _, _ = idb.update_connection(e["nas_dup"], {"parent_port": "14"})
        assert ok
        pending = {r["subject_key"] for r in s.list()}
        assert not (keys & pending)

        bad = migration_drift_key(e["bad_port"])
        assert bad in pending
        ok, _ = idb.delete_connection(e["bad_port"])
        assert ok
        assert bad not in {r["subject_key"] for r in s.list()}
        assert idb.delete_connection(e["bad_port"]) == (False, "connection not found")
        hdb.close()


def test_ports_for_device_free_text_and_missing():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = _lab_ready(d)
        ports, record = idb.ports_for_device(ids["desktop"])
        assert ports is None and record["id"] == ids["desktop"]
        assert idb.ports_for_device(99999) == (None, None)
        hdb.close()


def test_live_port_provider_overrides_port_count_and_failures_fall_back():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = _lab_ready(d)
        idb.live_port_provider = lambda r: (
            [{"name": "SFP+ 1", "up": True, "speed_mbps": 10000, "poe": None}]
            if r["id"] == ids["usw"] else None)
        ports, _ = idb.ports_for_device(ids["usw"])
        assert [p["name"] for p in ports] == ["SFP+ 1"]
        def broken(_r):
            raise RuntimeError("boom")
        idb.live_port_provider = broken
        ports, _ = idb.ports_for_device(ids["usw"])
        assert len(ports) == 16
        hdb.close()


# ── Task 6: HTTP endpoints — preview, quick add, ports, suggestions, status ──

from netwatch.http_handlers import (
    _h_get_connection_preview, _h_post_connection_quick_add, _h_get_ports,
    _h_get_suggestions, _h_post_suggestion_dismiss, _h_post_connection_update,
    build_api_payload,
)


def test_new_handlers_gate_on_migration_and_missing_db():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)  # not migrated
        assert _h_get_suggestions(idb) == (503, {"error": "migration_pending"})
        assert _h_post_connection_quick_add({}, idb)[0] == 503
        assert _h_get_connection_preview("/api/connections/preview?a=1&b=2", idb)[0] == 503
        assert _h_get_ports("/api/ports/1", idb)[0] == 503
        assert _h_post_suggestion_dismiss("/api/suggestions/1/dismiss", {}, idb)[0] == 503
        assert _h_get_suggestions(None)[0] == 500
        hdb.close()


def test_preview_handler_parses_query():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = _lab_ready(d)
        code, body = _h_get_connection_preview(
            f"/api/connections/preview?a={ids['usw']}&b={ids['xps']}&type=wifi", idb)
        assert code == 200 and body["child_id"] == ids["xps"]
        assert _h_get_connection_preview("/api/connections/preview?a=1", idb)[0] == 400
        assert _h_get_connection_preview("/api/connections/preview?a=x&b=2", idb)[0] == 400
        assert _h_get_connection_preview(f"/api/connections/preview?a={ids['usw']}&b=99999", idb)[0] == 404
        hdb.close()


def test_quick_add_handler():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = _lab_ready(d)
        code, body = _h_post_connection_quick_add(
            {"a_id": ids["usw"], "b_id": ids["xps"], "parent_port": "8"}, idb)
        assert code == 200 and body["ok"] and body["warnings"] == ["port_in_use"]
        code, body = _h_post_connection_quick_add({"a_id": ids["usw"], "b_id": ids["xps"], "parent_port": "zz"}, idb)
        assert code == 400 and "not a port on" in body["error"]
        hdb.close()


def test_ports_handler():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = _lab_ready(d)
        code, body = _h_get_ports(f"/api/ports/{ids['usw']}", idb)
        assert code == 200 and len(body["ports"]) == 16 and body["device_name"].startswith("Ubiquiti")
        assert _h_get_ports(f"/api/ports/{ids['desktop']}", idb)[1]["ports"] is None
        assert _h_get_ports("/api/ports/99999", idb)[0] == 404
        assert _h_get_ports("/api/ports/abc", idb)[0] == 400
        hdb.close()


def test_update_handler_404_and_warnings():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = _lab_ready(d)
        assert _h_post_connection_update("/api/connections/99999", {"notes": "x"}, idb)[0] == 404
        code, body = _h_post_connection_update(f"/api/connections/{e['pi4_wifi']}", {"notes": "x"}, idb)
        assert code == 200 and body == {"ok": True, "warnings": []}
        hdb.close()


def test_suggestions_list_and_dismiss_with_fingerprint():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = _lab_ready(d)
        code, body = _h_get_suggestions(idb)
        assert code == 200 and body["total"] == 6 and body["counts"] == {"drift": 6}
        item = body["items"][0]
        path = f"/api/suggestions/{item['id']}/dismiss"
        assert _h_post_suggestion_dismiss(path, {"fingerprint": "stale"}, idb) == (409, {"error": "suggestion_changed"})
        assert _h_post_suggestion_dismiss(path, {"fingerprint": item["fingerprint"]}, idb) == (200, {"ok": True})
        assert _h_post_suggestion_dismiss(path, {"fingerprint": item["fingerprint"]}, idb)[0] == 409  # no longer pending
        assert _h_post_suggestion_dismiss("/api/suggestions/99999/dismiss", {"fingerprint": "x"}, idb)[0] == 404
        assert _h_post_suggestion_dismiss("/api/suggestions/abc/dismiss", {}, idb)[0] == 400
        assert _h_get_suggestions(idb)[1]["total"] == 5
        hdb.close()


def test_status_payload_counts_pending_suggestions():
    class _HM:
        def list_hosts(self):
            return []
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = _lab_ready(d)
        assert build_api_payload(_HM(), {}, None, idb)["suggestions_pending"] == 6
        assert build_api_payload(_HM(), {})["suggestions_pending"] == 0
        hdb.close()


import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from netwatch.auth import AuthManager
from netwatch.server import make_handler


def _serve_once(idb, auth):
    handler = make_handler(None, {}, "/dev/null", auth_manager=auth, inventory_db=idb)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=server.handle_request)
    t.start()
    return server, server.server_address[1], t


def test_quick_add_post_requires_csrf_and_routes_with_it(tmp_path):
    hdb, idb, ids, e = _lab_ready(str(tmp_path))
    auth = AuthManager(str(tmp_path / "auth.json"))
    auth.create_user("bob", "password123")  # non-admin is enough
    cookie = auth.make_session_cookie("bob")
    body = json.dumps({"a_id": ids["usw"], "b_id": ids["xps"]}).encode()

    server, port, t = _serve_once(idb, auth)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/connections", data=body,
                                     method="POST", headers={"Cookie": f"nw_session={cookie}"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req)
        assert exc.value.code == 403
    finally:
        server.server_close()
        t.join()

    server, port, t = _serve_once(idb, auth)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/connections", data=body, method="POST",
            headers={"Cookie": f"nw_session={cookie}",
                     "X-CSRF-Token": auth.csrf_token_for_cookie(cookie),
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            assert json.loads(r.read())["ok"] is True
    finally:
        server.server_close()
        t.join()
    hdb.close()


def test_suggestions_get_routes(tmp_path):
    hdb, idb, ids, e = _lab_ready(str(tmp_path))
    auth = AuthManager(str(tmp_path / "auth.json"))
    auth.create_user("bob", "password123")
    cookie = auth.make_session_cookie("bob")
    server, port, t = _serve_once(idb, auth)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/suggestions",
                                     headers={"Cookie": f"nw_session={cookie}"})
        with urllib.request.urlopen(req) as r:
            assert json.loads(r.read())["total"] == 6
    finally:
        server.server_close()
        t.join()
    hdb.close()


# ── Final fix wave (whole-branch review) ────────────────────────────────────

from netwatch.storage import MANAGED_PROPERTY_KEYS


def test_update_preserves_managed_properties_not_present_in_incoming_dict():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = _lab_ready(d)
        # Drawer-shaped update: rebuilds `properties` from the per-type field
        # list only, dropping the server-managed network_role.
        ok, err = idb.update(ids["usw"], {
            "ip": "10.0.0.2", "properties": {"port_count": 16, "managed": True}})
        assert ok and err is None
        stored = idb.get(ids["usw"])["properties"]
        assert stored["network_role"] == "switch"
        assert stored["managed"] is True
        assert stored["port_count"] == 16
        # Orientation still keys off network_role: the switch is the AP's
        # parent, not the other way around.
        preview, _ = idb.preview_connection(ids["usw"], ids["basement"])
        assert preview["child_id"] == ids["basement"]
        hdb.close()


def test_update_explicit_managed_property_overwrites():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = _lab_ready(d)
        ok, err = idb.update(ids["usw"], {
            "properties": {"port_count": 16, "network_role": "gateway"}})
        assert ok and err is None
        assert idb.get(ids["usw"])["properties"]["network_role"] == "gateway"
        hdb.close()


# ── delete / replace_all resolve pending migration drift ────────────────────

def test_delete_device_resolves_drift_for_its_edges_and_relints_surviving_parents():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = _lab_ready(d)
        bad_key = migration_drift_key(e["bad_port"])
        wifi_key = migration_drift_key(e["pi4_wifi"])
        pending = {r["subject_key"] for r in idb.suggestions.list()}
        assert bad_key in pending
        ok, err = idb.delete(ids["pi4"])
        assert ok and err is None
        pending_after = {r["subject_key"] for r in idb.suggestions.list()}
        assert bad_key not in pending_after
        assert wifi_key not in pending_after
        hdb.close()


def test_replace_all_resolves_all_pending_migration_drift():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = _lab_ready(d)
        assert idb.suggestions.count_pending() == 6
        ok, fail = idb.replace_all([{"system": "Fresh Box", "device_type": "host"}])
        assert ok == 1 and fail == []
        remaining = [r for r in idb.suggestions.list()
                     if r["subject_key"].startswith("drift:migration:conn:")]
        assert remaining == []
        hdb.close()


# ── update_connection: notes-only edits don't re-validate stored ports ──────

def test_update_connection_notes_only_edit_skips_port_revalidation():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = _lab_ready(d)
        ok, err, warnings = idb.update_connection(e["bad_port"], {"notes": "moved to basement"})
        assert ok and err is None and warnings == []
        assert idb.get_connection(e["bad_port"])["notes"] == "moved to basement"
        hdb.close()


def test_update_connection_notes_only_edit_on_wifi_edge_is_ok():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = _lab_ready(d)
        ok, err, warnings = idb.update_connection(e["xps_wifi"], {"notes": "living room"})
        assert ok and err is None and warnings == []
        hdb.close()


def test_update_connection_clearing_wifi_parent_port_is_allowed_and_resolves_drift():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = _lab_ready(d)
        key = migration_drift_key(e["xps_wifi"])
        assert key in {r["subject_key"] for r in idb.suggestions.list()}
        ok, err, warnings = idb.update_connection(e["xps_wifi"], {"parent_port": ""})
        assert ok and err is None
        c = idb.get_connection(e["xps_wifi"])
        assert c["parent_port"] is None
        assert key not in {r["subject_key"] for r in idb.suggestions.list()}
        hdb.close()


def test_update_connection_setting_parent_port_on_wifi_edge_rejected():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = _lab_ready(d)
        ok, err, warnings = idb.update_connection(e["xps_wifi"], {"parent_port": "3"})
        assert not ok
        assert err == "wifi connections don't use a parent port"
        hdb.close()


# ── hosts.yaml backup rotation must not touch other backups/ tarballs ──────

from netwatch.hosts import save_hosts_config


def test_save_hosts_config_rotation_ignores_non_hosts_backups(tmp_path):
    config_path = str(tmp_path / "hosts.yaml")
    with open(config_path, "w") as f:
        f.write("hosts: []\n")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    for i in range(12):
        (backup_dir / f"hosts-2026010{i:02d}-000000.yaml").write_text("hosts: []\n")
    tarball = backup_dir / "pre-connections-v2-20260101-000000.tar.gz"
    tarball.write_bytes(b"fake tarball")
    save_hosts_config(config_path, [])
    assert tarball.exists()
    remaining_hosts_backups = sorted(p.name for p in backup_dir.glob("hosts-*.yaml"))
    # 12 pre-existing + 1 just written by save_hosts_config = 13; rotation
    # keeps the newest 10 and never touches the tarball.
    assert len(remaining_hosts_backups) == 10
    assert tarball.exists()


# ── swap only applies to ambiguous pairs (item 6) ───────────────────────────

def test_quick_add_swap_ignored_when_orientation_is_not_ambiguous():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = _lab_ready(d)
        new_id, warnings, err = idb.quick_add_connection(
            {"a_id": ids["nas"], "b_id": ids["owui"], "swap": True})
        assert err is None
        assert idb.get_connection(new_id)["child_id"] == ids["owui"]
        hdb.close()


def test_update_connection_swap_rejected_when_not_ambiguous():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = _lab_ready(d)
        ok, err, warnings = idb.update_connection(e["prodesk_usw"], {"swap": True})
        assert not ok
        assert err == "orientation is decided by device types; swap only applies to ambiguous pairs"
        hdb.close()


def test_update_connection_swap_still_applies_when_ambiguous():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = _lab_ready(d)
        ok, err, warnings = idb.update_connection(e["host_host"], {"swap": True})
        assert ok and err is None
        c = idb.get_connection(e["host_host"])
        assert c["child_id"] == ids["nas"] and c["parent_id"] == ids["desktop"]
        hdb.close()


# ── non-string notes/ports don't crash (item 7) ─────────────────────────────

def test_quick_add_connection_numeric_notes_are_stringified():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = _lab_ready(d)
        new_id, warnings, err = idb.quick_add_connection(
            {"a_id": ids["usw"], "b_id": ids["desktop"], "parent_port": "1", "notes": 42})
        assert err is None
        assert idb.get_connection(new_id)["notes"] == "42"
        hdb.close()


def test_legacy_create_connection_numeric_port_does_not_raise():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = _lab_ready(d)
        new_id, err = idb.create_connection({
            "from_device_id": ids["usw"], "to_device_id": ids["desktop"],
            "from_port": "4", "to_port": 4, "connection_type": "ethernet"})
        assert err is None
        c = idb.get_connection(new_id)
        assert c["child_port"] == "4"
        hdb.close()
