"""Tests for Netwatch 4.0 connections rework, plan 2 (discovery + UniFi)."""
import json
import os
import tempfile
import threading
import types

import pytest

from netwatch.connections import canonical_port, validate_parent_port, resolve_ports
from netwatch.storage import HistoryDB, InventoryDB

NOW = 1_800_000_000
DAY = 86400
USW_MAC = "74:fa:29:1d:a3:dc"


def live_usw_ports():
    ports = [{"name": f"Port {i}", "idx": i, "up": True, "speed_mbps": 1000, "poe": True}
             for i in range(1, 17)]
    ports += [{"name": "SFP+ 1", "idx": 17, "up": False, "speed_mbps": None, "poe": False},
              {"name": "SFP+ 2", "idx": 18, "up": False, "speed_mbps": None, "poe": False}]
    return ports


def make_idb(tmpdir):
    hdb = HistoryDB(os.path.join(tmpdir, "disc_test.db"))
    return hdb, InventoryDB(hdb)


def add_device(idb, system, device_type="host", mac=None, ip=None, **props):
    data = {"system": system, "device_type": device_type, "properties": props or None}
    if mac:
        data["mac"] = mac
    if ip:
        data["ip"] = ip
    new_id, err = idb.create(data)
    assert err is None, err
    return new_id


def insert_edge(idb, child, parent, to_port=None, ctype="ethernet", source="manual",
                from_port=None, last_seen=None, external_key=None):
    with idb.lock:
        cur = idb.conn.execute(
            "INSERT INTO inventory_connections (from_device_id, to_device_id, from_port, "
            "to_port, connection_type, created_at, source, last_seen, external_key) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)",
            (child, parent, from_port, to_port, ctype, source, last_seen, external_key))
        return cur.lastrowid


# ── Task 1: canonical ports ─────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("Port 8", "Port 8"), ("8", "Port 8"), (" 08 ", "Port 8"), (8, "Port 8"),
    ("port 8", "Port 8"), ("17", "SFP+ 1"), ("sfp+ 2", "SFP+ 2"),
    ("eth0", "eth0"), ("99", "99"), (None, None), ("", None),
])
def test_canonical_port_against_live_ports(raw, expected):
    assert canonical_port(raw, live_usw_ports()) == expected


def test_canonical_port_against_count_ports_and_free_text():
    count_ports = resolve_ports({"properties": {"port_count": 16}})
    assert canonical_port("08", count_ports) == "8"
    assert canonical_port("Port 8", count_ports) == "Port 8"  # unknown name passes through
    assert canonical_port(" x ", None) == "x"


def test_validate_parent_port_accepts_bare_index_for_named_live_ports():
    sw = {"system": "USW", "properties": {}}
    assert validate_parent_port(sw, "8", live_usw_ports()) is None
    assert validate_parent_port(sw, "Port 8", live_usw_ports()) is None
    assert "not a port on USW" in validate_parent_port(sw, "19", live_usw_ports())


def test_storage_treats_8_and_port_8_as_the_same_port():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        usw = add_device(idb, "USW", "network", mac=USW_MAC, network_role="switch", port_count=16)
        old = add_device(idb, "OldBox", "host")
        new = add_device(idb, "NewBox", "host")
        insert_edge(idb, old, usw, to_port="8")  # plan-1 era manual edge
        idb.live_port_provider = lambda r: live_usw_ports() if r["id"] == usw else None
        ports, _ = idb.ports_for_device(usw)
        port8 = next(p for p in ports if p["name"] == "Port 8")
        assert [o["device_id"] for o in port8["occupants"]] == [old]

        new_id, warnings, err = idb.quick_add_connection({"a_id": new, "b_id": usw, "parent_port": "8"})
        assert err is None and warnings == ["port_in_use"]
        assert idb.get_connection(new_id)["parent_port"] == "Port 8"

        ok, err, _ = idb.update_connection(new_id, {"parent_port": "17"})
        assert ok and idb.get_connection(new_id)["parent_port"] == "SFP+ 1"
        hdb.close()
