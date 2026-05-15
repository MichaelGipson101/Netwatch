#!/usr/bin/env python3
"""
netwatch patch: ntfy push notification alerts.

Adds push notifications via ntfy when monitored hosts go down or recover.
- Down alert fires after 3 consecutive failed pings
- Recovery alert fires when an alerted host comes back UP (only if the
  down alert was actually delivered)
- Per-host opt-in defaults to TRUE; alert: false silences a host
- DEGRADED state does not trigger alerts (only ping-based DOWN)

Configuration in hosts.yaml:
  settings:
    ntfy_topic: <your-topic-name>     # required to enable
    ntfy_server: https://ntfy.sh      # default; override for self-hosted
    dashboard_url: http://192.168.x.x:8080  # optional; auto-detected

Schema change: adds 'alert_sent' column to incidents table (idempotent).

Must be applied AFTER patch_orphan_incidents.py.
"""

import os, shutil, sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_ntfy"
SENTINEL = "send_ntfy_alert"


NEW_NTFY_MODULE = '''# ============================================================================
# Alerts (ntfy)
# ============================================================================

NTFY_DEFAULT_SERVER = "https://ntfy.sh"
NTFY_DOWN_THRESHOLD = 3

_DASHBOARD_URL_CACHE = None


def _get_dashboard_url(settings, port):
    """Return clickable dashboard URL for alert click actions."""
    global _DASHBOARD_URL_CACHE
    explicit = (settings or {}).get("dashboard_url")
    if explicit:
        return str(explicit).rstrip("/")
    if _DASHBOARD_URL_CACHE is not None:
        return _DASHBOARD_URL_CACHE if _DASHBOARD_URL_CACHE else None
    import subprocess
    try:
        result = subprocess.run(
            ["ip", "-o", "route", "get", "1.1.1.1"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            parts = result.stdout.split()
            if "src" in parts:
                src_ip = parts[parts.index("src") + 1]
                _DASHBOARD_URL_CACHE = f"http://{src_ip}:{port}"
                return _DASHBOARD_URL_CACHE
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        pass
    _DASHBOARD_URL_CACHE = ""
    return None


def send_ntfy_alert(settings, title, message, priority="default",
                    tags=None, click_url=None):
    """Send a ntfy notification. Returns True on success."""
    topic = (settings or {}).get("ntfy_topic")
    if not topic:
        return False
    server = (settings or {}).get("ntfy_server") or NTFY_DEFAULT_SERVER
    server = str(server).rstrip("/")
    url = f"{server}/{topic}"

    import urllib.request
    import urllib.error
    headers = {"Title": title, "Priority": priority}
    if tags: headers["Tags"] = tags
    if click_url: headers["Click"] = click_url

    data = (message or "").encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            ok = 200 <= resp.status < 300
            if ok:
                logging.info(f"ntfy: sent alert '{title}' to {topic}")
            else:
                logging.warning(f"ntfy: server returned {resp.status} for '{title}'")
            return ok
    except urllib.error.URLError as e:
        logging.warning(f"ntfy: could not deliver '{title}': {e}")
        return False
    except Exception as e:
        logging.warning(f"ntfy: unexpected error sending '{title}': {e}")
        return False


def _send_alert_async(settings, title, message, priority, tags, click_url,
                      on_success=None):
    """Fire-and-forget wrapper. on_success runs only if delivery succeeds."""
    def _worker():
        ok = send_ntfy_alert(settings, title, message, priority=priority,
                             tags=tags, click_url=click_url)
        if ok and on_success:
            try: on_success()
            except Exception as e:
                logging.warning(f"ntfy: on_success callback failed: {e}")
    threading.Thread(target=_worker, daemon=True, name="ntfy-alert").start()


# ============================================================================
# Wake-on-LAN
# ============================================================================'''


PATCHES = [
    # 1. Schema migration
    (
        '''        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(self.SCHEMA)
        logging.info(f"HistoryDB: opened {db_path} (retention {retention_days} days)")''',
        '''        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(self.SCHEMA)
        try:
            self.conn.execute("ALTER TABLE incidents ADD COLUMN alert_sent INTEGER DEFAULT 0")
            logging.info("HistoryDB: added alert_sent column to incidents")
        except sqlite3.OperationalError:
            pass
        logging.info(f"HistoryDB: opened {db_path} (retention {retention_days} days)")'''
    ),

    # 2. HistoryDB methods
    (
        '''    def update_incident_host_info(self, host_ip, host_name, host_group):
        """Keep host_name/host_group up to date on existing open incidents
        when a host gets renamed or moved between groups."""
        with self.lock:
            self.conn.execute(
                "UPDATE incidents SET host_name = ?, host_group = ? "
                "WHERE host_ip = ? AND ended IS NULL",
                (host_name, host_group, host_ip),
            )''',
        '''    def update_incident_host_info(self, host_ip, host_name, host_group):
        """Keep host_name/host_group up to date on existing open incidents
        when a host gets renamed or moved between groups."""
        with self.lock:
            self.conn.execute(
                "UPDATE incidents SET host_name = ?, host_group = ? "
                "WHERE host_ip = ? AND ended IS NULL",
                (host_name, host_group, host_ip),
            )

    def mark_incident_alerted(self, host_ip):
        """Set alert_sent=1 for the open incident for host_ip."""
        with self.lock:
            self.conn.execute(
                "UPDATE incidents SET alert_sent = 1 "
                "WHERE host_ip = ? AND ended IS NULL",
                (host_ip,),
            )

    def get_open_incident_alert_status(self, host_ip):
        """Return alert_sent (bool) for the open incident, or None."""
        with self.lock:
            cur = self.conn.execute(
                "SELECT alert_sent FROM incidents "
                "WHERE host_ip = ? AND ended IS NULL LIMIT 1",
                (host_ip,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return bool(row[0])

    def get_last_closed_incident_alert_status(self, host_ip):
        """Return alert_sent (bool) for the most recently closed incident."""
        with self.lock:
            cur = self.conn.execute(
                "SELECT alert_sent FROM incidents "
                "WHERE host_ip = ? AND ended IS NOT NULL "
                "ORDER BY ended DESC LIMIT 1",
                (host_ip,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return bool(row[0])'''
    ),

    # 3. HostState alert field
    (
        '''@dataclass
class HostState:
    name: str
    ip: str
    group: str
    interval: int
    always_on: bool = True
    specs: dict = field(default_factory=dict)''',
        '''@dataclass
class HostState:
    name: str
    ip: str
    group: str
    interval: int
    always_on: bool = True
    alert: bool = True
    specs: dict = field(default_factory=dict)'''
    ),

    # 4. _spawn signature
    (
        '''    def _spawn(self, name, ip, group, interval, always_on=True, specs=None, notes="", links=None, services=None, strict=False):
        history_window = self.history_window
        host = HostState(
            name=name, ip=ip, group=group, interval=interval,
            always_on=always_on,
            specs=specs or {},''',
        '''    def _spawn(self, name, ip, group, interval, always_on=True, specs=None, notes="", links=None, services=None, strict=False, alert=True):
        history_window = self.history_window
        host = HostState(
            name=name, ip=ip, group=group, interval=interval,
            always_on=always_on,
            alert=bool(alert),
            specs=specs or {},'''
    ),

    # 5. load_initial
    (
        '''            for h in raw_hosts:
                config_ips.add(h["ip"])
                self.hosts.append(self._spawn(
                    h["name"], h["ip"], h.get("group", "General"),
                    h.get("interval", default_interval),
                    bool(h.get("always_on", True)),
                    h.get("specs") if isinstance(h.get("specs"), dict) else None,
                    h.get("notes", "") if isinstance(h.get("notes"), str) else "",
                    h.get("links") if isinstance(h.get("links"), dict) else None,
                    h.get("services") if isinstance(h.get("services"), list) else None,
                    bool(h.get("strict", False))
                ))''',
        '''            for h in raw_hosts:
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
                ))'''
    ),

    # 6. reload_from_config
    (
        '''                services = h.get("services") if isinstance(h.get("services"), list) else []
                strict = bool(h.get("strict", False))
                if ip in current_by_ip:
                    existing = current_by_ip[ip]
                    name_changed = existing.name != name or existing.group != group
                    existing.name = name
                    existing.group = group
                    existing.interval = interval
                    existing.always_on = always_on
                    existing.specs = specs
                    existing.notes = notes
                    existing.links = links
                    # Reset service_results for ports that have been removed
                    new_ports = {s.get("port") for s in services if s.get("port") is not None}
                    existing.service_results = {p: v for p, v in existing.service_results.items() if p in new_ports}
                    existing.services = services
                    existing.strict = strict
                    if name_changed and self.incident_log:
                        self.incident_log.update_host_info(existing)
                    rebuilt.append(existing)
                else:
                    rebuilt.append(self._spawn(name, ip, group, interval, always_on, specs, notes, links, services, strict))''',
        '''                services = h.get("services") if isinstance(h.get("services"), list) else []
                strict = bool(h.get("strict", False))
                alert = bool(h.get("alert", True))
                if ip in current_by_ip:
                    existing = current_by_ip[ip]
                    name_changed = existing.name != name or existing.group != group
                    existing.name = name
                    existing.group = group
                    existing.interval = interval
                    existing.always_on = always_on
                    existing.specs = specs
                    existing.notes = notes
                    existing.links = links
                    # Reset service_results for ports that have been removed
                    new_ports = {s.get("port") for s in services if s.get("port") is not None}
                    existing.service_results = {p: v for p, v in existing.service_results.items() if p in new_ports}
                    existing.services = services
                    existing.strict = strict
                    existing.alert = alert
                    if name_changed and self.incident_log:
                        self.incident_log.update_host_info(existing)
                    rebuilt.append(existing)
                else:
                    rebuilt.append(self._spawn(name, ip, group, interval, always_on, specs, notes, links, services, strict, alert))'''
    ),

    # 7. Validator
    (
        '''        if "strict" in h and not isinstance(h["strict"], bool):
            return False, f"Host '{h['name']}': strict must be true or false."''',
        '''        if "strict" in h and not isinstance(h["strict"], bool):
            return False, f"Host '{h['name']}': strict must be true or false."
        if "alert" in h and not isinstance(h["alert"], bool):
            return False, f"Host '{h['name']}': alert must be true or false."'''
    ),

    # 8. poll_host signature
    (
        '''def poll_host(host, timeout, global_stop, incident_log=None, history_db=None, config_path_for_arp=None):''',
        '''def poll_host(host, timeout, global_stop, incident_log=None, history_db=None, config_path_for_arp=None, alert_settings=None, alert_port=8080):'''
    ),

    # 9. Alert-firing logic in the poll loop
    (
        '''        # Track incidents (only for always_on hosts)
        if incident_log is not None and host.always_on:
            if not is_up and (was_up or was_up is None):
                # Down transition (or first ping that's down)
                if not is_up:
                    incident_log.record_down(host)
            elif is_up and was_up is False:
                # Recovery
                incident_log.record_up(host)''',
        '''        # Track incidents (only for always_on hosts)
        if incident_log is not None and host.always_on:
            if not is_up and (was_up or was_up is None):
                # Down transition (or first ping that's down)
                if not is_up:
                    incident_log.record_down(host)
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
                    msg = (f"{host.name} ({host.ip}) is back online.\\n"
                           f"Group: {host.group}")
                    _send_alert_async(alert_settings, title, msg,
                                      priority="default",
                                      tags="white_check_mark,green_circle",
                                      click_url=click_url)

        # Down alert: fire when consecutive_down hits threshold
        if (incident_log is not None and host.always_on and not is_up
                and alert_settings and alert_settings.get("ntfy_topic")
                and host.alert and history_db is not None):
            with host.lock:
                cd = host.consecutive_down
            if cd == NTFY_DOWN_THRESHOLD:
                already = history_db.get_open_incident_alert_status(host.ip)
                if not already:
                    base = _get_dashboard_url(alert_settings, alert_port)
                    click_url = f"{base}/?host={host.ip}" if base else None
                    title = f"Host down: {host.name}"
                    msg = (f"{host.name} ({host.ip}) failed {cd} consecutive pings.\\n"
                           f"Group: {host.group}")
                    host_ip_for_cb = host.ip
                    _send_alert_async(
                        alert_settings, title, msg,
                        priority="high",
                        tags="warning,red_circle",
                        click_url=click_url,
                        on_success=lambda: history_db.mark_incident_alerted(host_ip_for_cb)
                    )'''
    ),

    # 10. HostManager init
    (
        '''class HostManager:
    def __init__(self, config_path, ping_timeout, history_window, global_stop, incident_log=None, history_db=None):
        self.config_path = config_path
        self.ping_timeout = ping_timeout
        self.history_window = history_window
        self.global_stop = global_stop
        self.incident_log = incident_log
        self.history_db = history_db''',
        '''class HostManager:
    def __init__(self, config_path, ping_timeout, history_window, global_stop, incident_log=None, history_db=None, alert_settings=None, alert_port=8080):
        self.config_path = config_path
        self.ping_timeout = ping_timeout
        self.history_window = history_window
        self.global_stop = global_stop
        self.incident_log = incident_log
        self.history_db = history_db
        self.alert_settings = alert_settings or {}
        self.alert_port = alert_port'''
    ),

    # 11. Thread args
    (
        '''        host.thread = threading.Thread(
            target=poll_host,
            args=(host, self.ping_timeout, self.global_stop, self.incident_log, self.history_db, self.config_path),
            daemon=True, name=f"ping-{name}",
        )''',
        '''        host.thread = threading.Thread(
            target=poll_host,
            args=(host, self.ping_timeout, self.global_stop, self.incident_log, self.history_db, self.config_path, self.alert_settings, self.alert_port),
            daemon=True, name=f"ping-{name}",
        )'''
    ),

    # 12. main(): pass alert settings to HostManager
    (
        '''    incident_log = IncidentLog(history_db=history_db)
    host_manager = HostManager(config_path, ping_timeout, history_window, stop_event, incident_log, history_db)''',
        '''    incident_log = IncidentLog(history_db=history_db)
    host_manager = HostManager(
        config_path, ping_timeout, history_window, stop_event,
        incident_log, history_db,
        alert_settings=settings, alert_port=args.port,
    )'''
    ),

    # 13. Frontend: ALERT checkbox in editor row
    (
        '''    + \'<div class="ao-cell"><input type="checkbox" class="f-alwayson" title="Always on? Uncheck for laptops/phones/etc." \' + (alwaysOn ? \'checked\' : \'\') + \'></div>\'''',
        '''    + \'<div class="ao-cell"><input type="checkbox" class="f-alwayson" title="Always on? Uncheck for laptops/phones/etc." \' + (alwaysOn ? \'checked\' : \'\') + \'></div>\'
    + \'<div class="ao-cell"><input type="checkbox" class="f-alert" title="Alert on down? Uncheck to silence ntfy notifications for this host." \' + (alertOn ? \'checked\' : \'\') + \'></div>\'''',
    ),

    # 14. alertOn variable
    (
        '''  const alwaysOn = !h || h.always_on !== false;''',
        '''  const alwaysOn = !h || h.always_on !== false;
  const alertOn  = !h || h.alert !== false;'''
    ),

    # 15. Header row
    (
        '''        <div>Name</div><div>IP address</div><div>Group</div><div>Interval</div><div>Always on</div><div></div><div></div>''',
        '''        <div>Name</div><div>IP address</div><div>Group</div><div>Interval</div><div>Always on</div><div>Alert</div><div></div><div></div>'''
    ),

    # 16. Grid template columns
    (
        '''.edit-row.hdr{font-size:10px;font-family:'DM Mono',monospace;letter-spacing:.07em;text-transform:uppercase;color:var(--hint);padding:6px 0;border-bottom:1px solid var(--border);display:grid;grid-template-columns:1fr 1fr 1fr 70px 70px 60px 32px;gap:8px;align-items:center}
.edit-row .row-main{display:grid;grid-template-columns:1fr 1fr 1fr 70px 70px 60px 32px;gap:8px;align-items:center}''',
        '''.edit-row.hdr{font-size:10px;font-family:'DM Mono',monospace;letter-spacing:.07em;text-transform:uppercase;color:var(--hint);padding:6px 0;border-bottom:1px solid var(--border);display:grid;grid-template-columns:1fr 1fr 1fr 70px 70px 60px 60px 32px;gap:8px;align-items:center}
.edit-row .row-main{display:grid;grid-template-columns:1fr 1fr 1fr 70px 70px 60px 60px 32px;gap:8px;align-items:center}'''
    ),

    # 17. saveHosts: include alert
    (
        '''    const alwaysOnEl = row.querySelector('.f-alwayson');
    entry.always_on = alwaysOnEl ? alwaysOnEl.checked : true;''',
        '''    const alwaysOnEl = row.querySelector('.f-alwayson');
    entry.always_on = alwaysOnEl ? alwaysOnEl.checked : true;
    const alertEl = row.querySelector('.f-alert');
    if(alertEl && !alertEl.checked) entry.alert = false;'''
    ),

    # 18. Version
    (
        'netwatch v3.13 - raspberry pi',
        'netwatch v3.14 - raspberry pi'
    ),
]


def main():
    if not os.path.isfile(TARGET):
        print(f"ERROR: {TARGET} not found.")
        sys.exit(1)

    content = open(TARGET).read()

    if SENTINEL in content:
        print(f"NOTE: '{SENTINEL}' found - patch already applied.")
        sys.exit(0)

    if "_close_orphaned_incident" not in content:
        print("ERROR: This patch requires patch_orphan_incidents first.")
        sys.exit(1)

    shutil.copy2(TARGET, BACKUP)
    print(f"[OK] Backed up {TARGET} -> {BACKUP}")

    OLD = '''# ============================================================================
# Wake-on-LAN
# ============================================================================'''
    if content.count(OLD) != 1:
        print(f"[FAIL] WoL anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW_NTFY_MODULE, 1)
    print("[OK] Inserted ntfy module")

    applied = 1
    for i, (old, new) in enumerate(PATCHES, 1):
        count = content.count(old)
        if count == 0:
            print(f"[FAIL] Patch #{i}: target not found")
            shutil.copy2(BACKUP, TARGET); sys.exit(1)
        if count > 1:
            print(f"[FAIL] Patch #{i}: matches {count}x")
            shutil.copy2(BACKUP, TARGET); sys.exit(1)
        content = content.replace(old, new, 1)
        applied += 1

    if 'VERSION = "3.13"' in content:
        content = content.replace('VERSION = "3.13"', 'VERSION = "3.14"', 1)

    open(TARGET, "w").write(content)

    import ast
    try:
        ast.parse(open(TARGET).read())
        print("[OK] Resulting Python is valid")
    except SyntaxError as e:
        print(f"[FAIL] Syntax error: {e}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)

    print(f"[OK] Applied {applied} patches")
    print()
    print("Next steps:")
    print("  1. Edit hosts.yaml, add to settings:")
    print("       ntfy_topic: <your-topic>      # e.g. 'Netwatch'")
    print("  2. (Optional) for self-hosted ntfy:")
    print("       ntfy_server: https://your-server")
    print("  3. sudo systemctl restart netwatch")
    print("  4. Subscribe to your topic in the ntfy phone app")
    print("  5. Test: take a host offline. After 3 failed pings (~15s),")
    print("     a notification fires. Restore - recovery notification fires.")
    print()
    print("Per-host opt-out: uncheck 'Alert' column in editor, or set")
    print("alert: false in the host's hosts.yaml entry.")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
