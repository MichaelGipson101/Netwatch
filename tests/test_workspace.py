"""Tests for Netwatch 4.0 connections rework, plan 3 (Connections workspace)."""
import json
import os
import shutil
import subprocess
import tempfile
import types

import pytest

from netwatch.connections import migration_drift_key
from netwatch.storage import HistoryDB, InventoryDB

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(REPO, "static")
DASHBOARD = os.path.join(REPO, "dashboard.html")


def make_idb(tmpdir):
    hdb = HistoryDB(os.path.join(tmpdir, "ws_test.db"))
    return hdb, InventoryDB(hdb)


def add_device(idb, system, device_type="host", mac=None, **props):
    data = {"system": system, "device_type": device_type, "properties": props or None}
    if mac:
        data["mac"] = mac
    new_id, err = idb.create(data)
    assert err is None, err
    return new_id


def insert_edge(idb, child, parent, to_port=None, ctype="ethernet", from_port=None):
    with idb.lock:
        return idb.conn.execute(
            "INSERT INTO inventory_connections (from_device_id, to_device_id, from_port, "
            "to_port, connection_type, created_at) VALUES (?, ?, ?, ?, ?, 0)",
            (child, parent, from_port, to_port, ctype)).lastrowid


def pending_keys(idb):
    return {s["subject_key"] for s in idb.suggestions.list()}


# ── Task 1: storage carry-forwards ──────────────────────────────────────────

def test_port_occupancy_ignores_wifi_edges():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        sw = add_device(idb, "Switch", "network", network_role="switch", port_count=8)
        wired = add_device(idb, "Wired")
        radio = add_device(idb, "Radio")
        insert_edge(idb, wired, sw, to_port="2")
        insert_edge(idb, radio, sw, to_port="3", ctype="wifi")  # legacy bad data
        ports, _ = idb.ports_for_device(sw)
        occ = {p["name"]: [o["device_id"] for o in p["occupants"]] for p in ports}
        assert occ["2"] == [wired] and occ["3"] == []
        hdb.close()


def test_changing_type_to_wifi_clears_the_parent_port():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        sw = add_device(idb, "Switch", "network", network_role="switch", port_count=8)
        h = add_device(idb, "Laptop")
        cid = insert_edge(idb, h, sw, to_port="2")
        ok, err, warnings = idb.update_connection(cid, {"connection_type": "wifi"})
        assert ok and err is None and warnings == ["parent_port_cleared"]
        c = idb.get_connection(cid)
        assert c["parent_port"] is None and c["connection_type"] == "wifi"
        ok, err, _ = idb.update_connection(cid, {"parent_port": "4"})
        assert not ok and "wifi" in err
        hdb.close()


def test_explicit_swap_settles_ambiguous_direction_drift():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        a, b = add_device(idb, "Box A"), add_device(idb, "Box B")
        cid = insert_edge(idb, a, b)
        assert idb.migrate_connections_v2()[0]
        assert migration_drift_key(cid) in pending_keys(idb)
        ok, err, _ = idb.update_connection(cid, {"swap": True})
        assert ok, err
        assert migration_drift_key(cid) not in pending_keys(idb)
        hdb.close()


def test_swap_keeps_drift_while_another_issue_remains():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        a = add_device(idb, "Box A", port_count=2)
        b = add_device(idb, "Box B", port_count=2)
        guest = add_device(idb, "Guest", "vm")
        cid = insert_edge(idb, a, b, to_port="1", from_port="2")   # ambiguous (host-host)
        insert_edge(idb, guest, a, to_port="2", ctype="virtual")    # already on Box A port 2
        assert idb.migrate_connections_v2()[0]
        ok, err, warnings = idb.update_connection(cid, {"swap": True})
        assert ok, err
        assert warnings == ["port_in_use"]
        # After the swap Box A is the parent on port "2", which the guest
        # already uses - the direction is settled but the duplicate isn't.
        assert migration_drift_key(cid) in pending_keys(idb)
        hdb.close()


def test_raising_port_count_resolves_bad_port_drift():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        sw = add_device(idb, "Switch", "network", network_role="switch", port_count=4)
        h = add_device(idb, "Host")
        cid = insert_edge(idb, h, sw, to_port="6")
        assert idb.migrate_connections_v2()[0]
        assert migration_drift_key(cid) in pending_keys(idb)
        ok, err = idb.update(sw, {"properties": {"port_count": 8}})
        assert ok, err
        assert migration_drift_key(cid) not in pending_keys(idb)
        assert idb.get(sw)["properties"]["network_role"] == "switch"
        hdb.close()


def test_changing_child_type_resolves_ambiguous_drift():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        guest, host = add_device(idb, "Guest"), add_device(idb, "Hypervisor")
        cid = insert_edge(idb, guest, host)
        assert idb.migrate_connections_v2()[0]
        assert migration_drift_key(cid) in pending_keys(idb)
        ok, err = idb.update(guest, {"device_type": "vm"})
        assert ok, err
        assert migration_drift_key(cid) not in pending_keys(idb)
        hdb.close()


def test_relint_failure_does_not_fail_a_record_edit(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        h = add_device(idb, "Host")

        def boom(*a, **k):
            raise RuntimeError("relint exploded")
        monkeypatch.setattr(idb, "relint_parent", boom)
        assert idb.update(h, {"notes": "hello"}) == (True, None)
        assert idb.get(h)["notes"] == "hello"
        hdb.close()
