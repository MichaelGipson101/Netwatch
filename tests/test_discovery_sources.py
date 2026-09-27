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
