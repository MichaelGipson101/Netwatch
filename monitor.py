#!/usr/bin/env python3
"""
netwatch - Homelab ping monitor with btop-style TUI, web dashboard,
and web-based hosts.yaml editor. (Version: see VERSION below.)

Usage:
    python monitor.py                  # TUI + web server
    python monitor.py --no-tui         # Headless + web server (for systemd)
    python monitor.py --no-web         # TUI only, no web
    python monitor.py --port 8080      # Custom port

Config: hosts.yaml in the same directory.
Dashboard: http://<pi-ip>:8080
"""

import os, sys, time, json, re, shutil, subprocess, threading, curses, yaml, argparse, logging, hmac
from datetime import datetime, timezone
from collections import deque
from dataclasses import dataclass, field
from typing import Optional
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from netwatch import BRAND, VERSION
from netwatch.auth import AuthManager, parse_cookies
from netwatch.storage import (
    _column_exists, HistoryDB, InventoryDB, _flush_loop, _prune_loop,
    export_inventory_to_xlsx, import_inventory_from_xlsx,
    BACKUP_MANIFEST_VERSION, create_backup_tarball, restore_backup,
)
from netwatch.network import (
    _detect_mac_for_ip, _normalise_mac, _save_detected_mac, _get_dashboard_url,
    send_ntfy_alert, _send_alert_async, _detect_broadcast_address, _get_wol_broadcast,
    send_wol_packet, _get_pi_local_ips, _is_local_ip, read_pi_health,
    _check_nmap_available, _detect_subnet_cidr, _run_nmap_scan,
    start_discovery_scan, get_discovery_state,
    _ARP_SAVED_THIS_SESSION, NTFY_DOWN_THRESHOLD,
)
from netwatch.hosts import (
    HostState, check_tcp_service, ping_host, _should_log_transition, poll_host,
    HostManager, IncidentLog, load_yaml, _validate_ip_or_hostname, _validate_url,
    validate_hosts_config, save_hosts_config,
)


# ============================================================================
# NAS Poller (TrueNAS REST API)
# ============================================================================

class NASPoller:
    POLL_INTERVAL_SECONDS = 900    # 15 minutes
    REPLICATION_STALE_HOURS = 25   # grace window for daily replication tasks

    def __init__(self, auth_manager, alert_settings=None, alert_port=None):
        self._auth_manager = auth_manager
        self._alert_settings = alert_settings or {}
        self._alert_port = alert_port
        self._cache = {
            "reachable": False,
            "last_updated": None,
            "error": None,
            "pools": [],
            "replication_tasks": [],
        }
        self._lock = threading.Lock()
        self._alert_state = {}  # condition_id -> bool, True = currently alerting

    def get_cache(self):
        with self._lock:
            import copy
            return copy.deepcopy(self._cache)

    def _get_config(self):
        data = self._auth_manager.data if self._auth_manager else {}
        return data.get("truenas_url", ""), data.get("truenas_api_key", "")

    @staticmethod
    def _fetch(url, api_key, path):
        import urllib.request
        req = urllib.request.Request(
            url.rstrip("/") + path,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())

    @staticmethod
    def _parse_vdevs(data_vdevs):
        vdevs = []
        for v in data_vdevs:
            vdev = {
                "type": v.get("type", "DISK"),
                "name": v.get("name", ""),
                "status": v.get("status", "UNKNOWN"),
                "disks": [],
            }
            for child in v.get("children", []):
                vdev["disks"].append({
                    "name": child.get("disk") or child.get("name", ""),
                    "status": child.get("status", "UNKNOWN"),
                })
            if not vdev["disks"] and v.get("disk"):
                vdev["disks"].append({"name": v["disk"], "status": v.get("status", "UNKNOWN")})
            vdevs.append(vdev)
        return vdevs

    @staticmethod
    def _parse_scrub(scan):
        if not scan:
            return {"status": None, "end_time": None, "errors": 0}
        end_raw = scan.get("end_time")
        end_str = None
        if isinstance(end_raw, str):
            end_str = end_raw
        elif isinstance(end_raw, dict):
            ms = end_raw.get("$date", {})
            if isinstance(ms, dict):
                ms = ms.get("$numberLong")
            if ms:
                from datetime import timezone
                end_str = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
        return {"status": scan.get("state"), "end_time": end_str, "errors": scan.get("errors", 0)}

    @staticmethod
    def _next_cron_run(minute, hour, dom, month, dow, start=None, tz=None):
        """Return next ISO datetime string for a simple cron expression (no ranges/steps/lists).

        TrueNAS's schedule hour/minute are in the NAS's configured local
        timezone, not UTC - `tz` (a tzinfo) must reflect that or the matched
        wall-clock time will be off by the NAS's UTC offset. `start` lets the
        search begin later than "now" - used to fold in a scrub task's
        threshold (the minimum days TrueNAS enforces between actual runs,
        independent of how often the cron itself fires)."""
        from datetime import timedelta, timezone
        tz = tz or timezone.utc
        now = (start or datetime.now(tz=timezone.utc)).astimezone(tz).replace(second=0, microsecond=0)
        t = now + timedelta(minutes=1)
        for _ in range(366 * 24 * 60):
            if (month == "*" or t.month == int(month)) and \
               (dom == "*" or t.day == int(dom)) and \
               (dow == "*" or t.weekday() == (int(dow) - 1) % 7) and \
               (hour == "*" or t.hour == int(hour)) and \
               (minute == "*" or t.minute == int(minute)):
                return t.isoformat()
            t += timedelta(minutes=1)
        return None

    @classmethod
    def _parse_pool(cls, raw, scrub_tasks, tz_name=None):
        from datetime import timedelta, timezone
        from zoneinfo import ZoneInfo
        try:
            tz = ZoneInfo(tz_name) if tz_name else timezone.utc
        except Exception:
            tz = timezone.utc
        name = raw.get("name", "")
        next_scrub = None
        last_scrub = cls._parse_scrub(raw.get("scan"))
        for st in scrub_tasks:
            if st.get("pool_name") == name:
                sch = st.get("schedule", {}) or {}
                def _f(k): v = sch.get(k); return "*" if v is None else str(v)
                # TrueNAS's schedule says how often to *check* (e.g. every
                # Sunday); "threshold" is the minimum days since the last
                # scrub before it's actually allowed to run again. Without
                # this, next_scrub understates the real wait whenever the
                # threshold is longer than the schedule's own interval.
                search_start = None
                threshold_days = st.get("threshold")
                if threshold_days and last_scrub.get("end_time"):
                    try:
                        last_end = datetime.fromisoformat(last_scrub["end_time"])
                        eligible = last_end + timedelta(days=int(threshold_days))
                        now = datetime.now(tz=timezone.utc)
                        search_start = eligible if eligible > now else now
                    except (ValueError, TypeError):
                        search_start = None
                next_scrub = cls._next_cron_run(
                    _f("minute"), _f("hour"), _f("dom"), _f("month"), _f("dow"),
                    start=search_start, tz=tz,
                )
                break
        topology = raw.get("topology", {}) or {}
        return {
            "name": name,
            "status": raw.get("status", "UNKNOWN"),
            "capacity_used_bytes": raw.get("allocated", 0),
            "capacity_total_bytes": raw.get("size", 0),
            "vdevs": cls._parse_vdevs(topology.get("data", [])),
            # Non-data vdevs (cache/L2ARC, log/SLOG, spares, special, dedup)
            # were previously dropped entirely - a failed one would never
            # show up anywhere, since pool "status" doesn't always reflect
            # a non-critical device like a cache disk failing.
            "cache_vdevs": cls._parse_vdevs(topology.get("cache", [])),
            "log_vdevs": cls._parse_vdevs(topology.get("log", [])),
            "spare_vdevs": cls._parse_vdevs(topology.get("spare", [])),
            "special_vdevs": cls._parse_vdevs(topology.get("special", [])),
            "dedup_vdevs": cls._parse_vdevs(topology.get("dedup", [])),
            "last_scrub": last_scrub,
            "next_scrub": next_scrub,
        }

    @staticmethod
    def _parse_replication(raw):
        state = raw.get("state") or {}
        dt_raw = state.get("datetime") or state.get("time_finished")
        last_run = None
        if isinstance(dt_raw, str):
            last_run = dt_raw
        elif isinstance(dt_raw, dict):
            inner = dt_raw.get("$date") or dt_raw.get("$numberLong")
            if isinstance(inner, dict):
                inner = inner.get("$numberLong")
            if isinstance(inner, (int, float)):
                last_run = datetime.fromtimestamp(inner / 1000, tz=timezone.utc).isoformat()
            elif isinstance(inner, str):
                last_run = inner
        return {
            "id": raw.get("id"),
            "name": raw.get("name", ""),
            "enabled": bool(raw.get("enabled", True)),
            "last_run": last_run,
            "last_state": state.get("state") or "UNKNOWN",
        }

    _ALERT_SEVERITY_ORDER = ["INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL", "ALERT", "EMERGENCY"]
    _ALERT_MIN_LEVEL = "WARNING"

    @staticmethod
    def _filter_alerts(raw_alerts, ignored_klasses):
        """Keep only WARNING-and-above TrueNAS alerts, excluding any klass
        the user has chosen to permanently ignore (comma-separated string).
        An unrecognized level is kept rather than dropped - better to show
        something unexpected than silently hide it."""
        ignored = {k.strip() for k in (ignored_klasses or "").split(",") if k.strip()}
        min_idx = NASPoller._ALERT_SEVERITY_ORDER.index(NASPoller._ALERT_MIN_LEVEL)
        kept = []
        for a in raw_alerts:
            klass = a.get("klass", "")
            if klass in ignored:
                continue
            level = (a.get("level") or "").upper()
            if level in NASPoller._ALERT_SEVERITY_ORDER:
                if NASPoller._ALERT_SEVERITY_ORDER.index(level) < min_idx:
                    continue
            kept.append({
                "id": a.get("id"),
                "klass": klass,
                "level": level or "UNKNOWN",
                "message": a.get("formatted") or a.get("text") or "",
            })
        return kept

    def start(self, stop_event):
        t = threading.Thread(target=self._loop, args=(stop_event,), daemon=True, name="nas-poller")
        t.start()
        return t

    def _loop(self, stop_event):
        self._poll()
        while not stop_event.is_set():
            stop_event.wait(timeout=self.POLL_INTERVAL_SECONDS)
            if not stop_event.is_set():
                self._poll()

    def _poll(self):
        url, api_key = self._get_config()
        if not url or not api_key:
            with self._lock:
                self._cache.update({"reachable": False, "error": "NAS not configured"})
            return
        try:
            pools_raw = self._fetch(url, api_key, "/api/v2.0/pool")
            scrub_tasks = self._fetch(url, api_key, "/api/v2.0/pool/scrub")
            replication_raw = self._fetch(url, api_key, "/api/v2.0/replication")
            system_info = self._fetch(url, api_key, "/api/v2.0/system/info")
            alerts_raw = self._fetch(url, api_key, "/api/v2.0/alert/list")
            tz_name = system_info.get("timezone") or "UTC"
            pools = [self._parse_pool(p, scrub_tasks, tz_name) for p in pools_raw]
            tasks = [self._parse_replication(r) for r in replication_raw]
            ignored_klasses = self._alert_settings.get("truenas_ignored_alert_klasses", "")
            alerts = self._filter_alerts(alerts_raw, ignored_klasses)
            with self._lock:
                self._cache = {
                    "reachable": True,
                    "last_updated": datetime.now(tz=timezone.utc).isoformat(),
                    "error": None,
                    "pools": pools,
                    "replication_tasks": tasks,
                    "alerts": alerts,
                }
            self._check_alerts(pools, tasks, alerts)
        except Exception as e:
            logging.warning(f"NASPoller: poll failed: {e}")
            with self._lock:
                self._cache.update({"reachable": False, "error": str(e)})

    def _fire_alert(self, condition_id, title, message):
        if not self._alert_state.get(condition_id, False):
            self._alert_state[condition_id] = True
            click_url = _get_dashboard_url(self._alert_settings, self._alert_port or 8080)
            _send_alert_async(
                self._alert_settings, title, message,
                priority="high", tags="warning", click_url=click_url,
            )

    def _clear_alert(self, condition_id):
        self._alert_state[condition_id] = False

    def _check_alerts(self, pools, tasks, alerts=None):
        from datetime import timezone, timedelta
        for pool in pools:
            cid = f"pool_health_{pool['name']}"
            if pool["status"] != "ONLINE":
                self._fire_alert(cid, "Netwatch · NAS Alert",
                                 f"Pool \"{pool['name']}\" is {pool['status']}")
            else:
                self._clear_alert(cid)
            cid_scrub = f"scrub_errors_{pool['name']}"
            errors = (pool.get("last_scrub") or {}).get("errors", 0) or 0
            if int(errors) > 0:
                self._fire_alert(cid_scrub, "Netwatch · NAS Alert",
                                 f"Scrub on \"{pool['name']}\" found {errors} error(s)")
            else:
                self._clear_alert(cid_scrub)

        now = datetime.now(tz=timezone.utc)
        stale_delta = timedelta(hours=self.REPLICATION_STALE_HOURS)
        for task in tasks:
            task_key = task["id"] if task["id"] is not None else task["name"]
            cid = f"replication_{task_key}"
            if not task.get("enabled", True):
                # A deliberately disabled task has no reason to alert as
                # stale/failed forever - clear any existing alert and skip.
                self._clear_alert(cid)
                continue
            ok_states = ("SUCCESS", "FINISHED", "PENDING", "RUNNING")
            failed = task["last_state"] not in ok_states
            stale = False
            last = None
            if task.get("last_run"):
                try:
                    last = datetime.fromisoformat(task["last_run"].rstrip("Z"))
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    stale = (now - last) > stale_delta
                except (ValueError, TypeError):
                    pass
            if failed or stale:
                hours_old = int((now - last).total_seconds() // 3600) if last else 0
                reason = "failed" if failed else f"stale ({hours_old}h)"
                self._fire_alert(cid, "Netwatch · NAS Alert",
                                 f"Replication \"{task['name']}\" {reason}")
            else:
                self._clear_alert(cid)

        alerts = alerts or []
        current_alert_cids = set()
        for alert in alerts:
            cid = f"truenas_alert_{alert['id']}"
            current_alert_cids.add(cid)
            self._fire_alert(cid, "Netwatch · NAS Alert", f"TrueNAS: {alert['message']}")
        # An alert that resolved (or got newly ignored) simply disappears from
        # TrueNAS's own list rather than arriving with a "resolved" state, so
        # clear any previously-firing TrueNAS alert no longer present here.
        for cid in list(self._alert_state.keys()):
            if cid.startswith("truenas_alert_") and cid not in current_alert_cids:
                self._clear_alert(cid)


# ============================================================================
# Proxmox Poller (Proxmox VE REST API)
# ============================================================================

# Proxmox node names are short hostnames; reject anything else so a
# crafted "node" value can't be used to traverse out of /api2/json/nodes/...
PROXMOX_NODE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$')


class ProxmoxPoller:
    POLL_INTERVAL_SECONDS = 60

    def __init__(self, auth_manager, alert_settings=None, alert_port=None):
        self._auth_manager = auth_manager
        self._alert_settings = alert_settings or {}
        self._alert_port = alert_port
        self._cache = {
            "reachable": False,
            "last_updated": None,
            "error": None,
            "nodes": [],
        }
        self._lock = threading.Lock()
        self._alert_state = {}    # condition_id -> bool (True = currently alerting)
        self._exemptions = {}     # vmid (int) -> float timestamp (exempt until)
        self._node_history = {}   # node name -> {"cpu": [...], "mem": [...]}

    @staticmethod
    def append_history(history, nodes, cap=20):
        """Record one CPU/RAM sample per node into `history` and attach the
        rolling series to each node dict (drives dashboard sparklines)."""
        for n in nodes:
            h = history.setdefault(n["name"], {"cpu": [], "mem": []})
            total = n.get("mem_total_bytes") or 0
            mem_pct = round(n.get("mem_used_bytes", 0) / total * 100, 1) if total else 0.0
            h["cpu"].append(n.get("cpu_percent", 0.0))
            h["mem"].append(mem_pct)
            del h["cpu"][:-cap]
            del h["mem"][:-cap]
            n["cpu_history"] = list(h["cpu"])
            n["mem_history"] = list(h["mem"])

    def get_cache(self):
        with self._lock:
            import copy
            return copy.deepcopy(self._cache)

    def exempt_vmid(self, vmid, seconds=30):
        """Suppress unexpected-stop alert for vmid for the next N seconds."""
        self._exemptions[int(vmid)] = time.time() + seconds

    def _get_config(self):
        data = self._auth_manager.data if self._auth_manager else {}
        return (
            data.get("proxmox_url", ""),
            data.get("proxmox_user", ""),
            data.get("proxmox_token_id", ""),
            data.get("proxmox_token_secret", ""),
        )

    def _make_ssl_ctx(self):
        import ssl
        verify = bool(self._alert_settings.get("proxmox_verify_ssl", True))
        if not verify:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        ca_cert = (self._alert_settings.get("proxmox_ca_cert") or "").strip()
        ctx = ssl.create_default_context(cafile=ca_cert or None)
        if ca_cert and hasattr(ssl, "VERIFY_X509_STRICT"):
            # Proxmox's self-generated cluster CA commonly omits the Key Usage
            # extension on its root cert; OpenSSL 3.x's strict X.509 policy
            # rejects that as an issuer even though the chain is otherwise
            # genuinely valid. Relax just that one RFC-strictness check for a
            # pinned self-managed CA - full chain-of-trust verification still
            # applies, this only stops a non-compliant-but-legitimate CA from
            # being rejected on a technicality.
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        return ctx

    def _fetch(self, base_url, user, token_id, token_secret, path):
        import urllib.request
        url = base_url.rstrip("/") + path
        token = f"{user}!{token_id}={token_secret}"
        req = urllib.request.Request(
            url, headers={"Authorization": f"PVEAPIToken={token}"}
        )
        with urllib.request.urlopen(req, context=self._make_ssl_ctx(), timeout=10) as r:
            return json.loads(r.read().decode())["data"]

    def _build_guest(self, raw, guest_type):
        return {
            "vmid":            raw.get("vmid"),
            "name":            raw.get("name", ""),
            "type":            guest_type,
            "status":          raw.get("status", "stopped"),
            "cpu_percent":     round((raw.get("cpu") or 0.0) * 100, 1),
            "mem_used_bytes":  raw.get("mem", 0),
            "mem_total_bytes": raw.get("maxmem", 0),
        }

    def _build_node(self, raw_node, qemu_list, lxc_list):
        guests = (
            [self._build_guest(g, "qemu") for g in qemu_list]
            + [self._build_guest(g, "lxc")  for g in lxc_list]
        )
        guests.sort(key=lambda g: g["vmid"] or 0)
        return {
            "name":            raw_node.get("node", ""),
            "status":          raw_node.get("status", "unknown"),
            "cpu_percent":     round((raw_node.get("cpu") or 0.0) * 100, 1),
            "mem_used_bytes":  raw_node.get("mem", 0),
            "mem_total_bytes": raw_node.get("maxmem", 0),
            "uptime_seconds":  raw_node.get("uptime", 0),
            "guests":          guests,
        }

    def _fire_alert(self, condition_id, message):
        if not self._alert_state.get(condition_id, False):
            self._alert_state[condition_id] = True
            click_url = _get_dashboard_url(self._alert_settings, self._alert_port or 8080)
            _send_alert_async(
                self._alert_settings, "Netwatch · Proxmox Alert", message,
                priority="high", tags="rotating_light", click_url=click_url,
            )

    def _clear_alert(self, condition_id):
        self._alert_state[condition_id] = False

    def _check_alerts(self, nodes, prev_nodes):
        now = time.time()
        prev_states = {}
        for n in prev_nodes:
            for g in n.get("guests", []):
                prev_states[g["vmid"]] = g["status"]

        for node in nodes:
            name = node["name"]

            cid_node = f"node:{name}"
            if node["status"] != "online":
                self._fire_alert(cid_node, f'Proxmox node "{name}" lost cluster heartbeat — check corosync if it persists')
            else:
                self._clear_alert(cid_node)

            for guest in node.get("guests", []):
                vmid   = guest["vmid"]
                gname  = guest["name"]
                status = guest["status"]

                cid_stop = f"stop:{vmid}"
                if (prev_states.get(vmid) == "running"
                        and status == "stopped"
                        and now > self._exemptions.get(vmid, 0)):
                    self._fire_alert(cid_stop,
                                     f'VM "{gname}" ({vmid}) stopped unexpectedly on {name}')
                elif status == "running":
                    self._clear_alert(cid_stop)

                cid_pause = f"pause:{vmid}"
                if status == "paused":
                    self._fire_alert(cid_pause,
                                     f'VM "{gname}" ({vmid}) is paused on {name}')
                else:
                    self._clear_alert(cid_pause)

    def _poll(self):
        url, user, token_id, token_secret = self._get_config()
        if not all([url, user, token_id, token_secret]):
            with self._lock:
                self._cache["error"] = "Proxmox not configured"
            return
        try:
            raw_nodes = self._fetch(url, user, token_id, token_secret, "/api2/json/nodes")
            nodes = []
            for raw in raw_nodes:
                name = raw.get("node", "")
                qemu = self._fetch(url, user, token_id, token_secret,
                                   f"/api2/json/nodes/{name}/qemu")
                lxc  = self._fetch(url, user, token_id, token_secret,
                                   f"/api2/json/nodes/{name}/lxc")
                nodes.append(self._build_node(raw, qemu, lxc))
            now_str = datetime.now().isoformat(timespec="seconds")
            with self._lock:
                prev_nodes = self._cache.get("nodes", [])
                self.append_history(self._node_history, nodes)
                self._cache.update({
                    "reachable":    True,
                    "last_updated": now_str,
                    "error":        None,
                    "nodes":        nodes,
                })
            self._check_alerts(nodes, prev_nodes)
        except Exception as e:
            logging.warning(f"ProxmoxPoller: poll failed: {e}")
            with self._lock:
                self._cache["reachable"] = False

    def start(self, stop_event):
        def _loop():
            while not stop_event.is_set():
                try:
                    self._poll()
                except Exception as e:
                    logging.warning(f"ProxmoxPoller: unexpected error in loop: {e}")
                stop_event.wait(self.POLL_INTERVAL_SECONDS)
        threading.Thread(target=_loop, daemon=True, name="proxmox-poller").start()


# ============================================================================
# PBS Poller (Proxmox Backup Server REST API)
# ============================================================================

class PBSPoller:
    POLL_INTERVAL_SECONDS = 300   # 5 minutes; backups run nightly, no need for Proxmox's 60s cadence
    STALE_HOURS = 25              # grace window for daily backup jobs, matches NASPoller.REPLICATION_STALE_HOURS

    def __init__(self, auth_manager, alert_settings=None, alert_port=None, proxmox_poller=None):
        self._auth_manager = auth_manager
        self._alert_settings = alert_settings or {}
        self._alert_port = alert_port
        self._proxmox_poller = proxmox_poller
        self._cache = {
            "reachable": False,
            "last_updated": None,
            "error": None,
            "datastores": [],
            "backups": [],
        }
        self._lock = threading.Lock()
        self._alert_state = {}    # condition_id -> bool (True = currently alerting)

    def get_cache(self):
        with self._lock:
            import copy
            return copy.deepcopy(self._cache)

    def _get_config(self):
        data = self._auth_manager.data if self._auth_manager else {}
        return (
            data.get("pbs_url", ""),
            data.get("pbs_api_token_id", ""),
            data.get("pbs_api_token_secret", ""),
        )

    def _make_ssl_ctx(self):
        import ssl
        verify = bool(self._alert_settings.get("pbs_verify_ssl", True))
        if not verify:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        ca_cert = (self._alert_settings.get("pbs_ca_cert") or "").strip()
        ctx = ssl.create_default_context(cafile=ca_cert or None)
        if ca_cert and hasattr(ssl, "VERIFY_X509_STRICT"):
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        return ctx

    def _fetch(self, base_url, token_id, token_secret, path):
        import urllib.request
        url = base_url.rstrip("/") + path
        # PBS's own API token header uses a colon between token name and
        # secret, unlike Proxmox VE's PVEAPIToken which uses "=".
        req = urllib.request.Request(
            url, headers={"Authorization": f"PBSAPIToken={token_id}:{token_secret}"}
        )
        with urllib.request.urlopen(req, context=self._make_ssl_ctx(), timeout=10) as r:
            return json.loads(r.read().decode())["data"]

    @staticmethod
    def _parse_datastore(raw):
        used = raw.get("used") or 0
        total = raw.get("total") or 0
        return {
            "name":        raw.get("store", ""),
            "used_bytes":  used,
            "total_bytes": total,
            "avail_bytes": raw.get("avail") or 0,
            "percent":     round(used / total * 100, 1) if total else 0.0,
        }

    @staticmethod
    def _classify_backup(last_backup_time, verify_state, stale_hours, now=None):
        """Classify a guest's most recent backup snapshot.

        `last_backup_time` is an aware UTC datetime or None if the guest has
        no backup history at all - that's "none", not "stale", since there's
        nothing to alert on for a guest that's simply never been backed up.

        Staleness is measured in business hours (Sat/Sun excluded) rather than
        flat wall-clock hours, so a Fri-night backup doesn't read as stale over
        the weekend on an M-F backup schedule - see PBSPoller._business_hours_elapsed."""
        if last_backup_time is None:
            return "none"
        if verify_state == "failed":
            return "failed"
        now = now or datetime.now(tz=timezone.utc)
        if PBSPoller._business_hours_elapsed(last_backup_time, now) > stale_hours:
            return "stale"
        return "ok"

    @staticmethod
    def _business_hours_elapsed(start, end):
        """Hours between two aware datetimes, excluding any time that falls on a
        Saturday or Sunday in whichever timezone the datetimes already carry -
        used so a Fri-night backup's staleness clock pauses across the weekend
        instead of accumulating two days of "missed backup" before Monday's job
        has even run. Deliberately does not call .astimezone() - see this
        plan's Global Constraints for why (ambient-local-timezone conversion
        made test results depend on the test-runner machine's timezone)."""
        from datetime import timedelta
        if end <= start:
            return 0.0
        total_hours = (end - start).total_seconds() / 3600.0
        weekend_hours = 0.0
        day = start.date()
        one_day = timedelta(days=1)
        while day <= end.date():
            if day.weekday() >= 5:  # Saturday=5, Sunday=6
                day_start = datetime(day.year, day.month, day.day, tzinfo=start.tzinfo)
                day_end = day_start + one_day
                overlap_start = max(start, day_start)
                overlap_end = min(end, day_end)
                if overlap_end > overlap_start:
                    weekend_hours += (overlap_end - overlap_start).total_seconds() / 3600.0
            day += one_day
        return total_hours - weekend_hours

    @classmethod
    def _group_backups(cls, snapshots, now=None):
        """Group PBS snapshot records by (backup-type, backup-id) and keep
        only the most recent snapshot per guest - that's "last backup" for
        that VM/CT."""
        latest = {}
        for snap in snapshots:
            btype = snap.get("backup-type")
            bid_raw = snap.get("backup-id")
            if btype is None or bid_raw is None:
                continue
            key = (btype, bid_raw)
            ts = snap.get("backup-time") or 0
            if key not in latest or ts > (latest[key].get("backup-time") or 0):
                latest[key] = snap

        backups = []
        for (btype, bid_raw), snap in latest.items():
            ts = snap.get("backup-time")
            last_backup_time = (
                datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
            )
            verification = snap.get("verification") or {}
            verify_state = verification.get("state")
            vmid = int(bid_raw) if str(bid_raw).isdigit() else bid_raw
            backups.append({
                "type":             btype,
                "vmid":             vmid,
                "last_backup_time": last_backup_time.isoformat() if last_backup_time else None,
                "size_bytes":       snap.get("size"),
                "verify_state":     verify_state,
                "status":           cls._classify_backup(last_backup_time, verify_state, cls.STALE_HOURS, now),
            })
        backups.sort(key=lambda b: (b["type"], str(b["vmid"])))
        return backups

    def _fire_alert(self, condition_id, message):
        if not self._alert_state.get(condition_id, False):
            self._alert_state[condition_id] = True
            click_url = _get_dashboard_url(self._alert_settings, self._alert_port or 8080)
            _send_alert_async(
                self._alert_settings, "Netwatch · Backup Alert", message,
                priority="high", tags="warning", click_url=click_url,
            )

    def _clear_alert(self, condition_id):
        self._alert_state[condition_id] = False

    def _known_guest_vmids(self):
        """VMIDs currently present in Proxmox, or None if that's not known
        (no Proxmox poller wired, or it hasn't successfully polled yet) -
        PBS keeps backup snapshots long after a guest is deleted from
        Proxmox, so without this a deleted VM/LXC's last backup just gets
        older forever and re-fires a "stale backup" alert on every restart."""
        if self._proxmox_poller is None:
            return None
        nodes = self._proxmox_poller.get_cache().get("nodes")
        if not nodes:
            return None
        return {
            g["vmid"] for n in nodes for g in n.get("guests", []) if g.get("vmid") is not None
        }

    def _check_alerts(self, backups):
        known_vmids = self._known_guest_vmids()
        for b in backups:
            cid = f"pbs-backup-{b['type']}-{b['vmid']}"
            if known_vmids is not None and b["vmid"] not in known_vmids:
                self._clear_alert(cid)
                continue
            if b["status"] == "failed":
                self._fire_alert(cid, f"Backup for {b['type'].upper()} {b['vmid']} failed verification")
            elif b["status"] == "stale":
                when = b["last_backup_time"] or "unknown"
                self._fire_alert(cid, f"No recent backup for {b['type'].upper()} {b['vmid']} — last backup {when}")
            else:
                self._clear_alert(cid)

    def _poll(self):
        url, token_id, token_secret = self._get_config()
        if not all([url, token_id, token_secret]):
            with self._lock:
                self._cache["error"] = "PBS not configured"
            return
        try:
            raw_usage = self._fetch(url, token_id, token_secret, "/api2/json/status/datastore-usage")
            datastores = [self._parse_datastore(d) for d in raw_usage]
            all_snapshots = []
            for ds in raw_usage:
                store = ds.get("store", "")
                if not store:
                    continue
                snaps = self._fetch(url, token_id, token_secret,
                                    f"/api2/json/admin/datastore/{store}/snapshots")
                all_snapshots.extend(snaps)
            backups = self._group_backups(all_snapshots)
            with self._lock:
                self._cache = {
                    "reachable":    True,
                    "last_updated": datetime.now(tz=timezone.utc).isoformat(),
                    "error":        None,
                    "datastores":   datastores,
                    "backups":      backups,
                }
            self._check_alerts(backups)
        except Exception as e:
            logging.warning(f"PBSPoller: poll failed: {e}")
            with self._lock:
                self._cache["reachable"] = False

    def start(self, stop_event):
        def _loop():
            while not stop_event.is_set():
                try:
                    self._poll()
                except Exception as e:
                    logging.warning(f"PBSPoller: unexpected error in loop: {e}")
                stop_event.wait(self.POLL_INTERVAL_SECONDS)
        threading.Thread(target=_loop, daemon=True, name="pbs-poller").start()


class HAPoller:
    POLL_INTERVAL_SECONDS = 60

    def __init__(self, auth_manager, history_db, alert_settings=None):
        self._auth_manager = auth_manager
        self._history_db = history_db
        self._cache = {
            "reachable":    False,
            "last_updated": None,
            "error":        None,
            "watts":        None,
            "voltage":      None,
            "current_a":    None,
            "energy_kwh":   None,
        }
        self._lock = threading.Lock()

    def get_cache(self):
        with self._lock:
            import copy
            return copy.deepcopy(self._cache)

    def _get_config(self):
        data = self._auth_manager.data if self._auth_manager else {}
        return {
            "url":            data.get("ha_url", ""),
            "token":          data.get("ha_token", ""),
            "entity_power":   data.get("ha_entity_power", ""),
            "entity_voltage":  data.get("ha_entity_voltage", ""),
            "entity_current":  data.get("ha_entity_current", ""),
            "entity_energy":   data.get("ha_entity_energy", ""),
        }

    @staticmethod
    def _fetch_state(base_url, token, entity_id):
        import urllib.request
        url = base_url.rstrip("/") + "/api/states/" + entity_id
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        state = data.get("state", "unavailable")
        try:
            return float(state)
        except (ValueError, TypeError):
            return None

    def _poll(self):
        cfg = self._get_config()
        if not cfg["url"] or not cfg["token"]:
            return
        metric_map = [
            ("entity_power",   "watts"),
            ("entity_voltage",  "voltage"),
            ("entity_current",  "current_a"),
            ("entity_energy",   "energy_kwh"),
        ]
        results = {}
        reachable = False
        error = None
        for cfg_key, cache_key in metric_map:
            entity_id = cfg[cfg_key]
            if not entity_id:
                results[cache_key] = None
                continue
            try:
                results[cache_key] = self._fetch_state(cfg["url"], cfg["token"], entity_id)
                reachable = True
            except Exception as e:
                results[cache_key] = None
                error = str(e)
                logging.warning(f"HAPoller: failed to fetch {entity_id}: {e}")

        ts = int(time.time())
        if reachable and self._history_db:
            try:
                self._history_db.insert_power_reading(
                    ts,
                    results.get("watts"),
                    results.get("voltage"),
                    results.get("current_a"),
                    results.get("energy_kwh"),
                )
            except Exception as e:
                logging.warning(f"HAPoller: DB write failed: {e}")

        with self._lock:
            self._cache.update(results)
            self._cache["reachable"] = reachable
            self._cache["last_updated"] = ts
            self._cache["error"] = error if not reachable else None

    def start(self, stop_event):
        def _loop():
            while not stop_event.is_set():
                try:
                    self._poll()
                except Exception as e:
                    logging.warning(f"HAPoller: unexpected error in loop: {e}")
                stop_event.wait(self.POLL_INTERVAL_SECONDS)
        threading.Thread(target=_loop, daemon=True, name="ha-poller").start()


# ============================================================================
# Dashboard HTML (loaded from dashboard.html at startup)
# ============================================================================

def _load_dashboard_html(base_dir):
    path = os.path.join(base_dir, "dashboard.html")
    with open(path, encoding="utf-8") as f:
        # {{VERSION}} markers cache-bust the /static/ asset URLs on upgrades
        return f.read().replace("{{VERSION}}", VERSION)



# ============================================================================
# HTTP server
# ============================================================================

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


_STATIC_FILES = {
    'main.css':    'text/css; charset=utf-8',
    'fonts.css':   'text/css; charset=utf-8',
    'utils.js':    'application/javascript; charset=utf-8',
    'overview.js': 'application/javascript; charset=utf-8',
    'core.js':     'application/javascript; charset=utf-8',
    'topology.js': 'application/javascript; charset=utf-8',
    'inventory.js':'application/javascript; charset=utf-8',
    'auth.js':     'application/javascript; charset=utf-8',
    'ai-panel.js':  'application/javascript; charset=utf-8',
    'nas.js':       'application/javascript; charset=utf-8',
    'proxmox.js':   'application/javascript; charset=utf-8',
    'settings.js':  'application/javascript; charset=utf-8',
    'd3.v7.min.js':    'application/javascript; charset=utf-8',
    'dmsans-300.woff2':'font/woff2',
    'dmsans-400.woff2':'font/woff2',
    'dmsans-500.woff2':'font/woff2',
    'dmsans-600.woff2':'font/woff2',
    'dmmono-400.woff2':'font/woff2',
    'dmmono-500.woff2':'font/woff2',
    'favicon.svg':     'image/svg+xml',
    'favicon-alert.svg':'image/svg+xml',
    'manifest.json':   'application/manifest+json',
    'icon-192.png':    'image/png',
    'icon-512.png':    'image/png',
    'apple-touch-icon.png':'image/png',
    'mira-avatar.png': 'image/png',
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


def make_handler(host_manager, settings, config_path, incident_log=None, auth_manager=None, inventory_db=None, dashboard_html="", history_db=None, nas_poller=None, proxmox_poller=None, ha_poller=None, pbs_poller=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args): pass

        def _client_ip(self):
            # Just use the direct connection IP - no proxy support for now
            return self.client_address[0] if self.client_address else "unknown"

        def _session_cookie_value(self):
            cookies = parse_cookies(self.headers.get("Cookie", ""))
            return cookies.get("nw_session", "")

        def _current_user(self):
            """Returns (username, is_admin) or (None, False) if not logged in."""
            if not auth_manager:
                return None, False
            return auth_manager.verify_session_cookie(self._session_cookie_value())

        def _require_auth(self, admin_only=False):
            """Returns True if request is authorised, else writes an error
            response and returns False. If no users exist yet, returns False
            with a 'setup_required' response so the frontend can prompt for
            first-run setup. POST requests additionally require a valid
            X-CSRF-Token header matching the session cookie."""
            if not auth_manager:
                return True  # auth disabled entirely
            if not auth_manager.has_users:
                self._send_json(401, {"error": "setup_required",
                                      "message": "No users configured yet. Set up the first admin user."})
                return False
            user, is_admin = self._current_user()
            if not user:
                self._send_json(401, {"error": "auth_required"})
                return False
            if admin_only and not is_admin:
                self._send_json(403, {"error": "admin_required"})
                return False
            if self.command == "POST":
                expected = auth_manager.csrf_token_for_cookie(self._session_cookie_value())
                provided = self.headers.get("X-CSRF-Token", "")
                if not provided or not hmac.compare_digest(expected, provided):
                    self._send_json(403, {"error": "csrf_required"})
                    return False
            return True

        def _set_session_cookie(self, username):
            cookie = auth_manager.make_session_cookie(username)
            max_age = auth_manager.SESSION_DAYS * 86400
            self.send_header("Set-Cookie",
                f"nw_session={cookie}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict")
            return cookie

        def _clear_session_cookie(self):
            self.send_header("Set-Cookie",
                "nw_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")


        def _read_json_body(self, max_bytes=1024 * 1024):
            """Read a JSON request body with size cap. Returns (data, error_response).

            On success: (parsed_dict, None).
            On error: (None, True) and an HTTP error response has been written.
            """
            try:
                length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                self._send_json(400, {"error": "invalid Content-Length"})
                return None, True
            if length > max_bytes:
                self._send_json(413, {"error": f"request body too large (max {max_bytes} bytes)"})
                return None, True
            try:
                body = self.rfile.read(length).decode()
                data = json.loads(body)
            except json.JSONDecodeError:
                self._send_json(400, {"error": "invalid JSON"})
                return None, True
            except (UnicodeDecodeError, ValueError):
                self._send_json(400, {"error": "invalid request body"})
                return None, True
            if not isinstance(data, dict):
                self._send_json(400, {"error": "expected a JSON object"})
                return None, True
            return data, None

        def _send_json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                body = dashboard_html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", len(body))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith('/static/'):
                fname = self.path[8:].split('?')[0]
                if fname not in _STATIC_FILES:
                    self._send_json(404, {'error': 'not found'})
                    return
                base_dir = os.path.dirname(os.path.abspath(config_path))
                fpath = os.path.join(base_dir, 'static', fname)
                try:
                    with open(fpath, 'rb') as f:
                        body = f.read()
                    self.send_response(200)
                    self.send_header('Content-Type', _STATIC_FILES[fname])
                    self.send_header('Content-Length', len(body))
                    # URLs carry ?v={VERSION}, so a day of caching is safe
                    self.send_header('Cache-Control', 'public, max-age=86400')
                    self.end_headers()
                    self.wfile.write(body)
                except FileNotFoundError:
                    self._send_json(404, {'error': f'static file not found: {fname}'})
                return
            if self.path == "/api/status":
                if not self._require_auth(): return
                self._send_json(*_h_get_status(host_manager, settings, incident_log, inventory_db))
                return
            if self.path == "/api/backup-status":
                if not self._require_auth(admin_only=True): return
                self._send_json(*_h_get_backup_status())
                return
            if self.path == "/api/inventory-backup-status":
                if not self._require_auth(admin_only=True): return
                self._send_json(*_h_get_inventory_backup_status())
                return
            if self.path == "/api/ai-config":
                if not self._require_auth(): return
                self._send_json(*_h_get_ai_config(settings, auth_manager))
                return
            if self.path == "/api/ai/usage":
                if not self._require_auth(): return
                self._send_json(*_h_get_ai_usage(auth_manager))
                return
            if self.path == "/api/settings":
                if not self._require_auth(admin_only=True): return
                self._send_json(*_h_get_settings(settings, auth_manager))
                return
            if self.path == "/api/hosts":
                if not self._require_auth(): return
                self._send_json(*_h_get_hosts(config_path))
                return
            if self.path == "/api/pi-health":
                if not self._require_auth(): return
                self._send_json(*_h_get_pi_health())
                return
            if self.path == "/api/nas" or self.path.startswith("/api/nas?"):
                if not self._require_auth(): return
                from urllib.parse import urlparse as _up_nas, parse_qs as _pqs_nas
                force = _pqs_nas(_up_nas(self.path).query).get("refresh", ["0"])[0] == "1"
                self._send_json(*_h_get_nas(nas_poller, force=force))
                return
            if self.path == "/api/proxmox" or self.path.startswith("/api/proxmox?"):
                if not self._require_auth(): return
                from urllib.parse import urlparse as _up_pve, parse_qs as _pqs_pve
                force = _pqs_pve(_up_pve(self.path).query).get("refresh", ["0"])[0] == "1"
                self._send_json(*_h_get_proxmox(proxmox_poller, force=force))
                return
            if self.path == "/api/pbs" or self.path.startswith("/api/pbs?"):
                if not self._require_auth(): return
                from urllib.parse import urlparse as _up_pbs, parse_qs as _pqs_pbs
                force = _pqs_pbs(_up_pbs(self.path).query).get("refresh", ["0"])[0] == "1"
                self._send_json(*_h_get_pbs(pbs_poller, force=force))
                return
            if self.path == "/api/power" or self.path.startswith("/api/power?"):
                if not self._require_auth(): return
                from urllib.parse import urlparse as _up_ha, parse_qs as _pqs_ha
                force = _pqs_ha(_up_ha(self.path).query).get("force", ["0"])[0] == "1"
                self._send_json(*_h_get_power(ha_poller, history_db, force=force))
                return
            if self.path == "/api/auth/status":
                self._send_json(*_h_get_auth_status(auth_manager, self._current_user, self._session_cookie_value()))
                return
            if self.path == "/api/auth/users":
                if not self._require_auth(admin_only=True): return
                self._send_json(*_h_get_auth_users(auth_manager))
                return
            if self.path == "/api/inventory-export" or self.path.startswith("/api/inventory-export?"):
                if not self._require_auth(admin_only=True): return
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"}); return
                try:
                    from urllib.parse import urlparse as _up, parse_qs as _pqs
                    _scope = _pqs(_up(self.path).query).get('scope', ['hosts'])[0]
                    if _scope not in ('hosts', 'all'):
                        _scope = 'hosts'
                    data, result = export_inventory_to_xlsx(inventory_db, scope=_scope)
                    if data is None:
                        self._send_json(500, {"error": result}); return
                    self.send_response(200)
                    self.send_header("Content-Type",
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Content-Disposition", f'attachment; filename="{result}"')
                    self.end_headers()
                    self.wfile.write(data)
                    logging.info(f"Inventory export: {result} ({len(data)} bytes)")
                except Exception as e:
                    logging.exception("inventory export error")
                    self._send_json(500, {"error": str(e)})
                return
            if self.path == "/api/inventory":
                if not self._require_auth(): return
                self._send_json(*_h_get_inventory(inventory_db, host_manager))
                return
            if self.path == "/api/topology":
                if not self._require_auth(): return
                self._send_json(*_h_get_topology(inventory_db, host_manager))
                return
            if self.path == "/api/connections":
                if not self._require_auth(): return
                self._send_json(*_h_get_connections(inventory_db))
                return
            if (self.path.startswith("/api/inventory/") and self.path.endswith("/connections")):
                if not self._require_auth(): return
                self._send_json(*_h_get_connections_for_device(self.path, inventory_db))
                return
            if self.path.startswith("/api/inventory/") and self.path != "/api/inventory/":
                if not self._require_auth(): return
                self._send_json(*_h_get_inventory_record(self.path, inventory_db, host_manager))
                return
            if self.path == "/api/discover":
                if not self._require_auth(): return
                self._send_json(*_h_get_discover(config_path))
                return
            if self.path.startswith("/api/history"):
                if not self._require_auth(): return
                self._send_json(*_h_get_history(self.path, history_db))
                return
            if self.path == "/api/brief":
                if not self._require_auth(): return
                self._send_json(*_h_get_briefs(history_db))
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            # Auth routes that set/clear cookies stay inline
            if self.path == "/api/auth/setup":
                if not auth_manager:
                    self._send_json(400, {"error": "auth disabled"}); return
                client_ip = self._client_ip()
                if client_ip not in ("127.0.0.1", "::1", "localhost"):
                    self._send_json(403, {
                        "error": "setup must be performed from localhost",
                        "message": "SSH to the Pi and run: curl -X POST http://localhost:8080/api/auth/setup -H 'Content-Type: application/json' -d '{\"username\":\"admin\",\"password\":\"...\"}'"
                    }); return
                if auth_manager.has_users:
                    self._send_json(400, {"error": "setup already complete"}); return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length).decode()
                    data = json.loads(body)
                    username = data.get("username", "")
                    password = data.get("password", "")
                    ok, err = auth_manager.create_user(username, password, admin=True)
                    if not ok:
                        self._send_json(400, {"error": err}); return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    cookie = self._set_session_cookie(username)
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "ok": True,
                        "username": username,
                        "csrf_token": auth_manager.csrf_token_for_cookie(cookie),
                    }).encode())
                except json.JSONDecodeError:
                    self._send_json(400, {"error": "invalid JSON"})
                except Exception as e:
                    logging.exception("setup error")
                    self._send_json(500, {"error": str(e)})
                return

            if self.path == "/api/auth/login":
                if not auth_manager:
                    self._send_json(400, {"error": "auth disabled"}); return
                ip = self._client_ip()
                if auth_manager.is_locked_out(ip):
                    self._send_json(429, {"error": "too many failed attempts, try again in 15 minutes"}); return
                data, err = self._read_json_body()
                if err: return
                try:
                    username = data.get("username", "")
                    password = data.get("password", "")
                    if auth_manager.verify_password(username, password):
                        auth_manager.record_successful_login(ip)
                        is_admin = auth_manager.is_admin(username.strip().lower())
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        cookie = self._set_session_cookie(username.strip().lower())
                        self.end_headers()
                        self.wfile.write(json.dumps({
                            "ok": True,
                            "username": username.strip().lower(),
                            "admin": is_admin,
                            "csrf_token": auth_manager.csrf_token_for_cookie(cookie),
                        }).encode())
                    else:
                        auth_manager.record_failed_attempt(ip)
                        self._send_json(401, {"error": "invalid username or password"})
                except Exception as e:
                    logging.exception("login error")
                    self._send_json(500, {"error": str(e)})
                return

            if self.path == "/api/auth/logout":
                if not self._require_auth(): return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self._clear_session_cookie()
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
                return

            if self.path == "/api/inventory":
                if not self._require_auth(): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_inventory_create(data, inventory_db))
                return

            if self.path.startswith("/api/inventory/") and self.path.endswith("/delete"):
                if not self._require_auth(): return
                self._send_json(*_h_post_inventory_delete(self.path, inventory_db))
                return

            if (self.path.startswith("/api/inventory/") and self.path.endswith("/connections")):
                if not self._require_auth(): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_connection_create(self.path, data, inventory_db))
                return

            if (self.path.startswith("/api/connections/") and self.path.endswith("/delete")):
                if not self._require_auth(): return
                self._send_json(*_h_post_connection_delete(self.path, inventory_db))
                return

            if self.path.startswith("/api/connections/"):
                if not self._require_auth(): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_connection_update(self.path, data, inventory_db))
                return

            if self.path.startswith("/api/inventory/"):
                if not self._require_auth(): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_inventory_update(self.path, data, inventory_db))
                return

            if self.path == "/api/inventory-import":
                if not self._require_auth(): return
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"}); return
                try:
                    ctype = self.headers.get("Content-Type", "")
                    length = int(self.headers.get("Content-Length", 0))
                    if length > 10 * 1024 * 1024:
                        self._send_json(400, {"error": "file too large (10MB max)"}); return
                    body = self.rfile.read(length)
                    if "multipart/form-data" not in ctype:
                        self._send_json(400, {"error": "expected multipart/form-data"}); return
                    boundary = None
                    for part in ctype.split(";"):
                        part = part.strip()
                        if part.startswith("boundary="):
                            boundary = part[9:].strip('"')
                    if not boundary:
                        self._send_json(400, {"error": "missing boundary"}); return
                    delimiter = ("--" + boundary).encode()
                    parts = body.split(delimiter)
                    file_bytes = None
                    mode = "add"
                    for p in parts:
                        if not p or p == b"--" or p.strip() in (b"--\r\n", b""):
                            continue
                        sep = p.find(b"\r\n\r\n")
                        if sep < 0: continue
                        headers_blob = p[:sep].decode("latin-1", errors="replace")
                        content = p[sep + 4:]
                        if content.endswith(b"\r\n"): content = content[:-2]
                        name = None
                        for hline in headers_blob.split("\r\n"):
                            if hline.lower().startswith("content-disposition"):
                                for piece in hline.split(";"):
                                    piece = piece.strip()
                                    if piece.startswith("name="):
                                        name = piece[5:].strip('"')
                        if name == "file": file_bytes = content
                        elif name == "mode":
                            try:
                                mode = content.decode().strip()
                                if mode not in ("add", "replace"): mode = "add"
                            except Exception: mode = "add"
                    if file_bytes is None:
                        self._send_json(400, {"error": "no file uploaded"}); return
                    added, skipped, errors = import_inventory_from_xlsx(inventory_db, file_bytes, mode=mode)
                    self._send_json(200, {"ok": True, "added": added, "skipped": skipped,
                                          "errors": errors[:20], "mode": mode})
                except Exception as e:
                    logging.exception("inventory import error")
                    self._send_json(500, {"error": str(e)})
                return

            if self.path == "/api/backup":
                if not self._require_auth(admin_only=True): return
                try:
                    auth_path_local = auth_manager.path if auth_manager else None
                    data, filename, manifest = create_backup_tarball(config_path, auth_path_local)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/gzip")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                    self.send_header("X-Netwatch-Backup-Version", str(manifest["manifest_version"]))
                    self.send_header("X-Netwatch-Source", manifest["source_hostname"])
                    self.end_headers()
                    self.wfile.write(data)
                    logging.info(f"Backup downloaded: {filename} ({len(data)} bytes)")
                except Exception as e:
                    logging.exception("Backup failed")
                    self._send_json(500, {"error": f"backup failed: {e}"})
                return

            if self.path == "/api/auth/users":
                if not self._require_auth(admin_only=True): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_auth_users(data, auth_manager))
                return

            if self.path == "/api/auth/password":
                if not self._require_auth(): return
                user, _ = self._current_user()
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_auth_password(data, user, auth_manager))
                return

            if self.path.startswith("/api/auth/users/"):
                if not self._require_auth(admin_only=True): return
                self._send_json(*_h_post_auth_user_delete(self.path, auth_manager))
                return

            if self.path == "/api/discover":
                if not self._require_auth(): return
                self._send_json(*_h_post_discover())
                return

            if self.path == "/api/detect-mac":
                if not self._require_auth(): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_detect_mac(data))
                return

            if self.path == "/api/wake":
                if not self._require_auth(): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_wake(data, host_manager, inventory_db))
                return

            if self.path == "/api/proxmox/action":
                if not self._require_auth(admin_only=True): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_proxmox_action(data, proxmox_poller, auth_manager))
                return

            if self.path == "/api/ai/chat":
                if not self._require_auth(): return
                data, err = self._read_json_body()
                if err: return
                _h_post_ai_chat(self, data, auth_manager)
                return

            if self.path == "/api/hosts":
                if not self._require_auth(admin_only=True): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_hosts(data, config_path, host_manager, settings))
                return

            if self.path == "/api/nas/ignore-alert":
                if not self._require_auth(admin_only=True): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_nas_ignore_alert(data, config_path, settings, auth_manager))
                return

            if self.path == "/api/nas/unignore-alert":
                if not self._require_auth(admin_only=True): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_nas_unignore_alert(data, config_path, settings, auth_manager))
                return

            if self.path == "/api/nas/acknowledge-alert":
                if not self._require_auth(admin_only=True): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_nas_acknowledge_alert(data, nas_poller))
                return

            if self.path == "/api/system/restart":
                if not self._require_auth(admin_only=True): return
                self._send_json(200, {"ok": True})
                _h_post_system_restart(history_db, auth_manager)
                return

            if self.path == "/api/settings":
                if not self._require_auth(admin_only=True): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_settings(data, config_path, settings, auth_manager))
                return

            if self.path == "/api/brief":
                if not self._require_auth(): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_brief(history_db, data))
                return

            self.send_response(404)
            self.end_headers()

    return Handler


def start_web_server(host_manager, settings, config_path, port, stop_event, incident_log=None, auth_manager=None, inventory_db=None, dashboard_html="", history_db=None, nas_poller=None, proxmox_poller=None, ha_poller=None, pbs_poller=None):
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(host_manager, settings, config_path, incident_log, auth_manager, inventory_db, dashboard_html, history_db, nas_poller=nas_poller, proxmox_poller=proxmox_poller, ha_poller=ha_poller, pbs_poller=pbs_poller))
    server.timeout = 1
    logging.info(f"Web dashboard: http://0.0.0.0:{port}")
    while not stop_event.is_set():
        server.handle_request()
    server.server_close()


# ============================================================================
# TUI
# ============================================================================

def safe_addstr(win, y, x, text, attr=0):
    try:
        max_y, max_x = win.getmaxyx()
        if y < 0 or y >= max_y or x < 0 or x >= max_x: return
        allowed = max_x - x - 1
        if allowed <= 0: return
        win.addstr(y, x, text[:allowed], attr)
    except curses.error:
        pass


def clamp(text, width):
    if len(text) >= width:
        return text[:width - 1] + "\u2026"
    return text


def draw_hline(win, y, x, width, char="\u2500", attr=0):
    safe_addstr(win, y, x, char * width, attr)


def draw_box(win, y, x, h, w, attr=0):
    if w < 2 or h < 2: return
    safe_addstr(win, y,         x,         "\u256d", attr)
    safe_addstr(win, y,         x + w - 1, "\u256e", attr)
    safe_addstr(win, y + h - 1, x,         "\u2570", attr)
    safe_addstr(win, y + h - 1, x + w - 1, "\u256f", attr)
    for i in range(1, w - 1):
        safe_addstr(win, y,         x + i, "\u2500", attr)
        safe_addstr(win, y + h - 1, x + i, "\u2500", attr)
    for i in range(1, h - 1):
        safe_addstr(win, y + i, x,         "\u2502", attr)
        safe_addstr(win, y + i, x + w - 1, "\u2502", attr)


def uptime_bar(pct, width=10):
    if pct is None:
        return "\u2500" * width
    filled = int(round(pct / 100 * width))
    return "\u2588" * filled + "\u2591" * (width - filled)


def _init_tui_colors():
    """Initialize curses color pairs for the TUI, with graceful fallback.

    Tries the full 256-color palette first; falls back to an 8-color-safe
    palette if the terminal can't do 256 colors; falls back further to an
    all-default (monochrome) attribute set if even basic color pairs fail.
    Never raises - draw_tui can always render *something* in this terminal.
    """
    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, 82,  -1)
        curses.init_pair(2, 196, -1)
        curses.init_pair(3, 214, -1)
        curses.init_pair(4, 39,  -1)
        curses.init_pair(5, 243, -1)
        curses.init_pair(6, 255, -1)
        curses.init_pair(7, 208, -1)
        curses.init_pair(8, 51,  -1)
        return {
            "C_UP":    curses.color_pair(1) | curses.A_BOLD,
            "C_DOWN":  curses.color_pair(2) | curses.A_BOLD,
            "C_WAIT":  curses.color_pair(3) | curses.A_BOLD,
            "C_HDR":   curses.color_pair(4),
            "C_HDRB":  curses.color_pair(4) | curses.A_BOLD,
            "C_MUTED": curses.color_pair(5),
            "C_TEXT":  curses.color_pair(6),
            "C_LATC":  curses.color_pair(7),
            "C_SPARK": curses.color_pair(8),
        }
    except (curses.error, ValueError):
        pass

    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN,  -1)
        curses.init_pair(2, curses.COLOR_RED,    -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_CYAN,   -1)
        curses.init_pair(5, curses.COLOR_WHITE,  -1)
        return {
            "C_UP":    curses.color_pair(1) | curses.A_BOLD,
            "C_DOWN":  curses.color_pair(2) | curses.A_BOLD,
            "C_WAIT":  curses.color_pair(3) | curses.A_BOLD,
            "C_HDR":   curses.color_pair(4),
            "C_HDRB":  curses.color_pair(4) | curses.A_BOLD,
            "C_MUTED": curses.color_pair(5),
            "C_TEXT":  0,
            "C_LATC":  curses.color_pair(3),
            "C_SPARK": curses.color_pair(4),
        }
    except (curses.error, ValueError):
        pass

    return {
        "C_UP": 0, "C_DOWN": 0, "C_WAIT": 0, "C_HDR": 0,
        "C_HDRB": curses.A_BOLD, "C_MUTED": 0, "C_TEXT": 0,
        "C_LATC": 0, "C_SPARK": 0,
    }


def _tui_status_role(pend, is_up, status):
    """Pure decision logic for which color role applies to a host's TUI row.

    Decoupled from curses attr objects so this is testable without a real
    screen. status == "DEGRADED" implies is_up is True (see Host.status_str),
    so this must be checked before the generic "up" branch - that ordering
    is the actual bug fix this function exists for: a naive is_up check
    alone misclassifies a degraded host as healthy.
    """
    if pend:
        return "wait"
    if status == "DEGRADED":
        return "degraded"
    if is_up:
        return "up"
    if status == "IDLE":
        return "idle"
    return "down"


def draw_tui(stdscr, host_manager, refresh_rate, port, stop_event):
    colors  = _init_tui_colors()
    C_UP    = colors["C_UP"]
    C_DOWN  = colors["C_DOWN"]
    C_WAIT  = colors["C_WAIT"]
    C_HDR   = colors["C_HDR"]
    C_HDRB  = colors["C_HDRB"]
    C_MUTED = colors["C_MUTED"]
    C_TEXT  = colors["C_TEXT"]
    C_LATC  = colors["C_LATC"]
    C_SPARK = colors["C_SPARK"]
    ROLE_ATTRS = {
        "wait":     (C_WAIT,  C_WAIT,  C_TEXT),
        "degraded": (C_WAIT,  C_WAIT,  C_TEXT),
        "up":       (C_UP,    C_UP,    C_TEXT),
        "idle":     (C_MUTED, C_MUTED, C_MUTED),
        "down":     (C_DOWN,  C_DOWN,  C_DOWN),
    }
    curses.curs_set(0)
    stdscr.nodelay(True)
    COL_IND, COL_NAME, COL_IP, COL_STAT, COL_LAT, COL_BAR, COL_PCT, COL_SPK, COL_CHK = 1, 4, 24, 42, 50, 62, 74, 82, 105

    while not stop_event.is_set():
        stdscr.erase()
        max_y, max_x = stdscr.getmaxyx()
        now_str = datetime.now().strftime("%a %Y-%m-%d  %H:%M:%S")
        hosts = host_manager.list_hosts()
        up_hosts = [h for h in hosts if h.is_up]
        dn_hosts = [h for h in hosts if not h.is_up and h.last_checked]
        latencies = [h.last_latency_ms for h in up_hosts if h.last_latency_ms]
        avg_lat = (sum(latencies) / len(latencies)) if latencies else None
        uptimes = [h.uptime_pct for h in hosts if h.uptime_pct is not None]
        avg_up = (sum(uptimes) / len(uptimes)) if uptimes else None

        row = 0
        safe_addstr(stdscr, row, 1, BRAND, C_HDRB)
        safe_addstr(stdscr, row, 1 + len(BRAND), f"  v{VERSION} \u00b7 homelab monitor", C_MUTED)
        url_str = f"http://0.0.0.0:{port}"
        safe_addstr(stdscr, row, max(0, max_x - len(now_str) - len(url_str) - 4), url_str, C_HDR)
        safe_addstr(stdscr, row, max(0, max_x - len(now_str) - 1), now_str, C_MUTED)
        row += 1
        draw_hline(stdscr, row, 0, max_x, "\u2500", C_MUTED)
        row += 1

        if max_x >= 60:
            num_cards = 5
            card_w = max_x // num_cards
            cards = [
                ("hosts up",    f"{len(up_hosts)} / {len(hosts)}",          C_UP),
                ("hosts down",  str(len(dn_hosts)),                          C_DOWN if dn_hosts else C_MUTED),
                ("avg latency", f"{avg_lat:.1f} ms" if avg_lat else "- ms",  C_LATC),
                ("avg uptime",  f"{avg_up:.1f}%" if avg_up else "-%",        C_UP if avg_up and avg_up >= 90 else C_WAIT),
                ("dashboard",   f":{port}",                                   C_HDR),
            ]
            for i, (label, val, color) in enumerate(cards):
                cx = i * card_w
                bw = card_w if i < num_cards - 1 else max_x - cx
                draw_box(stdscr, row, cx, 4, bw, C_MUTED)
                safe_addstr(stdscr, row + 1, cx + 2, label.upper(), C_MUTED)
                safe_addstr(stdscr, row + 2, cx + 2, val, color)
            row += 4

        draw_hline(stdscr, row, 0, max_x, "\u2500", C_MUTED)
        row += 1
        safe_addstr(stdscr, row, COL_IND,  "\u25cf",         C_MUTED)
        safe_addstr(stdscr, row, COL_NAME, "HOST",           C_HDR)
        safe_addstr(stdscr, row, COL_IP,   "IP ADDRESS",     C_HDR)
        safe_addstr(stdscr, row, COL_STAT, "STATUS",         C_HDR)
        safe_addstr(stdscr, row, COL_LAT,  "LATENCY",        C_HDR)
        safe_addstr(stdscr, row, COL_BAR,  "UPTIME",         C_HDR)
        safe_addstr(stdscr, row, COL_PCT,  "%",              C_HDR)
        if max_x > COL_SPK + 10:
            safe_addstr(stdscr, row, COL_SPK, "HISTORY",     C_HDR)
        if max_x > COL_CHK + 8:
            safe_addstr(stdscr, row, COL_CHK, "LAST PING",   C_HDR)
        row += 1
        draw_hline(stdscr, row, 0, max_x, "\u2500", C_MUTED)
        row += 1

        groups = {}
        for h in hosts:
            groups.setdefault(h.group, []).append(h)

        for group_name, group_hosts in groups.items():
            if row >= max_y - 2: break
            prefix = "\u2500\u2500 "
            suffix_len = max(0, max_x - len(prefix) - len(group_name) - 4)
            safe_addstr(stdscr, row, 0, prefix, C_MUTED)
            safe_addstr(stdscr, row, len(prefix), f" {group_name.upper()} ", C_HDRB)
            safe_addstr(stdscr, row, len(prefix) + len(group_name) + 2, " " + "\u2500" * suffix_len, C_MUTED)
            row += 1
            for host in group_hosts:
                if row >= max_y - 2: break
                with host.lock:
                    is_up   = host.is_up
                    pend    = host.last_checked is None
                    name    = clamp(host.name, 18)
                    ip      = host.ip
                    status  = host.status_str
                    latency = host.latency_str
                    upct    = host.uptime_pct
                    uptime  = host.uptime_str
                    spark   = host.spark_str(20)
                    checked = host.checked_str
                role = _tui_status_role(pend, is_up, status)
                ind_attr, st_attr, nm_attr = ROLE_ATTRS[role]
                bar_str = uptime_bar(upct, 10)
                if upct is None:
                    bar_attr = C_MUTED
                elif upct >= 95:
                    bar_attr = C_UP
                elif upct >= 80:
                    bar_attr = C_WAIT
                else:
                    bar_attr = C_DOWN
                safe_addstr(stdscr, row, COL_IND,  "\u25cf",         ind_attr)
                safe_addstr(stdscr, row, COL_NAME, f"{name:<18}",    nm_attr)
                safe_addstr(stdscr, row, COL_IP,   f"{ip:<16}",      C_MUTED)
                safe_addstr(stdscr, row, COL_STAT, f"{status:<9}",   st_attr)
                safe_addstr(stdscr, row, COL_LAT,  f" {latency:<10}", C_LATC if is_up else C_MUTED)
                safe_addstr(stdscr, row, COL_BAR,  bar_str,          bar_attr)
                safe_addstr(stdscr, row, COL_PCT,  f" {uptime:<6}",  bar_attr)
                if max_x > COL_SPK + 10:
                    for i, ch in enumerate(spark):
                        if COL_SPK + i >= max_x - 1: break
                        sp_color = C_SPARK if ch == "\u2588" else (C_DOWN if ch == "\u2581" else C_MUTED)
                        safe_addstr(stdscr, row, COL_SPK + i, ch, sp_color)
                if max_x > COL_CHK + 8:
                    safe_addstr(stdscr, row, COL_CHK, checked, C_MUTED)
                row += 1

        if max_y >= 3:
            draw_hline(stdscr, max_y - 2, 0, max_x, "\u2500", C_MUTED)
            keys = " [q] quit  [r] refresh "
            info = f" dashboard: http://0.0.0.0:{port}  \u00b7  {len(hosts)} hosts  \u00b7  {refresh_rate}s interval "
            safe_addstr(stdscr, max_y - 1, 0, keys, C_MUTED)
            safe_addstr(stdscr, max_y - 1, max(0, max_x - len(info) - 1), info, C_MUTED)

        stdscr.refresh()
        for _ in range(refresh_rate * 10):
            key = stdscr.getch()
            if key in (ord('q'), ord('Q')):
                stop_event.set()
                return
            if key in (ord('r'), ord('R')):
                break
            time.sleep(0.1)


# ============================================================================
# Entry point
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Netwatch - homelab ping monitor")
    parser.add_argument("--config", default="hosts.yaml")
    parser.add_argument("--no-tui", action="store_true", help="Headless / systemd mode")
    parser.add_argument("--no-web", action="store_true", help="Disable web dashboard")
    parser.add_argument("--port",   type=int, default=8080, help="Web server port")
    parser.add_argument("--log",    default="monitor.log")
    parser.add_argument("--restore", metavar="TARBALL",
                         help="Restore hosts.yaml/auth.json/netwatch.db from a backup tarball, then exit")
    parser.add_argument("--force", action="store_true",
                         help="With --restore, overwrite existing hosts.yaml/auth.json/netwatch.db")
    args = parser.parse_args()

    if args.restore:
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.config)
        ok, message = restore_backup(args.restore, config_path, force=args.force)
        print(message)
        sys.exit(0 if ok else 1)

    from logging.handlers import RotatingFileHandler
    _log_handler = RotatingFileHandler(args.log, maxBytes=10 * 1024 * 1024, backupCount=3)
    _log_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logging.basicConfig(level=logging.INFO, handlers=[_log_handler])

    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.config)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    dashboard_html = _load_dashboard_html(base_dir)
    config   = load_yaml(config_path)
    settings = config.get("settings", {})
    default_interval = settings.get("default_interval", 30)
    ping_timeout     = settings.get("ping_timeout", 2)
    history_window   = settings.get("history_window", 100)
    refresh_rate     = settings.get("refresh_rate", 5)
    # Make sure default_interval is always present on `settings` itself - the
    # rest of this function passes this exact dict object (not a copy) to
    # HostManager, NASPoller, ProxmoxPoller, and the web server, so that
    # POST /api/settings's in-place mutations are visible to all of them
    # immediately, without a restart.
    settings["default_interval"] = default_interval

    stop_event = threading.Event()

    # Authentication
    config_dir = os.path.dirname(os.path.abspath(config_path))
    auth_path = os.path.join(config_dir, "auth.json")
    db_path = os.path.join(config_dir, "netwatch.db")
    auth_manager = AuthManager(auth_path, db_path=db_path)
    if auth_manager.has_users:
        print(f"[netwatch] Auth enabled - {len(auth_manager.list_users())} user(s) configured")
    else:
        print(f"[netwatch] Auth NOT YET CONFIGURED - visit the dashboard to set up the first admin user")

    # SQLite-backed history (persists pings & incidents across restarts)
    retention_days = int(settings.get("history_days", 30))
    history_db = HistoryDB(db_path, retention_days=retention_days)
    print(f"[netwatch] History DB -> {db_path} (retention {retention_days} days)")
    inventory_db = InventoryDB(history_db)
    inv_count = len(inventory_db.list_all())
    print(f"[netwatch] Inventory  -> {inv_count} record(s)")

    # Daily prune task
    pt = threading.Thread(target=_prune_loop, args=(history_db, stop_event), daemon=True, name="prune")
    pt.start()

    # Ping flush task (batched inserts land every 30s)
    ft = threading.Thread(target=_flush_loop, args=(history_db, stop_event), daemon=True, name="ping-flush")
    ft.start()

    # systemd sends SIGTERM on stop; route it through the KeyboardInterrupt
    # path so buffered pings get flushed and the WAL is checkpointed.
    import signal
    def _sigterm(_sig, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _sigterm)

    incident_log = IncidentLog(history_db=history_db)
    host_manager = HostManager(
        config_path, ping_timeout, history_window, stop_event,
        incident_log, history_db,
        alert_settings=settings, alert_port=args.port,
    )
    host_manager.load_initial(config.get("hosts", []), default_interval)

    nas_poller = NASPoller(auth_manager, alert_settings=settings, alert_port=args.port)
    _nas_url, _ = nas_poller._get_config()
    if _nas_url:
        nas_poller.start(stop_event)
        print(f"[netwatch] NAS poller -> polling TrueNAS every {NASPoller.POLL_INTERVAL_SECONDS}s")

    proxmox_poller = ProxmoxPoller(auth_manager, alert_settings=settings, alert_port=args.port)
    _pve_url, _, _, _ = proxmox_poller._get_config()
    if _pve_url:
        proxmox_poller.start(stop_event)
        print(f"[netwatch] Proxmox poller -> polling every {ProxmoxPoller.POLL_INTERVAL_SECONDS}s")

    ha_poller = None
    _ha_url = (auth_manager.data if auth_manager else {}).get("ha_url", "")
    if _ha_url:
        ha_poller = HAPoller(auth_manager, history_db)
        ha_poller.start(stop_event)
        print(f"[netwatch] HA poller -> polling Home Assistant every {HAPoller.POLL_INTERVAL_SECONDS}s")

    pbs_poller = PBSPoller(auth_manager, alert_settings=settings, alert_port=args.port, proxmox_poller=proxmox_poller)
    _pbs_url, _, _ = pbs_poller._get_config()
    if _pbs_url:
        pbs_poller.start(stop_event)
        print(f"[netwatch] PBS poller -> polling every {PBSPoller.POLL_INTERVAL_SECONDS}s")

    if not args.no_web:
        wt = threading.Thread(
            target=start_web_server,
            args=(host_manager, settings, config_path, args.port, stop_event, incident_log, auth_manager, inventory_db, dashboard_html, history_db),
            kwargs={"nas_poller": nas_poller, "proxmox_poller": proxmox_poller, "ha_poller": ha_poller, "pbs_poller": pbs_poller},
            daemon=True
        )
        wt.start()
        print(f"[netwatch] Dashboard -> http://0.0.0.0:{args.port}")

    try:
        if args.no_tui:
            print(f"[netwatch] Monitoring {len(host_manager.list_hosts())} hosts")
            print("[netwatch] Headless mode. Ctrl+C to stop.")
            try:
                while True: time.sleep(1)
            except KeyboardInterrupt:
                stop_event.set()
        else:
            try:
                curses.wrapper(draw_tui, host_manager, refresh_rate, args.port, stop_event)
            except KeyboardInterrupt:
                pass
            except (curses.error, ValueError):
                print("\n[netwatch] Terminal doesn't support the TUI's required features. Try --no-tui.")
                sys.exit(1)
            finally:
                stop_event.set()
                print("\n[netwatch] Stopped.")
    finally:
        auth_manager.close()
        history_db.close()


if __name__ == "__main__":
    main()
