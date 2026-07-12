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

from netwatch.storage import InventoryDB
from netwatch.network import (
    _detect_mac_for_ip, send_wol_packet, read_pi_health,
    start_discovery_scan, get_discovery_state,
)
from netwatch.hosts import load_yaml, _validate_url, validate_hosts_config, save_hosts_config
from netwatch.pollers import PROXMOX_NODE_RE


def build_topology_payload(inventory_db, host_manager):
    """Bundle inventory records + connections + linked-host status into a
    single payload for the topology view. Doing this server-side cuts the
    frontend from 3 round trips to 1 and lets us join MAC -> host status
    without serialising the full host list."""
    if not inventory_db:
        return {"nodes": [], "edges": []}

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

    nodes = []
    for rec in inventory_db.list_all():
        norm_mac = InventoryDB.normalize_mac(rec.get("mac")) if rec.get("mac") else ""
        linked = host_by_mac.get(norm_mac) if norm_mac else None
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
        })

    edges = []
    for c in inventory_db.list_all_connections():
        edges.append({
            "id":              c["id"],
            "source":          c["from_device_id"],
            "target":          c["to_device_id"],
            "from_port":       c["from_port"],
            "to_port":         c["to_port"],
            "connection_type": c["connection_type"],
            "notes":           c.get("notes") or None,
        })

    return {"nodes": nodes, "edges": edges}


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
}

_SETTINGS_INT_RANGES = {
    "default_interval": (5,  3600),
    "ping_timeout":     (1,  30),
    "history_window":   (10, 10000),
    "refresh_rate":     (1,  60),
    "history_days":     (1,  365),
}

_SETTINGS_URL_KEYS = {"ntfy_server", "truenas_url", "proxmox_url", "ha_url", "pbs_url"}
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
    }


# ── Route handler functions (module-level; testable without HTTP) ─────────────

def _h_get_status(host_manager, settings, incident_log, inventory_db) -> tuple:
    return 200, build_api_payload(host_manager, settings, incident_log, inventory_db)


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
    "openrouter_api_key", "ha_token", "pbs_api_token_secret",
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
    ok, err = inventory_db.update_connection(conn_id, body)
    if not ok:
        return 400, {"error": err}
    return 200, {"ok": True}


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


