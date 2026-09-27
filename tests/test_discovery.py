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

from netwatch.connections import migration_drift_key, NETWORK_LINK_TYPES


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


# ── Task 4 fix round 1: I1-I4, S1, M2 - live rows must still match the ──────
# ── payload the suggestion's fingerprint was computed from ─────────────────

def test_fix_i1a_drift_replace_rejects_when_live_edge_type_changed():
    """I1(a): the Pi 5 wifi edge was hand-edited to ethernet after the scan -
    the drift payload's `current` no longer matches, so accepting must 409
    and leave the edge untouched."""
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        scan(idb)
        ok, err, _ = idb.update_connection(e["pi5_wifi"], {"connection_type": "ethernet"})
        assert ok, err
        before = idb.get_connection(e["pi5_wifi"])
        assert accept(idb, f"drift:unifi:{PI5_MAC}") == (False, "suggestion_changed", {})
        assert idb.get_connection(e["pi5_wifi"]) == before
        hdb.close()


def test_fix_i1b_drift_replace_tolerates_notes_only_edit():
    """I1(b): a notes-only edit doesn't touch parent/port/type, so it must
    not block the drift replace."""
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        scan(idb)
        ok, err, _ = idb.update_connection(e["usw_eero"], {"notes": "x"})
        assert ok, err
        ok, err, res = accept(idb, f"drift:unifi:{USW_MAC}")
        assert ok and err is None
        hdb.close()


def test_fix_i2_stale_drift_rejects_edge_turned_manual():
    """I2: hand-editing the stale UniFi edge (even just its notes) makes it
    manual - a manual edge is never auto-removed as stale."""
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        scan(idb)
        ok, err, _ = idb.update_connection(e["oldnas"], {"notes": "keep me"})
        assert ok, err
        assert accept(idb, "drift:stale:conn:%d" % e["oldnas"]) == (
            False, "suggestion_changed", {})
        assert idb.get_connection(e["oldnas"]) is not None
        hdb.close()


def test_fix_i3_edge_accept_rejects_second_network_link():
    """I3: VF2 got hand-wired to the eero (over wifi) after the scan - VF2
    already has a network link, so accepting the UniFi edge suggestion (which
    would add a second one, to the USW) must 409."""
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        scan(idb)
        new_id, warnings, err = idb.quick_add_connection(
            {"a_id": ids["vf2"], "b_id": ids["eero"], "connection_type": "wifi"})
        assert err is None
        assert accept(idb, f"edge:unifi:{VF2_MAC}") == (False, "suggestion_changed", {})
        links = [c for c in idb.list_all_connections()
                 if c["child_id"] == ids["vf2"] and c["connection_type"] in NETWORK_LINK_TYPES]
        assert len(links) == 1
        hdb.close()


def test_fix_i4a_shared_port_accept_rejects_when_port_now_occupied():
    """I4(a): the printer got hand-wired directly to USW Port 9 after the
    scan - accepting the shared_port suggestion for that same port must 409
    and must not create a placeholder switch."""
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        scan(idb)
        before_count = len(idb.list_all())
        new_id, warnings, err = idb.quick_add_connection(
            {"a_id": ids["printer"], "b_id": ids["usw"], "parent_port": "Port 9"})
        assert err is None
        assert accept(idb, f"shared_port:{USW_MAC}:Port 9") == (False, "suggestion_changed", {})
        assert len(idb.list_all()) == before_count  # the hand-wire adds an edge, not a record
        hdb.close()


def test_fix_i4b_shared_port_accept_skips_already_linked_matched_device():
    """I4(b): the printer got hand-wired to the eero (over wifi) after the
    scan - it already has a network link, so the shared_port accept must
    still create the placeholder switch (the port itself is unaffected) but
    must not also link the printer to it."""
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        scan(idb)
        new_id, warnings, err = idb.quick_add_connection(
            {"a_id": ids["printer"], "b_id": ids["eero"], "connection_type": "wifi"})
        assert err is None
        ok, err_, res = accept(idb, f"shared_port:{USW_MAC}:Port 9")
        assert ok and err_ is None and res["linked"] == []
        ph = idb.get(res["device_id"])
        assert ph["device_type"] == "network" and ph["properties"]["network_role"] == "switch"
        hdb.close()


def test_fix_s1a_identity_accept_rejects_when_candidate_deleted():
    """S1: the eero (the identity suggestion's own candidate) was deleted
    after the scan - accepting must 409, not ask for a device_id override."""
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        scan(idb)
        ok, err = idb.delete(ids["eero"])
        assert ok, err
        assert accept(idb, f"identity:lldp:{EERO_LLDP_MAC}") == (False, "suggestion_changed", {})
        hdb.close()


def test_fix_s1b_identity_accept_rejects_mac_already_aliased_elsewhere():
    """S1: the chassis mac got attached as another device's alias after the
    scan - accepting must be rejected (not silently double-owned)."""
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        scan(idb)
        ok, err = idb.update(ids["pi5"], {"properties": {"mac_aliases": [EERO_LLDP_MAC]}})
        assert ok, err
        assert accept(idb, f"identity:lldp:{EERO_LLDP_MAC}") == (
            False, "rejected", {"error": "that MAC already belongs to Raspberry Pi 5"})
        hdb.close()


def test_fix_m2_device_accept_rejects_mac_already_aliased_elsewhere():
    """M2: the workbench mac got attached as another device's alias after the
    scan - accepting the new-device suggestion must 409, matching the plain
    mac-column check."""
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        scan(idb)
        ok, err = idb.update(ids["printer"], {"properties": {"mac_aliases": [WORKBENCH_MAC]}})
        assert ok, err
        assert accept(idb, f"device:unifi:{WORKBENCH_MAC}") == (False, "suggestion_changed", {})
        hdb.close()


# ── Task 5: DiscoveryRunner, UniFi fetch/TLS, settings keys ─────────────────

import logging
import ssl
import urllib.error

from netwatch.auth import AuthManager
from netwatch.discovery import (
    DiscoveryRunner, UnifiError, safe_error, unifi_ssl_context,
)
from netwatch.http_handlers import _h_get_settings, _h_post_settings, SECRET_PLACEHOLDER

SECRET = "sekrit-api-key-value-0123456789ab"


def fake_auth(**data):
    return types.SimpleNamespace(data=dict(data))


def runner_for(idb, fetch, settings=None, **auth):
    auth = auth or {"unifi_url": "https://unifi.local:11443", "unifi_api_key": SECRET}
    return DiscoveryRunner(fake_auth(**auth), settings if settings is not None else {}, idb,
                           fetch_unifi=fetch)


def ok_fetch(calls=None):
    def fetch(url, api_key, site, ctx):
        if calls is not None:
            calls.append((url, api_key, site, isinstance(ctx, ssl.SSLContext)))
        return unifi_device_payload(), unifi_clients_payload()
    return fetch


def test_safe_error_never_echoes_text():
    http = urllib.error.HTTPError("https://x/?k=" + SECRET, 401, "Unauthorized " + SECRET, {}, None)
    assert safe_error(http) == "HTTP 401"
    assert safe_error(urllib.error.URLError(ssl.SSLCertVerificationError("bad " + SECRET))) == \
        "URLError: SSLCertVerificationError"
    assert safe_error(urllib.error.URLError("refused " + SECRET)) == "URLError: unreachable"
    assert safe_error(UnifiError("x")) == "controller returned an error"
    assert safe_error(TimeoutError(SECRET)) == "TimeoutError"


def test_unifi_ssl_context_reads_shared_settings_live():
    settings = {}
    assert unifi_ssl_context(settings).verify_mode == ssl.CERT_REQUIRED
    settings["unifi_verify_ssl"] = False  # same dict object, changed later
    ctx = unifi_ssl_context(settings)
    assert ctx.verify_mode == ssl.CERT_NONE and ctx.check_hostname is False


def test_scan_once_files_suggestions_and_records_health():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        calls = []
        r = runner_for(idb, ok_fetch(calls))
        assert r.scan_once(now=NOW) is True
        assert calls == [("https://unifi.local:11443", SECRET, "default", True)]
        assert f"edge:unifi:{VF2_MAC}" in pending(idb)
        st = r.status()
        assert st["last_scan"] == NOW and st["scanning"] is False
        assert st["sources"]["unifi"] == {"configured": True, "ok": True, "error": None,
                                          "at": NOW, "counts": {"switches": 1, "clients": 10}}
        hdb.close()


def test_live_ports_come_from_the_last_scan():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        r = runner_for(idb, ok_fetch())
        usw = idb.get(ids["usw"])
        assert r.live_ports_for(usw) is None
        r.scan_once(now=NOW)
        ports = r.live_ports_for(usw)
        assert [p["name"] for p in ports][-2:] == ["SFP+ 1", "SFP+ 2"]
        ports[0]["name"] = "mutated"
        assert r.live_ports_for(usw)[0]["name"] == "Port 1"
        eero = idb.get(ids["eero"])
        assert r.live_ports_for(eero) is None
        hdb.close()


def test_failed_scan_keeps_suggestions_logs_once_and_hides_key(caplog):
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        good = runner_for(idb, ok_fetch())
        good.scan_once(now=NOW)
        before = set(pending(idb))

        def boom(url, api_key, site, ctx):
            raise urllib.error.HTTPError(url, 401, "Unauthorized " + api_key, {}, None)
        r = runner_for(idb, boom)
        with caplog.at_level(logging.WARNING):
            r.scan_once(now=NOW + 900)
            r.scan_once(now=NOW + 1800)
        assert set(pending(idb)) == before
        assert r.status()["sources"]["unifi"]["error"] == "HTTP 401"
        warnings = [rec.getMessage() for rec in caplog.records if "unifi" in rec.getMessage().lower()]
        assert len(warnings) == 1 and SECRET not in caplog.text
        hdb.close()


def scan_hourly(r, start, end):
    for t in range(start, end + 1, 3600):
        r.scan_once(now=t)


def boom_fetch(*a):
    raise urllib.error.URLError("down")


def test_runner_tracks_healthy_streak_for_staleness():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        stale = "drift:stale:conn:%d" % e["oldnas"]
        r = runner_for(idb, ok_fetch())
        r.scan_once(now=NOW)  # streak starts now: nothing can be stale yet
        assert stale not in pending(idb)
        scan_hourly(r, NOW, NOW + 8 * DAY)  # healthy for 8 days
        assert stale in pending(idb)
        assert r._healthy_since["unifi"] == NOW

        r._fetch_unifi = boom_fetch
        r.scan_once(now=NOW + 9 * DAY)
        r._fetch_unifi = ok_fetch()
        r.scan_once(now=NOW + 20 * DAY)  # long gap between successes: new streak
        assert r._healthy_since["unifi"] == NOW + 20 * DAY
        hdb.close()


def test_short_outage_keeps_the_streak():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        r = runner_for(idb, ok_fetch())
        r.scan_once(now=NOW)
        r._fetch_unifi = boom_fetch
        r.scan_once(now=NOW + 900)
        r.scan_once(now=NOW + 1800)
        r._fetch_unifi = ok_fetch()
        r.scan_once(now=NOW + 2700)  # 45 min since the last success
        assert r._healthy_since["unifi"] == NOW
        r.scan_once(now=NOW + 2700 + 3601)  # just over an hour: new streak
        assert r._healthy_since["unifi"] == NOW + 2700 + 3601
        hdb.close()


def test_streak_survives_a_quick_restart_but_not_a_long_one():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        scan_hourly(runner_for(idb, ok_fetch()), NOW - 8 * DAY, NOW)
        restarted = runner_for(idb, ok_fetch())
        assert restarted._healthy_since["unifi"] == NOW - 8 * DAY
        restarted.scan_once(now=NOW + 1200)  # e.g. a deploy restart
        assert restarted._healthy_since["unifi"] == NOW - 8 * DAY
        assert "drift:stale:conn:%d" % e["oldnas"] in pending(idb)

        later = runner_for(idb, ok_fetch())
        later.scan_once(now=NOW + 3 * 3600)  # netwatch was down for hours
        assert later._healthy_since["unifi"] == NOW + 3 * 3600
        hdb.close()


def test_unreadable_persisted_streak_is_ignored():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        idb.set_meta("discovery_health_streaks", "{not json")
        r = runner_for(idb, ok_fetch())
        assert r._healthy_since == {}
        r.scan_once(now=NOW)
        assert r._healthy_since["unifi"] == NOW
        hdb.close()


def test_unconfigured_runner_is_idle():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        calls = []
        r = DiscoveryRunner(fake_auth(), {}, idb, fetch_unifi=ok_fetch(calls))
        assert r.unifi_configured() is False and r.any_source_configured() is False
        assert r.request_scan() is False
        r.scan_once(now=NOW)
        assert calls == []
        assert r.status()["sources"]["unifi"]["configured"] is False
        hdb.close()


def test_runner_picks_up_credentials_saved_after_start():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        auth = fake_auth()
        r = DiscoveryRunner(auth, {}, idb, fetch_unifi=ok_fetch())
        assert r.unifi_configured() is False
        auth.data.update(unifi_url="https://unifi.local:11443", unifi_api_key=SECRET)
        assert r.unifi_configured() is True and r.request_scan() is True
        hdb.close()


def test_background_loop_scans_when_woken():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        scanned = threading.Event()

        def fetch(*a):
            scanned.set()
            return unifi_device_payload(), unifi_clients_payload()
        r = runner_for(idb, fetch)
        r.FIRST_SCAN_DELAY_SECONDS = 3600
        stop = threading.Event()
        t = r.start(stop)
        r.request_scan()
        assert scanned.wait(5)
        stop.set()
        r.request_scan()  # wake the loop so it sees stop
        t.join(5)
        assert not t.is_alive()
        hdb.close()


def test_unifi_settings_round_trip_through_auth_json(tmp_path):
    am = AuthManager(str(tmp_path / "auth.json"))
    cfg = tmp_path / "hosts.yaml"
    cfg.write_text("settings: {}\nhosts: []\n")
    settings = {}
    code, body = _h_post_settings({"unifi_url": "https://192.168.6.194:11443",
                                   "unifi_api_key": SECRET, "unifi_site": "default",
                                   "unifi_verify_ssl": False}, str(cfg), settings, am)
    assert code == 200
    assert am.data["unifi_api_key"] == SECRET and am.data["unifi_url"].endswith(":11443")
    assert "unifi_api_key" not in settings and settings["unifi_verify_ssl"] is False
    assert SECRET not in cfg.read_text()
    code, body = _h_get_settings(settings, am)
    assert body["unifi_api_key"] == SECRET_PLACEHOLDER and body["unifi_site"] == "default"
    code, _ = _h_post_settings({"unifi_url": "not a url"}, str(cfg), settings, am)
    assert code == 400


# ── Task 6: accept / accept-all / discovery endpoints ───────────────────────

import urllib.request
from http.server import ThreadingHTTPServer

from netwatch.http_handlers import (
    _h_post_suggestion_accept, _h_post_suggestions_accept_all,
    _h_get_discovery_status, _h_post_discovery_scan,
)
from netwatch.server import make_handler


def test_accept_handler_status_codes():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        scan(idb)
        s = pending(idb)[f"edge:unifi:{VF2_MAC}"]
        path = f"/api/suggestions/{s['id']}/accept"
        assert _h_post_suggestion_accept(path, {"fingerprint": "old"}, idb) == (409, {"error": "suggestion_changed"})
        code, body = _h_post_suggestion_accept(path, {"fingerprint": s["fingerprint"]}, idb)
        assert code == 200 and body["ok"] and "connection_id" in body
        assert _h_post_suggestion_accept("/api/suggestions/99999/accept", {}, idb)[0] == 404
        assert _h_post_suggestion_accept("/api/suggestions/x/accept", {}, idb)[0] == 400
        mig = pending(idb)[migration_drift_key(e["usw_eero"])]
        assert _h_post_suggestion_accept(f"/api/suggestions/{mig['id']}/accept",
                                         {"fingerprint": mig["fingerprint"]}, idb) == (
            400, {"error": "this suggestion can only be dismissed"})
        hdb.close()


def test_accept_all_handler_validates_items():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        scan(idb)
        s = pending(idb)[f"edge:unifi:{VF2_MAC}"]
        code, body = _h_post_suggestions_accept_all(
            {"items": [{"id": s["id"], "fingerprint": s["fingerprint"]}]}, idb)
        assert code == 200 and body["results"] == [{"id": s["id"], "ok": True, "error": None}]
        assert _h_post_suggestions_accept_all({"items": "nope"}, idb)[0] == 400
        assert _h_post_suggestions_accept_all({"items": [{}] * 501}, idb)[0] == 400
        hdb.close()


def test_discovery_status_and_scan_handlers():
    assert _h_get_discovery_status(None) == (200, {"sources": {}, "last_scan": None, "scanning": False})
    assert _h_post_discovery_scan(None)[0] == 400
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        idle = DiscoveryRunner(fake_auth(), {}, idb, fetch_unifi=ok_fetch())
        assert _h_post_discovery_scan(idle) == (400, {"error": "no discovery source is configured"})
        r = runner_for(idb, ok_fetch())
        assert _h_post_discovery_scan(r) == (200, {"ok": True, "queued": True})
        code, body = _h_get_discovery_status(r)
        assert code == 200 and body["sources"]["unifi"]["configured"] is True
        hdb.close()


def _serve_once(idb, auth, runner):
    handler = make_handler(None, {}, "/dev/null", auth_manager=auth, inventory_db=idb,
                           discovery_runner=runner)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=server.handle_request)
    t.start()
    return server, server.server_address[1], t


def _post(port, path, cookie, token, body=b"{}"):
    headers = {"Cookie": f"nw_session={cookie}", "Content-Type": "application/json"}
    if token:
        headers["X-CSRF-Token"] = token
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body,
                                 method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_scan_route_is_admin_only_and_status_route_is_open_to_users(tmp_path):
    hdb, idb, ids, e = lab_db(str(tmp_path))
    auth = AuthManager(str(tmp_path / "auth.json"))
    auth.create_user("root", "password123", admin=True)
    auth.create_user("bob", "password123")
    runner = runner_for(idb, ok_fetch())
    bob, root = auth.make_session_cookie("bob"), auth.make_session_cookie("root")

    server, port, t = _serve_once(idb, auth, runner)
    try:
        assert _post(port, "/api/discovery/scan", bob, auth.csrf_token_for_cookie(bob))[0] == 403
    finally:
        server.server_close(); t.join()
    server, port, t = _serve_once(idb, auth, runner)
    try:
        assert _post(port, "/api/discovery/scan", root, auth.csrf_token_for_cookie(root)) == (
            200, {"ok": True, "queued": True})
    finally:
        server.server_close(); t.join()
    server, port, t = _serve_once(idb, auth, runner)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/discovery/status",
                                     headers={"Cookie": f"nw_session={bob}"})
        with urllib.request.urlopen(req) as r:
            assert json.loads(r.read())["sources"]["unifi"]["configured"] is True
    finally:
        server.server_close(); t.join()
    hdb.close()


def test_accept_routes_require_csrf_and_route_correctly(tmp_path):
    hdb, idb, ids, e = lab_db(str(tmp_path))
    scan(idb)
    auth = AuthManager(str(tmp_path / "auth.json"))
    auth.create_user("bob", "password123")
    bob = auth.make_session_cookie("bob")
    s = pending(idb)[f"edge:unifi:{VF2_MAC}"]
    body = json.dumps({"items": [{"id": s["id"], "fingerprint": s["fingerprint"]}]}).encode()

    server, port, t = _serve_once(idb, auth, None)
    try:
        assert _post(port, "/api/suggestions/accept-all", bob, None, body)[0] == 403
    finally:
        server.server_close(); t.join()
    server, port, t = _serve_once(idb, auth, None)
    try:
        code, out = _post(port, "/api/suggestions/accept-all", bob, auth.csrf_token_for_cookie(bob), body)
        assert code == 200 and out["results"][0]["ok"] is True
    finally:
        server.server_close(); t.join()
    hdb.close()


# ── Final-review fix wave ───────────────────────────────────────────────────

def suggestion_status(idb, key):
    with idb.lock:
        row = idb.conn.execute(
            "SELECT status FROM connection_suggestions WHERE subject_key = ?", (key,)).fetchone()
    return row and row[0]


def scan_clients(idb, clients, now):
    """scan() against a modified clients payload (a list of _sta rows)."""
    snap = parse_unifi(unifi_device_payload(), {"data": clients})
    changes = reconcile(unifi_observations(snap), records=idb.list_all(),
                        edges=idb.list_all_connections(), pending=idb.suggestions.list(),
                        healthy_sources={"unifi"}, now=now,
                        live_ports_for=idb.live_port_provider,
                        healthy_since={"unifi": NOW - 30 * DAY})
    idb.apply_discovery_changes(changes, now)
    return changes


def clients_without_port(port):
    return [c for c in unifi_clients_payload()["data"] if c["sw_port"] != port]


# C1: an accepted suggestion comes back when its subject is observed again
# after the accept (e.g. the accepted edge was deleted by hand).

def test_final_c1a_accepted_edge_reopens_after_edge_deleted():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        scan(idb)
        key = f"edge:unifi:{VF2_MAC}"
        ok, err, res = accept(idb, key)
        assert ok
        assert idb.delete_connection(res["connection_id"])
        scan(idb, NOW + 60)
        assert suggestion_status(idb, key) == "pending"
        hdb.close()


def test_final_c1b_scan_started_before_accept_keeps_it_accepted():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        scan(idb)
        key = f"edge:unifi:{VF2_MAC}"
        # A scan that read its inputs before the accept...
        changes = reconcile(unifi_observations(snapshot()), records=idb.list_all(),
                            edges=idb.list_all_connections(), pending=idb.suggestions.list(),
                            healthy_sources={"unifi"}, now=NOW,
                            live_ports_for=idb.live_port_provider,
                            healthy_since={"unifi": NOW - 30 * DAY})
        assert key in {u["subject_key"] for u in changes["upserts"]}
        assert accept(idb, key)[0]  # decided_at = NOW + 1
        # ...and applies after it must not reopen it.
        idb.apply_discovery_changes(changes, NOW)
        assert suggestion_status(idb, key) == "accepted"
        hdb.close()


def test_scan_request_during_a_scan_runs_one_more_scan():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        started, release, calls = threading.Event(), threading.Event(), []

        def fetch(*a):
            calls.append(1)
            started.set()
            release.wait(5)
            return unifi_device_payload(), unifi_clients_payload()
        r = runner_for(idb, fetch)
        r.FIRST_SCAN_DELAY_SECONDS = 3600
        r.SCAN_INTERVAL_SECONDS = 3600
        stop = threading.Event()
        t = r.start(stop)
        r.request_scan()
        assert started.wait(5)
        started.clear()
        assert r.request_scan() is True  # arrives mid-scan
        release.set()
        assert started.wait(5), "mid-scan request was dropped"
        assert len(calls) == 2
        stop.set()
        r.request_scan()
        t.join(5)
        assert not t.is_alive()
        hdb.close()


def test_clients_on_uplink_ports_are_ignored():
    devices, clients = unifi_device_payload(), unifi_clients_payload()
    clients["data"] += [_sta("30:00:00:00:00:01", 13, "upstream-a"),
                        _sta("30:00:00:00:00:02", 13, "upstream-b")]
    obs = unifi_observations(parse_unifi(devices, clients))
    assert not [o for o in obs if o.get("port") == "Port 13" and o["type"] != "lldp"]
    assert not [o for o in obs if o["type"] == "edge" and o["parent_port"] == "Port 13"]


# ── Lock ordering: the runner's lock and the DB lock are never nested ───────

def test_scan_never_touches_the_db_while_holding_the_runner_lock():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        r = runner_for(idb, ok_fetch())
        held_during_db_call = []
        real_set_meta = idb.set_meta

        def spy(key, value):
            held_during_db_call.append(r._lock.locked())
            return real_set_meta(key, value)
        idb.set_meta = spy
        r.scan_once(now=NOW)
        assert held_during_db_call == [False]
        hdb.close()


def test_accept_never_calls_the_live_port_provider_under_the_db_lock():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        scan(idb)
        real = idb.live_port_provider
        db_lock_held = []

        def provider(rec):
            db_lock_held.append(idb.lock.locked())
            return real(rec)
        idb.live_port_provider = provider
        ok, err, res = accept(idb, f"shared_port:{USW_MAC}:Port 9")
        assert ok, err
        assert db_lock_held and not any(db_lock_held)
        hdb.close()


# ── Final-review minors ─────────────────────────────────────────────────────

from http.server import BaseHTTPRequestHandler
from netwatch.discovery import _get_json


def test_api_key_is_not_sent_across_a_redirect():
    seen = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("X-API-Key"))
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"data": []}')
        def log_message(self, *a): pass
    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)

    class Redirector(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_address[1]}/x")
            self.end_headers()
        def log_message(self, *a): pass
    redirector = ThreadingHTTPServer(("127.0.0.1", 0), Redirector)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (target, redirector)]
    for t in threads: t.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get_json(f"http://127.0.0.1:{redirector.server_address[1]}/", SECRET,
                      ssl.create_default_context())
        assert safe_error(exc.value) == "HTTP 302"
        assert seen == []
    finally:
        for s in (target, redirector):
            s.shutdown(); s.server_close()


def test_non_int_lldp_port_idx_does_not_break_the_scan():
    devices = unifi_device_payload()
    devices["data"][0]["lldp_table"].append(
        {"chassis_id": "aa:00:00:00:00:09", "local_port_idx": "15", "local_port_name": None,
         "port_id": "x"})
    snap = parse_unifi(devices, unifi_clients_payload())
    assert snap["switches"][0]["lldp"][-1]["local_port_idx"] is None
    reconcile(unifi_observations(snap), records=lab_records(), edges=lab_edges(),
              pending=[], healthy_sources={"unifi"}, now=NOW, live_ports_for=live_for())


def test_lldp_port_that_is_not_on_the_parent_is_not_proposed():
    devices = unifi_device_payload()
    devices["data"][0]["lldp_table"][0]["port_id"] = "eth7"  # eero has ports 1-2
    changes = reconcile(unifi_observations(parse_unifi(devices, unifi_clients_payload())),
                        records=lab_records(), edges=lab_edges(), pending=[],
                        healthy_sources={"unifi"}, now=NOW, live_ports_for=live_for())
    blob = json.dumps(changes["upserts"])
    assert "eth7" not in blob


def test_apply_failure_logs_type_once_and_shows_in_status(caplog):
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids, e = lab_db(d)
        r = runner_for(idb, ok_fetch())
        real_apply = idb.apply_discovery_changes

        def broken(changes, now=None):
            raise RuntimeError("detail that must not be logged")
        idb.apply_discovery_changes = broken
        with caplog.at_level(logging.WARNING):
            r.scan_once(now=NOW)
            r.scan_once(now=NOW + 900)
        msgs = [m.getMessage() for m in caplog.records if "applying" in m.getMessage()]
        assert len(msgs) == 1 and "RuntimeError" in msgs[0]
        assert "must not be logged" not in caplog.text
        assert r.status()["apply_error"] == "RuntimeError"
        idb.apply_discovery_changes = real_apply
        r.scan_once(now=NOW + 1800)
        assert r.status()["apply_error"] is None
        hdb.close()
