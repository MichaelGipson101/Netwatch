"""Proxmox discovery source (Netwatch 4.0 connections rework, plan 4).

- parse_net_macs / proxmox_snapshot / proxmox_observations: pure.
- fetch_proxmox: I/O through ProxmoxPoller, reusing its credentials, TLS
  settings and node/guest cache (spec §2.3).

A guest whose config can't be read, or any guest on an offline node or on
a node whose guest list couldn't be fetched, is never guessed at: it becomes a hold so its suggestions neither resolve nor
age until Proxmox answers again.
"""
import re
import urllib.parse

from netwatch.storage import InventoryDB

_norm_mac = InventoryDB.normalize_mac
_NET_KEY = re.compile(r"^net(\d+)$")
_MAC_VALUE = re.compile(r"=((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})(?=,|$)")


class ProxmoxUnavailable(Exception):
    """The Proxmox poller has no fresh node/guest data to build on."""


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_net_macs(config):
    """MACs from a qemu/lxc config's netN entries, in N order: qemu
    'virtio=BC:24:11:..,bridge=vmbr0', lxc 'name=eth0,...,hwaddr=BC:..'."""
    config = config or {}
    keys = sorted((k for k in config if _NET_KEY.match(str(k))),
                  key=lambda k: int(_NET_KEY.match(k).group(1)))
    macs = []
    for key in keys:
        for raw in _MAC_VALUE.findall(str(config[key])):
            mac = _norm_mac(raw)
            if mac and mac not in macs:
                macs.append(mac)
    return macs


def proxmox_snapshot(nodes, cluster_status, configs):
    """Join the poller's node/guest cache, /cluster/status (node IPs) and
    per-guest configs (keyed (node, vmid); a non-dict means the read failed)."""
    ips = {e.get("name"): e.get("ip") for e in (cluster_status or [])
           if isinstance(e, dict) and e.get("type") == "node" and e.get("name")}
    out_nodes, guests, failed = [], [], []
    for n in nodes or []:
        name = n.get("name")
        if not name:
            continue
        online = n.get("status") == "online"
        # An online node whose guest list the poller couldn't fetch is, for
        # guest purposes, as unknowable as an offline one: held, not "empty".
        guests_known = online and n.get("guests_ok", True) is not False
        out_nodes.append({"name": name, "ip": ips.get(name), "online": online,
                          "guests_known": guests_known})
        if not guests_known:
            continue
        for g in n.get("guests") or []:
            vmid, kind = _int(g.get("vmid")), g.get("type")
            if vmid is None or kind not in ("qemu", "lxc"):
                continue
            cfg = (configs or {}).get((name, vmid))
            if not isinstance(cfg, dict):
                failed.append({"node": name, "vmid": vmid})
                continue
            guests.append({
                "node": name, "vmid": vmid,
                "name": (g.get("name") or cfg.get("name") or cfg.get("hostname")
                         or f"{kind} {vmid}"),
                "guest_type": kind,
                "macs": parse_net_macs(cfg),
                "cores": _int(cfg.get("cores")),
                "memory_mb": _int(cfg.get("memory")),
                "onboot": bool(_int(cfg.get("onboot"))),
                "status": g.get("status"),
            })
    return {"nodes": out_nodes, "guests": guests, "failed": failed,
            "complete": not failed and all(n["guests_known"] for n in out_nodes)}


def proxmox_observations(snap):
    obs = [{"type": "node", "source": "proxmox", "name": n["name"], "ip": n["ip"],
            "online": n["online"], "guests_known": n["guests_known"]}
           for n in snap["nodes"]]
    for g in snap["guests"]:
        obs.append({
            "type": "guest", "source": "proxmox", "node": g["node"], "vmid": g["vmid"],
            "name": g["name"], "guest_type": g["guest_type"], "macs": list(g["macs"]),
            "cores": g["cores"], "memory_mb": g["memory_mb"], "onboot": g["onboot"],
            "external_key": f"proxmox:guest:{g['node']}:{g['vmid']}",
        })
    for f in snap["failed"]:
        obs.append({"type": "held_guest", "source": "proxmox",
                    "node": f["node"], "vmid": f["vmid"]})
    return obs


def fetch_proxmox(poller):
    """One discovery read: the poller's (fresh) node/guest list, node IPs from
    /cluster/status, and each online guest's config for its MACs. A failed
    config read is recorded, not raised, so one slow guest can't sink the scan."""
    nodes = poller.fresh_nodes()
    if nodes is None:
        raise ProxmoxUnavailable()
    cluster = poller.api_get("/api2/json/cluster/status")
    configs = {}
    for n in nodes:
        name = n.get("name")
        if not name or n.get("status") != "online" or n.get("guests_ok", True) is False:
            continue
        for g in n.get("guests") or []:
            vmid, kind = _int(g.get("vmid")), g.get("type")
            if vmid is None or kind not in ("qemu", "lxc"):
                continue
            path = f"/api2/json/nodes/{urllib.parse.quote(name, safe='')}/{kind}/{vmid}/config"
            try:
                configs[(name, vmid)] = poller.api_get(path)
            except Exception:
                configs[(name, vmid)] = None
    return proxmox_snapshot(nodes, cluster, configs)
