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
import json
import logging
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

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
            "local_port_idx": (n.get("local_port_idx")
                               if _is_port_idx(n.get("local_port_idx")) else None),
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


def unifi_observations(snapshot, guest_macs=None, guest_node_of=None, guests_complete=True):
    """Observations from a parsed UniFi snapshot.

    guest_macs: the authoritative set of Proxmox guest MACs, or None when
    Proxmox data isn't available. Then any port carrying a likely-guest MAC
    is held (spec §2.5 rule 5) instead of guessed at.
    guest_node_of: {guest MAC: node name}. When given, the guests seen on a
    port vote for their node's port (spec §2.5 rule 2): each node gets one
    edge to the port with the most of its guests (ties: lowest port index).
    guests_complete: False when a node was offline or a guest config couldn't
    be read. Then a port whose leftover MACs include a likely-guest MAC is held
    too, since it may be a guest Proxmox couldn't tell us about.
    """
    obs = []
    switches = {s["mac"]: s for s in snapshot["switches"]}
    by_port = {}
    for c in snapshot["clients"]:
        if c["sw_mac"] in switches:
            by_port.setdefault((c["sw_mac"], c["sw_port"]), []).append(c)
    votes = {}  # node -> [(-guest_count, port_idx, switch_mac, port_name)]
    for sw_mac, idx in sorted(by_port):
        clients = by_port[(sw_mac, idx)]
        sw = switches[sw_mac]
        port_info = next((p for p in sw["ports"] if p["idx"] == idx), None)
        if port_info and port_info["is_uplink"]:
            # Everything upstream shows up on the uplink; LLDP covers that link.
            continue
        port = port_info["name"] if port_info else str(idx)
        held = {"type": "held", "source": "unifi",
                "macs": sorted(c["mac"] for c in clients), "switch_mac": sw_mac, "port": port}
        if guest_macs is None and any(is_likely_guest_mac(c["mac"]) for c in clients):
            obs.append(held)
            continue
        remaining = [c for c in clients if c["mac"] not in (guest_macs or ())]
        if (guest_macs is not None and not guests_complete
                and any(is_likely_guest_mac(c["mac"]) for c in remaining)):
            obs.append(held)
            continue
        if guest_node_of:
            counts = {}
            for c in clients:
                node = guest_node_of.get(c["mac"])
                if node:
                    counts[node] = counts.get(node, 0) + 1
            for node, n in counts.items():
                votes.setdefault(node, []).append((-n, idx, sw_mac, port))
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
    for node in sorted(votes):
        _count, _idx, sw_mac, port = min(votes[node])
        obs.append({
            "type": "edge", "source": "unifi",
            "child": {"proxmox_node": node},
            "parent": {"mac": sw_mac},
            "parent_port": port, "child_port": None,
            "connection_type": "ethernet",
            "external_key": f"unifi:node-port:{node}",
        })
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


def _match_node(o, records, by_ip):
    """Proxmox node -> inventory record: node IP, then properties.proxmox_node.

    Skips VM records in the properties.proxmox_node scan: on a VM that
    property means "runs on node X", not "is node X" - matching it here
    would misattribute the node-port edge to a guest (fix round 1, #1)."""
    ip = str(o.get("ip") or "").strip()
    if ip and ip in by_ip:
        return by_ip[ip]
    for r in records:
        if (r.get("device_type") != "vm"
                and (r.get("properties") or {}).get("proxmox_node") == o["name"]):
            return r
    return None


def _match_guest(o, records, by_mac):
    """Proxmox guest -> inventory record, first hit wins (spec §2.3): recorded
    vmid (and node, if recorded), then any of its MACs, then its name.

    Returns (record, how) where how is "vmid", "mac" or "name" - callers use
    it to decide whether a match is trustworthy enough to fill properties
    from (fix round 1, #4: a name-only match is too weak for that)."""
    for r in records:
        p = r.get("properties") or {}
        vmid = p.get("proxmox_vmid")
        if (vmid not in (None, "") and str(vmid) == str(o["vmid"])
                and p.get("proxmox_node") in (None, "", o["node"])):
            return r, "vmid"
    for m in o["macs"]:
        if m in by_mac:
            return by_mac[m], "mac"
    name = str(o.get("name") or "").strip().lower()
    if name:
        for r in records:
            if str(r.get("system") or "").strip().lower() == name:
                return r, "name"
    return None, None


def _most_common_category(records, device_type):
    counts = {}
    for r in records:
        cat = str(r.get("category") or "").strip()
        if r.get("device_type") == device_type and cat:
            counts[cat] = counts.get(cat, 0) + 1
    return min(counts, key=lambda c: (-counts[c], c)) if counts else None


def reconcile(observations, *, records, edges, pending, healthy_sources, now,
              live_ports_for=None, healthy_since=None, ip_macs=None):
    """Pure: decide what one scan means. See spec §2.5 for the rules.

    ip_macs: {ip: mac} from this scan's UniFi clients and the Pi's ARP table.
    Used only to propose which device an unidentified Proxmox node is.
    """
    if not healthy_sources:
        return {"upserts": [], "resolve": [], "touch": [], "props": []}
    by_id, by_mac, by_ip = _index_records(records)
    upserts, touch, props_fill = {}, {}, {}
    held_macs, held_ports = {}, set()        # held_macs: source -> {mac}
    held_keys_extra, held_prefixes = set(), set()
    stale_hold_keys, stale_hold_prefixes = set(), set()
    vm_category = _most_common_category(records, "vm")
    live = [o for o in observations if o["source"] in healthy_sources]

    def suggest(kind, source, key, payload, fp_basis):
        upserts[key] = {"kind": kind, "source": source, "subject_key": key,
                        "payload": payload, "fp": fingerprint([kind] + list(fp_basis))}

    def ports_of(rec):
        return resolve_ports(rec, live_ports_for(rec) if live_ports_for else None)

    def port_key(port, ports):
        return canonical_port(port, ports) if ports else normalize_port(port)

    def links_from(child_id, family=NETWORK_LINK_TYPES):
        return [e for e in edges if e["from_device_id"] == child_id
                and e["connection_type"] in family]

    def child_is_held(rec, source):
        if rec is None:
            return False
        held = held_macs.get(source, ())
        macs = [rec.get("mac")] + list((rec.get("properties") or {}).get("mac_aliases") or [])
        return any(_norm_mac(m) in held for m in macs)

    def stale_is_held(e):
        ext = e.get("external_key") or ""
        return (child_is_held(by_id.get(e["from_device_id"]), e.get("source"))
                or ext in stale_hold_keys
                or any(ext.startswith(p) for p in stale_hold_prefixes))

    def lldp_orientation(switch, neighbour, is_uplink):
        child, parent, ambiguous = orient_edge(switch, neighbour)
        if ambiguous:
            return (switch, neighbour) if is_uplink else (neighbour, switch)
        return child, parent

    def observe_edge(child, parent, parent_port, child_port, subject, ext_key,
                     ctype, source):
        ports = ports_of(parent)
        pport = port_key(parent_port, ports)
        if ports and pport not in {p["name"] for p in ports}:
            pport = None  # e.g. an LLDP port_id that's an ifname or a MAC
        proposed = {"child_id": child["id"], "child_name": child["system"],
                    "parent_id": parent["id"], "parent_name": parent["system"],
                    "parent_port": pport, "child_port": child_port,
                    "connection_type": ctype, "source": source, "external_key": ext_key}
        # Conflicts only within a type family: network links (ethernet/fiber/
        # wifi) or the observed type itself (virtual) - spec §2.5.
        family = NETWORK_LINK_TYPES if ctype in NETWORK_LINK_TYPES else (ctype,)
        links = links_from(child["id"], family)
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
            verb = {"virtual": "runs on", "wifi": "connects over wifi to"}.get(
                ctype, "is wired to")
            suggest("edge", source, f"edge:{source}:{subject}",
                    dict(proposed, message=f"{child['system']} {verb} {where}"),
                    [child["id"], parent["id"], pport, ctype])
            return
        cur_parent = by_id.get(current["to_device_id"], {}).get("system") or "?"
        cur_port = current.get("to_port")
        cur_desc = f"{cur_parent}{' · ' + cur_port if cur_port else ''} ({current['connection_type']})"
        new_desc = f"{parent['system']}{' · ' + pport if pport else ''} ({ctype})"
        label = SOURCE_LABELS.get(source, source)
        suggest("drift", source, f"drift:{source}:{subject}", {
            "connection_id": current["id"], "action": "replace",
            "child_id": child["id"], "child_name": child["system"],
            "current": {"parent_id": current["to_device_id"], "parent_name": cur_parent,
                        "parent_port": cur_port,
                        "connection_type": current["connection_type"]},
            "proposed": proposed,
            "message": f"Your graph: {child['system']} → {cur_desc}. {label} sees: {new_desc}",
        }, [current["id"], parent["id"], pport, ctype])

    # Held observations are collected before Proxmox nodes are matched: a
    # node's own MAC turning up in one is itself a reason to hold that
    # node's still-unproven node-port fact (fix round 1, #2).
    for o in live:
        if o["type"] == "held":
            held_macs.setdefault(o["source"], set()).update(o["macs"])
            switch = by_mac.get(o.get("switch_mac"))
            port_name = o.get("port")
            if port_name is not None:
                held_ports.add((o["switch_mac"],
                                port_key(port_name, ports_of(switch) if switch else None)))

    # ── Proxmox nodes: match to inventory, or ask which device each one is ──
    node_records, offline_nodes, node_names = {}, set(), set()
    for o in live:
        if o["type"] != "node":
            continue
        name = o["name"]
        node_names.add(name)
        if not o.get("online", True):
            offline_nodes.add(name)
        node = _match_node(o, records, by_ip)
        if node is not None:
            node_records[name] = node
            continue
        cand_mac = _norm_mac((ip_macs or {}).get(str(o.get("ip") or "")))
        cand = by_mac.get(cand_mac) if cand_mac else None
        if cand is None:
            cand = next((r for r in records
                         if str(r.get("system") or "").strip().lower() == name.lower()), None)
        where = f" ({o['ip']})" if o.get("ip") else ""
        suggest("identity", o["source"], f"identity:proxmox-node:{name}", {
            "proxmox_node": name, "node_ip": o.get("ip"),
            "candidate_id": cand["id"] if cand else None,
            "candidate_name": cand["system"] if cand else None,
            "message": (f"Is Proxmox node {name}{where} your {cand['system']}?" if cand
                        else f"Which device is Proxmox node {name}{where}?"),
        }, ["proxmox-node", name, cand["id"] if cand else None])
    for name in node_names:
        node = node_records.get(name)
        node_macs = set()
        if node is not None:
            node_macs = {m for m in (
                [_norm_mac(node.get("mac"))]
                + [_norm_mac(a) for a in (node.get("properties") or {}).get("mac_aliases") or []]
            ) if m}
        node_mac_held = any(node_macs & macs for macs in held_macs.values())
        unidentified_or_offline = name not in node_records or name in offline_nodes
        # Unknown or offline node: its guests are unknowable this scan.
        if unidentified_or_offline:
            held_prefixes.add(f"proxmox:{name}:")
            stale_hold_prefixes.add(f"proxmox:guest:{name}:")
        # Unidentified, offline, or the node's own MAC is itself held (e.g. an
        # unexplained likely-guest MAC on its port): its node-port fact can't
        # be re-derived this scan either (fix round 1, #2).
        if unidentified_or_offline or node_mac_held:
            held_keys_extra.update({f"edge:unifi:node-port:{name}",
                                    f"drift:unifi:node-port:{name}"})
            stale_hold_keys.add(f"unifi:node-port:{name}")
    if "proxmox" not in healthy_sources:
        # No node map this scan, so node-port facts can't be re-derived.
        stale_hold_prefixes.add("unifi:node-port:")

    def resolve_parent(spec):
        if "inventory_id" in spec:
            return by_id.get(spec["inventory_id"])
        return by_mac.get(spec.get("mac"))

    node_port_child_ids = {node_records[o["child"]["proxmox_node"]]["id"] for o in live
                           if o["type"] == "edge"
                           and o["child"].get("proxmox_node") in node_records}

    # F2(a): a neighbour already wired via the client path (a wired-client
    # `edge` observation resolving to the same inventory record) is never
    # also processed via LLDP - precomputed so order doesn't matter.
    client_edge_child_ids = set(node_port_child_ids)
    for o in live:
        if o["type"] == "edge" and o["source"] == "unifi" and "mac" in o["child"]:
            child = by_mac.get(o["child"]["mac"])
            if child is not None:
                client_edge_child_ids.add(child["id"])

    # F2(b): when more than one LLDP neighbour would make a switch its
    # child, keep exactly one (uplink first, then lowest local port index,
    # then lowest port name) - independent of lldp_table order.
    lldp_parent_candidates = {}
    for o in live:
        if o["type"] != "lldp":
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

    def process_edge(o):
        parent = resolve_parent(o["parent"])
        if parent is None:
            return
        spec = o["child"]
        if "proxmox_node" in spec:
            child = node_records.get(spec["proxmox_node"])
            if child is not None and child["id"] != parent["id"]:
                observe_edge(child, parent, o["parent_port"], o.get("child_port"),
                             f"node-port:{spec['proxmox_node']}", o["external_key"],
                             o["connection_type"], o["source"])
            return
        mac = spec["mac"]
        child = by_mac.get(mac)
        if child is not None:
            # A node's own MAC is superseded by its node-port observation.
            if child["id"] != parent["id"] and child["id"] not in node_port_child_ids:
                observe_edge(child, parent, o["parent_port"], o.get("child_port"), mac,
                             o["external_key"], o["connection_type"], o["source"])
            return
        name = spec.get("name") or f"Unknown device {mac}"
        pport = port_key(o["parent_port"], ports_of(parent))
        suggest("device", o["source"], f"device:{o['source']}:{mac}", {
            "device": {"system": name, "mac": mac, "ip": spec.get("ip"),
                       "device_type": "host", "category": None},
            "edge": {"parent_id": parent["id"], "parent_name": parent["system"],
                     "parent_port": pport, "child_port": o.get("child_port"),
                     "connection_type": o["connection_type"],
                     "source": o["source"], "external_key": o["external_key"]},
            "message": (f"{name} ({mac}) is wired to {parent['system']} · {pport} "
                        "but isn't in inventory"),
        }, [mac, parent["id"], pport])

    def process_guest(o):
        node = node_records.get(o["node"])
        if node is None or o["node"] in offline_nodes:
            return  # held above
        subject = f"{o['node']}:{o['vmid']}"
        guest, how = _match_guest(o, records, by_mac)
        if guest is not None:
            if guest["id"] == node["id"]:
                return
            # Only a vmid or MAC match is trustworthy enough to fill
            # properties from - a name-only match still gets its edge.
            if guest.get("device_type") == "vm" and how in ("vmid", "mac"):
                props = guest.get("properties") or {}
                fill = {k: v for k, v in (("guest_type", o["guest_type"]),
                                          ("proxmox_node", o["node"]),
                                          ("proxmox_vmid", o["vmid"]))
                        if props.get(k) in (None, "")}
                if fill:
                    props_fill[guest["id"]] = fill
            observe_edge(guest, node, None, None, subject, o["external_key"],
                         "virtual", o["source"])
            return
        props = {"hypervisor": "Proxmox", "guest_type": o["guest_type"],
                 "proxmox_node": o["node"], "proxmox_vmid": o["vmid"],
                 "vcpu_count": o.get("cores"),
                 "ram_alloc_gb": (round(o["memory_mb"] / 1024, 1)
                                  if o.get("memory_mb") else None),
                 "autostart": o.get("onboot")}
        props = {k: v for k, v in props.items() if v is not None}
        kind_label = "LXC" if o["guest_type"] == "lxc" else "VM"
        suggest("device", o["source"], f"device:{o['source']}:{subject}", {
            "device": {"system": o["name"], "mac": (o["macs"] or [None])[0], "ip": None,
                       "device_type": "vm", "category": vm_category, "properties": props},
            "edge": {"parent_id": node["id"], "parent_name": node["system"],
                     "parent_port": None, "child_port": None,
                     "connection_type": "virtual", "source": o["source"],
                     "external_key": o["external_key"]},
            "message": f"{o['name']} ({kind_label} {o['vmid']} on {o['node']}) isn't in inventory",
        }, [o["node"], o["vmid"], node["id"]])

    for o in live:
        t = o["type"]
        if t == "held":
            continue  # already folded into held_macs/held_ports above
        elif t == "held_guest":
            subject = f"{o['node']}:{o['vmid']}"
            held_keys_extra.update(f"{k}:proxmox:{subject}" for k in ("edge", "device", "drift"))
            stale_hold_keys.add(f"proxmox:guest:{subject}")
        elif t == "edge":
            process_edge(o)
        elif t == "guest":
            process_guest(o)
        elif t == "shared_port":
            switch = by_mac.get(o["switch_mac"])
            if switch is None:
                continue
            macs = [m for m in o["macs"]
                    if not (m in by_mac and by_mac[m]["id"] in node_port_child_ids)]
            if len(macs) == 1:
                # Only the node's own MAC made this port look shared.
                process_edge({"type": "edge", "source": o["source"],
                              "child": {"mac": macs[0], "ip": None, "name": None},
                              "parent": {"mac": o["switch_mac"]}, "parent_port": o["port"],
                              "child_port": None, "connection_type": "ethernet",
                              "external_key": f"unifi:port:{macs[0]}"})
                continue
            if not macs:
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
                       for m in macs if m in by_mac]
            suggest("shared_port", o["source"], f"shared_port:{o['switch_mac']}:{pport}", {
                "switch_id": switch["id"], "switch_name": switch["system"], "port": pport,
                "macs": macs, "matched": matched,
                "message": (f"{len(macs)} devices share {switch['system']} · {pport}. "
                            "Unmanaged switch or AP behind it?"),
            }, [switch["id"], pport, macs])
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
        if child is None or stale_is_held(e):
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

    held_keys = {f"{kind}:{src}:{m}" for src, macs in held_macs.items() for m in macs
                 for kind in ("edge", "device", "drift")}
    held_keys |= {f"shared_port:{switch_mac}:{port}" for switch_mac, port in held_ports}
    held_keys |= {f"drift:stale:conn:{e['id']}" for e in edges if stale_is_held(e)}
    held_keys |= held_keys_extra

    def is_held(key):
        if key in held_keys:
            return True
        kind, _, rest = key.partition(":")
        if kind not in ("edge", "device", "drift"):
            return False
        if "proxmox" not in healthy_sources and rest.startswith("unifi:node-port:"):
            return True
        return any(rest.startswith(p) for p in held_prefixes)

    resolve = [s["subject_key"] for s in pending
               if s["source"] in healthy_sources and s["subject_key"] not in upserts
               and not is_held(s["subject_key"])]
    return {"upserts": list(upserts.values()), "resolve": resolve,
            "touch": [{"id": k, "parent_port": v} for k, v in touch.items()],
            "props": [{"id": k, "set": v} for k, v in props_fill.items()]}


class UnifiError(Exception):
    """The controller answered, but with meta.rc != "ok"."""


def safe_error(exc):
    """Describe a fetch failure without echoing exception text, which can
    carry request details (spec §2.2: type and HTTP status only)."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        detail = "unreachable" if isinstance(reason, str) else type(reason).__name__
        return f"URLError: {detail}"
    if isinstance(exc, UnifiError):
        return "controller returned an error"
    return type(exc).__name__


def unifi_ssl_context(settings):
    """TLS context for the controller, read from the shared settings dict at
    call time so a Settings change applies on the next scan."""
    if not bool(settings.get("unifi_verify_ssl", True)):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    ca_cert = (settings.get("unifi_ca_cert") or "").strip()
    ctx = ssl.create_default_context(cafile=ca_cert or None)
    if ca_cert and hasattr(ssl, "VERIFY_X509_STRICT"):
        # Same relaxation as the Proxmox/PBS pollers: self-managed CAs often
        # lack the Key Usage extension OpenSSL 3's strict mode insists on.
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib re-sends custom headers on a redirect, even to another host or
    to plain http - so refuse redirects; the 30x surfaces as an HTTPError."""

    def redirect_request(self, *args, **kwargs):
        return None


def _get_json(url, api_key, ctx):
    req = urllib.request.Request(
        url, headers={"X-API-Key": api_key, "Accept": "application/json"})
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx), _NoRedirect())
    with opener.open(req, timeout=15) as resp:
        payload = json.load(resp)
    if (payload.get("meta") or {}).get("rc", "ok") != "ok":
        raise UnifiError()
    return payload


def fetch_unifi_classic(url, api_key, site, ctx):
    """(stat/device, stat/sta) from UniFi Network's classic API. The API key
    goes in the X-API-Key header only."""
    base = f"{url.rstrip('/')}/proxy/network/api/s/{urllib.parse.quote(site, safe='')}"
    return (_get_json(base + "/stat/device", api_key, ctx),
            _get_json(base + "/stat/sta", api_key, ctx))


class DiscoveryRunner:
    """Background discovery: scan every SCAN_INTERVAL_SECONDS (first scan
    ~FIRST_SCAN_DELAY_SECONDS after start) or on demand. Always running;
    idles while no source is configured so newly saved credentials work
    without a restart."""

    SCAN_INTERVAL_SECONDS = 900
    FIRST_SCAN_DELAY_SECONDS = 90
    # A gap longer than this between successful scans (controller outage, or
    # netwatch itself down) restarts a source's healthy streak.
    OUTAGE_GRACE_SECONDS = 3600
    _STREAKS_META_KEY = "discovery_health_streaks"

    def __init__(self, auth_manager, settings, inventory_db, fetch_unifi=None):
        self._auth = auth_manager
        self._settings = settings  # shared by reference - never copy
        self._db = inventory_db
        self._fetch_unifi = fetch_unifi or fetch_unifi_classic
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._scanning = False
        self._health = {}
        self._last_scan = None
        self._apply_error = None
        self._switches = []
        # Per source: start of the current healthy streak and the last
        # successful scan. Persisted in schema_meta so a quick restart
        # doesn't make stale-edge suggestions wait another 7 days.
        self._healthy_since, self._last_ok = self._load_streaks()

    def _unifi_config(self):
        data = self._auth.data if self._auth else {}
        return {
            "url": str(data.get("unifi_url") or "").strip(),
            "api_key": str(data.get("unifi_api_key") or "").strip(),
            "site": str(data.get("unifi_site") or "").strip() or "default",
        }

    def unifi_configured(self):
        cfg = self._unifi_config()
        return bool(cfg["url"] and cfg["api_key"])

    def any_source_configured(self):
        return self.unifi_configured()

    def _load_streaks(self):
        try:
            raw = json.loads(self._db.get_meta(self._STREAKS_META_KEY) or "{}")
        except (ValueError, TypeError, AttributeError):
            raw = {}
        since, last_ok = {}, {}
        for source, v in (raw.items() if isinstance(raw, dict) else ()):
            if (isinstance(v, dict) and isinstance(v.get("since"), int)
                    and isinstance(v.get("last_ok"), int)):
                since[source], last_ok[source] = v["since"], v["last_ok"]
        return since, last_ok

    def _record_success(self, source, now):
        """Extend or restart `source`'s healthy streak and persist it.

        Never call with _lock held: persisting takes the DB lock, and the
        accept path holds the DB lock while asking live_ports_for (which
        takes _lock) - nesting them the other way round would deadlock."""
        with self._lock:
            prev = self._last_ok.get(source)
            if prev is None or now - prev > self.OUTAGE_GRACE_SECONDS:
                self._healthy_since[source] = now
            self._last_ok[source] = now
            blob = json.dumps({s: {"since": self._healthy_since[s],
                                   "last_ok": self._last_ok[s]}
                               for s in self._last_ok})
        try:
            self._db.set_meta(self._STREAKS_META_KEY, blob)
        except Exception as e:
            logging.warning(f"Discovery: saving health streak failed: {type(e).__name__}")

    def _set_apply_error(self, error):
        """Type name only (exception text can carry data), logged once."""
        with self._lock:
            prev, self._apply_error = self._apply_error, error
        if error and error != prev:
            logging.warning(f"Discovery: applying scan failed: {error}")
        elif prev and not error:
            logging.info("Discovery: applying scans recovered")

    def _set_health(self, source, ok, error, at, counts):
        with self._lock:
            prev = self._health.get(source)
            self._health[source] = {"ok": ok, "error": error, "at": at, "counts": counts}
        if not ok and (prev is None or prev["ok"] or prev["error"] != error):
            logging.warning(f"Discovery: {source} scan failed: {error}")
        elif ok and prev is not None and not prev["ok"]:
            logging.info(f"Discovery: {source} scan recovered")

    def scan_once(self, now=None):
        """Run one scan. Returns False if a scan was already running."""
        now = int(now or time.time())
        with self._lock:
            if self._scanning:
                return False
            self._scanning = True
        try:
            observations, healthy = [], set()
            if self.unifi_configured():
                cfg = self._unifi_config()
                try:
                    devices, clients = self._fetch_unifi(
                        cfg["url"], cfg["api_key"], cfg["site"],
                        unifi_ssl_context(self._settings))
                    snap = parse_unifi(devices, clients)
                    observations += unifi_observations(snap, guest_macs=None)
                    healthy.add("unifi")
                    with self._lock:
                        self._switches = snap["switches"]
                    self._record_success("unifi", now)
                    self._set_health("unifi", True, None, now,
                                     {"switches": len(snap["switches"]),
                                      "clients": len(snap["clients"])})
                except Exception as e:
                    self._set_health("unifi", False, safe_error(e), now, None)
            if healthy:
                try:
                    changes = reconcile(
                        observations, records=self._db.list_all(),
                        edges=self._db.list_all_connections(),
                        pending=self._db.suggestions.list("pending"),
                        healthy_sources=healthy, now=now,
                        live_ports_for=self.live_ports_for,
                        healthy_since=self._healthy_snapshot())
                    self._db.apply_discovery_changes(changes, now)
                    self._set_apply_error(None)
                except Exception as e:
                    self._set_apply_error(type(e).__name__)
            with self._lock:
                self._last_scan = now
            return True
        finally:
            with self._lock:
                self._scanning = False

    def _healthy_snapshot(self):
        with self._lock:
            return dict(self._healthy_since)

    def request_scan(self):
        """Ask the background loop for a scan now. False when nothing is
        configured. The loop clears the wake flag before scanning, so a
        request that arrives mid-scan runs one more scan right after it."""
        if not self.any_source_configured():
            return False
        self._wake.set()
        return True

    def status(self):
        with self._lock:
            unifi = dict(self._health.get(
                "unifi", {"ok": None, "error": None, "at": None, "counts": None}))
            last, scanning = self._last_scan, self._scanning
            apply_error = self._apply_error
        unifi["configured"] = self.unifi_configured()
        return {"sources": {"unifi": unifi}, "last_scan": last, "scanning": scanning,
                "apply_error": apply_error}

    def switch_macs(self):
        """MACs of the switches seen in the last successful UniFi scan."""
        with self._lock:
            return [sw["mac"] for sw in self._switches]

    def live_ports_for(self, rec):
        """Live port table for an inventory record that is a scanned switch."""
        macs = {_norm_mac(rec.get("mac"))} | {
            _norm_mac(m) for m in ((rec.get("properties") or {}).get("mac_aliases") or [])}
        macs.discard("")
        with self._lock:
            for sw in self._switches:
                if sw["mac"] in macs:
                    return [dict(p) for p in sw["ports"]]
        return None

    def _loop(self, stop_event):
        delay = self.FIRST_SCAN_DELAY_SECONDS
        while not stop_event.is_set():
            self._wake.wait(timeout=delay)
            if stop_event.is_set():
                break
            self._wake.clear()
            self.scan_once()
            delay = self.SCAN_INTERVAL_SECONDS

    def start(self, stop_event):
        t = threading.Thread(target=self._loop, args=(stop_event,), daemon=True,
                             name="discovery")
        t.start()
        return t
