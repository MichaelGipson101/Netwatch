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
