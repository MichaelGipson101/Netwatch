"""Guest monitoring from discovery: resolve Proxmox guest IPs, fill them into
inventory, and add accepted guests to hosts.yaml."""
import os
import tempfile
import threading
import urllib.error

import yaml

from netwatch.discovery_proxmox import fetch_proxmox, pick_guest_ip, proxmox_observations

GUEST_MAC = "bc:24:11:f7:7e:65"

LXC_IFACES = [
    {"name": "lo", "hardware-address": "00:00:00:00:00:00", "inet": "127.0.0.1/8",
     "ip-addresses": [{"ip-address": "127.0.0.1", "ip-address-type": "inet", "prefix": "8"}]},
    {"name": "docker0", "hardware-address": "02:42:ac:11:00:01",
     "ip-addresses": [{"ip-address": "172.17.0.1", "ip-address-type": "inet", "prefix": "16"}]},
    {"name": "tailscale0", "hwaddr": "00:00:00:00:00:00", "inet": "100.87.134.73/32"},
    {"name": "eth0", "hardware-address": "BC:24:11:F7:7E:65", "inet": "192.168.6.14/22",
     "ip-addresses": [{"ip-address": "fe80::1", "ip-address-type": "inet6", "prefix": "64"},
                      {"ip-address": "192.168.6.14", "ip-address-type": "inet", "prefix": "22"}]},
]
QEMU_AGENT = {"result": [
    {"name": "lo", "hardware-address": "00:00:00:00:00:00",
     "ip-addresses": [{"ip-address": "127.0.0.1", "ip-address-type": "ipv4", "prefix": 8}]},
    {"name": "ens18", "hardware-address": "bc:24:11:54:3d:18",
     "ip-addresses": [{"ip-address": "192.168.6.125", "ip-address-type": "ipv4", "prefix": 22}]},
]}


def test_pick_guest_ip_uses_the_config_macs_interface():
    assert pick_guest_ip([GUEST_MAC], LXC_IFACES) == "192.168.6.14"
    assert pick_guest_ip(["bc:24:11:54:3d:18"], QEMU_AGENT) == "192.168.6.125"


def test_pick_guest_ip_never_guesses():
    assert pick_guest_ip(["bc:24:11:00:00:01"], LXC_IFACES) is None      # no matching NIC
    assert pick_guest_ip([], LXC_IFACES) is None
    assert pick_guest_ip([GUEST_MAC], None) is None
    assert pick_guest_ip([GUEST_MAC], {"result": "garbage"}) is None
    link_local = [{"hardware-address": GUEST_MAC, "inet": "169.254.3.3/16"}]
    assert pick_guest_ip([GUEST_MAC], link_local) is None


def test_pick_guest_ip_prefers_net0():
    two_nics = [{"hardware-address": "bc:24:11:00:00:02", "inet": "10.0.0.2/24"},
                {"hardware-address": "bc:24:11:00:00:01", "inet": "192.168.6.9/22"}]
    assert pick_guest_ip(["bc:24:11:00:00:01", "bc:24:11:00:00:02"], two_nics) == "192.168.6.9"


def _poller(nodes, configs, ifaces, fail=()):
    class Poller:
        paths = []

        def fresh_nodes(self):
            return nodes

        def api_get(self, path):
            self.paths.append(path)
            if path == "/api2/json/cluster/status":
                return []
            parts = path.split("/")
            key = (parts[4], int(parts[6]))
            if path.endswith("/config"):
                return configs[key]
            if key in fail:
                raise urllib.error.HTTPError(path, 500, "QEMU guest agent is not running", {}, None)
            return ifaces[key]
    return Poller()


def test_fetch_proxmox_reads_running_guests_ips():
    nodes = [{"name": "pve", "status": "online", "guests": [
        {"vmid": 120, "type": "lxc", "name": "pihole", "status": "running"},
        {"vmid": 121, "type": "qemu", "name": "TrueNAS", "status": "running"},
        {"vmid": 116, "type": "qemu", "name": "Solaris10", "status": "running"},
        {"vmid": 103, "type": "qemu", "name": "netbsd", "status": "stopped"}]}]
    configs = {("pve", 120): {"net0": f"name=eth0,hwaddr={GUEST_MAC.upper()},bridge=vmbr0"},
               ("pve", 121): {"net0": "virtio=BC:24:11:54:3D:18,bridge=vmbr0"},
               ("pve", 116): {"net0": "e1000=BC:24:11:FB:57:0F,bridge=vmbr0"},
               ("pve", 103): {"net0": "virtio=BC:24:11:6D:7D:40,bridge=vmbr0"}}
    p = _poller(nodes, configs, {("pve", 120): LXC_IFACES, ("pve", 121): QEMU_AGENT},
                fail={("pve", 116)})
    snap = fetch_proxmox(p)
    ips = {g["name"]: g["ip"] for g in snap["guests"]}
    assert ips == {"pihole": "192.168.6.14", "TrueNAS": "192.168.6.125",
                   "Solaris10": None, "netbsd": None}
    assert snap["failed"] == [] and snap["complete"]              # a missing IP is no hold
    assert "/api2/json/nodes/pve/lxc/120/interfaces" in p.paths
    assert "/api2/json/nodes/pve/qemu/121/agent/network-get-interfaces" in p.paths
    assert not any("/103/" in x and "config" not in x for x in p.paths)   # stopped: not asked
    obs = {o["name"]: o for o in proxmox_observations(snap) if o["type"] == "guest"}
    assert obs["pihole"]["ip"] == "192.168.6.14" and obs["netbsd"]["ip"] is None
