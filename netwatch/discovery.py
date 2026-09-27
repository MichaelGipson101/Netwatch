"""Connection discovery (Netwatch 4.0 connections rework, plan 2).

Layers, outermost last:
- parse_unifi / unifi_observations: pure adapters, raw UniFi JSON ->
  observations.
- reconcile: pure; observations + current inventory/edges/suggestions ->
  a change set (suggestion upserts/resolutions, edge touches).
- fetch_unifi_classic / unifi_ssl_context / safe_error: thin I/O helpers.
- DiscoveryRunner: background thread that scans every 15 minutes and
  applies change sets through InventoryDB.apply_discovery_changes.

Discovery never edits manual edges or inventory records - it files
suggestions (spec §2). Accepting them is InventoryDB.accept_suggestion.
"""
from netwatch.connections import normalize_port
from netwatch.storage import InventoryDB

PROXMOX_OUI = "bc:24:11"

_norm_mac = InventoryDB.normalize_mac


def is_likely_guest_mac(mac):
    """Proxmox's default OUI, or a locally-administered MAC (the HAOS VM
    uses one). Only a heuristic: used to hold judgement, never to decide."""
    m = _norm_mac(mac)
    if not m:
        return False
    if m.startswith(PROXMOX_OUI):
        return True
    try:
        return bool(int(m[:2], 16) & 0x02)
    except ValueError:
        return False


def _is_port_idx(value):
    """True for a real port_idx: an int, excluding bool (a bool is an int
    subclass in Python but never a legitimate port_idx)."""
    return isinstance(value, int) and not isinstance(value, bool)


def parse_unifi(devices_payload, clients_payload):
    """Classic-API JSON (stat/device, stat/sta) -> plain dicts.

    Malformed rows (wrong type, or a port_table entry whose port_idx isn't a
    real int) are skipped rather than raising - one bad row from the
    controller shouldn't stop the whole scan."""
    switches = []
    for d in (devices_payload or {}).get("data") or []:
        if not isinstance(d, dict) or d.get("type") != "usw":
            continue
        port_rows = [p for p in (d.get("port_table") or [])
                     if isinstance(p, dict) and _is_port_idx(p.get("port_idx"))]
        ports = []
        for p in sorted(port_rows, key=lambda p: p["port_idx"]):
            idx = p.get("port_idx")
            ports.append({
                "name": p.get("name") or f"Port {idx}",
                "idx": idx,
                "up": bool(p.get("up")),
                "speed_mbps": p.get("speed") or None,
                "poe": bool(p.get("poe_enable")),
                "is_uplink": bool(p.get("is_uplink")),
            })
        lldp = [{
            "local_port_idx": n.get("local_port_idx"),
            "local_port_name": n.get("local_port_name"),
            "chassis_mac": _norm_mac(n.get("chassis_id")),
            "mgmt_ips": list(n.get("mgmt_ips") or []),
            "remote_port": normalize_port(n.get("port_id")),
        } for n in (d.get("lldp_table") or []) if isinstance(n, dict) and n.get("chassis_id")]
        switches.append({
            "mac": _norm_mac(d.get("mac")),
            "name": d.get("name") or d.get("model") or "UniFi switch",
            "ip": d.get("ip"),
            "ports": ports,
            "lldp": lldp,
        })
    clients = []
    for c in (clients_payload or {}).get("data") or []:
        if not isinstance(c, dict) or not c.get("is_wired") or not c.get("sw_mac"):
            continue
        try:
            sw_port = int(c.get("sw_port"))
        except (TypeError, ValueError):
            continue
        clients.append({
            "mac": _norm_mac(c.get("mac")),
            "ip": c.get("ip"),
            "name": c.get("name") or c.get("hostname") or None,
            "sw_mac": _norm_mac(c.get("sw_mac")),
            "sw_port": sw_port,
            "last_seen": c.get("last_seen"),
        })
    return {"switches": switches, "clients": clients}


def unifi_observations(snapshot, guest_macs=None):
    """Observations from a parsed UniFi snapshot.

    guest_macs: the authoritative set of Proxmox guest MACs, or None when
    Proxmox data isn't available - then any port carrying a likely-guest MAC
    is held (spec §2.5 rule 5) instead of guessed at.
    """
    obs = []
    switches = {s["mac"]: s for s in snapshot["switches"]}
    by_port = {}
    for c in snapshot["clients"]:
        if c["sw_mac"] in switches:
            by_port.setdefault((c["sw_mac"], c["sw_port"]), []).append(c)
    for sw_mac, idx in sorted(by_port):
        clients = by_port[(sw_mac, idx)]
        sw = switches[sw_mac]
        port = next((p["name"] for p in sw["ports"] if p["idx"] == idx), str(idx))
        if guest_macs is None and any(is_likely_guest_mac(c["mac"]) for c in clients):
            obs.append({"type": "held", "source": "unifi",
                        "macs": sorted(c["mac"] for c in clients)})
            continue
        remaining = [c for c in clients if c["mac"] not in (guest_macs or ())]
        if len(remaining) == 1:
            c = remaining[0]
            obs.append({
                "type": "edge", "source": "unifi",
                "child": {"mac": c["mac"], "ip": c["ip"], "name": c["name"]},
                "parent": {"mac": sw_mac},
                "parent_port": port, "child_port": None,
                "connection_type": "ethernet",
                "external_key": f"unifi:port:{c['mac']}",
            })
        elif len(remaining) > 1:
            obs.append({"type": "shared_port", "source": "unifi", "switch_mac": sw_mac,
                        "port": port, "macs": sorted(c["mac"] for c in remaining)})
    for sw in snapshot["switches"]:
        for n in sw["lldp"]:
            obs.append({
                "type": "lldp", "source": "unifi", "switch_mac": sw["mac"],
                "port": n["local_port_name"] or str(n["local_port_idx"]),
                "chassis_mac": n["chassis_mac"], "mgmt_ips": n["mgmt_ips"],
                "remote_port": n["remote_port"],
            })
    return obs
