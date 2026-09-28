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


# ── hosts.yaml: guest entries + add_monitored_hosts ──────────────────────────

from netwatch.hosts import add_monitored_hosts, guest_host_entry
from netwatch.network import HOSTS_WRITE_LOCK

BASE_CONFIG = {"settings": {"default_interval": 30, "ntfy_topic": "Netwatch"},
               "hosts": [{"name": "PrintServer", "ip": "192.168.6.170", "group": "Virtual Machines"},
                         {"name": "HomeAssistant", "ip": "192.168.5.110"}]}


def _write_config(d, cfg=BASE_CONFIG):
    path = os.path.join(d, "hosts.yaml")
    with open(path, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    return path


def test_guest_entry_follows_autostart():
    on = guest_host_entry({"system": "pihole", "ip": "192.168.6.14",
                           "properties": {"autostart": True}})
    assert on == {"name": "pihole", "ip": "192.168.6.14", "group": "Virtual Machines",
                  "interval": 15, "always_on": True, "alert": True}
    off = guest_host_entry({"system": "Solaris10", "ip": "192.168.6.129",
                            "properties": {"autostart": False}})
    assert (off["always_on"], off["alert"]) == (False, False)
    unknown = guest_host_entry({"system": "Jellyfin", "ip": "192.168.6.224", "properties": {}})
    assert (unknown["always_on"], unknown["alert"]) == (True, True)
    assert guest_host_entry({"system": "wow", "ip": None, "properties": {}}) is None
    assert guest_host_entry({"system": "", "ip": "1.2.3.4"}) is None


def test_add_monitored_hosts_dedupes_and_keeps_settings():
    with tempfile.TemporaryDirectory() as d:
        path = _write_config(d)
        entries = [guest_host_entry({"system": "pihole", "ip": "192.168.6.14"}),
                   guest_host_entry({"system": "printserver", "ip": "192.168.6.99"}),   # name, any case
                   guest_host_entry({"system": "haos13.2", "ip": "192.168.5.110"}),     # same IP
                   guest_host_entry({"system": "pihole", "ip": "192.168.6.15"}),        # dup in batch
                   None]
        added, hosts = add_monitored_hosts(path, entries)
        assert [a["name"] for a in added] == ["pihole"]
        with open(path) as f:
            cfg = yaml.safe_load(f)
        assert cfg["settings"] == BASE_CONFIG["settings"]
        assert [h["name"] for h in cfg["hosts"]] == ["PrintServer", "HomeAssistant", "pihole"]
        assert hosts == cfg["hosts"]
        assert os.listdir(os.path.join(d, "backups"))            # save_hosts_config backed up
        assert add_monitored_hosts(path, entries)[0] == []       # idempotent: nothing to add


def test_add_monitored_hosts_waits_for_the_shared_write_lock():
    with tempfile.TemporaryDirectory() as d:
        path = _write_config(d)
        done = threading.Event()

        def worker():
            add_monitored_hosts(path, [guest_host_entry({"system": "pihole", "ip": "192.168.6.14"})])
            done.set()

        with HOSTS_WRITE_LOCK:
            t = threading.Thread(target=worker)
            t.start()
            assert not done.wait(0.3)                            # blocked behind the holder
        t.join(5)
        assert done.is_set()


# ── API: monitor on accept, accept-all, backfill ─────────────────────────────

import json
import urllib.request
from http.server import ThreadingHTTPServer

from netwatch.auth import AuthManager
from netwatch.http_handlers import (
    _h_get_unmonitored_guests, _h_post_monitor_guests, _h_post_suggestion_accept,
    _h_post_suggestions_accept_all,
)
from netwatch.server import make_handler
from netwatch.storage import HistoryDB, InventoryDB


class FakeHostManager:
    def __init__(self):
        self.reloads = []

    def reload_from_config(self, hosts, default_interval):
        self.reloads.append(([h["name"] for h in hosts], default_interval))


def _idb(d):
    hdb = HistoryDB(os.path.join(d, "t.db"))
    idb = InventoryDB(hdb)
    assert idb.migrate_connections_v2()[0]      # production DBs are migrated
    return hdb, idb


def _add(idb, system, device_type="host", ip=None, **props):
    data = {"system": system, "device_type": device_type, "properties": props or None}
    if ip:
        data["ip"] = ip
    new_id, err = idb.create(data)
    assert err is None, err
    return new_id


def _guest_suggestion(idb, node_id, name, vmid, ip, autostart=True, mac=None):
    payload = {
        "device": {"system": name, "mac": mac, "ip": ip, "device_type": "vm",
                   "category": None,
                   "properties": {"hypervisor": "Proxmox", "guest_type": "lxc",
                                  "proxmox_node": "pve", "proxmox_vmid": vmid,
                                  "autostart": autostart}},
        "edge": {"parent_id": node_id, "parent_name": "pve", "parent_port": None,
                 "child_port": None, "connection_type": "virtual", "source": "proxmox",
                 "external_key": f"proxmox:guest:pve:{vmid}"},
        "message": f"{name} isn't in inventory"}
    fp = f"fp{vmid}"
    sid = idb.suggestions.upsert("device", "proxmox", f"device:proxmox:pve:{vmid}", payload, fp)
    return sid, fp


def _ctx(d, is_admin=True):
    return {"config_path": _write_config(d), "host_manager": FakeHostManager(),
            "settings": {"default_interval": 30}, "is_admin": is_admin}


def _names(path):
    with open(path) as f:
        return [h["name"] for h in yaml.safe_load(f)["hosts"]]


def test_accept_with_monitor_adds_the_guest_and_reloads():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = _idb(d)
        node = _add(idb, "HP EliteDesk")
        ctx = _ctx(d)
        sid, fp = _guest_suggestion(idb, node, "pihole", 120, "192.168.6.14")
        code, body = _h_post_suggestion_accept(
            f"/api/suggestions/{sid}/accept", {"fingerprint": fp, "monitor": True}, idb, ctx)
        assert code == 200 and body["monitored"] is True and "monitor_skipped" not in body
        assert _names(ctx["config_path"])[-1] == "pihole"
        assert ctx["host_manager"].reloads == [(["PrintServer", "HomeAssistant", "pihole"], 30)]
        hdb.close()


def test_accept_without_monitor_or_ip_or_admin_still_accepts():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = _idb(d)
        node = _add(idb, "HP EliteDesk")
        ctx = _ctx(d)
        s1, f1 = _guest_suggestion(idb, node, "quiet", 1, "192.168.6.1")
        s2, f2 = _guest_suggestion(idb, node, "wow", 2, None)
        s3, f3 = _guest_suggestion(idb, node, "authentik", 3, "192.168.7.23")
        s4, f4 = _guest_suggestion(idb, node, "haos", 4, "192.168.5.110")    # IP already monitored
        _, b1 = _h_post_suggestion_accept(f"/api/suggestions/{s1}/accept", {"fingerprint": f1}, idb, ctx)
        _, b2 = _h_post_suggestion_accept(f"/api/suggestions/{s2}/accept",
                                          {"fingerprint": f2, "monitor": True}, idb, ctx)
        _, b3 = _h_post_suggestion_accept(f"/api/suggestions/{s3}/accept",
                                          {"fingerprint": f3, "monitor": True}, idb,
                                          dict(ctx, is_admin=False))
        _, b4 = _h_post_suggestion_accept(f"/api/suggestions/{s4}/accept",
                                          {"fingerprint": f4, "monitor": True}, idb, ctx)
        assert b1["ok"] and "monitored" not in b1
        assert b2["ok"] and b2["monitor_skipped"] == "no_ip"
        assert b3["ok"] and b3["monitor_skipped"] == "admin_required"
        assert b4["ok"] and b4["monitor_skipped"] == "already_monitored"
        assert _names(ctx["config_path"]) == ["PrintServer", "HomeAssistant"]
        assert ctx["host_manager"].reloads == []
        hdb.close()


def test_accept_all_monitors_only_the_items_that_asked_in_one_write():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = _idb(d)
        node = _add(idb, "HP EliteDesk")
        ctx = _ctx(d)
        s1, f1 = _guest_suggestion(idb, node, "immich", 1, "192.168.6.13")
        s2, f2 = _guest_suggestion(idb, node, "Solaris10", 2, "192.168.6.129", autostart=False)
        s3, f3 = _guest_suggestion(idb, node, "paperless", 3, "192.168.7.11")
        code, body = _h_post_suggestions_accept_all({"items": [
            {"id": s1, "fingerprint": f1, "monitor": True},
            {"id": s2, "fingerprint": f2, "monitor": True},
            {"id": s3, "fingerprint": f3}]}, idb, ctx)
        r = {x["id"]: x for x in body["results"]}
        assert r[s1]["monitored"] and r[s2]["monitored"] and "monitored" not in r[s3]
        with open(ctx["config_path"]) as f:
            hosts = {h["name"]: h for h in yaml.safe_load(f)["hosts"]}
        assert set(hosts) == {"PrintServer", "HomeAssistant", "immich", "Solaris10"}
        assert (hosts["Solaris10"]["always_on"], hosts["Solaris10"]["alert"]) == (False, False)
        assert len(ctx["host_manager"].reloads) == 1
        hdb.close()


def test_backfill_lists_and_monitors_unmonitored_guests():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = _idb(d)
        ctx = _ctx(d)
        pihole = _add(idb, "pihole", "vm", ip="192.168.6.14", proxmox_vmid=120, autostart=True)
        _add(idb, "printserver", "vm", ip="192.168.6.170", proxmox_vmid=126)   # monitored by name/IP
        _add(idb, "wow", "vm", proxmox_vmid=306)                               # no IP yet
        _add(idb, "Hand VM", "vm", ip="192.168.6.50")                          # not a Proxmox guest
        solaris = _add(idb, "Solaris10", "vm", ip="192.168.6.129", proxmox_vmid=116, autostart=False)
        code, body = _h_get_unmonitored_guests(idb, ctx["config_path"])
        assert code == 200 and body["no_ip"] == 1
        assert body["guests"] == [
            {"id": pihole, "name": "pihole", "ip": "192.168.6.14", "alert": True},
            {"id": solaris, "name": "Solaris10", "ip": "192.168.6.129", "alert": False}]
        code, out = _h_post_monitor_guests({"ids": [pihole, solaris, 99999]}, idb, ctx)
        assert code == 200 and out["added"] == [pihole, solaris]
        assert out["skipped"] == {"99999": "not_a_guest"}
        assert _h_get_unmonitored_guests(idb, ctx["config_path"])[1]["guests"] == []
        assert _h_post_monitor_guests({"ids": "all"}, idb, ctx)[0] == 400
        assert _h_post_monitor_guests({"ids": [True]}, idb, ctx)[0] == 400
        hdb.close()


def test_monitor_guests_route_is_admin_only(tmp_path):
    hdb, idb = _idb(str(tmp_path))
    auth = AuthManager(str(tmp_path / "auth.json"))
    auth.create_user("root", "password123", admin=True)
    auth.create_user("bob", "password123")
    bob = auth.make_session_cookie("bob")
    handler = make_handler(FakeHostManager(), {}, _write_config(str(tmp_path)),
                           auth_manager=auth, inventory_db=idb)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=server.handle_request)
    t.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/api/discovery/monitor-guests",
            data=b'{"ids": []}', method="POST",
            headers={"Cookie": f"nw_session={bob}", "Content-Type": "application/json",
                     "X-CSRF-Token": auth.csrf_token_for_cookie(bob)})
        try:
            urllib.request.urlopen(req)
            code = 200
        except urllib.error.HTTPError as err:
            code = err.code
        assert code == 403
    finally:
        server.server_close(); t.join()
    hdb.close()
