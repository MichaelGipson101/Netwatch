"""Tests for Netwatch 4.0 connections rework, plan 4 (Proxmox + wifi inference)."""
import os
import tempfile
import types
import urllib.error

import pytest

from netwatch.pollers import ProxmoxPoller

NOW = 1_800_000_000
DAY = 86400


# ── Task 1: Proxmox poller hooks ─────────────────────────────────────────────

def make_poller(configured=True):
    data = ({"proxmox_url": "https://pve.local:8006", "proxmox_user": "root@pam",
             "proxmox_token_id": "netwatch", "proxmox_token_secret": "s3cret"}
            if configured else {})
    p = ProxmoxPoller(types.SimpleNamespace(data=data), alert_settings={})
    p._check_alerts = lambda *a: None  # no ntfy from tests
    return p


def fake_fetch(calls, fail=False):
    def fetch(url, user, token_id, token_secret, path):
        calls.append(path)
        if fail:
            raise OSError("down")
        if path == "/api2/json/nodes":
            return [{"node": "pve", "status": "online"}]
        if path.endswith("/qemu"):
            return [{"vmid": 108, "name": "haos13.2", "status": "running"}]
        if path.endswith("/lxc"):
            return []
        return {"echo": path}
    return fetch


def test_proxmox_poller_api_get_uses_its_own_credentials():
    p = make_poller()
    p._fetch = fake_fetch([])
    assert p.configured() is True
    assert p.api_get("/api2/json/cluster/status") == {"echo": "/api2/json/cluster/status"}
    idle = make_poller(configured=False)
    assert idle.configured() is False
    with pytest.raises(RuntimeError):
        idle.api_get("/api2/json/cluster/status")


def test_fresh_nodes_polls_only_when_the_cache_is_stale():
    p = make_poller()
    calls = []
    p._fetch = fake_fetch(calls)
    nodes = p.fresh_nodes()
    assert [n["name"] for n in nodes] == ["pve"]
    assert nodes[0]["guests"][0]["vmid"] == 108
    polled = len(calls)
    p.fresh_nodes()
    assert len(calls) == polled          # still fresh: no second poll
    p._last_ok_at -= 301
    p.fresh_nodes()
    assert len(calls) > polled           # stale: polled again


def test_fresh_nodes_is_none_when_polling_fails():
    p = make_poller()
    p._fetch = fake_fetch([], fail=True)
    assert p.fresh_nodes() is None


def test_concurrent_polls_do_not_overlap():
    """Verify _poll_lock serializes concurrent polls from loop, discovery, and HTTP force-refresh."""
    import threading
    import time as time_module

    p = make_poller()
    state = {"in_flight": 0, "max_in_flight": 0}
    lock = threading.Lock()

    def fake_fetch_with_concurrency_check(url, user, token_id, token_secret, path):
        with lock:
            state["in_flight"] += 1
            state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
        try:
            time_module.sleep(0.05)  # Simulate network latency; concurrent polls would spike in_flight
            if path == "/api2/json/nodes":
                return [{"node": "pve", "status": "online"}]
            if path.endswith("/qemu"):
                return [{"vmid": 108, "name": "haos13.2", "status": "running"}]
            if path.endswith("/lxc"):
                return []
            return {"echo": path}
        finally:
            with lock:
                state["in_flight"] -= 1

    p._fetch = fake_fetch_with_concurrency_check

    # Launch two _poll threads plus one fresh_nodes (which may call _poll if cache stale)
    results = []
    def poll_thread():
        try:
            p._poll()
            results.append("poll_ok")
        except Exception as e:
            results.append(f"poll_err: {e}")

    def fresh_thread():
        try:
            p.fresh_nodes()
            results.append("fresh_ok")
        except Exception as e:
            results.append(f"fresh_err: {e}")

    t1 = threading.Thread(target=poll_thread)
    t2 = threading.Thread(target=poll_thread)
    t3 = threading.Thread(target=fresh_thread)

    t1.start()
    t2.start()
    t3.start()

    t1.join()
    t2.join()
    t3.join()

    # All threads completed successfully
    assert all("ok" in r for r in results), f"Thread results: {results}"
    # No concurrent polls were in-flight simultaneously
    assert state["max_in_flight"] == 1, f"max_in_flight was {state['max_in_flight']}, expected 1"


# ── Task 2: Proxmox adapter ──────────────────────────────────────────────────

from netwatch.discovery_proxmox import (
    ProxmoxUnavailable, fetch_proxmox, parse_net_macs, proxmox_observations,
    proxmox_snapshot,
)

HA_MAC = "02:ac:a5:69:78:a5"          # HAOS VM: locally-administered MAC
MC_MAC = "bc:24:11:9e:39:2e"
SOL_MAC0, SOL_MAC1 = "bc:24:11:fb:57:0f", "bc:24:11:c2:5e:b7"
FORGEJO_MAC = "bc:24:11:22:39:9e"


def pve_cache():
    """Shaped like ProxmoxPoller's cached nodes (captured 2026-09-27, trimmed)."""
    return [
        {"name": "pve", "status": "online", "guests": [
            {"vmid": 108, "name": "haos13.2", "type": "qemu", "status": "running"},
            {"vmid": 116, "name": "Solaris10", "type": "qemu", "status": "stopped"},
        ]},
        {"name": "prodesk1", "status": "online", "guests": [
            {"vmid": 301, "name": "Minecraft", "type": "lxc", "status": "running"},
        ]},
        {"name": "NASMachineV3", "status": "offline", "guests": []},
    ]


def cluster_status():
    return [{"type": "cluster", "name": "ServerGroup", "id": "cluster"},
            {"type": "node", "name": "pve", "ip": "192.168.4.237", "online": 1},
            {"type": "node", "name": "prodesk1", "ip": "192.168.6.219", "online": 1},
            {"type": "node", "name": "NASMachineV3", "ip": "192.168.6.60", "online": 0}]


CONFIGS = {
    ("pve", 108): {"net0": "virtio=02:AC:A5:69:78:A5,bridge=vmbr0", "cores": 2,
                   "memory": 4096, "onboot": 1},
    ("pve", 116): {"net0": "e1000=BC:24:11:FB:57:0F,bridge=vmbr0,firewall=1",
                   "net1": "e1000=BC:24:11:C2:5E:B7,bridge=vmbr1,firewall=1",
                   "cores": 2, "memory": 4096},
    ("prodesk1", 301): {"net0": "name=eth0,bridge=vmbr0,firewall=1,hwaddr=BC:24:11:9E:39:2E,"
                                "ip=dhcp,type=veth",
                        "cores": 2, "memory": 4096, "onboot": 1, "hostname": "Minecraft"},
}


def test_parse_net_macs_reads_qemu_and_lxc_entries():
    assert parse_net_macs(CONFIGS[("pve", 116)]) == [SOL_MAC0, SOL_MAC1]
    assert parse_net_macs(CONFIGS[("prodesk1", 301)]) == [MC_MAC]
    assert parse_net_macs({"net10": "virtio=AA:BB:CC:DD:EE:0A", "net2": "virtio=AA:BB:CC:DD:EE:02",
                           "scsi0": "x=AA:BB:CC:DD:EE:FF"}) == [
        "aa:bb:cc:dd:ee:02", "aa:bb:cc:dd:ee:0a"]
    assert parse_net_macs({}) == [] and parse_net_macs(None) == []


def test_proxmox_snapshot_joins_cache_status_and_configs():
    snap = proxmox_snapshot(pve_cache(), cluster_status(), CONFIGS)
    assert snap["nodes"] == [
        {"name": "pve", "ip": "192.168.4.237", "online": True, "guests_known": True},
        {"name": "prodesk1", "ip": "192.168.6.219", "online": True, "guests_known": True},
        {"name": "NASMachineV3", "ip": "192.168.6.60", "online": False,
         "guests_known": False}]
    assert [(g["node"], g["vmid"]) for g in snap["guests"]] == [
        ("pve", 108), ("pve", 116), ("prodesk1", 301)]
    assert snap["guests"][0] == {
        "node": "pve", "vmid": 108, "name": "haos13.2", "guest_type": "qemu",
        "macs": [HA_MAC], "ip": None, "cores": 2, "memory_mb": 4096, "onboot": True,
        "status": "running"}
    assert snap["guests"][1]["macs"] == [SOL_MAC0, SOL_MAC1]
    assert snap["guests"][1]["onboot"] is False
    assert snap["failed"] == []
    assert snap["complete"] is False       # NASMachineV3 is offline


def test_unreadable_config_is_reported_not_guessed():
    configs = {k: v for k, v in CONFIGS.items() if k != ("prodesk1", 301)}
    snap = proxmox_snapshot(pve_cache(), cluster_status(), configs)
    assert snap["failed"] == [{"node": "prodesk1", "vmid": 301}]
    assert [g["vmid"] for g in snap["guests"]] == [108, 116]
    assert {"type": "held_guest", "source": "proxmox", "node": "prodesk1", "vmid": 301} in (
        proxmox_observations(snap))


def test_proxmox_observations_shapes():
    obs = proxmox_observations(proxmox_snapshot(pve_cache(), cluster_status(), CONFIGS))
    nodes = [o for o in obs if o["type"] == "node"]
    assert nodes[0] == {"type": "node", "source": "proxmox", "name": "pve",
                        "ip": "192.168.4.237", "online": True, "guests_known": True}
    [mc] = [o for o in obs if o["type"] == "guest" and o["vmid"] == 301]
    assert mc == {"type": "guest", "source": "proxmox", "node": "prodesk1", "vmid": 301,
                  "name": "Minecraft", "guest_type": "lxc", "macs": [MC_MAC], "cores": 2,
                  "memory_mb": 4096, "onboot": True, "ip": None,
                  "external_key": "proxmox:guest:prodesk1:301"}


def test_fetch_proxmox_reads_configs_through_the_poller():
    class Poller:
        def __init__(self):
            self.paths = []

        def fresh_nodes(self):
            return pve_cache()

        def api_get(self, path):
            self.paths.append(path)
            if path == "/api2/json/cluster/status":
                return cluster_status()
            if path.endswith("/108/config"):
                raise urllib.error.URLError("timeout")
            parts = path.split("/")  # ['', 'api2', 'json', 'nodes', node, kind, vmid, 'config']
            return CONFIGS[(parts[4], int(parts[6]))]

    p = Poller()
    snap = fetch_proxmox(p)
    assert p.paths[0] == "/api2/json/cluster/status"
    assert "/api2/json/nodes/prodesk1/lxc/301/config" in p.paths
    assert "/api2/json/nodes/pve/qemu/116/config" in p.paths
    assert not any("NASMachineV3" in x for x in p.paths)   # offline node isn't queried
    assert snap["failed"] == [{"node": "pve", "vmid": 108}]


def test_fetch_proxmox_without_fresh_data_raises():
    class Poller:
        def fresh_nodes(self):
            return None

    with pytest.raises(ProxmoxUnavailable):
        fetch_proxmox(Poller())

# ── Task 3: guest precedence on switch ports ─────────────────────────────────

from netwatch.discovery import unifi_observations

GW_MAC = "d4:3f:32:eb:2a:f2"
NODE_PVE_MAC = "10:e7:c6:08:2e:39"           # EliteDesk: no IP in inventory
NODE_PRODESK_MAC = "6c:02:e0:98:df:1d"
PI5_MAC = "d8:3a:dd:ad:2d:b7"
MYSTERY_MAC = "bc:24:11:77:77:77"            # Proxmox OUI, but no known guest has it
USW_MAC = "74:fa:29:1d:a3:dc"

LIVE = [{"name": f"Port {i}", "idx": i, "up": True, "speed_mbps": 1000, "poe": True,
         "is_uplink": i == 13} for i in range(1, 17)]

LAB_CLIENTS = [  # (mac, port, ip, hostname)
    (NODE_PVE_MAC, 11, "192.168.4.237", None),
    (HA_MAC, 11, "192.168.5.110", "homeassistant"),
    (NODE_PRODESK_MAC, 8, "192.168.6.219", None),
    (MC_MAC, 8, "192.168.6.220", "Minecraft"),
    (MYSTERY_MAC, 5, "192.168.6.70", "mystery"),
    (PI5_MAC, 7, "192.168.6.90", "ApplePi5"),
]

PVE_GUESTS = {HA_MAC: "pve", SOL_MAC0: "pve", SOL_MAC1: "pve", MC_MAC: "prodesk1"}


def usw_snapshot(clients):
    return {"switches": [{"mac": USW_MAC, "name": "USW", "ip": None,
                          "ports": [dict(p) for p in LIVE], "lldp": []}],
            "clients": [{"mac": m, "ip": ip, "name": n, "sw_mac": USW_MAC, "sw_port": port,
                         "last_seen": NOW} for (m, port, ip, n) in clients]}


def node_port_obs(obs):
    return {o["child"]["proxmox_node"]: o for o in obs
            if o["type"] == "edge" and "proxmox_node" in o["child"]}


def test_guest_ports_yield_node_port_edges_and_drop_guests():
    obs = unifi_observations(usw_snapshot(LAB_CLIENTS), guest_macs=set(PVE_GUESTS),
                             guest_node_of=PVE_GUESTS, guests_complete=True)
    nodes = node_port_obs(obs)
    assert set(nodes) == {"pve", "prodesk1"}
    assert nodes["pve"] == {
        "type": "edge", "source": "unifi", "child": {"proxmox_node": "pve"},
        "parent": {"mac": USW_MAC}, "parent_port": "Port 11", "child_port": None,
        "connection_type": "ethernet", "external_key": "unifi:node-port:pve"}
    assert nodes["prodesk1"]["parent_port"] == "Port 8"
    macs = {o["child"]["mac"] for o in obs if o["type"] == "edge" and "mac" in o["child"]}
    assert macs == {NODE_PVE_MAC, NODE_PRODESK_MAC, PI5_MAC, MYSTERY_MAC}
    assert not [o for o in obs if o["type"] == "held"]


def test_incomplete_guest_set_holds_ports_with_unexplained_likely_guests():
    obs = unifi_observations(usw_snapshot(LAB_CLIENTS), guest_macs=set(PVE_GUESTS),
                             guest_node_of=PVE_GUESTS, guests_complete=False)
    held = [o for o in obs if o["type"] == "held"]
    assert [(o["port"], o["macs"]) for o in held] == [("Port 5", [MYSTERY_MAC])]
    assert set(node_port_obs(obs)) == {"pve", "prodesk1"}


def test_node_port_vote_prefers_most_guests_then_lowest_port():
    extra = "bc:24:11:00:00:09"
    clients = [(HA_MAC, 11, None, None), (SOL_MAC0, 11, None, None), (SOL_MAC1, 3, None, None),
               (MC_MAC, 6, None, None), (extra, 4, None, None)]
    guests = {**PVE_GUESTS, extra: "prodesk1"}
    obs = unifi_observations(usw_snapshot(clients), guest_macs=set(guests),
                             guest_node_of=guests)
    assert {n: o["parent_port"] for n, o in node_port_obs(obs).items()} == {
        "pve": "Port 11", "prodesk1": "Port 4"}


def test_without_node_map_output_is_unchanged():
    obs = unifi_observations(usw_snapshot(LAB_CLIENTS), guest_macs=set(PVE_GUESTS))
    assert node_port_obs(obs) == {}


# ── Task 4: reconciler - nodes, node-port edges, holds ───────────────────────

from netwatch.discovery import reconcile


def rec(id_, system, device_type="host", mac=None, ip=None, category=None, **props):
    return {"id": id_, "system": system, "device_type": device_type, "mac": mac, "ip": ip,
            "role": None, "category": category, "properties": dict(props)}


def edge(id_, child, parent, to_port=None, ctype="ethernet", source="manual",
         last_seen=None, external_key=None):
    return {"id": id_, "from_device_id": child, "to_device_id": parent, "from_port": None,
            "to_port": to_port, "connection_type": ctype, "source": source,
            "external_key": external_key, "last_seen": last_seen, "updated_at": None}


def lab_records(pve_known=False):
    return [
        rec(1, "USW Pro Max 16 PoE", "network", USW_MAC, network_role="switch", port_count=16),
        rec(2, "Eero Pro 6E — Gateway", "network", GW_MAC, "192.168.4.1",
            network_role="gateway"),
        rec(11, "HP EliteDesk 800 G3 Mini", "host", NODE_PVE_MAC,
            **({"proxmox_node": "pve"} if pve_known else {})),
        rec(56, "HP Prodesk 405 G6 Mini", "host", NODE_PRODESK_MAC, "192.168.6.219"),
        rec(3, "Raspberry Pi 5", "host", PI5_MAC),
    ]


def pve_obs(configs=None, cache=None):
    return proxmox_observations(proxmox_snapshot(
        cache if cache is not None else pve_cache(), cluster_status(),
        CONFIGS if configs is None else configs))


def lab_unifi_obs(guests_complete=True):
    return unifi_observations(usw_snapshot(LAB_CLIENTS), guest_macs=set(PVE_GUESTS),
                              guest_node_of=PVE_GUESTS, guests_complete=guests_complete)


def run(observations, records, edges=(), pending=(), healthy=("unifi", "proxmox"),
        ip_macs=None):
    return reconcile(observations, records=records, edges=list(edges),
                     pending=list(pending), healthy_sources=set(healthy), now=NOW,
                     live_ports_for=lambda r: ([dict(p) for p in LIVE]
                                               if r.get("mac") == USW_MAC else None),
                     healthy_since={s: NOW - 30 * DAY for s in healthy}, ip_macs=ip_macs)


def keys(changes):
    return {u["subject_key"]: u for u in changes["upserts"]}


def pend(*pairs):
    return [{"subject_key": k, "source": s} for k, s in pairs]


def test_nodes_match_by_ip_and_unknown_nodes_ask_with_a_candidate():
    ch = keys(run(pve_obs() + lab_unifi_obs(), lab_records(),
                  ip_macs={"192.168.4.237": NODE_PVE_MAC}))
    ident = ch["identity:proxmox-node:pve"]
    assert (ident["kind"], ident["source"]) == ("identity", "proxmox")
    p = ident["payload"]
    assert (p["proxmox_node"], p["node_ip"]) == ("pve", "192.168.4.237")
    assert (p["candidate_id"], p["candidate_name"]) == (11, "HP EliteDesk 800 G3 Mini")
    assert p["message"] == "Is Proxmox node pve (192.168.4.237) your HP EliteDesk 800 G3 Mini?"
    nas = ch["identity:proxmox-node:NASMachineV3"]["payload"]
    assert nas["candidate_id"] is None
    assert nas["message"] == "Which device is Proxmox node NASMachineV3 (192.168.6.60)?"
    assert "identity:proxmox-node:prodesk1" not in ch      # matched by IP


def test_node_matches_by_recorded_proxmox_node_property():
    ch = keys(run(pve_obs(), lab_records(pve_known=True), healthy=("proxmox",)))
    assert "identity:proxmox-node:pve" not in ch


def test_node_port_edge_replaces_the_nodes_own_mac_edge():
    ch = keys(run(pve_obs() + lab_unifi_obs(), lab_records()))
    np = ch["edge:unifi:node-port:prodesk1"]["payload"]
    assert (np["child_id"], np["parent_id"], np["parent_port"], np["connection_type"]) == (
        56, 1, "Port 8", "ethernet")
    assert np["external_key"] == "unifi:node-port:prodesk1"
    assert f"edge:unifi:{NODE_PRODESK_MAC}" not in ch
    # pve isn't identified yet: no node-port edge, its own MAC edge stands in
    assert "edge:unifi:node-port:pve" not in ch
    assert ch[f"edge:unifi:{NODE_PVE_MAC}"]["payload"]["parent_port"] == "Port 11"


def test_unidentified_or_offline_node_holds_its_guests_and_node_port():
    records = lab_records() + [
        rec(70, "TrueNAS", "vm", proxmox_vmid=121),
        rec(90, "Custom NAS", "host", "9c:6b:00:aa:8c:09", "192.168.6.60")]
    edges = [edge(40, 70, 90, ctype="virtual", source="proxmox",
                  last_seen=NOW - 9 * DAY, external_key="proxmox:guest:NASMachineV3:121")]
    pending = pend(("edge:unifi:node-port:pve", "unifi"),
                   ("device:proxmox:pve:116", "proxmox"),
                   ("drift:proxmox:NASMachineV3:121", "proxmox"),
                   ("device:proxmox:prodesk1:999", "proxmox"))
    ch = run(pve_obs() + lab_unifi_obs(), records, edges=edges, pending=pending)
    assert not [k for k in keys(ch) if k.startswith("device:proxmox:pve:")]
    assert ch["resolve"] == ["device:proxmox:prodesk1:999"]
    assert "drift:stale:conn:40" not in keys(ch)          # offline node: not aged


def test_node_port_suggestions_are_held_while_proxmox_is_down():
    # Isolated to just the blanket "proxmox is down" stale-hold (fix round 1,
    # #5): the port carries no likely-guest MAC at all - prodesk1 itself is
    # simply absent from this scan's clients - so the per-mac hold path can't
    # be what's protecting the edge/pending key.
    pending = pend(("edge:unifi:node-port:prodesk1", "unifi"))
    edges = [edge(41, 56, 1, to_port="Port 8", source="unifi", last_seen=NOW - 9 * DAY,
                  external_key="unifi:node-port:prodesk1")]
    clients = [(PI5_MAC, 7, "192.168.6.90", "ApplePi5")]
    obs = unifi_observations(usw_snapshot(clients), guest_macs=None)
    assert not [o for o in obs if o["type"] == "held"]     # confirms isolation
    ch = run(obs, lab_records(), edges=edges, pending=pending, healthy=("unifi",))
    assert ch["resolve"] == []
    assert "drift:stale:conn:41" not in keys(ch)


def test_held_macs_only_protect_their_own_source():
    laptop = "aa:bb:cc:dd:ee:10"
    records = lab_records() + [rec(10, "Laptop", "host", laptop)]
    edges = [edge(70, 10, 2, ctype="wifi", source="inferred", last_seen=NOW - 9 * DAY,
                  external_key=f"inferred:wifi:{laptop}")]
    obs = [{"type": "held", "source": "inferred", "macs": [laptop]}]
    pending = pend((f"edge:unifi:{laptop}", "unifi"), (f"edge:inferred:{laptop}", "inferred"))
    ch = run(obs, records, edges=edges, pending=pending, healthy=("unifi", "inferred"))
    assert ch["resolve"] == [f"edge:unifi:{laptop}"]
    assert "drift:stale:conn:70" not in keys(ch)


# ── Fix round 1 ───────────────────────────────────────────────────────────────

def test_a_vm_carrying_proxmox_node_never_matches_the_node():
    """Finding #1 (critical): on a VM record, proxmox_node means "runs on
    node X", not "is node X". A VM record sorting before the real host (and
    itself matching the HAOS guest by vmid) must not steal the node match -
    the host still matches by its own recorded proxmox_node, and the VM
    guest still gets its own virtual edge to that host, not to itself."""
    records = [
        rec(5, "AAA Decoy VM", "vm", proxmox_node="pve", proxmox_vmid=108),
    ] + lab_records(pve_known=True)
    ch = keys(run(pve_obs(), records, healthy=("proxmox",)))
    assert "identity:proxmox-node:pve" not in ch          # host still matches
    guest_edge = ch["edge:proxmox:pve:108"]
    assert guest_edge["payload"]["parent_id"] == 11       # HAOS -> the real host
    assert guest_edge["payload"]["child_id"] == 5         # HAOS's record is the decoy VM


def test_offline_identified_node_holds_its_node_port_edge():
    """Finding #2(a): an offline node that IS identified still can't have its
    node-port fact re-derived this scan - the pending suggestion must not
    resolve and the existing edge must not age."""
    records = lab_records() + [rec(90, "Custom NAS", "host", "9c:6b:00:aa:8c:09",
                                   "192.168.6.60")]
    edges = [edge(42, 90, 1, to_port="Port 12", source="unifi", last_seen=NOW - 9 * DAY,
                  external_key="unifi:node-port:NASMachineV3")]
    pending = pend(("edge:unifi:node-port:NASMachineV3", "unifi"))
    ch = run(pve_obs() + lab_unifi_obs(), records, edges=edges, pending=pending)
    assert ch["resolve"] == []
    assert "drift:stale:conn:42" not in keys(ch)


def test_held_node_own_mac_holds_its_node_port_edge():
    """Finding #2(b): the node's own MAC turns up in a `held` observation (an
    unexplained likely-guest MAC on its port keeps UniFi from voting for the
    node) - the node's still-unproven node-port fact must not resolve or age
    either, even though the node itself is identified and online."""
    configs = {k: v for k, v in CONFIGS.items() if k != ("prodesk1", 301)}
    clients = [
        (NODE_PVE_MAC, 11, "192.168.4.237", None),
        (HA_MAC, 11, "192.168.5.110", "homeassistant"),
        (NODE_PRODESK_MAC, 8, "192.168.6.219", None),
        (MC_MAC, 8, "192.168.6.220", "Minecraft"),
        (MYSTERY_MAC, 8, "192.168.6.70", "mystery"),
        (PI5_MAC, 7, "192.168.6.90", "ApplePi5"),
    ]
    obs = pve_obs(configs=configs) + unifi_observations(
        usw_snapshot(clients), guest_macs=set(PVE_GUESTS), guest_node_of=PVE_GUESTS,
        guests_complete=False)
    edges = [edge(43, 56, 1, to_port="Port 8", source="unifi", last_seen=NOW - 9 * DAY,
                  external_key="unifi:node-port:prodesk1")]
    pending = pend(("edge:unifi:node-port:prodesk1", "unifi"))
    ch = run(obs, lab_records(), edges=edges, pending=pending)
    assert ch["resolve"] == []
    assert "drift:stale:conn:43" not in keys(ch)


def test_ip_macs_lookup_normalizes_the_candidate_mac():
    """Finding #3 (minor): an ip_macs MAC in a different case still matches
    by_mac, which is keyed by normalized MACs."""
    ch = keys(run(pve_obs() + lab_unifi_obs(), lab_records(),
                  ip_macs={"192.168.4.237": NODE_PVE_MAC.upper()}))
    p = ch["identity:proxmox-node:pve"]["payload"]
    assert (p["candidate_id"], p["candidate_name"]) == (11, "HP EliteDesk 800 G3 Mini")


def test_name_only_guest_match_gets_its_edge_but_no_props_fill():
    """Finding #4 (minor): a same-named VM record with no recorded vmid or
    matching MAC is too weak a match to trust for filling properties, but it
    still gets its virtual edge to the node."""
    records = lab_records(pve_known=True) + [rec(80, "haos13.2", "vm")]  # no vmid, no MAC
    ch = run(pve_obs(), records, healthy=("proxmox",))
    edge_key = "edge:proxmox:pve:108"
    assert edge_key in keys(ch)
    assert keys(ch)[edge_key]["payload"]["child_id"] == 80
    assert not [p for p in ch["props"] if p["id"] == 80]


# ── Task 5: reconciler - guests ──────────────────────────────────────────────

def guest_records():
    return lab_records(pve_known=True) + [
        rec(71, "Home Assistant OS", "vm", HA_MAC, category="Virtual Machine"),
        rec(72, "Sun Solaris", "vm", category="Virtual Machine", proxmox_vmid=116),
        rec(73, "Retired VM", "vm", category="Virtual Machine", proxmox_vmid=999,
            proxmox_node="pve"),
    ]


def test_known_guests_get_virtual_edges_and_missing_properties():
    edges = [edge(50, 72, 56, ctype="virtual")]          # Solaris drawn on the wrong node
    ch = run(pve_obs() + lab_unifi_obs(), guest_records(), edges=edges)
    k = keys(ch)
    e = k["edge:proxmox:pve:108"]["payload"]
    assert (e["child_id"], e["parent_id"], e["connection_type"], e["external_key"]) == (
        71, 11, "virtual", "proxmox:guest:pve:108")
    assert e["message"] == "Home Assistant OS runs on HP EliteDesk 800 G3 Mini"
    d = k["drift:proxmox:pve:116"]["payload"]
    assert (d["connection_id"], d["current"]["parent_id"], d["proposed"]["parent_id"]) == (
        50, 56, 11)
    fills = {p["id"]: p["set"] for p in ch["props"]}
    assert fills[71] == {"guest_type": "qemu", "proxmox_node": "pve", "proxmox_vmid": 108}
    assert fills[72] == {"guest_type": "qemu", "proxmox_node": "pve"}


def test_unknown_guest_becomes_a_prefilled_vm_device_suggestion():
    s = keys(run(pve_obs() + lab_unifi_obs(), guest_records()))["device:proxmox:prodesk1:301"]
    p = s["payload"]
    assert p["device"] == {
        "system": "Minecraft", "mac": MC_MAC, "ip": None, "device_type": "vm",
        "category": "Virtual Machine",
        "properties": {"hypervisor": "Proxmox", "guest_type": "lxc", "proxmox_node": "prodesk1",
                       "proxmox_vmid": 301, "vcpu_count": 2, "ram_alloc_gb": 4.0,
                       "autostart": True}}
    assert p["edge"] == {"parent_id": 56, "parent_name": "HP Prodesk 405 G6 Mini",
                         "parent_port": None, "child_port": None,
                         "connection_type": "virtual", "source": "proxmox",
                         "external_key": "proxmox:guest:prodesk1:301"}
    assert p["message"] == "Minecraft (LXC 301 on prodesk1) isn't in inventory"


def test_sourced_guest_edge_is_touched_and_a_vanished_guest_goes_stale():
    edges = [edge(60, 71, 11, ctype="virtual", source="proxmox", last_seen=NOW - DAY,
                  external_key="proxmox:guest:pve:108"),
             edge(61, 73, 11, ctype="virtual", source="proxmox", last_seen=NOW - 8 * DAY,
                  external_key="proxmox:guest:pve:999")]
    ch = run(pve_obs() + lab_unifi_obs(), guest_records(), edges=edges)
    assert {"id": 60, "parent_port": None} in ch["touch"]
    assert "edge:proxmox:pve:108" not in keys(ch)
    assert keys(ch)["drift:stale:conn:61"]["payload"]["action"] == "remove"


def test_migrated_guest_matches_by_mac_and_proposes_the_new_node():
    records = guest_records() + [rec(74, "forgejo", "vm", FORGEJO_MAC, proxmox_node="pve",
                                     proxmox_vmid=303)]
    edges = [edge(62, 74, 11, ctype="virtual", source="proxmox", last_seen=NOW - DAY,
                  external_key="proxmox:guest:pve:303")]
    cache = pve_cache()
    cache[1]["guests"].append({"vmid": 303, "name": "forgejo", "type": "lxc",
                               "status": "running"})
    configs = dict(CONFIGS)
    configs[("prodesk1", 303)] = {"net0": f"name=eth0,hwaddr={FORGEJO_MAC.upper()},type=veth"}
    ch = run(pve_obs(configs=configs, cache=cache), records, edges=edges,
             healthy=("proxmox",))
    d = keys(ch)["drift:proxmox:prodesk1:303"]["payload"]
    assert (d["connection_id"], d["proposed"]["parent_id"], d["action"]) == (62, 56, "replace")
    fills = {p["id"]: p["set"] for p in ch["props"]}
    assert fills[74] == {"guest_type": "lxc"}     # recorded proxmox_node is never overwritten


def test_unreadable_guest_config_holds_that_guest_only():
    configs = {k: v for k, v in CONFIGS.items() if k != ("pve", 108)}
    edges = [edge(60, 71, 11, ctype="virtual", source="proxmox", last_seen=NOW - 9 * DAY,
                  external_key="proxmox:guest:pve:108")]
    pending = pend(("edge:proxmox:pve:108", "proxmox"),
                   ("device:proxmox:prodesk1:777", "proxmox"))
    ch = run(pve_obs(configs=configs) + lab_unifi_obs(guests_complete=False),
             guest_records(), edges=edges, pending=pending)
    assert ch["resolve"] == ["device:proxmox:prodesk1:777"]
    assert "drift:stale:conn:60" not in keys(ch)
    assert "device:proxmox:prodesk1:301" in keys(ch)     # other guests still work


# ── Task 6: storage - accepting Proxmox suggestions ──────────────────────────

from netwatch.storage import HistoryDB, InventoryDB


def make_idb(tmpdir):
    hdb = HistoryDB(os.path.join(tmpdir, "sources_test.db"))
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


def insert_edge(idb, child, parent, ctype="ethernet", to_port=None, source="manual"):
    with idb.lock:
        cur = idb.conn.execute(
            "INSERT INTO inventory_connections (from_device_id, to_device_id, to_port, "
            "connection_type, created_at, source) VALUES (?, ?, ?, ?, 0, ?)",
            (child, parent, to_port, ctype, source))
        return cur.lastrowid


def pending_suggestion(idb, kind, source, key, payload):
    idb.apply_discovery_changes({"upserts": [{"kind": kind, "source": source,
                                              "subject_key": key, "payload": payload,
                                              "fp": "f" * 16}]}, NOW)
    return next(s for s in idb.suggestions.list("pending") if s["subject_key"] == key)


def test_accept_guest_device_saves_vm_properties_and_a_virtual_edge():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        node = add_device(idb, "HP Prodesk 405 G6 Mini", ip="192.168.6.219")
        s = pending_suggestion(idb, "device", "proxmox", "device:proxmox:prodesk1:301", {
            "device": {"system": "Minecraft", "mac": MC_MAC, "ip": None, "device_type": "vm",
                       "category": "Virtual Machine",
                       "properties": {"guest_type": "lxc", "proxmox_node": "prodesk1",
                                      "proxmox_vmid": 301, "vcpu_count": 2}},
            "edge": {"parent_id": node, "parent_name": "HP Prodesk 405 G6 Mini",
                     "parent_port": None, "child_port": None, "connection_type": "virtual",
                     "source": "proxmox", "external_key": "proxmox:guest:prodesk1:301"},
            "message": "Minecraft (LXC 301 on prodesk1) isn't in inventory"})
        ok, err, res = idb.accept_suggestion(s["id"], s["fingerprint"])
        assert ok, (err, res)
        vm = idb.get(res["device_id"])
        assert (vm["device_type"], vm["category"], vm["mac"]) == ("vm", "Virtual Machine", MC_MAC)
        assert vm["properties"]["proxmox_vmid"] == 301
        assert vm["properties"]["guest_type"] == "lxc"
        [c] = [c for c in idb.list_all_connections() if c["from_device_id"] == res["device_id"]]
        assert (c["to_device_id"], c["connection_type"], c["source"], c["external_key"]) == (
            node, "virtual", "proxmox", "proxmox:guest:prodesk1:301")
        hdb.close()


def test_accept_virtual_edge_ignores_network_links_but_not_a_second_virtual_edge():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        vm = add_device(idb, "Home Assistant OS", "vm", mac=HA_MAC)
        node = add_device(idb, "HP EliteDesk 800 G3 Mini")
        other = add_device(idb, "HP Prodesk 405 G6 Mini")
        sw = add_device(idb, "USW", "network")
        insert_edge(idb, vm, sw, ctype="ethernet")         # someone drew the VM on the switch
        payload = {"child_id": vm, "child_name": "Home Assistant OS", "parent_id": node,
                   "parent_name": "HP EliteDesk 800 G3 Mini", "parent_port": None,
                   "child_port": None, "connection_type": "virtual", "source": "proxmox",
                   "external_key": "proxmox:guest:pve:108", "message": "m"}
        s = pending_suggestion(idb, "edge", "proxmox", "edge:proxmox:pve:108", payload)
        ok, err, _ = idb.accept_suggestion(s["id"], s["fingerprint"])
        assert ok, err
        s2 = pending_suggestion(idb, "edge", "proxmox", "edge:proxmox:prodesk1:108",
                                dict(payload, parent_id=other,
                                     external_key="proxmox:guest:prodesk1:108"))
        ok, err, _ = idb.accept_suggestion(s2["id"], s2["fingerprint"])
        assert (ok, err) == (False, "suggestion_changed")
        hdb.close()


def test_accept_proxmox_node_identity_sets_the_property_once():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        a = add_device(idb, "HP EliteDesk 800 G3 Mini", mac=NODE_PVE_MAC)
        b = add_device(idb, "Spare box")
        payload = {"proxmox_node": "pve", "node_ip": "192.168.4.237", "candidate_id": a,
                   "candidate_name": "HP EliteDesk 800 G3 Mini", "message": "m"}
        s = pending_suggestion(idb, "identity", "proxmox", "identity:proxmox-node:pve", payload)
        ok, err, res = idb.accept_suggestion(s["id"], s["fingerprint"])
        assert (ok, res) == (True, {"device_id": a}), err
        assert idb.get(a)["properties"]["proxmox_node"] == "pve"
        s2 = pending_suggestion(idb, "identity", "proxmox", "identity:proxmox-node:pve#2",
                                dict(payload, candidate_id=b, candidate_name="Spare box"))
        ok, err, res = idb.accept_suggestion(s2["id"], s2["fingerprint"])
        assert (ok, err) == (False, "rejected")
        assert "already Proxmox node pve" in res["error"]
        hdb.close()


def test_accept_proxmox_node_identity_ignores_vm_records_holding_the_node_name():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        # A VM record with properties.proxmox_node="pve" means "runs on node
        # pve", not "is node pve" - it must never block accepting the pve
        # identity for the actual EliteDesk host.
        a = add_device(idb, "HP EliteDesk 800 G3 Mini", mac=NODE_PVE_MAC)
        add_device(idb, "Minecraft", device_type="vm", proxmox_node="pve", proxmox_vmid=301)
        payload = {"proxmox_node": "pve", "node_ip": "192.168.4.237", "candidate_id": a,
                   "candidate_name": "HP EliteDesk 800 G3 Mini", "message": "m"}
        s = pending_suggestion(idb, "identity", "proxmox", "identity:proxmox-node:pve", payload)
        ok, err, res = idb.accept_suggestion(s["id"], s["fingerprint"])
        assert ok, (err, res)
        assert idb.get(a)["properties"]["proxmox_node"] == "pve"
        hdb.close()


def test_scan_fills_missing_guest_properties_without_overwriting():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        vm = add_device(idb, "Sun Solaris", "vm", proxmox_vmid=116)
        with idb.lock:  # a user-recorded node the scan must not overwrite
            idb.conn.execute("UPDATE inventory SET properties = ? WHERE id = ?",
                             ('{"proxmox_vmid": 116, "proxmox_node": "prodesk1"}', vm))
        idb.apply_discovery_changes({"props": [
            {"id": vm, "set": {"guest_type": "qemu", "proxmox_node": "pve",
                               "proxmox_vmid": 116}},
            {"id": 99999, "set": {"guest_type": "lxc"}}]}, NOW)
        props = idb.get(vm)["properties"]
        assert props == {"proxmox_vmid": 116, "proxmox_node": "prodesk1", "guest_type": "qemu"}
        hdb.close()


# ── Task 7: wifi inference ───────────────────────────────────────────────────

from netwatch.discovery_wifi import NO_GATEWAY, parse_arp, wifi_observations

DESKTOP_MAC, DECK_MAC = "d8:bb:c1:03:ac:0e", "14:d4:24:63:3b:b5"
LAPTOP_MAC, NAS_MAC, TPL_CLIENT_MAC = "aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:10", "aa:bb:cc:dd:ee:08"

ARP_TEXT = """IP address       HW type     Flags       HW address            Mask     Device
192.168.4.1      0x1         0x2         d4:3f:32:eb:2a:f2     *        eth0
192.168.4.89     0x1         0x2         d8:bb:c1:03:ac:0e     *        eth0
192.168.4.26     0x1         0x2         14:d4:24:63:3b:b5     *        eth0
192.168.6.91     0x1         0x0         00:00:00:00:00:00     *        eth0
192.168.4.50     0x1         0x2         aa:bb:cc:dd:ee:01     *        eth0
192.168.4.71     0x1         0x2         aa:bb:cc:dd:ee:08     *        eth0
192.168.6.90     0x1         0x2         d8:3a:dd:ad:2d:b7     *        eth0
"""


def host(name, ip, mac=None, always_on=True, is_up=True):
    return {"name": name, "ip": ip, "always_on": always_on, "is_up": is_up,
            "specs": {"mac": mac} if mac else {}}


def wifi_records():
    return lab_records() + [
        rec(4, "Custom Desktop PC", "host", DESKTOP_MAC, "192.168.4.89"),
        rec(7, "Valve Steam Deck", "host", DECK_MAC),
        rec(9, "Laptop", "host", LAPTOP_MAC),
        rec(10, "Old NAS", "host", NAS_MAC),
        rec(8, "Behind the TP-Link", "host", TPL_CLIENT_MAC),
        rec(45, "TP Link 24-port", "network", "aa:bb:cc:00:00:45", network_role="switch"),
    ]


def wifi_edges():
    return [edge(80, 7, 2, ctype="wifi"),                  # Deck already drawn on the eero
            edge(81, 8, 45, ctype="ethernet", to_port="3")]


def wifi_hosts():
    return [host("Custom Desktop PC", "192.168.4.89"),         # MAC via ARP
            host("Steam Deck", "192.168.4.26", DECK_MAC),
            host("Laptop", "192.168.4.50", LAPTOP_MAC, always_on=False),
            host("Old NAS", "192.168.4.99", NAS_MAC, is_up=False),
            host("TP-Link client", "192.168.4.71"),
            host("ApplePi5", "192.168.6.90", PI5_MAC)]


def test_parse_arp_keeps_complete_entries_only():
    arp = parse_arp(ARP_TEXT)
    assert arp["192.168.4.89"] == DESKTOP_MAC and arp["192.168.4.1"] == GW_MAC
    assert "192.168.6.91" not in arp
    assert parse_arp("") == {} and parse_arp(None) == {}


def test_wifi_observations_pick_up_live_unwired_hosts():
    obs, err = wifi_observations(wifi_hosts(), parse_arp(ARP_TEXT), wired_macs={PI5_MAC},
                                 guest_macs={HA_MAC}, records=wifi_records(),
                                 edges=wifi_edges())
    assert err is None
    edges_ = {o["child"]["mac"]: o for o in obs if o["type"] == "edge"}
    assert set(edges_) == {DESKTOP_MAC, DECK_MAC}
    assert edges_[DESKTOP_MAC] == {
        "type": "edge", "source": "inferred",
        "child": {"mac": DESKTOP_MAC, "ip": "192.168.4.89", "name": "Custom Desktop PC"},
        "parent": {"inventory_id": 2}, "parent_port": None, "child_port": None,
        "connection_type": "wifi", "external_key": f"inferred:wifi:{DESKTOP_MAC}"}
    assert [o["macs"] for o in obs if o["type"] == "held"] == [[NAS_MAC]]


def test_wifi_inference_needs_a_gateway():
    records = [r for r in wifi_records() if r["id"] != 2]
    assert wifi_observations(wifi_hosts(), parse_arp(ARP_TEXT), set(), set(), records,
                             []) == ([], NO_GATEWAY)


def test_reconcile_turns_inferred_observations_into_wifi_suggestions():
    obs, _ = wifi_observations(wifi_hosts(), parse_arp(ARP_TEXT), {PI5_MAC}, set(),
                               wifi_records(), wifi_edges())
    pending = pend((f"edge:inferred:{NAS_MAC}", "inferred"))
    ch = run(obs, wifi_records(), edges=wifi_edges(), pending=pending, healthy=("inferred",))
    k = keys(ch)
    assert k[f"edge:inferred:{DESKTOP_MAC}"]["payload"]["message"] == (
        "Custom Desktop PC connects over wifi to Eero Pro 6E — Gateway")
    assert {"id": 80, "parent_port": None} in ch["touch"]     # manual Deck edge confirmed
    assert ch["resolve"] == []                                # the down NAS is held


# ── Task 8: runner integration ───────────────────────────────────────────────

from netwatch.discovery import DiscoveryRunner, safe_error


class FakeHosts:
    def __init__(self, hosts):
        self._hosts = hosts

    def list_hosts(self):
        return [types.SimpleNamespace(to_dict=lambda h=h: dict(h)) for h in self._hosts]


class FakePoller:
    def __init__(self, configured=True):
        self.is_configured = configured

    def configured(self):
        return self.is_configured


def raw_unifi():
    table = [{"port_idx": i, "name": f"Port {i}", "up": True, "speed": 1000,
              "poe_enable": True, "is_uplink": i == 13} for i in range(1, 17)]
    devices = {"meta": {"rc": "ok"}, "data": [{"type": "usw", "mac": USW_MAC, "name": "USW",
                                               "port_table": table, "lldp_table": []}]}
    clients = {"meta": {"rc": "ok"}, "data": [
        {"mac": m, "ip": ip, "hostname": n, "is_wired": True, "sw_mac": USW_MAC,
         "sw_port": port, "last_seen": NOW} for (m, port, ip, n) in LAB_CLIENTS]}
    return devices, clients


def lab_idb(d):
    hdb, idb = make_idb(d)
    add_device(idb, "USW Pro Max 16 PoE", "network", mac=USW_MAC, network_role="switch",
               port_count=16)
    add_device(idb, "Eero Pro 6E — Gateway", "network", mac=GW_MAC, ip="192.168.4.1",
               network_role="gateway")
    add_device(idb, "HP EliteDesk 800 G3 Mini", mac=NODE_PVE_MAC)
    add_device(idb, "HP Prodesk 405 G6 Mini", mac=NODE_PRODESK_MAC, ip="192.168.6.219")
    add_device(idb, "Raspberry Pi 5", mac=PI5_MAC)
    add_device(idb, "Custom Desktop PC", mac=DESKTOP_MAC, ip="192.168.4.89")
    return hdb, idb


def make_runner(idb, unifi_ok=True, proxmox=None, hosts=(), arp=None):
    auth = types.SimpleNamespace(data={"unifi_url": "https://unifi.local:11443",
                                       "unifi_api_key": "k"})

    def fetch_unifi(url, key, site, ctx):
        if not unifi_ok:
            raise urllib.error.URLError("down")
        return raw_unifi()

    return DiscoveryRunner(
        auth, {}, idb, fetch_unifi=fetch_unifi, proxmox_poller=FakePoller(),
        host_manager=FakeHosts(list(hosts)),
        fetch_proxmox=proxmox or (lambda p: proxmox_snapshot(pve_cache(), cluster_status(),
                                                             CONFIGS)),
        read_arp=lambda: dict(arp or {}))


def pending_keys(idb):
    return {s["subject_key"] for s in idb.suggestions.list("pending")}


def test_safe_error_names_a_missing_proxmox_cache():
    from netwatch.discovery_proxmox import ProxmoxUnavailable
    assert safe_error(ProxmoxUnavailable()) == "Proxmox poller has no fresh data"


def test_scan_runs_proxmox_then_unifi_then_inference():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = lab_idb(d)
        r = make_runner(idb, hosts=[host("Custom Desktop PC", "192.168.4.89")],
                        arp={"192.168.4.89": DESKTOP_MAC,
                             "192.168.4.237": NODE_PVE_MAC})
        assert r.scan_once(now=NOW) is True
        k = pending_keys(idb)
        assert {"device:proxmox:prodesk1:301", "edge:unifi:node-port:prodesk1",
                f"edge:inferred:{DESKTOP_MAC}", "identity:proxmox-node:pve"} <= k
        assert not any(MC_MAC in key or HA_MAC in key for key in k)   # guests never on the switch
        [ident] = [s for s in idb.suggestions.list("pending")
                   if s["subject_key"] == "identity:proxmox-node:pve"]
        assert ident["payload"]["candidate_name"] == "HP EliteDesk 800 G3 Mini"
        src = r.status()["sources"]
        assert src["proxmox"] == {"ok": True, "error": None, "at": NOW,
                                  "counts": {"nodes": 3, "guests": 3}, "configured": True}
        assert (src["inferred"]["ok"], src["inferred"]["counts"]) == (True, {"devices": 1})
        hdb.close()


def test_proxmox_failure_holds_guest_ports():
    from netwatch.discovery_proxmox import ProxmoxUnavailable

    def boom(poller):
        raise ProxmoxUnavailable()

    with tempfile.TemporaryDirectory() as d:
        hdb, idb = lab_idb(d)
        r = make_runner(idb, proxmox=boom)
        r.scan_once(now=NOW)
        src = r.status()["sources"]["proxmox"]
        assert (src["ok"], src["error"]) == (False, "Proxmox poller has no fresh data")
        k = pending_keys(idb)
        assert "edge:unifi:node-port:prodesk1" not in k
        assert f"edge:unifi:{NODE_PRODESK_MAC}" not in k    # port 8 carries a likely guest
        hdb.close()


def test_inference_waits_for_a_successful_unifi_scan():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = lab_idb(d)
        r = make_runner(idb, unifi_ok=False,
                        hosts=[host("Custom Desktop PC", "192.168.4.89")],
                        arp={"192.168.4.89": DESKTOP_MAC})
        r.scan_once(now=NOW)
        src = r.status()["sources"]["inferred"]
        assert (src["ok"], src["error"]) == (False, "waiting for a successful UniFi scan")
        assert not [s for s in idb.suggestions.list("pending") if s["source"] == "inferred"]
        hdb.close()


def test_proxmox_alone_counts_as_a_configured_source():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        r = DiscoveryRunner(types.SimpleNamespace(data={}), {}, idb,
                            proxmox_poller=FakePoller(True), host_manager=FakeHosts([]))
        assert r.any_source_configured() is True and r.request_scan() is True
        src = r.status()["sources"]
        assert (src["proxmox"]["configured"], src["unifi"]["configured"],
                src["inferred"]["configured"]) == (True, False, False)
        idle = DiscoveryRunner(types.SimpleNamespace(data={}), {}, idb,
                               proxmox_poller=FakePoller(False))
        assert idle.any_source_configured() is False
        hdb.close()


# ── Final review fix wave ────────────────────────────────────────────────────

def failed_pve_cache():
    """pve is online, but its /qemu or /lxc fetch failed this poll."""
    cache = pve_cache()
    cache[0]["guests"], cache[0]["guests_ok"] = [], False
    return cache


def test_poller_marks_a_node_whose_guest_fetch_failed():
    """Finding 1: an online node whose guest list couldn't be fetched isn't
    cached as 'online with zero guests' indistinguishable from the real thing."""
    p = make_poller()

    def fetch(url, user, token_id, token_secret, path):
        if path == "/api2/json/nodes":
            return [{"node": "pve", "status": "online"},
                    {"node": "prodesk1", "status": "online"}]
        if path == "/api2/json/nodes/pve/qemu":
            raise OSError("timeout")
        if path.endswith("/qemu"):
            return [{"vmid": 301, "name": "Minecraft", "status": "running"}]
        return []

    p._fetch = fetch
    nodes = {n["name"]: n for n in p.fresh_nodes()}
    assert nodes["pve"]["guests_ok"] is False and nodes["pve"]["guests"] == []
    assert nodes["prodesk1"]["guests_ok"] is True


def test_snapshot_treats_a_failed_guest_list_as_unknown_guests():
    snap = proxmox_snapshot(failed_pve_cache(), cluster_status(), CONFIGS)
    assert snap["nodes"][0] == {"name": "pve", "ip": "192.168.4.237", "online": True,
                                "guests_known": False}
    assert snap["nodes"][1]["guests_known"] is True
    assert [g["node"] for g in snap["guests"]] == ["prodesk1"]
    assert snap["complete"] is False
    online = proxmox_snapshot([n for n in failed_pve_cache() if n["name"] == "prodesk1"],
                              cluster_status(), CONFIGS)
    assert online["complete"] is True
    [node] = [o for o in proxmox_observations(snap) if o["type"] == "node" and o["name"] == "pve"]
    assert node == {"type": "node", "source": "proxmox", "name": "pve", "ip": "192.168.4.237",
                    "online": True, "guests_known": False}


def failed_pve_obs():
    snap = proxmox_snapshot(failed_pve_cache(), cluster_status(), CONFIGS)
    node_of = {m: g["node"] for g in snap["guests"] for m in g["macs"]}
    return proxmox_observations(snap) + unifi_observations(
        usw_snapshot(LAB_CLIENTS), guest_macs=set(node_of), guest_node_of=node_of,
        guests_complete=snap["complete"])


def test_failed_guest_list_holds_that_nodes_guests_node_port_and_port():
    edges = [edge(60, 71, 11, ctype="virtual", source="proxmox", last_seen=NOW - 9 * DAY,
                  external_key="proxmox:guest:pve:108"),
             edge(44, 11, 1, to_port="Port 11", source="unifi", last_seen=NOW - 9 * DAY,
                  external_key="unifi:node-port:pve")]
    pending = pend(("edge:proxmox:pve:116", "proxmox"),
                   ("edge:unifi:node-port:pve", "unifi"),
                   (f"shared_port:{USW_MAC}:Port 11", "unifi"),
                   ("device:proxmox:prodesk1:999", "proxmox"))
    ch = run(failed_pve_obs(), guest_records(), edges=edges, pending=pending)
    assert ch["resolve"] == ["device:proxmox:prodesk1:999"]
    k = keys(ch)
    assert "drift:stale:conn:60" not in k and "drift:stale:conn:44" not in k
    assert not [x for x in k if x.startswith("shared_port:") or HA_MAC in x]
    assert "identity:proxmox-node:pve" not in k              # still matched
    assert "device:proxmox:prodesk1:301" in k                # other nodes still work
    # an unidentified node with an unknown guest list can still be identified
    k2 = keys(run(failed_pve_obs(), lab_records(), ip_macs={"192.168.4.237": NODE_PVE_MAC}))
    assert k2["identity:proxmox-node:pve"]["payload"]["candidate_id"] == 11


DESK_MAC = "aa:bb:cc:dd:ee:20"


def port9_obs(clients):
    return pve_obs() + unifi_observations(usw_snapshot(clients), guest_macs=set(PVE_GUESTS),
                                          guest_node_of=PVE_GUESTS)


def port9_records():
    return lab_records() + [rec(20, "Desk PC", "host", DESK_MAC)]


def test_node_sharing_a_port_with_another_device_stays_in_the_shared_port():
    """Finding 2(a): node + one other device on a port is a shared port, not
    two direct edges to the same switch port."""
    obs = port9_obs([(NODE_PRODESK_MAC, 9, None, None), (DESK_MAC, 9, None, None),
                     (MC_MAC, 9, None, None)])
    k = keys(run(obs, port9_records()))
    sp = k[f"shared_port:{USW_MAC}:Port 9"]["payload"]
    assert sp["macs"] == sorted([NODE_PRODESK_MAC, DESK_MAC])
    assert "edge:unifi:node-port:prodesk1" not in k
    assert f"edge:unifi:{DESK_MAC}" not in k


def placeholder_setup():
    records = port9_records() + [rec(30, "Unmanaged switch (USW · Port 9)", "network",
                                     network_role="switch")]
    edges = [edge(90, 56, 30), edge(91, 20, 30), edge(92, 30, 1, to_port="Port 9")]
    return records, edges


def test_node_behind_an_accepted_placeholder_is_touched_not_drifted():
    """Finding 2(b): the node sits behind an unmanaged switch on the port its
    guests vote for - that's agreement, not drift."""
    records, edges = placeholder_setup()
    # desk is off: the port carries only the node and its guest
    ch = run(port9_obs([(NODE_PRODESK_MAC, 9, None, None), (MC_MAC, 9, None, None)]),
             records, edges=edges)
    k = keys(ch)
    assert "drift:unifi:node-port:prodesk1" not in k
    assert "edge:unifi:node-port:prodesk1" not in k
    touched = {t["id"] for t in ch["touch"]}
    assert {90, 92} <= touched
    # desk is on: shared port, still behind the placeholder, nothing drifts
    ch = run(port9_obs([(NODE_PRODESK_MAC, 9, None, None), (DESK_MAC, 9, None, None),
                        (MC_MAC, 9, None, None)]), records, edges=edges)
    k = keys(ch)
    assert not [x for x in k if x.startswith("drift:")], sorted(k)
    assert {90, 91, 92} <= {t["id"] for t in ch["touch"]}


def test_held_port_holds_node_port_of_the_nodes_whose_guests_it_carries():
    """Finding 3: a held port casts no votes, so the nodes whose guests are on
    it must have their node-port facts held, not resolved."""
    clients = [(MC_MAC, 8, None, None), (MYSTERY_MAC, 8, None, None), (PI5_MAC, 7, None, None)]
    uobs = unifi_observations(usw_snapshot(clients), guest_macs=set(PVE_GUESTS),
                              guest_node_of=PVE_GUESTS, guests_complete=False)
    [held] = [o for o in uobs if o["type"] == "held"]
    assert held["nodes"] == ["prodesk1"]
    edges = [edge(45, 56, 1, to_port="Port 8", source="unifi", last_seen=NOW - 9 * DAY,
                  external_key="unifi:node-port:prodesk1")]
    pending = pend(("edge:unifi:node-port:prodesk1", "unifi"),
                   ("drift:unifi:node-port:prodesk1", "unifi"))
    ch = run(pve_obs() + uobs, lab_records(), edges=edges, pending=pending)
    assert ch["resolve"] == []
    assert "drift:stale:conn:45" not in keys(ch)


def test_node_ip_match_and_identity_candidates_skip_vm_records():
    """Finding 4: a VM can't be a Proxmox node - not by IP, ip_macs or name."""
    records = [r for r in lab_records() if r["id"] != 56] + [
        rec(57, "Some VM", "vm", "bc:24:11:00:00:57", "192.168.6.219"),
        rec(58, "pve", "vm")]
    k = keys(run(pve_obs(), records, healthy=("proxmox",),
                 ip_macs={"192.168.6.219": "bc:24:11:00:00:57"}))
    assert k["identity:proxmox-node:prodesk1"]["payload"]["candidate_id"] is None
    assert k["identity:proxmox-node:pve"]["payload"]["candidate_id"] is None


def test_accept_node_identity_rejects_vms_and_devices_already_another_node():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        vm = add_device(idb, "Some VM", "vm")
        other = add_device(idb, "HP Prodesk 405 G6 Mini", proxmox_node="prodesk1")
        payload = {"proxmox_node": "pve", "node_ip": "192.168.4.237", "candidate_id": vm,
                   "candidate_name": "Some VM", "message": "m"}
        s = pending_suggestion(idb, "identity", "proxmox", "identity:proxmox-node:pve", payload)
        ok, err, res = idb.accept_suggestion(s["id"], s["fingerprint"])
        assert (ok, err) == (False, "rejected")
        assert res["error"] == "a VM can't be a Proxmox node"
        s2 = pending_suggestion(idb, "identity", "proxmox", "identity:proxmox-node:pve#2",
                                dict(payload, candidate_id=other))
        ok, err, res = idb.accept_suggestion(s2["id"], s2["fingerprint"])
        assert (ok, err) == (False, "rejected")
        assert res["error"] == "HP Prodesk 405 G6 Mini is already Proxmox node prodesk1"
        assert idb.get(other)["properties"]["proxmox_node"] == "prodesk1"
        hdb.close()


def test_name_only_guest_match_is_vm_only_unclaimed_and_first_come():
    """Finding 5."""
    cache = pve_cache()
    cache[0]["guests"] += [{"vmid": 130, "name": "dup", "type": "qemu"},
                           {"vmid": 131, "name": "dup", "type": "qemu"}]
    cache[1]["guests"].insert(0, {"vmid": 300, "name": "Minecraft", "type": "lxc"})
    configs = dict(CONFIGS)
    configs[("pve", 130)] = {"net0": "virtio=BC:24:11:00:01:30"}
    configs[("pve", 131)] = {"net0": "virtio=BC:24:11:00:01:31"}
    configs[("prodesk1", 300)] = {"net0": "name=eth0,hwaddr=BC:24:11:00:03:00"}
    records = lab_records(pve_known=True) + [
        rec(81, "dup", "vm"),
        rec(82, "haos13.2", "host"),                 # not a VM: never a name match
        rec(83, "Minecraft", "vm", MC_MAC)]          # claimed by guest 301's MAC
    k = keys(run(pve_obs(configs=configs, cache=cache), records, healthy=("proxmox",)))
    assert k["edge:proxmox:pve:130"]["payload"]["child_id"] == 81
    assert "device:proxmox:pve:131" in k
    assert "device:proxmox:pve:108" in k
    assert k["edge:proxmox:prodesk1:301"]["payload"]["child_id"] == 83
    assert "device:proxmox:prodesk1:300" in k


def test_is_likely_guest_mac_is_shared_and_reexported():
    from netwatch import connections, discovery
    assert discovery.is_likely_guest_mac is connections.is_likely_guest_mac
    assert discovery.PROXMOX_OUI == connections.PROXMOX_OUI


def test_wifi_holds_likely_guest_macs_when_the_guest_set_is_unknown():
    """Finding 6."""
    odd = "02:00:00:00:00:99"
    records = wifi_records() + [rec(99, "Mystery box", "host", odd)]
    arp = dict(parse_arp(ARP_TEXT), **{"192.168.4.99": odd})
    hosts = [host("Custom Desktop PC", "192.168.4.89"), host("Mystery box", "192.168.4.99")]
    obs, err = wifi_observations(hosts, arp, set(), None, records, [])
    assert err is None
    assert [o["child"]["mac"] for o in obs if o["type"] == "edge"] == [DESKTOP_MAC]
    assert [o["macs"] for o in obs if o["type"] == "held"] == [[odd]]
    obs, _ = wifi_observations(hosts, arp, set(), set(), records, [])
    assert {o["child"]["mac"] for o in obs if o["type"] == "edge"} == {DESKTOP_MAC, odd}


def test_runner_passes_unknown_guest_set_to_inference_when_proxmox_fails():
    from netwatch.discovery_proxmox import ProxmoxUnavailable

    def boom(poller):
        raise ProxmoxUnavailable()

    odd = "02:00:00:00:00:99"
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = lab_idb(d)
        add_device(idb, "Mystery box", mac=odd, ip="192.168.4.99")
        r = make_runner(idb, proxmox=boom,
                        hosts=[host("Custom Desktop PC", "192.168.4.89"),
                               host("Mystery box", "192.168.4.99")],
                        arp={"192.168.4.89": DESKTOP_MAC, "192.168.4.99": odd})
        r.scan_once(now=NOW)
        k = pending_keys(idb)
        assert f"edge:inferred:{DESKTOP_MAC}" in k
        assert f"edge:inferred:{odd}" not in k
        hdb.close()
