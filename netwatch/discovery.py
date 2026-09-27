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
import time

from netwatch.connections import (
    NETWORK_LINK_TYPES, canonical_port, fingerprint, normalize_port, orient_edge,
    resolve_ports,
)
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
                        "macs": sorted(c["mac"] for c in clients),
                        "switch_mac": sw_mac, "port": port})
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
            is_uplink = next((p["is_uplink"] for p in sw["ports"]
                              if p["idx"] == n["local_port_idx"]), False)
            obs.append({
                "type": "lldp", "source": "unifi", "switch_mac": sw["mac"],
                "port": n["local_port_name"] or str(n["local_port_idx"]),
                "chassis_mac": n["chassis_mac"], "mgmt_ips": n["mgmt_ips"],
                "remote_port": n["remote_port"],
                "is_uplink": is_uplink, "local_port_idx": n["local_port_idx"],
            })
    return obs


STALE_AFTER_SECONDS = 7 * 86400
SOURCE_LABELS = {"unifi": "UniFi", "proxmox": "Proxmox", "inferred": "Inference"}


def _index_records(records):
    by_id, by_mac, by_ip = {}, {}, {}
    for r in records:
        by_id[r["id"]] = r
        props = r.get("properties") or {}
        for m in [r.get("mac")] + list(props.get("mac_aliases") or []):
            n = _norm_mac(m)
            if n:
                by_mac[n] = r
        if r.get("ip"):
            by_ip[str(r["ip"]).strip()] = r
    return by_id, by_mac, by_ip


def reconcile(observations, *, records, edges, pending, healthy_sources, now,
              live_ports_for=None, healthy_since=None):
    """Pure: decide what one scan means. See spec §2.5 for the rules."""
    if not healthy_sources:
        return {"upserts": [], "resolve": [], "touch": []}
    by_id, by_mac, by_ip = _index_records(records)
    upserts, touch, held_macs, held_ports = {}, {}, set(), set()

    def suggest(kind, source, key, payload, fp_basis):
        upserts[key] = {"kind": kind, "source": source, "subject_key": key,
                        "payload": payload, "fp": fingerprint([kind] + list(fp_basis))}

    def ports_of(rec):
        return resolve_ports(rec, live_ports_for(rec) if live_ports_for else None)

    def port_key(port, ports):
        return canonical_port(port, ports) if ports else normalize_port(port)

    def links_from(child_id):
        return [e for e in edges if e["from_device_id"] == child_id
                and e["connection_type"] in NETWORK_LINK_TYPES]

    def child_is_held(rec):
        if rec is None:
            return False
        macs = [rec.get("mac")] + list((rec.get("properties") or {}).get("mac_aliases") or [])
        return any(_norm_mac(m) in held_macs for m in macs)

    def lldp_orientation(switch, neighbour, is_uplink):
        child, parent, ambiguous = orient_edge(switch, neighbour)
        if ambiguous:
            return (switch, neighbour) if is_uplink else (neighbour, switch)
        return child, parent

    def observe_edge(child, parent, parent_port, child_port, subject_mac, ext_key,
                     ctype, source):
        ports = ports_of(parent)
        pport = port_key(parent_port, ports)
        proposed = {"child_id": child["id"], "child_name": child["system"],
                    "parent_id": parent["id"], "parent_name": parent["system"],
                    "parent_port": pport, "child_port": child_port,
                    "connection_type": ctype, "source": source, "external_key": ext_key}
        links = links_from(child["id"])
        same = [e for e in links if e["to_device_id"] == parent["id"]]
        if same:
            current = same[0]
            stored = port_key(current["to_port"], ports)
            if current.get("source", "manual") != "manual":
                touch[current["id"]] = pport if (pport and stored != pport) else None
                return
            touch[current["id"]] = None
            if pport is None or stored == pport:
                return
        elif links:
            current = links[0]
        else:
            where = f"{parent['system']} · {pport}" if pport else parent["system"]
            suggest("edge", source, f"edge:{source}:{subject_mac}",
                    dict(proposed, message=f"{child['system']} is wired to {where}"),
                    [child["id"], parent["id"], pport, ctype])
            return
        cur_parent = by_id.get(current["to_device_id"], {}).get("system") or "?"
        cur_port = current.get("to_port")
        cur_desc = f"{cur_parent}{' · ' + cur_port if cur_port else ''} ({current['connection_type']})"
        new_desc = f"{parent['system']}{' · ' + pport if pport else ''} ({ctype})"
        label = SOURCE_LABELS.get(source, source)
        suggest("drift", source, f"drift:{source}:{subject_mac}", {
            "connection_id": current["id"], "action": "replace",
            "child_id": child["id"], "child_name": child["system"],
            "current": {"parent_id": current["to_device_id"], "parent_name": cur_parent,
                        "parent_port": cur_port,
                        "connection_type": current["connection_type"]},
            "proposed": proposed,
            "message": f"Your graph: {child['system']} → {cur_desc}. {label} sees: {new_desc}",
        }, [current["id"], parent["id"], pport, ctype])

    # F2(a): a neighbour already wired via the client path (a wired-client
    # `edge` observation resolving to the same inventory record) is never
    # also processed via LLDP - precomputed so order doesn't matter.
    client_edge_child_ids = set()
    for o in observations:
        if o["source"] in healthy_sources and o["type"] == "edge":
            child = by_mac.get(o["child"]["mac"])
            if child is not None:
                client_edge_child_ids.add(child["id"])

    # F2(b): when more than one LLDP neighbour would make a switch its
    # child, keep exactly one (uplink first, then lowest local port index,
    # then lowest port name) - independent of lldp_table order.
    lldp_parent_candidates = {}
    for o in observations:
        if o["source"] not in healthy_sources or o["type"] != "lldp":
            continue
        switch = by_mac.get(o["switch_mac"])
        if switch is None:
            continue
        neighbour = by_mac.get(o["chassis_mac"])
        if neighbour is None:
            for ip in o["mgmt_ips"]:
                if ip in by_ip:
                    neighbour = by_ip[ip]
                    break
        if (neighbour is None or neighbour["id"] == switch["id"]
                or neighbour["id"] in client_edge_child_ids):
            continue
        child, _parent = lldp_orientation(switch, neighbour, o["is_uplink"])
        if child is switch:
            lldp_parent_candidates.setdefault(switch["id"], []).append(o)
    lldp_winner = {}
    for switch_id, candidates in lldp_parent_candidates.items():
        candidates.sort(key=lambda o: (
            not o["is_uplink"],
            o["local_port_idx"] if o["local_port_idx"] is not None else float("inf"),
            o["port"]))
        lldp_winner[switch_id] = candidates[0]

    for o in observations:
        if o["source"] not in healthy_sources:
            continue
        t = o["type"]
        if t == "held":
            held_macs.update(o["macs"])
            switch = by_mac.get(o.get("switch_mac"))
            port_name = o.get("port")
            if port_name is not None:
                held_ports.add((o["switch_mac"],
                                port_key(port_name, ports_of(switch) if switch else None)))
        elif t == "edge":
            parent = by_mac.get(o["parent"]["mac"])
            if parent is None:
                continue
            mac = o["child"]["mac"]
            child = by_mac.get(mac)
            if child is not None:
                observe_edge(child, parent, o["parent_port"], o.get("child_port"), mac,
                             o["external_key"], o["connection_type"], o["source"])
                continue
            name = o["child"].get("name") or f"Unknown device {mac}"
            pport = port_key(o["parent_port"], ports_of(parent))
            suggest("device", o["source"], f"device:{o['source']}:{mac}", {
                "device": {"system": name, "mac": mac, "ip": o["child"].get("ip"),
                           "device_type": "host", "category": None},
                "edge": {"parent_id": parent["id"], "parent_name": parent["system"],
                         "parent_port": pport, "child_port": o.get("child_port"),
                         "connection_type": o["connection_type"],
                         "source": o["source"], "external_key": o["external_key"]},
                "message": (f"{name} ({mac}) is wired to {parent['system']} · {pport} "
                            "but isn't in inventory"),
            }, [mac, parent["id"], pport])
        elif t == "shared_port":
            switch = by_mac.get(o["switch_mac"])
            if switch is None:
                continue
            ports = ports_of(switch)
            pport = port_key(o["port"], ports)
            on_port = [e for e in edges if e["to_device_id"] == switch["id"]
                       and e["connection_type"] in NETWORK_LINK_TYPES
                       and port_key(e["to_port"], ports) == pport]
            if on_port:
                behind = {e["from_device_id"] for e in on_port}
                for e in edges:
                    if e in on_port or e["to_device_id"] in behind:
                        touch.setdefault(e["id"], None)
                continue
            matched = [{"id": by_mac[m]["id"], "name": by_mac[m]["system"], "mac": m}
                       for m in o["macs"] if m in by_mac]
            suggest("shared_port", o["source"], f"shared_port:{o['switch_mac']}:{pport}", {
                "switch_id": switch["id"], "switch_name": switch["system"], "port": pport,
                "macs": o["macs"], "matched": matched,
                "message": (f"{len(o['macs'])} devices share {switch['system']} · {pport}. "
                            "Unmanaged switch or AP behind it?"),
            }, [switch["id"], pport, o["macs"]])
        elif t == "lldp":
            switch = by_mac.get(o["switch_mac"])
            if switch is None:
                continue
            neighbour, via_ip = by_mac.get(o["chassis_mac"]), None
            if neighbour is None:
                for ip in o["mgmt_ips"]:
                    if ip in by_ip:
                        neighbour, via_ip = by_ip[ip], ip
                        break
            if neighbour is None or via_ip:
                suggest("identity", o["source"], f"identity:lldp:{o['chassis_mac']}", {
                    "chassis_mac": o["chassis_mac"], "mgmt_ips": o["mgmt_ips"],
                    "switch_id": switch["id"], "switch_name": switch["system"],
                    "port": o["port"],
                    "candidate_id": neighbour["id"] if neighbour else None,
                    "candidate_name": neighbour["system"] if neighbour else None,
                    "message": (
                        f"Is LLDP neighbour {o['chassis_mac']} on {switch['system']} · "
                        f"{o['port']} your {neighbour['system']}? (it answers on {via_ip})"
                        if neighbour else
                        f"Unknown LLDP neighbour {o['chassis_mac']} on {switch['system']} · "
                        f"{o['port']}: which device is it?"),
                }, [o["chassis_mac"], neighbour["id"] if neighbour else None])
            if neighbour is None or neighbour["id"] == switch["id"]:
                continue
            if neighbour["id"] in client_edge_child_ids:
                continue
            child, _parent = lldp_orientation(switch, neighbour, o["is_uplink"])
            if child is switch and lldp_winner.get(switch["id"]) is not o:
                continue
            ext = f"unifi:lldp:{o['switch_mac']}:{o['port']}"
            if child is switch:
                observe_edge(switch, neighbour, o["remote_port"], o["port"],
                             o["switch_mac"], ext, "ethernet", o["source"])
            else:
                observe_edge(neighbour, switch, o["port"], o["remote_port"],
                             o["chassis_mac"], ext, "ethernet", o["source"])

    for e in edges:
        if (e.get("source") not in healthy_sources or e["id"] in touch):
            continue
        child = by_id.get(e["from_device_id"])
        if child is None or child_is_held(child):
            continue
        streak_start = (healthy_since or {}).get(e.get("source"))
        if streak_start is None or streak_start > now - STALE_AFTER_SECONDS:
            continue
        seen = e.get("last_seen") or e.get("updated_at") or 0
        if seen >= now - STALE_AFTER_SECONDS:
            continue
        parent = by_id.get(e["to_device_id"], {})
        label = SOURCE_LABELS.get(e["source"], e["source"])
        when = time.strftime("%Y-%m-%d", time.localtime(seen)) if seen else "never"
        suggest("drift", e["source"], f"drift:stale:conn:{e['id']}", {
            "connection_id": e["id"], "action": "remove",
            "child_id": child["id"], "child_name": child["system"],
            "parent_id": e["to_device_id"], "parent_name": parent.get("system"),
            "parent_port": e.get("to_port"), "last_seen": seen,
            "message": (f"{child['system']} → {parent.get('system') or '?'} hasn't been "
                        f"seen by {label} since {when}. Remove it?"),
        }, ["stale", e["id"]])

    held_keys = {f"{kind}:{src}:{m}" for m in held_macs for src in healthy_sources
                 for kind in ("edge", "device", "drift")}
    held_keys |= {f"shared_port:{switch_mac}:{port}" for switch_mac, port in held_ports}
    held_keys |= {f"drift:stale:conn:{e['id']}" for e in edges
                  if child_is_held(by_id.get(e["from_device_id"]))}
    resolve = [s["subject_key"] for s in pending
               if s["source"] in healthy_sources and s["subject_key"] not in upserts
               and s["subject_key"] not in held_keys]
    return {"upserts": list(upserts.values()), "resolve": resolve,
            "touch": [{"id": k, "parent_port": v} for k, v in touch.items()]}
