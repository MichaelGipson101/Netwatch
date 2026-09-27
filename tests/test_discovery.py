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


# ── Task 3: reconciler ───────────────────────────────────────────────────────

from netwatch.discovery import reconcile, STALE_AFTER_SECONDS


def rec(id_, system, device_type="host", mac=None, ip=None, **props):
    return {"id": id_, "system": system, "device_type": device_type, "mac": mac,
            "ip": ip, "role": None, "properties": dict(props)}


def edge(id_, child, parent, to_port=None, ctype="ethernet", source="manual",
         from_port=None, last_seen=None):
    return {"id": id_, "from_device_id": child, "to_device_id": parent,
            "from_port": from_port, "to_port": to_port, "connection_type": ctype,
            "source": source, "external_key": None, "last_seen": last_seen,
            "updated_at": None}


def lab_records():
    return [
        rec(1, "USW Pro Max 16 PoE", "network", USW_MAC, network_role="switch", port_count=16),
        rec(2, "Eero Pro 6E — Gateway", "network", EERO_MAC, "192.168.4.1",
            network_role="gateway", port_count=2),
        rec(3, "Raspberry Pi 5", "host", PI5_MAC),
        rec(4, "Mystery printer", "printer", SHARED_A),
        rec(5, "Old NAS", "host", "00:11:22:33:44:55"),
        rec(6, "VisionFive 2", "host", VF2_MAC),
    ]


def lab_edges():
    return [
        edge(10, 1, 2, to_port="eth0", from_port="13"),        # USW -> eero, bad parent port
        edge(11, 3, 2, ctype="wifi"),                          # Pi 5 drawn on wifi
        edge(12, 5, 1, to_port="Port 3", source="unifi", last_seen=NOW - 8 * DAY),
    ]


def live_for(records_by_mac=USW_MAC):
    ports = snapshot()["switches"][0]["ports"]
    return lambda r: [dict(p) for p in ports] if r.get("mac") == records_by_mac else None


def run(records=None, edges=None, pending=(), healthy=("unifi",), guest_macs=None,
        healthy_since={"unifi": NOW - 30 * DAY}):
    return reconcile(unifi_observations(snapshot(), guest_macs),
                     records=records if records is not None else lab_records(),
                     edges=edges if edges is not None else lab_edges(),
                     pending=list(pending), healthy_sources=set(healthy), now=NOW,
                     live_ports_for=live_for(), healthy_since=healthy_since)


def by_key(changes):
    return {u["subject_key"]: u for u in changes["upserts"]}


def test_reconcile_produces_the_expected_suggestions():
    ch = by_key(run())
    assert set(ch) == {
        f"drift:unifi:{PI5_MAC}", f"edge:unifi:{VF2_MAC}", f"device:unifi:{WORKBENCH_MAC}",
        f"shared_port:{USW_MAC}:Port 9", f"identity:lldp:{EERO_LLDP_MAC}",
        f"drift:unifi:{USW_MAC}", "drift:stale:conn:12",
    }
    assert all(u["source"] == "unifi" and len(u["fp"]) == 16 for u in ch.values())


def test_reconcile_pi5_wifi_edge_becomes_replace_drift():
    u = by_key(run())[f"drift:unifi:{PI5_MAC}"]
    p = u["payload"]
    assert u["kind"] == "drift" and p["action"] == "replace" and p["connection_id"] == 11
    assert p["current"] == {"parent_id": 2, "parent_name": "Eero Pro 6E — Gateway",
                            "parent_port": None, "connection_type": "wifi"}
    assert (p["proposed"]["parent_id"], p["proposed"]["parent_port"],
            p["proposed"]["connection_type"], p["proposed"]["external_key"]) == (
        1, "Port 7", "ethernet", f"unifi:port:{PI5_MAC}")
    assert "UniFi" in p["message"]


def test_reconcile_new_edge_and_new_device_payloads():
    ch = by_key(run())
    e = ch[f"edge:unifi:{VF2_MAC}"]["payload"]
    assert (e["child_id"], e["parent_id"], e["parent_port"], e["source"]) == (6, 1, "Port 4", "unifi")
    d = ch[f"device:unifi:{WORKBENCH_MAC}"]["payload"]
    assert d["device"] == {"system": "WORKBENCH-PC", "mac": WORKBENCH_MAC, "ip": "192.168.6.45",
                           "device_type": "host", "category": None}
    assert (d["edge"]["parent_id"], d["edge"]["parent_port"]) == (1, "Port 10")


def test_reconcile_lldp_matches_eero_by_ip_and_flags_port_drift():
    ch = by_key(run())
    ident = ch[f"identity:lldp:{EERO_LLDP_MAC}"]["payload"]
    assert (ident["candidate_id"], ident["switch_id"], ident["port"]) == (2, 1, "Port 13")
    d = ch[f"drift:unifi:{USW_MAC}"]["payload"]
    assert d["connection_id"] == 10
    assert (d["proposed"]["parent_id"], d["proposed"]["parent_port"],
            d["proposed"]["child_port"]) == (2, "1", "Port 13")


def test_reconcile_lldp_by_alias_needs_no_identity():
    records = lab_records()
    records[1]["properties"]["mac_aliases"] = [EERO_LLDP_MAC]
    assert f"identity:lldp:{EERO_LLDP_MAC}" not in by_key(run(records=records))


def test_reconcile_unknown_lldp_neighbour_asks_which_device():
    records = lab_records()
    records[1]["ip"] = "10.0.0.1"  # no longer matches mgmt_ips
    u = by_key(run(records=records))[f"identity:lldp:{EERO_LLDP_MAC}"]["payload"]
    assert u["candidate_id"] is None and u["mgmt_ips"][-1] == "192.168.4.1"


def test_reconcile_shared_port_lists_matched_devices():
    p = by_key(run())[f"shared_port:{USW_MAC}:Port 9"]["payload"]
    assert p["macs"] == [SHARED_A, SHARED_B]
    assert p["matched"] == [{"id": 4, "name": "Mystery printer", "mac": SHARED_A}]


def test_reconcile_touches_matching_edges_and_refreshes_sourced_ports():
    edges = lab_edges() + [
        edge(13, 6, 1, to_port="4"),                               # manual "4" == "Port 4"
        edge(14, 3, 1, to_port="Port 5", source="unifi"),          # sourced, re-cabled to 7
    ]
    edges = [e for e in edges if e["id"] != 11]
    ch = run(edges=edges)
    touch = {t["id"]: t["parent_port"] for t in ch["touch"]}
    assert touch[13] is None and touch[14] == "Port 7" and touch[10] is None
    keys = set(by_key(ch))
    assert f"edge:unifi:{VF2_MAC}" not in keys and f"drift:unifi:{VF2_MAC}" not in keys
    assert f"drift:unifi:{PI5_MAC}" not in keys


def test_reconcile_manual_edge_on_other_port_is_drift_not_touch_of_port():
    edges = [e for e in lab_edges() if e["id"] != 11] + [edge(13, 6, 1, to_port="12")]
    ch = run(edges=edges)
    assert {t["id"]: t["parent_port"] for t in ch["touch"]}[13] is None
    p = by_key(ch)[f"drift:unifi:{VF2_MAC}"]["payload"]
    assert (p["current"]["parent_port"], p["proposed"]["parent_port"]) == ("12", "Port 4")


def test_reconcile_stale_edges_only_when_old_and_not_held():
    p = by_key(run())["drift:stale:conn:12"]["payload"]
    assert p["action"] == "remove" and p["connection_id"] == 12 and p["last_seen"] == NOW - 8 * DAY
    fresh = [e if e["id"] != 12 else dict(e, last_seen=NOW - DAY) for e in lab_edges()]
    assert "drift:stale:conn:12" not in by_key(run(edges=fresh))
    records = lab_records() + [rec(7, "Minecraft", "vm", GUEST_MC)]
    held_edge = lab_edges() + [edge(15, 7, 1, to_port="Port 8", source="unifi", last_seen=NOW - 30 * DAY)]
    assert "drift:stale:conn:15" not in by_key(run(records=records, edges=held_edge))


def test_reconcile_resolution_rules():
    pending = [
        {"subject_key": "edge:unifi:ff:ff:ff:ff:ff:01", "source": "unifi"},
        {"subject_key": f"edge:unifi:{GUEST_MC}", "source": "unifi"},       # held
        {"subject_key": "drift:migration:conn:10", "source": "migration"},
        {"subject_key": f"drift:unifi:{PI5_MAC}", "source": "unifi"},      # re-produced
    ]
    assert run(pending=pending)["resolve"] == ["edge:unifi:ff:ff:ff:ff:ff:01"]
    unhealthy = reconcile([], records=lab_records(), edges=lab_edges(), pending=pending,
                          healthy_sources=set(), now=NOW)
    assert unhealthy == {"upserts": [], "resolve": [], "touch": []}


def test_reconcile_fingerprint_ignores_renames():
    a = by_key(run())[f"edge:unifi:{VF2_MAC}"]["fp"]
    records = lab_records()
    records[5]["system"] = "VF2 renamed"
    assert by_key(run(records=records))[f"edge:unifi:{VF2_MAC}"]["fp"] == a


def test_reconcile_ignores_switch_missing_from_inventory():
    records = [r for r in lab_records() if r["id"] != 1]
    edges = [e for e in lab_edges() if 1 not in (e["from_device_id"], e["to_device_id"])]
    assert run(records=records, edges=edges)["upserts"] == []


# ── Task 3 fix round 1: F1 held ports/aliases, F2 LLDP dedup + ordering, ────
# ── F3 healthy-streak staleness ─────────────────────────────────────────────

def test_reconcile_held_port_and_stale_key_are_protected_from_resolution():
    """F1: a pending shared_port suggestion for a now-held port, and a pending
    stale-drift suggestion whose child is a held MAC, must not be resolved
    just because this scan didn't reproduce them."""
    records = lab_records() + [rec(7, "Minecraft", "vm", GUEST_MC)]
    edges = lab_edges() + [edge(15, 7, 1, to_port="Port 8", source="unifi",
                                last_seen=NOW - 30 * DAY)]
    pending = [
        {"subject_key": f"shared_port:{USW_MAC}:Port 8", "source": "unifi"},  # port 8 is held
        {"subject_key": "drift:stale:conn:15", "source": "unifi"},           # child is held
    ]
    assert run(records=records, edges=edges, pending=pending)["resolve"] == []


def test_reconcile_stale_respects_held_mac_only_present_as_alias():
    """F1: a child counts as held via properties.mac_aliases too, not just its
    primary mac."""
    records = lab_records() + [rec(8, "Aliased NAS", "host", "aa:aa:aa:aa:aa:aa",
                                    mac_aliases=[GUEST_MC])]
    edges = lab_edges() + [edge(16, 8, 1, to_port="Port 6", source="unifi",
                                last_seen=NOW - 30 * DAY)]
    assert "drift:stale:conn:16" not in by_key(run(records=records, edges=edges))


AP_IFACE_MAC, AP_CHASSIS_MAC = "10:11:22:33:44:01", "10:11:22:33:44:02"


def ap_dual_path_snapshot():
    """An AP wired to USW Port 3 (interface mac, client path) that is also
    the LLDP neighbour on that same port under a different (chassis) mac."""
    devices = {"data": [
        {"type": "usw", "mac": USW_MAC.upper(), "name": "USW Pro Max 16 PoE",
         "ip": "192.168.6.2",
         "port_table": [{"port_idx": 3, "name": "Port 3", "up": True, "speed": 1000,
                         "poe_enable": True, "is_uplink": False}],
         "lldp_table": [{"chassis_id": AP_CHASSIS_MAC, "local_port_idx": 3,
                         "local_port_name": "Port 3", "port_id": "1"}]},
    ]}
    clients = {"data": [_sta(AP_IFACE_MAC, 3, "Lobby AP", "192.168.6.50")]}
    return parse_unifi(devices, clients)


def test_reconcile_lldp_skips_neighbour_already_covered_by_client_path():
    """F2(a): the client path already covers this physical link (same
    inventory record, matched by alias) - the LLDP path must not duplicate
    it with a second suggestion keyed off the chassis mac."""
    records = [
        rec(1, "USW Pro Max 16 PoE", "network", USW_MAC, network_role="switch"),
        rec(2, "Lobby AP", "network", AP_IFACE_MAC, network_role="ap",
            mac_aliases=[AP_CHASSIS_MAC]),
    ]
    obs = unifi_observations(ap_dual_path_snapshot())
    ch = by_key(reconcile(obs, records=records, edges=[], pending=[],
                          healthy_sources={"unifi"}, now=NOW))
    assert f"edge:unifi:{AP_IFACE_MAC}" in ch
    assert f"edge:unifi:{AP_CHASSIS_MAC}" not in ch
    assert f"drift:unifi:{AP_CHASSIS_MAC}" not in ch
    assert f"identity:lldp:{AP_CHASSIS_MAC}" not in ch


SWITCH2_MAC = "10:30:30:30:30:02"


def two_equal_switches_snapshot():
    """Two equal-role (network_role='switch') devices linked by LLDP on a
    non-uplink USW port."""
    devices = {"data": [
        {"type": "usw", "mac": USW_MAC.upper(), "name": "USW Pro Max 16 PoE",
         "ip": "192.168.6.2",
         "port_table": [{"port_idx": 5, "name": "Port 5", "up": True, "speed": 1000,
                         "poe_enable": True, "is_uplink": False}],
         "lldp_table": [{"chassis_id": SWITCH2_MAC, "local_port_idx": 5,
                         "local_port_name": "Port 5", "port_id": "1"}]},
    ]}
    return parse_unifi(devices, {"data": []})


def test_reconcile_lldp_equal_role_switches_use_uplink_tiebreak():
    """F2(b): ambiguous orientation (switch vs switch) on a non-uplink port
    resolves to the neighbour being the child, not USW's parent."""
    records = [
        rec(1, "USW Pro Max 16 PoE", "network", USW_MAC, network_role="switch"),
        rec(2, "Downstream switch", "network", SWITCH2_MAC, network_role="switch"),
    ]
    obs = unifi_observations(two_equal_switches_snapshot())
    ch = by_key(reconcile(obs, records=records, edges=[], pending=[],
                          healthy_sources={"unifi"}, now=NOW))
    assert f"edge:unifi:{SWITCH2_MAC}" in ch
    assert f"edge:unifi:{USW_MAC}" not in ch and f"drift:unifi:{USW_MAC}" not in ch


GW1_MAC, GW2_MAC = "10:40:40:40:40:01", "10:40:40:40:40:02"


def two_parent_candidates_snapshot(reverse=False):
    """USW has two LLDP neighbours that would each become its parent: a
    gateway on the uplink port (13) and another gateway-role device on a
    non-uplink port (6). The uplink one must always win, regardless of
    lldp_table order."""
    port_table = [
        {"port_idx": 6, "name": "Port 6", "up": True, "speed": 1000,
         "poe_enable": True, "is_uplink": False},
        {"port_idx": 13, "name": "Port 13", "up": True, "speed": 2500,
         "poe_enable": False, "is_uplink": True},
    ]
    lldp_table = [
        {"chassis_id": GW1_MAC, "local_port_idx": 13, "local_port_name": "Port 13",
         "port_id": "1"},
        {"chassis_id": GW2_MAC, "local_port_idx": 6, "local_port_name": "Port 6",
         "port_id": "1"},
    ]
    if reverse:
        lldp_table = list(reversed(lldp_table))
    devices = {"data": [
        {"type": "usw", "mac": USW_MAC.upper(), "name": "USW Pro Max 16 PoE",
         "ip": "192.168.6.2", "port_table": port_table, "lldp_table": lldp_table},
    ]}
    return parse_unifi(devices, {"data": []})


def test_reconcile_lldp_multi_parent_candidates_prefer_uplink_regardless_of_order():
    records = [
        rec(1, "USW Pro Max 16 PoE", "network", USW_MAC, network_role="switch"),
        rec(2, "Gateway1", "network", GW1_MAC, network_role="gateway"),
        rec(3, "Gateway2", "network", GW2_MAC, network_role="gateway"),
    ]
    results = {}
    for reverse in (False, True):
        obs = unifi_observations(two_parent_candidates_snapshot(reverse=reverse))
        results[reverse] = by_key(reconcile(obs, records=records, edges=[], pending=[],
                                            healthy_sources={"unifi"}, now=NOW))
    forward, backward = results[False], results[True]
    assert set(forward) == set(backward)
    assert all(forward[k]["fp"] == backward[k]["fp"] for k in forward)
    assert f"edge:unifi:{USW_MAC}" in forward
    assert forward[f"edge:unifi:{USW_MAC}"]["payload"]["parent_id"] == 2
    assert f"edge:unifi:{GW2_MAC}" not in forward and f"drift:unifi:{GW2_MAC}" not in forward


def test_reconcile_stale_needs_a_full_week_of_healthy_streak():
    """F3: staleness is measured only across healthy time - a source whose
    current healthy streak is under 7 days (or unknown) never flags stale
    edges, even if last_seen itself is more than 7 days old."""
    assert "drift:stale:conn:12" not in by_key(run(healthy_since={"unifi": NOW - DAY}))
    assert "drift:stale:conn:12" not in by_key(run(healthy_since=None))


# ── Task 4: applying scans and accepting suggestions (storage) ──────────────

from netwatch.connections import migration_drift_key


def lab_db(d):
    """Real DB mirroring lab_records()/lab_edges(), migrated like production."""
    hdb, idb = make_idb(d)
    ids = {
        "usw": add_device(idb, "USW Pro Max 16 PoE", "network", mac=USW_MAC,
                          network_role="switch", port_count=16),
        "eero": add_device(idb, "Eero Pro 6E — Gateway", "network", mac=EERO_MAC,
                           ip="192.168.4.1", network_role="gateway", port_count=2),
        "pi5": add_device(idb, "Raspberry Pi 5", "host", mac=PI5_MAC),
        "printer": add_device(idb, "Mystery printer", "printer", mac=SHARED_A),
        "oldnas": add_device(idb, "Old NAS", "host", mac="00:11:22:33:44:55"),
        "vf2": add_device(idb, "VisionFive 2", "host", mac=VF2_MAC),
    }
    e = {
        "usw_eero": insert_edge(idb, ids["usw"], ids["eero"], to_port="eth0", from_port="13"),
        "pi5_wifi": insert_edge(idb, ids["pi5"], ids["eero"], ctype="wifi"),
    }
    assert idb.migrate_connections_v2()[0]
    # Inserted after the migration: the migration marks every existing edge
    # manual, and this one must stay UniFi-sourced to exercise staleness.
    e["oldnas"] = insert_edge(idb, ids["oldnas"], ids["usw"], to_port="Port 3", source="unifi",
                              last_seen=NOW - 8 * DAY, external_key="unifi:port:00:11:22:33:44:55")
    ports = snapshot()["switches"][0]["ports"]
    idb.live_port_provider = lambda r: [dict(p) for p in ports] if r.get("mac") == USW_MAC else None
    return hdb, idb, ids, e


def scan(idb, now=NOW):
    changes = reconcile(unifi_observations(snapshot()), records=idb.list_all(),
                        edges=idb.list_all_connections(), pending=idb.suggestions.list(),
                        healthy_sources={"unifi"}, now=now,
                        live_ports_for=idb.live_port_provider,
                        healthy_since={"unifi": NOW - 30 * DAY})
    idb.apply_discovery_changes(changes, now)
    return changes


def pending(idb):
    return {s["subject_key"]: s for s in idb.suggestions.list()}


def accept(idb, key, **kw):
    s = pending(idb)[key]
    return idb.accept_suggestion(s["id"], s["fingerprint"], now=NOW + 1, **kw)


def test_apply_writes_suggestions_and_touches_in_one_go():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        scan(idb)
        keys = set(pending(idb))
        assert {f"drift:unifi:{PI5_MAC}", f"edge:unifi:{VF2_MAC}", "drift:stale:conn:%d" % e["oldnas"],
                f"identity:lldp:{EERO_LLDP_MAC}", f"shared_port:{USW_MAC}:Port 9",
                f"device:unifi:{WORKBENCH_MAC}", f"drift:unifi:{USW_MAC}"} <= keys
        c = idb.get_connection(e["usw_eero"])
        assert c["last_seen"] == NOW and c["parent_port"] == "eth0" and c["source"] == "manual"
        hdb.close()


def test_apply_rolls_back_on_error():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        before = idb.suggestions.count_pending()
        bad = {"upserts": [{"kind": "edge", "source": "unifi", "subject_key": "edge:unifi:x",
                            "payload": {}, "fp": "f"}],
               "resolve": [], "touch": [{"id": e["usw_eero"]}]}  # missing parent_port key
        with pytest.raises(KeyError):
            idb.apply_discovery_changes(bad, NOW)
        assert idb.suggestions.count_pending() == before
        hdb.close()


def test_accept_edge_then_rescan_is_quiet():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        scan(idb)
        ok, err, res = accept(idb, f"edge:unifi:{VF2_MAC}")
        assert ok and err is None
        c = idb.get_connection(res["connection_id"])
        assert (c["child_id"], c["parent_id"], c["parent_port"], c["source"], c["last_seen"]) == (
            ids["vf2"], ids["usw"], "Port 4", "unifi", NOW + 1)
        scan(idb, NOW + 60)
        assert f"edge:unifi:{VF2_MAC}" not in pending(idb)
        hdb.close()


def test_accept_device_with_overrides_creates_record_and_edge():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        scan(idb)
        ok, err, res = accept(idb, f"device:unifi:{WORKBENCH_MAC}",
                              overrides={"system": "Workbench PC", "category": "Computers"})
        assert ok
        r = idb.get(res["device_id"])
        assert (r["system"], r["mac"], r["ip"], r["category"], r["device_type"]) == (
            "Workbench PC", WORKBENCH_MAC, "192.168.6.45", "Computers", "host")
        c = idb.get_connection(res["connection_id"])
        assert (c["child_id"], c["parent_id"], c["parent_port"]) == (res["device_id"], ids["usw"], "Port 10")
        hdb.close()


def test_accept_device_rejects_bad_override_and_409s_when_mac_added_by_hand():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        scan(idb)
        assert accept(idb, f"device:unifi:{WORKBENCH_MAC}", overrides={"device_type": "toaster"}) == (
            False, "rejected", {"error": "unknown device type 'toaster'"})
        add_device(idb, "Hand-made", "host", mac=WORKBENCH_MAC)
        assert accept(idb, f"device:unifi:{WORKBENCH_MAC}")[1] == "suggestion_changed"
        assert f"device:unifi:{WORKBENCH_MAC}" in pending(idb)  # nothing applied
        hdb.close()


def test_accept_drift_replace_moves_edge_and_clears_migration_drift():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        assert migration_drift_key(e["usw_eero"]) in pending(idb)
        scan(idb)
        assert accept(idb, f"drift:unifi:{USW_MAC}")[0]
        c = idb.get_connection(e["usw_eero"])
        assert (c["parent_id"], c["parent_port"], c["child_port"], c["source"]) == (
            ids["eero"], "1", "Port 13", "unifi")
        assert migration_drift_key(e["usw_eero"]) not in pending(idb)

        assert accept(idb, f"drift:unifi:{PI5_MAC}")[0]
        c = idb.get_connection(e["pi5_wifi"])
        assert (c["parent_id"], c["parent_port"], c["connection_type"]) == (ids["usw"], "Port 7", "ethernet")
        scan(idb, NOW + 60)
        assert not {f"drift:unifi:{PI5_MAC}", f"drift:unifi:{USW_MAC}"} & set(pending(idb))
        hdb.close()


def test_accept_stale_drift_removes_edge():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        scan(idb)
        ok, err, res = accept(idb, "drift:stale:conn:%d" % e["oldnas"])
        assert ok and res == {"removed_connection_id": e["oldnas"]}
        assert idb.get_connection(e["oldnas"]) is None
        hdb.close()


def test_accept_identity_adds_alias_and_rescan_matches_by_mac():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        scan(idb)
        assert accept(idb, f"identity:lldp:{EERO_LLDP_MAC}")[0]
        props = idb.get(ids["eero"])["properties"]
        assert props["mac_aliases"] == [EERO_LLDP_MAC] and props["network_role"] == "gateway"
        scan(idb, NOW + 60)
        assert f"identity:lldp:{EERO_LLDP_MAC}" not in pending(idb)
        hdb.close()


def test_accept_identity_without_candidate_needs_device_id():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        idb.update(ids["eero"], {"ip": "10.0.0.1"})
        scan(idb)
        assert accept(idb, f"identity:lldp:{EERO_LLDP_MAC}")[1] == "rejected"
        assert accept(idb, f"identity:lldp:{EERO_LLDP_MAC}", overrides={"device_id": ids["eero"]})[0]
        hdb.close()


def test_accept_shared_port_creates_placeholder_and_rescan_is_quiet():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        scan(idb)
        ok, err, res = accept(idb, f"shared_port:{USW_MAC}:Port 9")
        assert ok and res["linked"] == [ids["printer"]]
        ph = idb.get(res["device_id"])
        assert ph["device_type"] == "network" and ph["properties"]["network_role"] == "switch"
        assert "Port 9" in ph["system"]
        edges = {(c["child_id"], c["parent_id"], c["parent_port"]) for c in idb.list_all_connections()}
        assert (res["device_id"], ids["usw"], "Port 9") in edges
        assert (ids["printer"], res["device_id"], None) in edges
        scan(idb, NOW + 60)
        assert f"shared_port:{USW_MAC}:Port 9" not in pending(idb)
        hdb.close()


def test_accept_is_409_for_old_fingerprint_deleted_device_and_decided_rows():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        scan(idb)
        s = pending(idb)[f"edge:unifi:{VF2_MAC}"]
        assert idb.accept_suggestion(s["id"], "stale-fp")[1] == "suggestion_changed"
        idb.delete(ids["vf2"])
        assert idb.accept_suggestion(s["id"], s["fingerprint"])[1] == "suggestion_changed"
        assert f"edge:unifi:{VF2_MAC}" in pending(idb)  # nothing applied
        assert idb.accept_suggestion(99999, "x") == (False, "not_found", {})
        mig = pending(idb)[migration_drift_key(e["usw_eero"])]
        assert idb.accept_suggestion(mig["id"], mig["fingerprint"]) == (
            False, "rejected", {"error": "this suggestion can only be dismissed"})
        hdb.close()


def test_accept_suggestions_reports_per_item():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        scan(idb)
        good = pending(idb)[f"edge:unifi:{VF2_MAC}"]
        results = idb.accept_suggestions([
            {"id": good["id"], "fingerprint": good["fingerprint"]},
            {"id": good["id"], "fingerprint": good["fingerprint"]},  # already accepted
            {"id": "nope"},
        ], now=NOW + 1)
        assert results == [
            {"id": good["id"], "ok": True, "error": None},
            {"id": good["id"], "ok": False, "error": "suggestion_changed"},
            {"id": "nope", "ok": False, "error": "invalid id"},
        ]
        hdb.close()
