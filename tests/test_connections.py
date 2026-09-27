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
