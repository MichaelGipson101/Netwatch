import os
import time
import shutil
import logging
import subprocess
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import yaml

from netwatch.network import (
    _detect_mac_for_ip, _normalise_mac, _save_detected_mac, _get_dashboard_url,
    _send_alert_async, _is_local_ip, _ARP_SAVED_THIS_SESSION, NTFY_DOWN_THRESHOLD,
)


# ============================================================================
# Host state
# ============================================================================

@dataclass
class HostState:
    name: str
    ip: str
    group: str
    interval: int
    always_on: bool = True
    alert: bool = True
    specs: dict = field(default_factory=dict)
    notes: str = ""
    links: dict = field(default_factory=dict)
    services: list = field(default_factory=list)
    strict: bool = False
    service_results: dict = field(default_factory=dict)
    history: deque = field(default_factory=lambda: deque(maxlen=100))
    last_latency_ms: Optional[float] = None
    last_checked: Optional[datetime] = None
    last_seen_up: Optional[datetime] = None
    consecutive_down: int = 0
    first_down_at: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)
    thread: Optional[threading.Thread] = None
    stop_event: Optional[threading.Event] = None

    @property
    def is_up(self):
        return bool(self.history) and self.history[-1]

    @property
    def uptime_pct(self):
        if not self.history:
            return None
        return (sum(self.history) / len(self.history)) * 100

    @property
    def status_str(self):
        if not self.last_checked:
            return "WAIT"
        if self.is_up:
            # Ping is fine. If strict mode is on AND any configured service is
            # currently failing, surface DEGRADED. Otherwise UP.
            if self.strict and self.services:
                for svc in self.services:
                    port = svc.get("port")
                    if port is None:
                        continue
                    res = self.service_results.get(port)
                    # Only count a service as failing if we have a result and
                    # it's a failure. While we're still waiting on the first
                    # check, we treat the service as not-yet-known.
                    if res is not None and not res.get("ok", True):
                        return "DEGRADED"
            return "UP"
        return "DOWN" if self.always_on else "IDLE"

    @property
    def latency_str(self):
        if not self.is_up or self.last_latency_ms is None:
            return "- ms"
        return f"{self.last_latency_ms:.1f} ms"

    @property
    def uptime_str(self):
        if self.uptime_pct is None:
            return "-%"
        return f"{self.uptime_pct:.1f}%"

    @property
    def checked_str(self):
        if not self.last_checked:
            return "-"
        return self.last_checked.strftime("%H:%M:%S")

    def spark_str(self, width=20):
        hist = list(self.history)[-width:]
        while len(hist) < width:
            hist.insert(0, None)
        result = []
        for v in hist:
            if v is None:
                result.append(" ")
            elif v:
                result.append("\u2588")
            else:
                result.append("\u2581")
        return "".join(result)

    def to_dict(self):
        with self.lock:
            last_seen_secs = None
            if self.last_seen_up:
                last_seen_secs = int((datetime.now() - self.last_seen_up).total_seconds())
            primary_url = (self.links or {}).get("primary", "").strip()
            if not primary_url:
                primary_url = f"http://{self.ip}"
            extra_links = []
            for entry in (self.links or {}).get("extras", []) or []:
                if isinstance(entry, dict) and entry.get("url") and entry.get("name"):
                    extra_links.append({"name": str(entry["name"]), "url": str(entry["url"])})
            services_out = []
            for svc in (self.services or []):
                port = svc.get("port")
                if port is None:
                    continue
                res = self.service_results.get(port, {})
                services_out.append({
                    "port":     port,
                    "name":     svc.get("name", f"port {port}"),
                    "ok":       res.get("ok"),
                    "error":    res.get("error"),
                    "checked":  res.get("checked"),
                })
            return {
                "name":         self.name,
                "ip":           self.ip,
                "group":        self.group,
                "interval":     self.interval,
                "always_on":    self.always_on,
                "specs":        dict(self.specs) if self.specs else {},
                "notes":        self.notes,
                "links":        {"primary": primary_url, "extras": extra_links},
                "services":     services_out,
                "strict":       self.strict,
                "is_pi":        _is_local_ip(self.ip),
                "status":       self.status_str,
                "is_up":        self.is_up,
                "latency_ms":   round(self.last_latency_ms, 2) if self.last_latency_ms else None,
                "uptime_pct":   round(self.uptime_pct, 1) if self.uptime_pct is not None else None,
                "last_checked": self.checked_str,
                "last_seen_up_seconds": last_seen_secs,
                "history":      list(self.history)[-50:],
                "consecutive_down": self.consecutive_down,
            }


# ============================================================================
# Ping logic
# ============================================================================

def check_tcp_service(ip, port, timeout=2):
    """Attempt a TCP connect to ip:port. Returns (ok, error_str_or_None).

    A successful connect is enough - we close the socket immediately. We
    do not send any data, since some services (SSH, IMAP, etc.) don't like
    being connected-to without a proper handshake.
    """
    import socket
    try:
        port = int(port)
    except (ValueError, TypeError):
        return False, "invalid port"
    if port < 1 or port > 65535:
        return False, "port out of range"
    sock = None
    try:
        sock = socket.create_connection((ip, port), timeout=timeout)
        return True, None
    except socket.timeout:
        return False, "timeout"
    except ConnectionRefusedError:
        return False, "refused"
    except OSError as e:
        return False, str(e.strerror or e)
    finally:
        if sock is not None:
            try: sock.close()
            except Exception: pass


def ping_host(ip, timeout):
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), "--", ip],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout + 1
        )
        if result.returncode == 0:
            output = result.stdout.decode()
            for part in output.split():
                if part.startswith("time="):
                    try:
                        return True, float(part.split("=")[1])
                    except ValueError:
                        pass
            return True, None
        return False, None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False, None


def _should_log_transition(was_up, is_up):
    """INFO-log only state changes (and the first ping). Steady-state pings
    go to DEBUG so monitor.log doesn't grow ~12MB/day."""
    return was_up is None or bool(is_up) != bool(was_up)


def poll_host(host, timeout, global_stop, incident_log=None, history_db=None, config_path_for_arp=None, alert_settings=None, alert_port=8080):
    while not global_stop.is_set() and not host.stop_event.is_set():
        was_up = host.is_up if host.history else None
        is_up, latency = ping_host(host.ip, timeout)
        with host.lock:
            host.history.append(is_up)
            host.last_latency_ms = latency if is_up else None
            host.last_checked = datetime.now()
            if is_up:
                host.last_seen_up = datetime.now()
                host.consecutive_down = 0
            else:
                host.consecutive_down += 1
                if host.consecutive_down == 1:
                    host.first_down_at = time.time()

        cd = host.consecutive_down  # safe read: only this thread writes it

        # Persist to DB if available
        if history_db is not None:
            try:
                history_db.record_ping(host.ip, is_up, latency)
            except Exception as e:
                logging.warning(f"HistoryDB record_ping failed: {e}")

        # ARP-based MAC auto-detection (only on successful pings, since
        # only successful pings populate the kernel's ARP cache fresh)
        if is_up and host.ip not in _ARP_SAVED_THIS_SESSION:
            detected = _detect_mac_for_ip(host.ip)
            if detected:
                with host.lock:
                    configured = (host.specs or {}).get("mac", "")
                if not configured:
                    # No MAC configured - save the detected one
                    if _save_detected_mac(config_path_for_arp, host.ip, detected):
                        with host.lock:
                            if not isinstance(host.specs, dict):
                                host.specs = {}
                            host.specs["mac"] = detected
                            host.specs["mac_auto"] = True
                        _ARP_SAVED_THIS_SESSION.add(host.ip)
                elif _normalise_mac(configured) != _normalise_mac(detected):
                    # Mismatch - log a warning but keep the user's value.
                    # We add to the session set so we don't spam this every
                    # ping cycle.
                    logging.warning(
                        f"ARP detect: configured MAC {configured} for {host.ip} "
                        f"({host.name}) differs from network-detected MAC {detected}. "
                        f"Keeping configured value. If the host's NIC was changed, "
                        f"clear the MAC in the editor and let it re-detect."
                    )
                    _ARP_SAVED_THIS_SESSION.add(host.ip)
                else:
                    # Match - no need to keep checking this session
                    _ARP_SAVED_THIS_SESSION.add(host.ip)

        # TCP service checks (only if ping succeeded - no point checking ports
        # if the host itself is offline).
        if host.services:
            now_str = datetime.now().strftime("%H:%M:%S")
            for svc in host.services:
                port = svc.get("port")
                if port is None:
                    continue
                if is_up:
                    ok, err = check_tcp_service(host.ip, port, timeout=timeout)
                else:
                    ok, err = False, "host offline"
                with host.lock:
                    host.service_results[port] = {
                        "ok": ok, "error": err, "checked": now_str,
                    }

        # Track incidents (only for always_on hosts)
        if incident_log is not None and host.always_on:
            if not is_up:
                if cd == NTFY_DOWN_THRESHOLD:
                    incident_log.record_down(host, started_at=host.first_down_at)
            elif is_up and was_up is False:
                # Capture alert status BEFORE record_up closes the incident
                was_alerted = None
                if history_db is not None:
                    was_alerted = history_db.get_open_incident_alert_status(host.ip)
                # Recovery
                incident_log.record_up(host)
                # Recovery alert: only if down alert was actually delivered
                if (alert_settings and alert_settings.get("ntfy_topic")
                        and host.alert and was_alerted is True):
                    base = _get_dashboard_url(alert_settings, alert_port)
                    click_url = f"{base}/?host={host.ip}" if base else None
                    title = f"Recovered: {host.name}"
                    msg = (f"{host.name} ({host.ip}) is back online.\n"
                           f"Group: {host.group}")
                    _send_alert_async(alert_settings, title, msg,
                                      priority="default",
                                      tags="white_check_mark,green_circle",
                                      click_url=click_url)

        # Down alert: fire when consecutive_down hits threshold
        if (incident_log is not None and host.always_on and not is_up
                and alert_settings and alert_settings.get("ntfy_topic")
                and host.alert and history_db is not None):
            if cd == NTFY_DOWN_THRESHOLD:
                already = history_db.get_open_incident_alert_status(host.ip)
                if not already:
                    base = _get_dashboard_url(alert_settings, alert_port)
                    click_url = f"{base}/?host={host.ip}" if base else None
                    title = f"Host down: {host.name}"
                    msg = (f"{host.name} ({host.ip}) failed {cd} consecutive pings.\n"
                           f"Group: {host.group}")
                    host_ip_for_cb = host.ip
                    _send_alert_async(
                        alert_settings, title, msg,
                        priority="high",
                        tags="warning,red_circle",
                        click_url=click_url,
                        on_success=lambda: history_db.mark_incident_alerted(host_ip_for_cb)
                    )

        line = (
            f"{'UP  ' if is_up else 'DOWN'} | {host.name:<20} | {host.ip:<16} | "
            f"{f'{latency:.1f}ms' if latency else '-':>8}"
        )
        if was_up is None or (is_up and not was_up) or (not is_up and cd == NTFY_DOWN_THRESHOLD):
            logging.info(line)
        else:
            logging.debug(line)
        elapsed = 0
        while elapsed < host.interval and not global_stop.is_set() and not host.stop_event.is_set():
            time.sleep(0.5)
            elapsed += 0.5


# ============================================================================
# Host manager: thread-safe add/remove, hot-reload
# ============================================================================

class HostManager:
    def __init__(self, config_path, ping_timeout, history_window, global_stop, incident_log=None, history_db=None, alert_settings=None, alert_port=8080):
        self.config_path = config_path
        self.ping_timeout = ping_timeout
        self.history_window = history_window
        self.global_stop = global_stop
        self.incident_log = incident_log
        self.history_db = history_db
        self.alert_settings = alert_settings or {}
        self.alert_port = alert_port
        self.hosts = []
        self.lock = threading.Lock()

    def _spawn(self, name, ip, group, interval, always_on=True, specs=None, notes="", links=None, services=None, strict=False, alert=True):
        history_window = self.history_window
        host = HostState(
            name=name, ip=ip, group=group, interval=interval,
            always_on=always_on,
            alert=bool(alert),
            specs=specs or {},
            notes=notes or "",
            links=links or {},
            services=services or [],
            strict=bool(strict),
            history=deque(maxlen=history_window),
            stop_event=threading.Event(),
        )
        # Restore history from DB if available
        if self.history_db:
            try:
                recent = self.history_db.recent_pings(ip, limit=history_window)
                for is_up, latency_ms in recent:
                    host.history.append(is_up)
                # Also restore last_latency_ms / last_seen_up from latest ping
                latest = self.history_db.latest_ping(ip)
                if latest is not None:
                    last_up, last_lat, last_ts = latest
                    if last_up:
                        host.last_seen_up = datetime.fromtimestamp(last_ts)
                        host.last_latency_ms = last_lat
            except Exception as e:
                logging.warning(f"HistoryDB restore failed for {ip}: {e}")
        host.thread = threading.Thread(
            target=poll_host,
            args=(host, self.ping_timeout, self.global_stop, self.incident_log, self.history_db, self.config_path, self.alert_settings, self.alert_port),
            daemon=True, name=f"ping-{name}",
        )
        host.thread.start()
        return host

    def load_initial(self, raw_hosts, default_interval):
        with self.lock:
            config_ips = set()
            for h in raw_hosts:
                config_ips.add(h["ip"])
                self.hosts.append(self._spawn(
                    h["name"], h["ip"], h.get("group", "General"),
                    h.get("interval", default_interval),
                    bool(h.get("always_on", True)),
                    h.get("specs") if isinstance(h.get("specs"), dict) else None,
                    h.get("notes", "") if isinstance(h.get("notes"), str) else "",
                    h.get("links") if isinstance(h.get("links"), dict) else None,
                    h.get("services") if isinstance(h.get("services"), list) else None,
                    bool(h.get("strict", False)),
                    bool(h.get("alert", True))
                ))
            # Reconcile orphans: any ongoing incident for an IP not in the
            # current config is stale (host was removed/renamed while netwatch
            # was offline). Close them so the Events tab is honest.
            if self.history_db and self.incident_log:
                try:
                    orphan_count = 0
                    for inc in self.history_db.list_incidents(limit=500):
                        if inc.get("ongoing") and inc.get("host_ip") not in config_ips:
                            self.incident_log._close_orphaned_incident(inc["host_ip"])
                            orphan_count += 1
                    if orphan_count:
                        logging.info(f"Reconciled {orphan_count} orphaned ongoing incident(s) at startup")
                except Exception as e:
                    logging.warning(f"Orphan reconciliation failed: {e}")

    def list_hosts(self):
        with self.lock:
            return list(self.hosts)

    def reload_from_config(self, new_hosts_config, default_interval):
        with self.lock:
            current_by_ip = {h.ip: h for h in self.hosts}
            new_ips = set()
            rebuilt = []
            for h in new_hosts_config:
                ip = h["ip"]
                new_ips.add(ip)
                interval = h.get("interval", default_interval)
                group = h.get("group", "General")
                name = h["name"]
                always_on = bool(h.get("always_on", True))
                specs = h.get("specs") if isinstance(h.get("specs"), dict) else {}
                notes = h.get("notes", "") if isinstance(h.get("notes"), str) else ""
                links = h.get("links") if isinstance(h.get("links"), dict) else {}
                services = h.get("services") if isinstance(h.get("services"), list) else []
                strict = bool(h.get("strict", False))
                alert = bool(h.get("alert", True))
                if ip in current_by_ip:
                    existing = current_by_ip[ip]
                    name_changed = existing.name != name or existing.group != group
                    monitoring_disabled = existing.always_on and not always_on
                    new_ports = {s.get("port") for s in services if s.get("port") is not None}
                    with existing.lock:
                        existing.name = name
                        existing.group = group
                        existing.interval = interval
                        existing.always_on = always_on
                        existing.specs = specs
                        existing.notes = notes
                        existing.links = links
                        # Reset service_results for ports that have been removed
                        existing.service_results = {p: v for p, v in existing.service_results.items() if p in new_ports}
                        existing.services = services
                        existing.strict = strict
                        existing.alert = alert
                    if name_changed and self.incident_log:
                        self.incident_log.update_host_info(existing)
                    if monitoring_disabled and self.incident_log:
                        self.incident_log._close_orphaned_incident(ip)
                    rebuilt.append(existing)
                else:
                    rebuilt.append(self._spawn(name, ip, group, interval, always_on, specs, notes, links, services, strict, alert))
            for ip, existing in current_by_ip.items():
                if ip not in new_ips:
                    existing.stop_event.set()
                    # Close any ongoing incident for this IP. Without this, the
                    # incident stays "ongoing" forever in the Events tab even
                    # though the host is no longer monitored.
                    if self.incident_log:
                        try:
                            self.incident_log._close_orphaned_incident(ip)
                        except Exception as e:
                            logging.warning(f"Could not close orphaned incident for {ip}: {e}")
            self.hosts = rebuilt


# ============================================================================
# Incident log
# ============================================================================

class IncidentLog:
    """Records down/up incidents to the HistoryDB. Thread-safe.

    With SQLite-backed history, this becomes a thin wrapper around HistoryDB.
    The previous in-memory implementation was capped at 100 incidents and
    reset on restart - the DB-backed version preserves history forever
    (subject to the retention_days setting)."""

    def __init__(self, history_db=None):
        self.history_db = history_db
        self.lock = threading.Lock()

    def record_down(self, host, started_at=None):
        if self.history_db:
            self.history_db.open_incident(host.ip, host.name, host.group,
                                          started_at=started_at)

    def record_up(self, host):
        if self.history_db:
            self.history_db.close_incident(host.ip)

    def _close_orphaned_incident(self, ip):
        """Close any ongoing incident for an IP, used when a host has been
        removed from the config or had its IP changed. Unlike record_up, this
        doesn't need a HostState object."""
        if self.history_db:
            self.history_db.close_incident(ip)

    def update_host_info(self, host):
        """Called when a host is renamed/regrouped so existing open incidents
        keep matching the host's current display values."""
        if self.history_db:
            self.history_db.update_incident_host_info(host.ip, host.name, host.group)

    def list_incidents(self):
        if not self.history_db:
            return []
        return self.history_db.list_incidents(limit=100)


# ============================================================================
# YAML config: read, validate, save (with backups)
# ============================================================================

def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def _validate_ip_or_hostname(s):
    """Returns True if s is a valid IPv4/IPv6 address or DNS-safe hostname.

    Used to reject argument-injection attempts via the 'ip' field in
    hosts.yaml entries. The ping subprocess takes the IP as a positional
    argument; without this, a leading '-' could be interpreted as a flag
    by some ping implementations. We pass '--' separator in ping_host
    too as belt-and-suspenders.
    """
    if not s or not isinstance(s, str):
        return False
    s = s.strip()
    if not s or len(s) > 253:
        return False
    # Reject anything starting with '-' immediately (argument injection guard)
    if s.startswith('-'):
        return False
    # Try as IP first
    try:
        import ipaddress
        ipaddress.ip_address(s)
        return True
    except (ValueError, ImportError):
        pass
    # Otherwise validate as hostname (RFC 1123-ish)
    import re as _re
    label_re = _re.compile(r'^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?$')
    labels = s.split('.')
    return all(label_re.match(label) for label in labels)


def _validate_url(s):
    """Allow only http/https URLs. Rejects javascript:, file:, etc."""
    if not isinstance(s, str):
        return False
    s = s.strip()
    return s.startswith("http://") or s.startswith("https://")


def validate_hosts_config(config):
    if not isinstance(config, dict):
        return False, "Config must be a YAML mapping."
    if "hosts" not in config or not isinstance(config["hosts"], list):
        return False, "Missing or invalid 'hosts' list."
    seen = set()
    for i, h in enumerate(config["hosts"]):
        if not isinstance(h, dict):
            return False, f"Host #{i+1} is not a mapping."
        if not str(h.get("name", "")).strip():
            return False, f"Host #{i+1} is missing a name."
        if not str(h.get("ip", "")).strip():
            return False, f"Host '{h.get('name','?')}' is missing an ip."
        ip = str(h["ip"]).strip()
        if not _validate_ip_or_hostname(ip):
            return False, f"Host '{h.get('name','?')}': '{ip}' is not a valid IP address or hostname"
        if ip in seen:
            return False, f"Duplicate IP: {ip}"
        seen.add(ip)
        if "interval" in h:
            try:
                iv = int(h["interval"])
                if iv < 5:
                    return False, f"Host '{h['name']}': interval must be >= 5 seconds."
            except (ValueError, TypeError):
                return False, f"Host '{h['name']}': interval must be an integer."
        if "always_on" in h and not isinstance(h["always_on"], bool):
            return False, f"Host '{h['name']}': always_on must be true or false."
        if "specs" in h:
            if not isinstance(h["specs"], dict):
                return False, f"Host '{h['name']}': specs must be a mapping."
            mac = h["specs"].get("mac")
            if mac:
                m = str(mac).strip()
                import re as _re
                if not _re.match(r"^([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}$", m) and not _re.match(r"^[0-9a-fA-F]{12}$", m):
                    return False, f"Host '{h['name']}': MAC address format looks invalid."
        if "notes" in h and not isinstance(h["notes"], str):
            return False, f"Host '{h['name']}': notes must be a string."
        if "strict" in h and not isinstance(h["strict"], bool):
            return False, f"Host '{h['name']}': strict must be true or false."
        if "alert" in h and not isinstance(h["alert"], bool):
            return False, f"Host '{h['name']}': alert must be true or false."
        if "services" in h:
            if not isinstance(h["services"], list):
                return False, f"Host '{h['name']}': services must be a list."
            for i, svc in enumerate(h["services"]):
                if not isinstance(svc, dict):
                    return False, f"Host '{h['name']}': service #{i+1} must be a mapping."
                port = svc.get("port")
                try:
                    p = int(port)
                except (ValueError, TypeError):
                    return False, f"Host '{h['name']}': service #{i+1} port must be an integer."
                if p < 1 or p > 65535:
                    return False, f"Host '{h['name']}': service #{i+1} port {p} is out of range (1-65535)."
                if "name" in svc and not isinstance(svc["name"], str):
                    return False, f"Host '{h['name']}': service #{i+1} name must be a string."
        if "links" in h:
            if not isinstance(h["links"], dict):
                return False, f"Host '{h['name']}': links must be a mapping."
            primary = h["links"].get("primary", "")
            if primary and not _validate_url(primary):
                return False, f"Host '{h['name']}': primary URL must start with http:// or https://"
            extras = h["links"].get("extras", []) or []
            if extras and not isinstance(extras, list):
                return False, f"Host '{h['name']}': links.extras must be a list."
            for i, x in enumerate(extras):
                if not isinstance(x, dict):
                    return False, f"Host '{h['name']}': link #{i+1} must be a mapping."
                if not x.get("name") or not isinstance(x["name"], str):
                    return False, f"Host '{h['name']}': link #{i+1} is missing a name."
                if not x.get("url") or not _validate_url(x["url"]):
                    return False, f"Host '{h['name']}': link '{x.get('name')}' has an invalid URL."
    return True, None


def save_hosts_config(path, new_hosts):
    try:
        existing = load_yaml(path) or {}
    except Exception:
        existing = {}
    settings = existing.get("settings", {})

    if os.path.exists(path):
        backup_dir = os.path.join(os.path.dirname(path), "backups")
        os.makedirs(backup_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, os.path.join(backup_dir, f"hosts-{stamp}.yaml"))
        backups = sorted(os.listdir(backup_dir))
        for old in backups[:-10]:
            try:
                os.remove(os.path.join(backup_dir, old))
            except OSError:
                pass

    new_config = {}
    if settings:
        new_config["settings"] = settings
    new_config["hosts"] = new_hosts

    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        yaml.safe_dump(new_config, f, sort_keys=False, default_flow_style=False)
    os.chmod(tmp_path, 0o600)  # hosts.yaml carries the OpenRouter key + ntfy topic
    os.replace(tmp_path, path)
