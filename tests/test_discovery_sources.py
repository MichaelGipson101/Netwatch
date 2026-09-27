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
        {"name": "pve", "ip": "192.168.4.237", "online": True},
        {"name": "prodesk1", "ip": "192.168.6.219", "online": True},
        {"name": "NASMachineV3", "ip": "192.168.6.60", "online": False}]
    assert [(g["node"], g["vmid"]) for g in snap["guests"]] == [
        ("pve", 108), ("pve", 116), ("prodesk1", 301)]
    assert snap["guests"][0] == {
        "node": "pve", "vmid": 108, "name": "haos13.2", "guest_type": "qemu",
        "macs": [HA_MAC], "cores": 2, "memory_mb": 4096, "onboot": True, "status": "running"}
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
                        "ip": "192.168.4.237", "online": True}
    [mc] = [o for o in obs if o["type"] == "guest" and o["vmid"] == 301]
    assert mc == {"type": "guest", "source": "proxmox", "node": "prodesk1", "vmid": 301,
                  "name": "Minecraft", "guest_type": "lxc", "macs": [MC_MAC], "cores": 2,
                  "memory_mb": 4096, "onboot": True,
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
