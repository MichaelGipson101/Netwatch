import json
import logging
import re
import threading
import time
from datetime import datetime, timezone

from netwatch.network import _get_dashboard_url, _send_alert_async


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
                # A node that's offline in the cluster view can't answer a
                # proxied qemu/lxc query at all - Proxmox hangs the request
                # trying to reach it instead of failing fast, which blows
                # past our 10s timeout and (if not isolated here) aborts the
                # whole poll, wiping out the other, healthy nodes' data too.
                if raw.get("status") != "online":
                    qemu, lxc = [], []
                else:
                    try:
                        qemu = self._fetch(url, user, token_id, token_secret,
                                           f"/api2/json/nodes/{name}/qemu")
                        lxc  = self._fetch(url, user, token_id, token_secret,
                                           f"/api2/json/nodes/{name}/lxc")
                    except Exception as e:
                        logging.warning(f"ProxmoxPoller: node '{name}' guest fetch failed: {e}")
                        qemu, lxc = [], []
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
            "cpu_percent": None,
            "mem_used_bytes": None,
            "mem_total_bytes": None,
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
    def _parse_node_status(raw):
        # PBS's own status endpoint reports `cpu` as a 0-1 fraction (unlike
        # Proxmox VE's node status, also a fraction, but kept separate here
        # since PBS is a standalone host with no cluster context).
        mem = raw.get("memory") or {}
        return {
            "cpu_percent":     round((raw.get("cpu") or 0.0) * 100, 1),
            "mem_used_bytes":  mem.get("used") or 0,
            "mem_total_bytes": mem.get("total") or 0,
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
            raw_node_status = self._fetch(url, token_id, token_secret, "/api2/json/nodes/localhost/status")
            node_status = self._parse_node_status(raw_node_status)
            with self._lock:
                self._cache = {
                    "reachable":    True,
                    "last_updated": datetime.now(tz=timezone.utc).isoformat(),
                    "error":        None,
                    "datastores":   datastores,
                    "backups":      backups,
                    **node_status,
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


class UPSPoller:
    POLL_INTERVAL_SECONDS = 15  # tight cadence - battery state can change fast during an outage

    def __init__(self, auth_manager, alert_settings=None, alert_port=None):
        self._auth_manager = auth_manager
        self._alert_settings = alert_settings or {}
        self._alert_port = alert_port
        self._cache = {
            "reachable":       False,
            "last_updated":    None,
            "error":           None,
            "status":          None,
            "charge_percent":  None,
            "load_percent":    None,
            "runtime_seconds": None,
            "input_voltage":   None,
            "battery_voltage": None,
        }
        self._lock = threading.Lock()
        self._alert_state = {}  # condition_id -> bool (True = currently alerting)

    def get_cache(self):
        with self._lock:
            import copy
            return copy.deepcopy(self._cache)

    def _get_config(self):
        data = self._auth_manager.data if self._auth_manager else {}
        return (
            data.get("nut_server", ""),
            data.get("nut_port") or 3493,
            data.get("nut_ups_name", ""),
            data.get("nut_username", ""),
            data.get("nut_password", ""),
        )

    @staticmethod
    def _unescape_nut_value(escaped_str):
        r"""Unescape NUT protocol escape sequences: \\ -> \, \" -> "."""
        result = []
        i = 0
        while i < len(escaped_str):
            if escaped_str[i] == '\\' and i + 1 < len(escaped_str):
                next_char = escaped_str[i + 1]
                if next_char == '\\':
                    result.append('\\')
                    i += 2
                elif next_char == '"':
                    result.append('"')
                    i += 2
                else:
                    # Unknown escape, keep as-is
                    result.append(escaped_str[i])
                    i += 1
            else:
                result.append(escaped_str[i])
                i += 1
        return ''.join(result)

    @staticmethod
    def _parse_list_response(lines, ups_name):
        r"""Turn raw `LIST VAR <ups_name>` protocol lines into a flat
        {variable_name: raw_string_value} dict. Each data line has the shape
        `VAR <ups_name> <key> "<value>"` - BEGIN/END framing lines are skipped.
        Values are unescaped according to NUT protocol rules (\\ -> \, \" -> ")."""
        prefix = f'VAR {ups_name} '
        result = {}
        for line in lines:
            if not line.startswith(prefix):
                continue
            rest = line[len(prefix):]
            key, _, quoted = rest.partition(' ')
            if quoted.startswith('"') and quoted.endswith('"') and len(quoted) >= 2:
                escaped_value = quoted[1:-1]
                result[key] = UPSPoller._unescape_nut_value(escaped_value)
        return result

    @staticmethod
    def _parse_status_flags(status):
        """Split a raw `ups.status` string (e.g. "OL CHRG") into a set of
        flags (e.g. {"OL", "CHRG"}). Multiple flags can be active at once."""
        return set((status or "").split())

    @staticmethod
    def _to_float(raw, key):
        v = raw.get(key)
        if v is None:
            return None
        try:
            return float(v)
        except ValueError:
            return None

    def _fetch_vars(self):
        import socket
        server, port, ups_name, username, password = self._get_config()

        def _recv_line(sock):
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = sock.recv(1)
                if not chunk:
                    raise OSError("connection closed while reading a line")
                buf += chunk
            return buf.decode()

        sock = socket.create_connection((server, port), timeout=5)
        try:
            sock.sendall(f"USERNAME {username}\n".encode())
            if not _recv_line(sock).startswith("OK"):
                raise OSError("NUT USERNAME command rejected")
            sock.sendall(f"PASSWORD {password}\n".encode())
            if not _recv_line(sock).startswith("OK"):
                raise OSError("NUT PASSWORD command rejected")
            sock.sendall(f"LIST VAR {ups_name}\n".encode())
            lines = []
            end_marker = f"END LIST VAR {ups_name}"
            while True:
                line = _recv_line(sock).rstrip("\n")
                lines.append(line)
                if line.startswith(end_marker):
                    break
            sock.sendall(b"LOGOUT\n")
            return self._parse_list_response(lines, ups_name)
        finally:
            sock.close()

    def _poll(self):
        server, port, ups_name, username, password = self._get_config()
        if not all([server, ups_name, username, password]):
            with self._lock:
                self._cache["error"] = "NUT (UPS) not configured"
            return
        try:
            raw = self._fetch_vars()
            status = raw.get("ups.status", "")
            runtime = self._to_float(raw, "battery.runtime")
            with self._lock:
                self._cache = {
                    "reachable":       True,
                    "last_updated":    datetime.now(tz=timezone.utc).isoformat(),
                    "error":           None,
                    "status":          status,
                    "charge_percent":  self._to_float(raw, "battery.charge"),
                    "load_percent":    self._to_float(raw, "ups.load"),
                    "runtime_seconds": None if runtime is None else int(runtime),
                    "input_voltage":   self._to_float(raw, "input.voltage"),
                    "battery_voltage": self._to_float(raw, "battery.voltage"),
                }
            self._check_alerts(status)
        except Exception as e:
            logging.warning(f"UPSPoller: poll failed: {e}")
            with self._lock:
                self._cache["reachable"] = False

    def _check_alerts(self, status):
        # Implemented in Task 3 - no-op placeholder removed once that task lands.
        pass

    def start(self, stop_event):
        def _loop():
            while not stop_event.is_set():
                try:
                    self._poll()
                except Exception as e:
                    logging.warning(f"UPSPoller: unexpected error in loop: {e}")
                stop_event.wait(self.POLL_INTERVAL_SECONDS)
        threading.Thread(target=_loop, daemon=True, name="ups-poller").start()
