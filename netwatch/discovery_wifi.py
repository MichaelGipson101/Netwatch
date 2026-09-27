"""Wifi inference source (Netwatch 4.0 connections rework, plan 4, spec §2.4).

The eero has no local API, so wifi edges are inferred. An always-on monitored
host that's up, answers on the Pi's L2 (ARP), isn't seen wired by UniFi and
isn't a Proxmox guest is presumed to be a wifi client of the gateway. The
runner only calls this in a scan where UniFi succeeded; otherwise every wired
device would look wireless.
"""
from netwatch.connections import NETWORK_LINK_TYPES, is_likely_guest_mac
from netwatch.storage import InventoryDB

_norm_mac = InventoryDB.normalize_mac
NO_GATEWAY = "no device has network role 'gateway'"
_SKIP_TYPES = ("network", "vm")   # infrastructure and guests are never wifi clients here


def parse_arp(text):
    """/proc/net/arp -> {ip: mac}, complete entries (flag 0x2) only."""
    out = {}
    for line in (text or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        ip, flags, mac = parts[0], parts[2], parts[3]
        try:
            if not int(flags, 16) & 0x2:
                continue
        except ValueError:
            continue
        mac = _norm_mac(mac)
        if mac and mac != "00:00:00:00:00:00":
            out[ip] = mac
    return out


def read_arp(path="/proc/net/arp"):
    try:
        with open(path, encoding="ascii", errors="replace") as f:
            return parse_arp(f.read())
    except OSError:
        return {}


def find_gateway(records):
    gateways = sorted((r for r in records
                       if (r.get("properties") or {}).get("network_role") == "gateway"),
                      key=lambda r: r["id"])
    return gateways[0] if gateways else None


def wifi_observations(hosts, arp, wired_macs, guest_macs, records, edges):
    """(observations, error). A linked always-on host that's down or missing
    from ARP is held, so its inferred edge neither ages nor resolves.

    guest_macs: the Proxmox guest MACs, or None when they're unknown this
    scan - then a candidate with a likely-guest MAC is held, not edged."""
    gateway = find_gateway(records)
    if gateway is None:
        return [], NO_GATEWAY
    by_mac = {}
    for r in records:
        for m in [r.get("mac")] + list((r.get("properties") or {}).get("mac_aliases") or []):
            n = _norm_mac(m)
            if n:
                by_mac.setdefault(n, r)
    links = {}
    for e in edges:
        if e["connection_type"] in NETWORK_LINK_TYPES:
            links.setdefault(e["from_device_id"], []).append(e)
    arp_macs = set(arp.values())
    obs, seen = [], set()
    for h in hosts:
        if not h.get("always_on"):
            continue
        mac = _norm_mac((h.get("specs") or {}).get("mac")) or arp.get(h.get("ip") or "")
        rec = by_mac.get(mac) if mac else None
        if (rec is None or rec["id"] in seen or rec["id"] == gateway["id"]
                or rec.get("device_type") in _SKIP_TYPES):
            continue
        seen.add(rec["id"])
        if mac in wired_macs or (guest_macs is not None and mac in guest_macs):
            continue
        # Already linked somewhere other than wifi-to-the-gateway: not ours to judge.
        if any(e["to_device_id"] != gateway["id"] or e["connection_type"] != "wifi"
               for e in links.get(rec["id"], [])):
            continue
        if (not h.get("is_up") or mac not in arp_macs
                or (guest_macs is None and is_likely_guest_mac(mac))):
            obs.append({"type": "held", "source": "inferred", "macs": [mac]})
            continue
        obs.append({
            "type": "edge", "source": "inferred",
            "child": {"mac": mac, "ip": h.get("ip"), "name": h.get("name")},
            "parent": {"inventory_id": gateway["id"]},
            "parent_port": None, "child_port": None,
            "connection_type": "wifi", "external_key": f"inferred:wifi:{mac}",
        })
    return obs, None
