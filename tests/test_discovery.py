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


# ── Task 2: UniFi adapter ────────────────────────────────────────────────────

from netwatch.discovery import (
    PROXMOX_OUI, is_likely_guest_mac, parse_unifi, unifi_observations,
)

EERO_MAC = "d4:3f:32:eb:2a:f2"
EERO_LLDP_MAC = "d4:3f:32:eb:2a:e0"
PI5_MAC = "d8:3a:dd:ad:2d:b7"
VF2_MAC = "6c:cf:39:00:ab:f2"
WORKBENCH_MAC = "7c:83:34:bb:13:12"
SHARED_A, SHARED_B = "a8:b1:3b:f8:1f:dc", "a8:b1:3b:f8:1f:dd"
GUEST_MC, GUEST_HA = "bc:24:11:9e:39:2e", "02:ac:a5:69:78:a5"
NODE8, NODE11 = "6c:02:e0:98:df:1d", "10:e7:c6:08:2e:39"


def unifi_device_payload():
    """Shaped like the live controller's classic stat/device (UniFi 10.6)."""
    table = [{"port_idx": i, "name": f"Port {i}", "up": i in (4, 5, 7, 8, 9, 10, 11, 13),
              "speed": 2500 if i == 13 else (1000 if i != 12 else 0),
              "poe_enable": i != 13, "is_uplink": i == 13, "media": "GE"}
             for i in range(16, 0, -1)]  # deliberately unsorted
    table += [{"port_idx": 17, "name": "SFP+ 1", "up": False, "speed": 0, "poe_enable": False},
              {"port_idx": 18, "name": "SFP+ 2", "up": False, "speed": 0, "poe_enable": False}]
    return {"meta": {"rc": "ok"}, "data": [
        {"type": "usw", "mac": USW_MAC.upper(), "name": "USW Pro Max 16 PoE",
         "model": "USPM16P", "ip": "192.168.6.2", "port_table": table,
         "lldp_table": [{"chassis_id": EERO_LLDP_MAC, "local_port_idx": 13,
                         "local_port_name": "Port 13", "port_id": "1", "is_wired": True,
                         "mgmt_ips": ["fd10:9dd9:eb90:1::1", "192.168.4.1"]}]},
        {"type": "uap", "mac": "aa:bb:cc:00:00:01", "name": "Some AP", "port_table": []},
    ]}


def _sta(mac, port, hostname=None, ip=None, wired=True, sw=USW_MAC):
    return {"mac": mac, "ip": ip, "hostname": hostname, "name": None, "is_wired": wired,
            "sw_mac": sw, "sw_port": port, "last_seen": NOW - 60}


def unifi_clients_payload():
    return {"meta": {"rc": "ok"}, "data": [
        _sta(PI5_MAC, 7, "ApplePi5", "192.168.6.90"),
        _sta(VF2_MAC, 4, "vf2", "192.168.7.17"),
        _sta(WORKBENCH_MAC.upper(), 10, "WORKBENCH-PC", "192.168.6.45"),
        _sta(NODE8, 8, None, "192.168.6.219"),
        _sta(GUEST_MC, 8, "Minecraft", "192.168.6.220"),
        _sta(GUEST_HA, 11, "homeassistant", "192.168.5.110"),
        _sta(NODE11, 11, None, "192.168.4.237"),
        _sta(SHARED_A, 9, None, "192.168.6.166"),
        _sta(SHARED_B, 9, None, "192.168.6.167"),
        _sta("11:22:33:44:55:66", None, "wifi-thing", "192.168.4.50", wired=False, sw=None),
        _sta("20:00:00:00:00:01", 12, "behind-other-switch", sw="99:99:99:99:99:99"),
        _sta("20:00:00:00:00:02", "n/a", "garbage-port"),
    ]}


def snapshot():
    return parse_unifi(unifi_device_payload(), unifi_clients_payload())


@pytest.mark.parametrize("mac,expected", [
    (GUEST_MC, True), (GUEST_HA, True), ("BC:24:11:00:00:01", True),
    (PI5_MAC, False), (NODE8, False), ("nonsense", False), (None, False),
])
def test_is_likely_guest_mac(mac, expected):
    assert is_likely_guest_mac(mac) is expected


def test_parse_unifi_switches_ports_and_lldp():
    snap = snapshot()
    [sw] = snap["switches"]  # the UAP is ignored
    assert (sw["mac"], sw["name"], sw["ip"]) == (USW_MAC, "USW Pro Max 16 PoE", "192.168.6.2")
    assert [p["idx"] for p in sw["ports"]] == list(range(1, 19))
    p13 = sw["ports"][12]
    assert p13 == {"name": "Port 13", "idx": 13, "up": True, "speed_mbps": 2500,
                   "poe": False, "is_uplink": True}
    assert sw["ports"][11]["speed_mbps"] is None  # speed 0 -> None
    assert sw["lldp"] == [{"local_port_idx": 13, "local_port_name": "Port 13",
                           "chassis_mac": EERO_LLDP_MAC,
                           "mgmt_ips": ["fd10:9dd9:eb90:1::1", "192.168.4.1"],
                           "remote_port": "1"}]


def test_parse_unifi_clients_keep_only_wired_switch_ports():
    macs = {c["mac"] for c in snapshot()["clients"]}
    assert WORKBENCH_MAC in macs  # upper-case input normalised
    assert "11:22:33:44:55:66" not in macs  # wireless
    assert "20:00:00:00:00:02" not in macs  # non-integer port
    assert "20:00:00:00:00:01" in macs  # kept here; unknown switch filtered later
    pi = next(c for c in snapshot()["clients"] if c["mac"] == PI5_MAC)
    assert pi == {"mac": PI5_MAC, "ip": "192.168.6.90", "name": "ApplePi5",
                  "sw_mac": USW_MAC, "sw_port": 7, "last_seen": NOW - 60}


def test_parse_unifi_tolerates_empty_payloads():
    assert parse_unifi({}, None) == {"switches": [], "clients": []}


def test_observations_hold_guest_ports_without_proxmox_data():
    obs = unifi_observations(snapshot())
    held = [o for o in obs if o["type"] == "held"]
    assert sorted(tuple(o["macs"]) for o in held) == sorted([
        tuple(sorted([NODE8, GUEST_MC])), tuple(sorted([GUEST_HA, NODE11]))])
    edges = {o["child"]["mac"]: o for o in obs if o["type"] == "edge"}
    assert set(edges) == {PI5_MAC, VF2_MAC, WORKBENCH_MAC}
    assert edges[PI5_MAC] == {
        "type": "edge", "source": "unifi",
        "child": {"mac": PI5_MAC, "ip": "192.168.6.90", "name": "ApplePi5"},
        "parent": {"mac": USW_MAC}, "parent_port": "Port 7", "child_port": None,
        "connection_type": "ethernet", "external_key": f"unifi:port:{PI5_MAC}"}
    [shared] = [o for o in obs if o["type"] == "shared_port"]
    assert (shared["switch_mac"], shared["port"], shared["macs"]) == (USW_MAC, "Port 9", [SHARED_A, SHARED_B])
    [lldp] = [o for o in obs if o["type"] == "lldp"]
    assert (lldp["switch_mac"], lldp["port"], lldp["chassis_mac"], lldp["remote_port"]) == (
        USW_MAC, "Port 13", EERO_LLDP_MAC, "1")
    assert not any(o.get("child", {}).get("mac") == "20:00:00:00:00:01" for o in obs)


def test_observations_with_guest_set_drop_guests_and_keep_nodes():
    obs = unifi_observations(snapshot(), guest_macs={GUEST_MC, GUEST_HA})
    assert not [o for o in obs if o["type"] == "held"]
    edges = {o["child"]["mac"]: o["parent_port"] for o in obs if o["type"] == "edge"}
    assert edges[NODE8] == "Port 8" and edges[NODE11] == "Port 11"
    assert GUEST_MC not in edges and GUEST_HA not in edges


def test_parse_unifi_skips_malformed_rows():
    """A malformed row anywhere in the payload is skipped, not fatal (review
    fix round 1): non-dict entries in devices/clients `data`, non-dict
    entries in `port_table`/`lldp_table`, and port_table rows whose
    `port_idx` isn't a real int."""
    devices_payload = {"data": [
        "junk",
        {"type": "usw", "mac": USW_MAC, "name": "SW1", "ip": "192.168.1.1",
         "port_table": [
             {"port_idx": 2, "name": "Port 2", "up": True, "speed": 1000, "poe_enable": True},
             {"port_idx": "weird", "name": "X"},
             {"port_idx": None},
             "junk",
             {"port_idx": 1, "name": "Port 1", "up": False, "speed": 0, "poe_enable": False},
         ],
         "lldp_table": ["junk", {"chassis_id": EERO_LLDP_MAC, "local_port_idx": 1,
                                 "local_port_name": "Port 1", "port_id": "1"}]},
    ]}
    clients_payload = {"data": ["junk", _sta(PI5_MAC, 1)]}

    snap = parse_unifi(devices_payload, clients_payload)  # must not raise

    [sw] = snap["switches"]
    assert [p["idx"] for p in sw["ports"]] == [1, 2]  # weird/None/junk rows dropped, sorted
    assert sw["lldp"] == [{"local_port_idx": 1, "local_port_name": "Port 1",
                           "chassis_mac": EERO_LLDP_MAC, "mgmt_ips": [], "remote_port": "1"}]
    assert {c["mac"] for c in snap["clients"]} == {PI5_MAC}
