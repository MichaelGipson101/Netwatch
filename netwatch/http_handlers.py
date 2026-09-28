"""HTTP route handler functions (_h_get_*/_h_post_*) plus the payload
builders and settings/secret constants they depend on. Each handler takes
its dependencies (host_manager, settings, inventory_db, auth_manager,
pollers, etc.) as explicit arguments rather than reaching into module
state, so they're unit-testable in isolation -- see tests/test_netwatch.py."""

import os
import sys
import json
import time
import logging
import yaml
from datetime import datetime
from urllib.parse import urlparse, parse_qs

from netwatch.attention import build_attention, host_facts
from netwatch.storage import InventoryDB
from netwatch.network import (
    _detect_mac_for_ip, send_wol_packet, read_pi_health,
    start_discovery_scan, get_discovery_state, HOSTS_WRITE_LOCK,
)
from netwatch.hosts import (
    load_yaml, _validate_url, validate_hosts_config, save_hosts_config,
    add_monitored_hosts, guest_host_entry, monitored_keys,
)
from netwatch.pollers import PROXMOX_NODE_RE
from netwatch.auth import verify_maintenance_token


# Spec §4.3: which edge makes a device "behind" another. Lower wins.
PRIMARY_TYPE_PRIORITY = {"virtual": 0, "ethernet": 1, "fiber": 1, "wifi": 2,
                         "usb": 3, "console": 3, "other": 4, "power": 5}


def _primary_rank(edge):
    """Sort key: type priority, then most recent last_seen, then lowest id."""
    return (PRIMARY_TYPE_PRIORITY.get(edge.get("connection_type"), 4),
            -(edge.get("last_seen") or 0), edge["id"])


def _find_primary_cycle(chosen):
    """First cycle among chosen primary edges (child -> edge), or None."""
    color = {}
    for start in sorted(chosen):
        if color.get(start):
            continue
        path, node = [], start
        while node is not None and color.get(node) is None:
            color[node] = 1
            path.append(node)
            edge = chosen.get(node)
            node = edge["to_device_id"] if edge is not None else None
        if node is not None and color.get(node) == 1:
            return [chosen[n] for n in path[path.index(node):]]
        for n in path:
            color[n] = 2
    return None


def compute_primary_parents(nodes, edges):
    """Pure (spec §4.3): each node's primary parent, and the set of primary
    edge ids. The dependency-suppression work must call this same function.

    Cycles (only possible via bad manual data) are broken by excluding the
    lowest-priority edge in the cycle; its child then falls back to its next
    candidate. Excluded edges still render as cross-links."""
    ids = {n["id"] for n in nodes}
    candidates = {}
    for edge in edges:
        child, parent = edge["from_device_id"], edge["to_device_id"]
        if child in ids and parent in ids and child != parent:
            candidates.setdefault(child, []).append(edge)
    for lst in candidates.values():
        lst.sort(key=_primary_rank)
    excluded = set()

    def choose(child):
        return next((c for c in candidates.get(child, ()) if c["id"] not in excluded), None)

    chosen = {child: choose(child) for child in candidates}
    while True:
        cycle = _find_primary_cycle(chosen)
        if not cycle:
            break
        worst = max(cycle, key=_primary_rank)
        excluded.add(worst["id"])
        chosen[worst["from_device_id"]] = choose(worst["from_device_id"])
    parents = {i: None for i in ids}
    for child, edge in chosen.items():
        if edge is not None:
            parents[child] = edge["to_device_id"]
    return parents, {edge["id"] for edge in chosen.values() if edge is not None}


def build_topology_payload(inventory_db, host_manager):
    """Bundle inventory records + connections + linked-host status into a
    single payload for the topology view. Doing this server-side cuts the
    frontend from 3 round trips to 1 and lets us join MAC -> host status
    without serialising the full host list."""
    if not inventory_db:
        return {"nodes": [], "edges": [], "suggested_edges": []}

    # Build a MAC -> host status lookup
    host_by_mac = {}
    if host_manager:
        for h in host_manager.list_hosts():
            d = h.to_dict()
            mac = (d.get("specs") or {}).get("mac")
            norm = InventoryDB.normalize_mac(mac) if mac else ""
            if norm:
                host_by_mac[norm] = {
                    "name":   d.get("name"),
                    "ip":     d.get("ip"),
                    "is_up":  d.get("is_up"),
                    "status": d.get("status"),
                }

    records = inventory_db.list_all()
    conns = inventory_db.list_all_connections()
    parents, primary_ids = compute_primary_parents(records, conns)
    children = {}
    for pid in parents.values():
        if pid is not None:
            children[pid] = children.get(pid, 0) + 1

    nodes = []
    for rec in records:
        norm_mac = InventoryDB.normalize_mac(rec.get("mac")) if rec.get("mac") else ""
        linked = host_by_mac.get(norm_mac) if norm_mac else None
        props = rec.get("properties") if isinstance(rec.get("properties"), dict) else {}
        nodes.append({
            "id":          rec["id"],
            "name":        rec.get("system") or "(unnamed)",
            "category":    rec.get("category"),
            "device_type": rec.get("device_type") or "host",
            "linked_host": linked,
            # Status inherits from linked host. Devices without a linked
            # monitored host (peripherals, switches we don't monitor) show
            # as UNKNOWN which renders as a neutral border.
            "status":      (linked["status"] if linked else "UNKNOWN"),
            "is_up":       (linked["is_up"] if linked else None),
            "ip":          rec.get("ip"),
            "mac":         rec.get("mac"),
            # Additive (spec §4.2) - hearthboard/tiger ignore unknown keys.
            "network_role":      props.get("network_role"),
            "primary_parent_id": parents.get(rec["id"]),
            "children_count":    children.get(rec["id"], 0),
        })

    edges = []
    for c in conns:
        edges.append({
            "id":              c["id"],
            "source":          c["from_device_id"],
            "target":          c["to_device_id"],
            "from_port":       c["from_port"],
            "to_port":         c["to_port"],
            "connection_type": c["connection_type"],
            "notes":           c.get("notes") or None,
            # Provenance is "origin": "source" is already the D3 child id.
            "origin":          c.get("source") or "manual",
            "is_primary":      c["id"] in primary_ids,
        })

    # Pending edge suggestions as ghosts, kept OUT of `edges` so existing
    # consumers never draw a suggestion as a real link (spec §4.2).
    node_ids = {n["id"] for n in nodes}
    suggested = []
    try:
        pending = inventory_db.suggestions.list("pending")
    except Exception as e:
        logging.warning(f"topology: suggested_edges unavailable: {type(e).__name__}")
        pending = []
    for s in pending:
        p = s.get("payload") or {}
        if s.get("kind") != "edge":
            continue
        child, parent = p.get("child_id"), p.get("parent_id")
        if child in node_ids and parent in node_ids:
            suggested.append({"suggestion_id": s["id"], "source": child, "target": parent,
                              "connection_type": p.get("connection_type"),
                              "parent_port": p.get("parent_port"),
                              "origin": s.get("source")})

    return {"nodes": nodes, "edges": edges, "suggested_edges": suggested}


# Settings keys safe to expose via /api/status. Everything else (API keys,
# ntfy topic) stays server-side; the AI panel uses /api/ai-config instead.
SETTINGS_PUBLIC_KEYS = ("default_interval", "ping_timeout", "history_window",
                        "refresh_rate", "history_days")

# All settings readable/writable via /api/settings (admin only).
SETTINGS_EDITABLE_KEYS = {
    "default_interval":     int,
    "ping_timeout":         int,
    "history_window":       int,
    "refresh_rate":         int,
    "history_days":         int,
    "alert_cooldown_seconds": int,
    "ntfy_topic":           str,
    "ntfy_server":          str,
    "truenas_url":          str,
    "truenas_api_key":      str,
    "proxmox_url":          str,
    "proxmox_user":         str,
    "proxmox_password":     str,
    "proxmox_token_id":     str,
    "proxmox_token_secret": str,
    "proxmox_node":         str,
    "proxmox_verify_ssl":   bool,
    "proxmox_ca_cert":      str,
    "openrouter_api_key":   str,
    "ai_model":             str,
    "setup_wizard_complete": bool,
    "truenas_ignored_alert_klasses": str,
    "ha_url":              str,
    "ha_token":            str,
    "ha_entity_power":     str,
    "ha_entity_voltage":   str,
    "ha_entity_current":   str,
    "ha_entity_energy":    str,
    "pbs_url":              str,
    "pbs_api_token_id":     str,
    "pbs_api_token_secret": str,
    "pbs_verify_ssl":       bool,
    "pbs_ca_cert":          str,
    "nut_server":           str,
    "nut_port":             int,
    "nut_ups_name":         str,
    "nut_username":         str,
    "nut_password":         str,
    "unifi_url":            str,
    "unifi_api_key":        str,
    "unifi_site":           str,
    "unifi_verify_ssl":     bool,
    "unifi_ca_cert":        str,
}

_SETTINGS_INT_RANGES = {
    "default_interval": (5,  3600),
    "ping_timeout":     (1,  30),
    "history_window":   (10, 10000),
    "refresh_rate":     (1,  60),
    "history_days":     (1,  365),
    "alert_cooldown_seconds": (0, 86400),
    "nut_port":         (1,  65535),
}

_SETTINGS_URL_KEYS = {"ntfy_server", "truenas_url", "proxmox_url", "ha_url", "pbs_url", "unifi_url"}
_SETTINGS_REQUIRED_INT_KEYS = {"default_interval", "ping_timeout", "history_window",
                                "refresh_rate", "history_days"}
# These keys live in auth.json (alongside user credentials), not hosts.yaml
_AUTH_STORED_KEYS = {
    "truenas_url", "truenas_api_key",
    "proxmox_url", "proxmox_user", "proxmox_token_id", "proxmox_token_secret",
    "openrouter_api_key",
    "ha_url", "ha_token", "ha_entity_power", "ha_entity_voltage",
    "ha_entity_current", "ha_entity_energy",
    "pbs_url", "pbs_api_token_id", "pbs_api_token_secret",
    "nut_server", "nut_port", "nut_ups_name", "nut_username", "nut_password",
    "unifi_url", "unifi_api_key", "unifi_site",
}


def build_api_payload(host_manager, settings, incident_log=None, inventory_db=None):
    hosts = host_manager.list_hosts()
    events = incident_log.list_incidents() if incident_log else []
    device_types = inventory_db.get_device_type_map() if inventory_db else {}
    return {
        "generated": datetime.now().isoformat(),
        "settings":  {k: settings[k] for k in SETTINGS_PUBLIC_KEYS if k in settings},
        "summary": {
            "total":   len(hosts),
            "up":      sum(1 for h in hosts if h.is_up),
            "down":    sum(1 for h in hosts if not h.is_up and h.last_checked and h.always_on),
            "idle":    sum(1 for h in hosts if not h.is_up and h.last_checked and not h.always_on),
            "pending": sum(1 for h in hosts if not h.last_checked),
        },
        "hosts": [
            {**h.to_dict(), "device_type": device_types.get(h.ip, "host")}
            for h in hosts
        ],
        "events": events,
        "suggestions_pending": (inventory_db.suggestions.count_pending()
                                if getattr(inventory_db, "suggestions", None) else 0),
    }


# ── Route handler functions (module-level; testable without HTTP) ─────────────

def _h_get_status(host_manager, settings, incident_log, inventory_db) -> tuple:
    return 200, build_api_payload(host_manager, settings, incident_log, inventory_db)


_HEARTBEAT_CACHE = {}
_HEARTBEAT_TTL_SECONDS = 60
_HEARTBEAT_CACHE_MAX = 32


def _clamp_int(raw, default, lo, hi):
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _h_get_heartbeat(history_db, host_manager, query="", now=None) -> tuple:
    """24h-style per-host heartbeat for the Home page. Never errors: missing pieces degrade
    to all-None buckets. Cached for 60s because it scans a day of pings."""
    params = parse_qs(query or "")
    hours = _clamp_int((params.get("hours") or [None])[0], 24, 1, 72)
    n = _clamp_int((params.get("buckets") or [None])[0], 48, 1, 96)
    now = time.time() if now is None else float(now)
    ips = sorted(h.ip for h in host_manager.list_hosts()) if host_manager else []
    key = (hours, n, tuple(ips))
    hit = _HEARTBEAT_CACHE.get(key)
    if hit and hit[0] > now:
        return 200, hit[1]
    bucket_seconds = max(1, hours * 3600 // n)
    end = (int(now) // bucket_seconds + 1) * bucket_seconds
    start = end - bucket_seconds * n
    data = {}
    if history_db is not None:
        try:
            data = history_db.heartbeat(start, bucket_seconds, n)
        except Exception as e:
            logging.warning(f"heartbeat query failed: {e}")
    payload = {
        "generated": datetime.fromtimestamp(now).isoformat(),
        "bucket_seconds": bucket_seconds,
        "start": start,
        "hosts": {ip: data.get(ip, [None] * n) for ip in ips},
    }
    if len(_HEARTBEAT_CACHE) >= _HEARTBEAT_CACHE_MAX:
        _HEARTBEAT_CACHE.clear()
    _HEARTBEAT_CACHE[key] = (now + _HEARTBEAT_TTL_SECONDS, payload)
    return 200, payload


def _h_get_attention(host_manager, inventory_db, ledger=None, drift_monitor=None, now=None) -> tuple:
    """The verdict + needs-attention list for Home. Every input is optional and every
    failure degrades to fewer items, never an error."""
    facts = host_facts(host_manager.list_hosts()) if host_manager else []
    records, parents = [], {}
    if inventory_db is not None:
        try:
            records = inventory_db.list_all()
            parents, _ = compute_primary_parents(records, inventory_db.list_all_connections())
        except Exception as e:
            logging.warning(f"attention: inventory read failed: {e}")
            records, parents = [], {}
    rows = []
    if ledger is not None:
        try:
            rows = ledger.active()
        except Exception as e:
            logging.warning(f"attention: ledger read failed: {e}")
    pending = 0
    suggestions = getattr(inventory_db, "suggestions", None)
    if suggestions is not None:
        try:
            pending = suggestions.count_pending()
        except Exception as e:
            logging.warning(f"attention: suggestion count failed: {e}")
    drift = drift_monitor.get() if drift_monitor is not None else []
    return 200, build_attention(facts, records, parents, rows, pending, drift, now=now)


NAS_BACKUP_STATUS_PATH = "/mnt/nas-shared/netwatch/backup/_status.json"
NAS_INVENTORY_STATUS_PATH = "/mnt/nas-shared/Homelab Inventory/_status.json"


def _read_backup_status_file(path: str) -> tuple:
    if not os.path.isfile(path):
        return 200, {"configured": False}
    try:
        with open(path) as f:
            status = json.load(f)
    except (OSError, ValueError) as e:
        return 200, {"configured": False, "error": f"could not read status file: {e}"}
    status["configured"] = True
    return 200, status


def _h_get_backup_status() -> tuple:
    return _read_backup_status_file(NAS_BACKUP_STATUS_PATH)


def _h_get_inventory_backup_status() -> tuple:
    return _read_backup_status_file(NAS_INVENTORY_STATUS_PATH)


def _h_get_ai_config(settings: dict, auth_manager=None) -> tuple:
    # The OpenRouter API key never leaves the server; chat requests are
    # proxied through /api/ai/chat so the key can't be lifted from the browser.
    api_key = ""
    if auth_manager:
        with auth_manager.lock:
            api_key = auth_manager.data.get("openrouter_api_key", "")
    if not api_key.strip():
        return 404, {"error": "ai_not_configured"}
    return 200, {
        "model": settings.get("ai_model", "openrouter/free"),
    }


ALLOWED_AI_MODELS = frozenset({
    "openrouter/free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
})


def _get_openrouter_key(auth_manager) -> str:
    if not auth_manager:
        return ""
    with auth_manager.lock:
        return auth_manager.data.get("openrouter_api_key", "")


def _h_get_ai_usage(auth_manager) -> tuple:
    api_key = _get_openrouter_key(auth_manager)
    if not api_key.strip():
        return 404, {"error": "ai_not_configured"}
    import urllib.request, urllib.error as _urlerr
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/auth/key",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200, json.loads(r.read().decode())
    except _urlerr.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except (ValueError, TypeError):
            return e.code, {"error": "openrouter request failed"}
    except Exception as e:
        logging.warning(f"AI usage proxy error: {e}")
        return 502, {"error": str(e)}


def _h_post_ai_chat(handler, data, auth_manager) -> None:
    """Stream a chat completion from OpenRouter back to the client.

    Writes directly to the handler's socket (unlike the other _h_* handlers)
    because the response is a long-lived SSE stream, not a single JSON body.
    The OpenRouter API key is read server-side only and never sent to the browser.
    """
    import urllib.request, urllib.error as _urlerr

    api_key = _get_openrouter_key(auth_manager)
    if not api_key.strip():
        handler._send_json(404, {"error": "ai_not_configured"})
        return

    model = (data.get("model") or "").strip()
    if model not in ALLOWED_AI_MODELS:
        model = "openrouter/free"
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        handler._send_json(400, {"error": "messages required"})
        return

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=payload, method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://netwatch.local",
            "X-Title": "Mira (Netwatch)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as upstream:
            handler.send_response(upstream.status)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Cache-Control", "no-cache")
            handler.end_headers()
            while True:
                chunk = upstream.read(1024)
                if not chunk:
                    break
                try:
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
    except _urlerr.HTTPError as e:
        body = e.read()
        try:
            handler.send_response(e.code)
            handler.send_header("Content-Type", "application/json")
            handler.end_headers()
            handler.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
    except Exception as e:
        logging.warning(f"AI chat proxy error: {e}")
        try:
            handler._send_json(502, {"error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass


# Secrets must never be sent to the browser in readable form. GET /api/settings
# substitutes this sentinel for any set secret; POST treats the sentinel as
# "unchanged" so a round-tripped form doesn't wipe stored credentials.
# An empty string still means "clear this key".
SECRET_SETTINGS_KEYS = {
    "truenas_api_key", "proxmox_password", "proxmox_token_secret",
    "openrouter_api_key", "ha_token", "pbs_api_token_secret", "nut_password",
    "unifi_api_key",
}
SECRET_PLACEHOLDER = "••••••••"


def _redact_secrets(result: dict) -> dict:
    for k in SECRET_SETTINGS_KEYS:
        if result.get(k):
            result[k] = SECRET_PLACEHOLDER
    return result


def _h_get_settings(settings: dict, auth_manager=None) -> tuple:
    result = {k: settings[k] for k in SETTINGS_EDITABLE_KEYS if k in settings}
    if auth_manager:
        with auth_manager.lock:
            for k in _AUTH_STORED_KEYS:
                if k in auth_manager.data:
                    result[k] = auth_manager.data[k]
    return 200, _redact_secrets(result)


def _h_post_settings(data: dict, config_path: str, settings: dict, auth_manager=None) -> tuple:
    data = {k: v for k, v in data.items()
            if not (k in SECRET_SETTINGS_KEYS and v == SECRET_PLACEHOLDER)}
    updates = {}
    for k, typ in SETTINGS_EDITABLE_KEYS.items():
        if k not in data:
            continue
        val = data[k]
        if val is None or val == "":
            if k in _SETTINGS_REQUIRED_INT_KEYS:
                continue  # never clear required numeric settings
            updates[k] = None
            continue
        if typ == int:
            try:
                updates[k] = int(val)
            except (ValueError, TypeError):
                return 400, {"error": f"'{k}' must be an integer"}
            lo, hi = _SETTINGS_INT_RANGES.get(k, (None, None))
            if lo is not None and not (lo <= updates[k] <= hi):
                return 400, {"error": f"'{k}' must be between {lo} and {hi}"}
        elif typ == bool:
            if not isinstance(val, bool):
                return 400, {"error": f"'{k}' must be true or false"}
            updates[k] = val
        else:
            updates[k] = str(val).strip()
            if k in _SETTINGS_URL_KEYS and updates[k] and not _validate_url(updates[k]):
                return 400, {"error": f"'{k}' must be a valid http:// or https:// URL"}

    # TrueNAS credentials live in auth.json alongside user data, not hosts.yaml
    auth_updates = {k: v for k, v in updates.items() if k in _AUTH_STORED_KEYS}
    yaml_updates  = {k: v for k, v in updates.items() if k not in _AUTH_STORED_KEYS}

    if auth_updates and auth_manager:
        with auth_manager.lock:
            for k, v in auth_updates.items():
                if v is None:
                    auth_manager.data.pop(k, None)
                else:
                    auth_manager.data[k] = v
            auth_manager._save()

    # Same lock as every other hosts.yaml writer, so a guest added by
    # discovery between our read and write isn't dropped.
    with HOSTS_WRITE_LOCK:
        try:
            existing = load_yaml(config_path) or {}
        except Exception:
            existing = {}
        existing_settings = dict(existing.get("settings", {}))

        for k, v in yaml_updates.items():
            if v is None:
                existing_settings.pop(k, None)
                settings.pop(k, None)
            else:
                existing_settings[k] = v
                settings[k] = v

        new_config = {"settings": existing_settings, "hosts": existing.get("hosts", [])}
        tmp_path = config_path + ".tmp"
        try:
            with open(tmp_path, "w") as f:
                yaml.safe_dump(new_config, f, sort_keys=False, default_flow_style=False)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, config_path)
        except Exception as e:
            logging.exception("settings save error")
            return 500, {"error": f"Failed to save settings: {e}"}

    result = {k: settings[k] for k in SETTINGS_EDITABLE_KEYS if k in settings}
    if auth_manager:
        with auth_manager.lock:
            for k in _AUTH_STORED_KEYS:
                if k in auth_manager.data:
                    result[k] = auth_manager.data[k]
    return 200, {"ok": True, "settings": _redact_secrets(result)}


def _h_post_nas_ignore_alert(data: dict, config_path: str, settings: dict, auth_manager=None) -> tuple:
    klass = (data.get("klass") or "").strip()
    if not klass:
        return 400, {"error": "klass is required"}
    current = [k.strip() for k in (settings.get("truenas_ignored_alert_klasses") or "").split(",") if k.strip()]
    if klass not in current:
        current.append(klass)
    return _h_post_settings({"truenas_ignored_alert_klasses": ",".join(current)},
                             config_path, settings, auth_manager)


def _h_post_nas_unignore_alert(data: dict, config_path: str, settings: dict, auth_manager=None) -> tuple:
    klass = (data.get("klass") or "").strip()
    if not klass:
        return 400, {"error": "klass is required"}
    current = [k.strip() for k in (settings.get("truenas_ignored_alert_klasses") or "").split(",") if k.strip()]
    current = [k for k in current if k != klass]
    return _h_post_settings({"truenas_ignored_alert_klasses": ",".join(current)},
                             config_path, settings, auth_manager)


def _h_post_nas_acknowledge_alert(data: dict, nas_poller) -> tuple:
    if nas_poller is None:
        return 503, {"error": "NAS poller not available"}
    alert_id = (data.get("id") or "").strip()
    if not alert_id:
        return 400, {"error": "id is required"}
    url, api_key = nas_poller._get_config()
    if not url or not api_key:
        return 503, {"error": "NAS not configured"}
    import urllib.request, urllib.error as _urlerr
    req = urllib.request.Request(
        url.rstrip("/") + "/api/v2.0/alert/dismiss",
        data=json.dumps(alert_id).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except _urlerr.HTTPError as e:
        return e.code, {"error": e.read().decode(errors="replace")}
    except Exception as e:
        return 500, {"error": str(e)}
    # Reflect the change immediately rather than waiting up to 15 minutes
    # for the next scheduled poll - same force-repoll pattern as "Refresh now".
    nas_poller._poll()
    return 200, {"ok": True}


def _h_post_system_restart(history_db, auth_manager) -> tuple:
    if history_db is not None:
        history_db.close()
    if auth_manager is not None:
        auth_manager.close()
    os.execv(sys.executable, [sys.executable] + sys.argv)
    return 200, {"ok": True}  # unreachable; satisfies callers/tests when os.execv is mocked


def _h_get_hosts(config_path: str) -> tuple:
    try:
        cfg = load_yaml(config_path) or {}
        return 200, {"hosts": cfg.get("hosts", [])}
    except Exception as e:
        return 500, {"error": f"Could not read config: {e}"}


def _h_get_pi_health() -> tuple:
    try:
        return 200, read_pi_health()
    except Exception as e:
        logging.exception("Error reading Pi health")
        return 500, {"error": str(e)}


def _h_get_nas(nas_poller, force=False) -> tuple:
    if nas_poller is None:
        return 503, {"reachable": False, "error": "NAS poller not available"}
    if force:
        # "Refresh now" in the UI - without this, the button just re-reads
        # whatever the last background poll (every 15 min) happened to cache,
        # which can look like it did nothing for most of that window.
        nas_poller._poll()
    return 200, nas_poller.get_cache()


def _h_get_proxmox(proxmox_poller, force=False) -> tuple:
    if proxmox_poller is None:
        return 503, {"reachable": False, "error": "Proxmox poller not running"}
    if force:
        proxmox_poller._poll()
    cache = proxmox_poller.get_cache()
    url, _, _, _ = proxmox_poller._get_config()
    if not url and not cache.get("nodes"):
        cache["error"] = "Proxmox not configured"
    return 200, cache


def _h_get_pbs(pbs_poller, force=False) -> tuple:
    if pbs_poller is None:
        return 503, {"reachable": False, "error": "PBS poller not running"}
    if force:
        pbs_poller._poll()
    cache = pbs_poller.get_cache()
    url, _, _ = pbs_poller._get_config()
    if not url and not cache.get("backups"):
        cache["error"] = "PBS not configured"
    return 200, cache


def _h_get_power(ha_poller, history_db, force=False) -> tuple:
    if ha_poller is None:
        return 200, {"configured": False}
    if force:
        ha_poller._poll()
    cache = ha_poller.get_cache()
    history = history_db.get_power_readings(days=7) if history_db else []
    return 200, {"configured": True, "live": cache, "history": history}


def _h_get_ups(ups_poller) -> tuple:
    if ups_poller is None:
        return 200, {"configured": False}
    server, _, ups_name, _, _ = ups_poller._get_config()
    if not server or not ups_name:
        return 200, {"configured": False}
    return 200, {"configured": True, "live": ups_poller.get_cache()}


def _h_post_proxmox_action(data, proxmox_poller, auth_manager) -> tuple:
    import urllib.request, urllib.error as _urlerr
    node   = (data.get("node") or "").strip()
    vmid   = data.get("vmid")
    gtype  = (data.get("type") or "").strip()
    action = (data.get("action") or "").strip()

    if not node or not vmid or gtype not in ("qemu", "lxc") \
            or action not in ("start", "stop", "reboot"):
        return 400, {"error": "Required: node, vmid, type (qemu/lxc), action (start/stop/reboot)"}

    if not PROXMOX_NODE_RE.match(node):
        return 400, {"error": "Invalid node name"}

    try:
        vmid = int(vmid)
    except (TypeError, ValueError):
        return 400, {"error": "vmid must be an integer"}

    if action in ("stop", "reboot") and proxmox_poller:
        proxmox_poller.exempt_vmid(vmid, 30)

    auth_data    = auth_manager.data if auth_manager else {}
    base_url     = auth_data.get("proxmox_url", "")
    user         = auth_data.get("proxmox_user", "")
    token_id     = auth_data.get("proxmox_token_id", "")
    token_secret = auth_data.get("proxmox_token_secret", "")

    if not all([base_url, user, token_id, token_secret]):
        return 503, {"error": "Proxmox not configured"}

    url = (f"{base_url.rstrip('/')}/api2/json/nodes"
           f"/{node}/{gtype}/{vmid}/status/{action}")
    token = f"{user}!{token_id}={token_secret}"
    req = urllib.request.Request(
        url, data=b"", method="POST",
        headers={"Authorization": f"PVEAPIToken={token}"},
    )
    ctx = proxmox_poller._make_ssl_ctx() if proxmox_poller else None
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10):
            return 200, {"ok": True}
    except _urlerr.HTTPError as e:
        body = e.read().decode(errors="replace")
        return e.code, {"error": body}
    except Exception as e:
        return 500, {"error": str(e)}


def _h_get_auth_status(auth_manager, current_user_fn, cookie_value) -> tuple:
    user, is_admin = current_user_fn() if auth_manager else (None, False)
    result = {
        "logged_in":      bool(user),
        "username":       user,
        "admin":          is_admin,
        "setup_required": bool(auth_manager and not auth_manager.has_users),
    }
    if user and auth_manager:
        result["csrf_token"] = auth_manager.csrf_token_for_cookie(cookie_value)
    return 200, result


def _h_get_auth_users(auth_manager) -> tuple:
    if not auth_manager:
        return 404, {"error": "auth disabled"}
    return 200, {"users": auth_manager.list_users()}


def _h_get_inventory(inventory_db, host_manager) -> tuple:
    try:
        items = inventory_db.list_all() if inventory_db else []
        host_map = {}
        if host_manager:
            for h in [h.to_dict() for h in host_manager.list_hosts()]:
                mac = (h.get("specs", {}) or {}).get("mac")
                if mac:
                    key = InventoryDB.normalize_mac(mac)
                    if key:
                        host_map[key] = {
                            "name":       h.get("name"),
                            "ip":         h.get("ip"),
                            "is_up":      h.get("is_up"),
                            "status":     h.get("status"),
                            "uptime_pct": h.get("uptime_pct"),
                        }
        for item in items:
            m = InventoryDB.normalize_mac(item.get("mac"))
            item["linked_host"] = host_map.get(m) if m else None
        return 200, {"items": items}
    except Exception as e:
        logging.exception("inventory list error")
        return 500, {"error": str(e)}


def _h_get_inventory_record(path: str, inventory_db, host_manager) -> tuple:
    try:
        inv_id = int(path.split("/")[-1])
    except ValueError:
        return 400, {"error": "invalid id"}
    if not inventory_db:
        return 500, {"error": "inventory not available"}
    rec = inventory_db.get(inv_id)
    if not rec:
        return 404, {"error": "not found"}
    m = InventoryDB.normalize_mac(rec.get("mac"))
    rec["linked_host"] = None
    if m and host_manager:
        for h in [hh.to_dict() for hh in host_manager.list_hosts()]:
            h_mac = (h.get("specs", {}) or {}).get("mac")
            if InventoryDB.normalize_mac(h_mac) == m:
                rec["linked_host"] = {
                    "name":       h.get("name"),
                    "ip":         h.get("ip"),
                    "is_up":      h.get("is_up"),
                    "status":     h.get("status"),
                    "uptime_pct": h.get("uptime_pct"),
                }
                break
    return 200, rec


def _h_get_topology(inventory_db, host_manager) -> tuple:
    if not inventory_db:
        return 500, {"error": "inventory not available"}
    try:
        return 200, build_topology_payload(inventory_db, host_manager)
    except Exception as e:
        logging.exception("topology fetch error")
        return 500, {"error": str(e)}


def _h_get_connections(inventory_db) -> tuple:
    if not inventory_db:
        return 500, {"error": "inventory not available"}
    try:
        return 200, {"items": inventory_db.list_all_connections()}
    except Exception as e:
        logging.exception("connections list error")
        return 500, {"error": str(e)}


def _h_get_connections_for_device(path: str, inventory_db) -> tuple:
    if not inventory_db:
        return 500, {"error": "inventory not available"}
    try:
        inv_id = int(path.split("/")[-2])
    except (ValueError, IndexError):
        return 400, {"error": "invalid id"}
    try:
        return 200, {"items": inventory_db.list_connections_for_device(inv_id)}
    except Exception as e:
        logging.exception("connections fetch error")
        return 500, {"error": str(e)}


def _h_get_discover(config_path: str) -> tuple:
    try:
        state = get_discovery_state()
        cfg = load_yaml(config_path) or {}
        known_ips = {h.get("ip") for h in cfg.get("hosts", []) if isinstance(h, dict)}
        state["results"] = [
            {**r, "already_monitored": r["ip"] in known_ips}
            for r in state.get("results", [])
        ]
        return 200, state
    except Exception as e:
        logging.exception("Error reading discovery state")
        return 500, {"error": str(e)}


def _h_post_brief(db, data: dict) -> tuple:
    for field in ("subject", "stats", "narrative"):
        if field not in data:
            return 400, {"error": f"missing required field: {field}"}
    try:
        created_ts = int(data["ts"]) if data.get("ts") else int(time.time())
    except (TypeError, ValueError):
        created_ts = int(time.time())
    db.insert_brief(
        created_ts=created_ts,
        subject=str(data["subject"])[:500],
        stats_json=json.dumps(data["stats"]),
        narrative=str(data["narrative"]),
        analysis_json=json.dumps(data["analysis"]) if data.get("analysis") else None,
    )
    return 200, {"ok": True}


def _h_get_briefs(db) -> tuple:
    return 200, {"briefs": db.get_briefs(days=30)}


def _h_get_history(path: str, history_db) -> tuple:
    if history_db is None:
        return 500, {"error": "history not available"}
    from urllib.parse import urlparse, parse_qs
    qs = parse_qs(urlparse(path).query)
    ip = (qs.get("ip", [""])[0] or "").strip()
    if not ip:
        return 400, {"error": "ip required"}
    try:
        hours = max(1, min(int(qs.get("hours", ["24"])[0]), 168))
    except ValueError:
        return 400, {"error": "hours must be an integer"}
    try:
        days = max(1, min(int(qs.get("days", ["60"])[0]), 365))
    except ValueError:
        return 400, {"error": "days must be an integer"}
    try:
        series = history_db.history_series(ip, hours=hours)
        daily = history_db.daily_history(ip, days=days)
    except Exception as e:
        logging.exception("history fetch error")
        return 500, {"error": str(e)}
    return 200, {"ip": ip, "hours": hours, **series, "daily": daily}


def _h_post_inventory_create(body: dict, inventory_db) -> tuple:
    if not inventory_db:
        return 500, {"error": "inventory not available"}
    inv_id, err = inventory_db.create(body)
    if err:
        return 400, {"error": err}
    return 200, {"ok": True, "id": inv_id}


def _h_post_inventory_update(path: str, body: dict, inventory_db) -> tuple:
    if not inventory_db:
        return 500, {"error": "inventory not available"}
    try:
        inv_id = int(path.split("/")[-1])
    except ValueError:
        return 400, {"error": "invalid id"}
    ok, err = inventory_db.update(inv_id, body)
    if not ok:
        return 400, {"error": err}
    return 200, {"ok": True}


def _h_post_inventory_delete(path: str, inventory_db) -> tuple:
    if not inventory_db:
        return 500, {"error": "inventory not available"}
    try:
        inv_id = int(path.split("/")[-2])
    except (ValueError, IndexError):
        return 400, {"error": "invalid id"}
    ok, err = inventory_db.delete(inv_id)
    if not ok:
        return 404, {"error": err}
    return 200, {"ok": True}


def _h_get_quicklinks(quicklinks_db) -> tuple:
    if not quicklinks_db:
        return 500, {"error": "quick links not available"}
    return 200, {"links": quicklinks_db.list_links()}


def _h_post_quicklinks_create(body: dict, quicklinks_db) -> tuple:
    if not quicklinks_db:
        return 500, {"error": "quick links not available"}
    label = str(body.get("label", "")).strip()
    url = str(body.get("url", "")).strip()
    icon = str(body.get("icon") or "")[:8]
    if not label:
        return 400, {"error": "label is required"}
    if not _validate_url(url):
        return 400, {"error": "url must start with http:// or https://"}
    link_id = quicklinks_db.create_link(label, url, icon)
    return 200, {"ok": True, "id": link_id}


def _h_post_quicklinks_update(path: str, body: dict, quicklinks_db) -> tuple:
    if not quicklinks_db:
        return 500, {"error": "quick links not available"}
    try:
        link_id = int(path.split("/")[-1])
    except ValueError:
        return 400, {"error": "invalid id"}
    fields = {}
    if "label" in body:
        label = str(body["label"]).strip()
        if not label:
            return 400, {"error": "label cannot be empty"}
        fields["label"] = label
    if "url" in body:
        url = str(body["url"]).strip()
        if not _validate_url(url):
            return 400, {"error": "url must start with http:// or https://"}
        fields["url"] = url
    if "icon" in body:
        fields["icon"] = str(body["icon"] or "")[:8]
    ok = quicklinks_db.update_link(link_id, **fields)
    if not ok:
        return 404, {"error": "link not found"}
    return 200, {"ok": True}


def _h_post_quicklinks_delete(path: str, quicklinks_db) -> tuple:
    if not quicklinks_db:
        return 500, {"error": "quick links not available"}
    try:
        link_id = int(path.split("/")[-2])
    except (ValueError, IndexError):
        return 400, {"error": "invalid id"}
    ok = quicklinks_db.delete_link(link_id)
    if not ok:
        return 404, {"error": "link not found"}
    return 200, {"ok": True}


def _h_post_quicklinks_move(path: str, body: dict, quicklinks_db) -> tuple:
    if not quicklinks_db:
        return 500, {"error": "quick links not available"}
    try:
        link_id = int(path.split("/")[-2])
    except (ValueError, IndexError):
        return 400, {"error": "invalid id"}
    direction = body.get("direction")
    if direction not in ("up", "down"):
        return 400, {"error": "direction must be 'up' or 'down'"}
    ok = quicklinks_db.move_link(link_id, direction)
    if not ok:
        return 404, {"error": "link not found"}
    return 200, {"ok": True}


def _h_post_connection_create(path: str, body: dict, inventory_db) -> tuple:
    if not inventory_db:
        return 500, {"error": "inventory not available"}
    try:
        inv_id = int(path.split("/")[-2])
    except (ValueError, IndexError):
        return 400, {"error": "invalid id"}
    body = dict(body)
    body["from_device_id"] = inv_id
    new_id, err = inventory_db.create_connection(body)
    if err:
        return 400, {"error": err}
    return 200, {"ok": True, "id": new_id}


def _h_post_connection_update(path: str, body: dict, inventory_db) -> tuple:
    if not inventory_db:
        return 500, {"error": "inventory not available"}
    try:
        conn_id = int(path.split("/")[-1])
    except ValueError:
        return 400, {"error": "invalid id"}
    ok, err, warnings = inventory_db.update_connection(conn_id, body or {})
    if not ok:
        return (404 if err == "connection not found" else 400), {"error": err}
    return 200, {"ok": True, "warnings": warnings}


def _h_post_connection_delete(path: str, inventory_db) -> tuple:
    if not inventory_db:
        return 500, {"error": "inventory not available"}
    try:
        conn_id = int(path.split("/")[-2])
    except (ValueError, IndexError):
        return 400, {"error": "invalid id"}
    ok, err = inventory_db.delete_connection(conn_id)
    if not ok:
        return 404, {"error": err}
    return 200, {"ok": True}


def _conn_v2_gate(inventory_db):
    """None when the connections-v2 endpoints may run, else an error tuple."""
    if not inventory_db:
        return 500, {"error": "inventory not available"}
    if not inventory_db.connections_v2_ready():
        return 503, {"error": "migration_pending"}
    return None


def _h_get_connection_preview(path: str, inventory_db) -> tuple:
    gate = _conn_v2_gate(inventory_db)
    if gate:
        return gate
    q = parse_qs(urlparse(path).query)
    try:
        a_id, b_id = int(q["a"][0]), int(q["b"][0])
    except (KeyError, IndexError, ValueError):
        return 400, {"error": "a and b (device ids) are required"}
    ctype = (q.get("type") or [None])[0]
    preview, err = inventory_db.preview_connection(
        a_id, b_id, inventory_db._normalize_conn_type(ctype) if ctype else None)
    if err:
        return (404 if "do not exist" in err else 400), {"error": err}
    return 200, preview


def _h_post_connection_quick_add(body: dict, inventory_db) -> tuple:
    gate = _conn_v2_gate(inventory_db)
    if gate:
        return gate
    new_id, warnings, err = inventory_db.quick_add_connection(body or {})
    if err:
        return (404 if "do not exist" in err else 400), {"error": err}
    return 200, {"ok": True, "id": new_id, "warnings": warnings}


def _h_get_ports(path: str, inventory_db) -> tuple:
    gate = _conn_v2_gate(inventory_db)
    if gate:
        return gate
    try:
        device_id = int(path.rstrip("/").split("/")[-1])
    except ValueError:
        return 400, {"error": "invalid id"}
    ports, record = inventory_db.ports_for_device(device_id)
    if record is None:
        return 404, {"error": "device not found"}
    return 200, {"device_id": device_id, "device_name": record["system"], "ports": ports,
                 "live": bool(inventory_db._live_ports_for(record))}


def _h_get_suggestions(inventory_db) -> tuple:
    gate = _conn_v2_gate(inventory_db)
    if gate:
        return gate
    items = inventory_db.suggestions.list("pending")
    counts, by_source = {}, {}
    for it in items:
        counts[it["kind"]] = counts.get(it["kind"], 0) + 1
        kind_sources = by_source.setdefault(it["kind"], {})
        kind_sources[it["source"]] = kind_sources.get(it["source"], 0) + 1
    return 200, {"items": items, "counts": counts, "counts_by_source": by_source,
                 "total": len(items)}


def _h_post_suggestion_dismiss(path: str, body: dict, inventory_db) -> tuple:
    gate = _conn_v2_gate(inventory_db)
    if gate:
        return gate
    try:
        sid = int(path.split("/")[-2])
    except (ValueError, IndexError):
        return 400, {"error": "invalid id"}
    row = inventory_db.suggestions.get(sid)
    if row is None:
        return 404, {"error": "suggestion not found"}
    if row["status"] != "pending" or (body or {}).get("fingerprint") != row["fingerprint"]:
        return 409, {"error": "suggestion_changed"}
    inventory_db.suggestions.set_status(sid, "dismissed")
    return 200, {"ok": True}


MAX_ACCEPT_ALL_ITEMS = 500


def _is_proxmox_guest(record):
    return bool(record) and record.get("device_type") == "vm" and (
        (record.get("properties") or {}).get("proxmox_vmid") is not None)


def _monitor_guests(device_ids, inventory_db, monitor):
    """Add these inventory guests to hosts.yaml and start pinging them.
    `monitor`: {config_path, host_manager, settings, is_admin}. Returns
    {device_id: None (now monitored) | reason skipped}."""
    out, entries = {}, {}
    if not monitor or not monitor.get("is_admin"):
        return {i: "admin_required" for i in device_ids}
    for i in device_ids:
        rec = inventory_db.get(i)
        entry = guest_host_entry(rec) if _is_proxmox_guest(rec) else None
        if entry is None:
            out[i] = "no_ip" if _is_proxmox_guest(rec) else "not_a_guest"
        else:
            entries[i] = entry
    if not entries:
        return out
    try:
        added, hosts = add_monitored_hosts(monitor["config_path"], list(entries.values()))
    except Exception as e:
        logging.warning(f"guest monitoring: could not update hosts.yaml: {type(e).__name__}")
        out.update({i: "error" for i in entries})
        return out
    if added and monitor.get("host_manager") is not None:
        settings = monitor.get("settings") or {}
        monitor["host_manager"].reload_from_config(hosts, settings.get("default_interval", 30))
        logging.info(f"guest monitoring: added {', '.join(a['name'] for a in added)}")
    added_ips = {a["ip"] for a in added}
    for i, e in entries.items():
        out[i] = None if e["ip"] in added_ips else "already_monitored"
    return out


def _h_post_suggestion_accept(path: str, body: dict, inventory_db, monitor=None) -> tuple:
    gate = _conn_v2_gate(inventory_db)
    if gate:
        return gate
    try:
        sid = int(path.split("/")[-2])
    except (ValueError, IndexError):
        return 400, {"error": "invalid id"}
    body = body or {}
    ok, err, result = inventory_db.accept_suggestion(
        sid, body.get("fingerprint"), body.get("overrides"), body.get("action"))
    if ok:
        result = dict(result)
        if body.get("monitor") is True and result.get("device_id") is not None:
            # The accept already committed: a monitoring problem is reported,
            # never turned into a failed accept.
            reason = _monitor_guests([result["device_id"]], inventory_db, monitor)[result["device_id"]]
            result["monitored"] = reason is None
            if reason:
                result["monitor_skipped"] = reason
        return 200, {"ok": True, **result}
    if err == "not_found":
        return 404, {"error": "suggestion not found"}
    if err == "suggestion_changed":
        return 409, {"error": "suggestion_changed"}
    return 400, {"error": result.get("error") or "rejected"}


def _h_post_suggestions_accept_all(body: dict, inventory_db, monitor=None) -> tuple:
    gate = _conn_v2_gate(inventory_db)
    if gate:
        return gate
    items = (body or {}).get("items")
    if not isinstance(items, list) or len(items) > MAX_ACCEPT_ALL_ITEMS:
        return 400, {"error": f"items must be a list of at most {MAX_ACCEPT_ALL_ITEMS}"}
    results = inventory_db.accept_suggestions(items)
    wanted = {str(it.get("id")) for it in items if isinstance(it, dict) and it.get("monitor") is True}
    ids = [r["device_id"] for r in results
           if r["ok"] and r.get("device_id") is not None and str(r["id"]) in wanted]
    if ids:
        reasons = _monitor_guests(ids, inventory_db, monitor)   # one write + one reload
        for r in results:
            if r.get("device_id") in reasons:
                r["monitored"] = reasons[r["device_id"]] is None
                if reasons[r["device_id"]]:
                    r["monitor_skipped"] = reasons[r["device_id"]]
    return 200, {"results": results}


def _h_get_unmonitored_guests(inventory_db, config_path) -> tuple:
    """Accepted Proxmox guests with no hosts.yaml entry (by IP or name): the
    backfill banner's list. Guests without an IP are counted separately."""
    if not inventory_db:
        return 200, {"guests": [], "no_ip": 0}
    try:
        hosts = (load_yaml(config_path) or {}).get("hosts") or []
    except Exception:
        hosts = []
    ips, names = monitored_keys(hosts)
    guests, no_ip = [], 0
    for r in inventory_db.list_all():
        if not _is_proxmox_guest(r):
            continue
        name = str(r.get("system") or "").strip()
        ip = str(r.get("ip") or "").strip()
        if name.lower() in names or (ip and ip in ips):
            continue
        entry = guest_host_entry(r)
        if entry is None:
            no_ip += 1
            continue
        guests.append({"id": r["id"], "name": name, "ip": ip, "alert": entry["alert"]})
    guests.sort(key=lambda g: g["name"].lower())
    return 200, {"guests": guests, "no_ip": no_ip}


def _h_post_monitor_guests(body: dict, inventory_db, monitor) -> tuple:
    ids = (body or {}).get("ids")
    if (not isinstance(ids, list) or len(ids) > MAX_ACCEPT_ALL_ITEMS
            or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids)):
        return 400, {"error": f"ids must be a list of at most {MAX_ACCEPT_ALL_ITEMS} integers"}
    reasons = _monitor_guests(ids, inventory_db, monitor)
    return 200, {"added": [i for i in ids if reasons.get(i) is None],
                 "skipped": {str(i): r for i, r in reasons.items() if r}}


def _port_map_devices(switch_macs, inventory_db):
    """Inventory records that are switches from the last scan, matched by
    MAC or MAC alias - the devices the workspace draws a port map for."""
    wanted = {InventoryDB.normalize_mac(m) for m in (switch_macs or ())} - {""}
    if not wanted or not inventory_db:
        return []
    out = []
    for r in inventory_db.list_all():
        aliases = (r.get("properties") or {}).get("mac_aliases") or []
        macs = {InventoryDB.normalize_mac(r.get("mac"))} | {
            InventoryDB.normalize_mac(m) for m in aliases if isinstance(m, str)}
        if macs & wanted:
            out.append({"device_id": r["id"], "name": r["system"]})
    return sorted(out, key=lambda x: (x["name"] or "").lower())


def _h_get_discovery_status(discovery_runner, inventory_db=None) -> tuple:
    if discovery_runner is None:
        return 200, {"sources": {}, "last_scan": None, "scanning": False, "port_maps": []}
    body = dict(discovery_runner.status())
    body["port_maps"] = _port_map_devices(discovery_runner.switch_macs(), inventory_db)
    return 200, body


def _h_post_discovery_scan(discovery_runner) -> tuple:
    if discovery_runner is None or not discovery_runner.any_source_configured():
        return 400, {"error": "no discovery source is configured"}
    return 200, {"ok": True, "queued": discovery_runner.request_scan()}


def _h_post_discover() -> tuple:
    try:
        started, msg = start_discovery_scan()
        if started:
            return 200, {"ok": True, "message": msg}
        return 400, {"error": msg}
    except Exception as e:
        logging.exception("Error starting discovery scan")
        return 500, {"error": str(e)}


def _h_post_detect_mac(body: dict) -> tuple:
    ip = (body.get("ip") or "").strip()
    if not ip:
        return 400, {"error": "ip required"}
    try:
        mac = _detect_mac_for_ip(ip)
        if mac:
            return 200, {"ok": True, "mac": mac}
        return 404, {"error": "not in ARP cache (host may be offline or not yet pinged)"}
    except Exception as e:
        logging.exception("detect-mac error")
        return 500, {"error": str(e)}


def _h_post_wake(body: dict, host_manager, inventory_db) -> tuple:
    target_ip = body.get("ip", "").strip()
    if not target_ip:
        return 400, {"error": "ip is required"}
    target_host = next((h for h in host_manager.list_hosts() if h.ip == target_ip), None)
    if not target_host:
        return 404, {"error": "Host not found"}
    mac = (target_host.specs or {}).get("mac", "")
    if not mac and inventory_db:
        try:
            for rec in inventory_db.list_all():
                if rec.get("ip") == target_ip and rec.get("mac"):
                    mac = rec["mac"]
                    logging.info("WoL: using MAC from inventory record %s for %s",
                                 rec.get("id"), target_ip)
                    break
        except Exception as e:
            logging.warning("WoL inventory MAC lookup failed: %s", e)
    if not mac:
        return 400, {"error": "No MAC address configured for this host (in hosts.yaml or inventory)"}
    ok, err = send_wol_packet(mac)
    if ok:
        return 200, {"ok": True, "message": f"Magic packet sent to {mac}"}
    return 500, {"error": err or "Failed to send magic packet"}


# Maintenance mode handlers


MAINTENANCE_MAX_DURATION_SECONDS = 86400  # 24h cap - guards against a typo'd duration muting a host indefinitely
MAINTENANCE_QUICKSTART_DURATION_SECONDS = 3600  # fixed 1h, matches the single ntfy action button


def _apply_maintenance_start(host, history_db, duration_seconds, reason):
    expires_at = int(time.time()) + duration_seconds
    with host.lock:
        host.maintenance_until = datetime.fromtimestamp(expires_at)
        host.maintenance_reason = reason
    history_db.start_maintenance(host.ip, host.name, expires_at, reason=reason)
    return expires_at


def _h_post_maintenance_start(data: dict, host_manager, history_db) -> tuple:
    ip = (data.get("ip") or "").strip()
    if not ip:
        return 400, {"error": "ip is required"}
    duration_seconds = data.get("duration_seconds")
    if not isinstance(duration_seconds, int) or duration_seconds <= 0:
        return 400, {"error": "duration_seconds must be a positive integer"}
    if duration_seconds > MAINTENANCE_MAX_DURATION_SECONDS:
        return 400, {"error": f"duration_seconds must not exceed {MAINTENANCE_MAX_DURATION_SECONDS}"}
    host = next((h for h in host_manager.list_hosts() if h.ip == ip), None)
    if not host:
        return 404, {"error": "Host not found"}
    reason = (data.get("reason") or "").strip()
    expires_at = _apply_maintenance_start(host, history_db, duration_seconds, reason)
    return 200, {"ok": True, "expires_at": expires_at}


def _h_post_maintenance_clear(data: dict, host_manager, history_db) -> tuple:
    ip = (data.get("ip") or "").strip()
    if not ip:
        return 400, {"error": "ip is required"}
    host = next((h for h in host_manager.list_hosts() if h.ip == ip), None)
    if not host:
        return 404, {"error": "Host not found"}
    with host.lock:
        host.maintenance_until = None
        host.maintenance_reason = ""
    history_db.clear_maintenance(ip)
    return 200, {"ok": True}


def _h_post_maintenance_quickstart(data: dict, host_manager, history_db, auth_manager) -> tuple:
    ip = (data.get("ip") or "").strip()
    token = data.get("token") or ""
    if not ip or not token:
        return 400, {"error": "ip and token are required"}
    secret_key = (auth_manager.data or {}).get("secret_key") if auth_manager else None
    if not secret_key or not verify_maintenance_token(secret_key, ip, token):
        return 403, {"error": "invalid or expired token"}
    host = next((h for h in host_manager.list_hosts() if h.ip == ip), None)
    if not host:
        return 404, {"error": "Host not found"}
    expires_at = _apply_maintenance_start(
        host, history_db, MAINTENANCE_QUICKSTART_DURATION_SECONDS, "ntfy quick action")
    return 200, {"ok": True, "expires_at": expires_at}


def _h_post_hosts(body: dict, config_path: str, host_manager, settings: dict) -> tuple:
    new_hosts = body.get("hosts", [])
    if not isinstance(new_hosts, list):
        return 400, {"error": "'hosts' must be a list"}
    ok, err = validate_hosts_config({"hosts": new_hosts})
    if not ok:
        return 400, {"error": err}
    try:
        save_hosts_config(config_path, new_hosts)
        logging.info(f"hosts.yaml updated via web: {len(new_hosts)} hosts")
        host_manager.reload_from_config(new_hosts, settings.get("default_interval", 30))
        return 200, {"ok": True, "count": len(new_hosts)}
    except Exception as e:
        logging.exception("Error saving hosts")
        return 500, {"error": str(e)}


def _h_post_auth_users(body: dict, auth_manager) -> tuple:
    username = body.get("username", "")
    password = body.get("password", "")
    is_admin = bool(body.get("admin", False))
    ok, err = auth_manager.create_user(username, password, admin=is_admin)
    if not ok:
        return 400, {"error": err}
    return 200, {"ok": True}


def _h_post_auth_password(body: dict, user: str, auth_manager) -> tuple:
    current = body.get("current", "")
    new_pw = body.get("new", "")
    if not auth_manager.verify_password(user, current):
        return 401, {"error": "current password is incorrect"}
    ok, err = auth_manager.change_password(user, new_pw)
    if not ok:
        return 400, {"error": err}
    return 200, {"ok": True}


def _h_post_auth_user_delete(path: str, auth_manager) -> tuple:
    username = path[len("/api/auth/users/"):]
    if not username:
        return 400, {"error": "username required"}
    ok, err = auth_manager.delete_user(username)
    if not ok:
        return 400, {"error": err}
    return 200, {"ok": True}


