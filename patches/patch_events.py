#!/usr/bin/env python3
"""
netwatch patch: incident tracking + events tab.

Tracks down/up incidents per host and exposes them via the API. The events
tab on the dashboard then displays them as a chronological log.

Behavior:
  - Only tracks incidents for always_on=True hosts (intermittent ones excluded)
  - Stores up to the last 100 incidents in memory
  - Cleared on Pi reboot (no persistence by design)
  - An incident starts when a host registers its first DOWN ping
    (not threshold-based - any DOWN counts as an incident)
  - An incident ends when the host's next ping comes back UP
  - 'Ongoing' incidents (host still down) show in the live list

This patch must be applied AFTER patch_dashboard_v3.py.

Run once from ~/netwatch/:
    python3 patch_events.py
    sudo systemctl restart netwatch

Backup saved to monitor.py.bak_events.
Idempotent - safe to re-run.
"""

import os
import shutil
import sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_events"
SENTINEL = "IncidentLog"  # presence means already patched


PATCHES = [
    # ──── 1. Version bump ────
    (
        'VERSION = "3.0"',
        'VERSION = "3.1"'
    ),

    # ──── 2. Insert IncidentLog class right after the HostManager class.
    # We anchor on the start of "# YAML config" comment block.
    (
        '''# ============================================================================
# YAML config: read, validate, save (with backups)
# ============================================================================''',
        '''# ============================================================================
# Incident log
# ============================================================================

class IncidentLog:
    """Tracks down/up incidents. Thread-safe, in-memory, capped at MAX entries."""

    MAX = 100

    def __init__(self):
        self._open = {}        # ip -> open incident dict (no end_time yet)
        self._closed = []      # list of closed incidents (most recent last)
        self.lock = threading.Lock()

    def record_down(self, host):
        """Open a new incident for host if there isn't one already."""
        with self.lock:
            if host.ip in self._open:
                return  # already tracking
            self._open[host.ip] = {
                "host_name":  host.name,
                "host_ip":    host.ip,
                "host_group": host.group,
                "started":    datetime.now(),
                "ended":      None,
            }

    def record_up(self, host):
        """Close any open incident for this host."""
        with self.lock:
            inc = self._open.pop(host.ip, None)
            if inc is None:
                return
            inc["ended"] = datetime.now()
            self._closed.append(inc)
            # Cap memory: keep only the most recent MAX incidents
            if len(self._closed) > self.MAX:
                self._closed = self._closed[-self.MAX:]

    def list_incidents(self):
        """Returns most-recent-first list of dicts suitable for JSON."""
        with self.lock:
            now = datetime.now()
            ongoing = []
            for inc in self._open.values():
                duration = int((now - inc["started"]).total_seconds())
                ongoing.append({
                    "host_name":        inc["host_name"],
                    "host_ip":          inc["host_ip"],
                    "host_group":       inc["host_group"],
                    "started_str":      inc["started"].strftime("%H:%M:%S"),
                    "started_iso":      inc["started"].isoformat(),
                    "duration_seconds": duration,
                    "ongoing":          True,
                })
            resolved = []
            for inc in self._closed:
                duration = int((inc["ended"] - inc["started"]).total_seconds())
                resolved.append({
                    "host_name":        inc["host_name"],
                    "host_ip":          inc["host_ip"],
                    "host_group":       inc["host_group"],
                    "started_str":      inc["started"].strftime("%H:%M:%S"),
                    "started_iso":      inc["started"].isoformat(),
                    "ended_iso":        inc["ended"].isoformat(),
                    "duration_seconds": duration,
                    "ongoing":          False,
                })
        # Ongoing first, then resolved most-recent-first
        return ongoing + list(reversed(resolved))


# ============================================================================
# YAML config: read, validate, save (with backups)
# ============================================================================'''
    ),

    # ──── 3. poll_host: accept incident_log and record transitions
    (
        '''def poll_host(host, timeout, global_stop):
    while not global_stop.is_set() and not host.stop_event.is_set():
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
        logging.info(
            f"{'UP  ' if is_up else 'DOWN'} | {host.name:<20} | {host.ip:<16} | "
            f"{f'{latency:.1f}ms' if latency else '-':>8}"
        )
        elapsed = 0
        while elapsed < host.interval and not global_stop.is_set() and not host.stop_event.is_set():
            time.sleep(0.5)
            elapsed += 0.5''',
        '''def poll_host(host, timeout, global_stop, incident_log=None):
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

        # Track incidents (only for always_on hosts)
        if incident_log is not None and host.always_on:
            if not is_up and (was_up or was_up is None):
                # Down transition (or first ping that's down)
                if not is_up:
                    incident_log.record_down(host)
            elif is_up and was_up is False:
                # Recovery
                incident_log.record_up(host)

        logging.info(
            f"{'UP  ' if is_up else 'DOWN'} | {host.name:<20} | {host.ip:<16} | "
            f"{f'{latency:.1f}ms' if latency else '-':>8}"
        )
        elapsed = 0
        while elapsed < host.interval and not global_stop.is_set() and not host.stop_event.is_set():
            time.sleep(0.5)
            elapsed += 0.5'''
    ),

    # ──── 4. HostManager: hold reference to incident_log, pass to poll_host
    (
        '''class HostManager:
    def __init__(self, config_path, ping_timeout, history_window, global_stop):
        self.config_path = config_path
        self.ping_timeout = ping_timeout
        self.history_window = history_window
        self.global_stop = global_stop
        self.hosts = []
        self.lock = threading.Lock()''',
        '''class HostManager:
    def __init__(self, config_path, ping_timeout, history_window, global_stop, incident_log=None):
        self.config_path = config_path
        self.ping_timeout = ping_timeout
        self.history_window = history_window
        self.global_stop = global_stop
        self.incident_log = incident_log
        self.hosts = []
        self.lock = threading.Lock()'''
    ),

    # ──── 5. _spawn: pass incident_log to thread
    (
        '''        host.thread = threading.Thread(
            target=poll_host, args=(host, self.ping_timeout, self.global_stop),
            daemon=True, name=f"ping-{name}",
        )''',
        '''        host.thread = threading.Thread(
            target=poll_host,
            args=(host, self.ping_timeout, self.global_stop, self.incident_log),
            daemon=True, name=f"ping-{name}",
        )'''
    ),

    # ──── 6. main(): instantiate incident log + pass to HostManager
    (
        '''    stop_event = threading.Event()
    host_manager = HostManager(config_path, ping_timeout, history_window, stop_event)
    host_manager.load_initial(config.get("hosts", []), default_interval)''',
        '''    stop_event = threading.Event()
    incident_log = IncidentLog()
    host_manager = HostManager(config_path, ping_timeout, history_window, stop_event, incident_log)
    host_manager.load_initial(config.get("hosts", []), default_interval)'''
    ),

    # ──── 7. build_api_payload: thread incident_log through and emit events
    (
        '''def build_api_payload(host_manager, settings):
    hosts = host_manager.list_hosts()
    return {
        "generated": datetime.now().isoformat(),
        "settings":  settings,
        "summary": {
            "total":   len(hosts),
            "up":      sum(1 for h in hosts if h.is_up),
            "down":    sum(1 for h in hosts if not h.is_up and h.last_checked and h.always_on),
            "idle":    sum(1 for h in hosts if not h.is_up and h.last_checked and not h.always_on),
            "pending": sum(1 for h in hosts if not h.last_checked),
        },
        "hosts":  [h.to_dict() for h in hosts],
        "events": [],
    }''',
        '''def build_api_payload(host_manager, settings, incident_log=None):
    hosts = host_manager.list_hosts()
    events = incident_log.list_incidents() if incident_log else []
    return {
        "generated": datetime.now().isoformat(),
        "settings":  settings,
        "summary": {
            "total":   len(hosts),
            "up":      sum(1 for h in hosts if h.is_up),
            "down":    sum(1 for h in hosts if not h.is_up and h.last_checked and h.always_on),
            "idle":    sum(1 for h in hosts if not h.is_up and h.last_checked and not h.always_on),
            "pending": sum(1 for h in hosts if not h.last_checked),
        },
        "hosts":  [h.to_dict() for h in hosts],
        "events": events,
    }'''
    ),

    # ──── 8. make_handler: accept incident_log
    (
        '''def make_handler(host_manager, settings, config_path):
    class Handler(BaseHTTPRequestHandler):''',
        '''def make_handler(host_manager, settings, config_path, incident_log=None):
    class Handler(BaseHTTPRequestHandler):'''
    ),

    # ──── 9. Handler.do_GET /api/status: pass incident_log to build_api_payload
    (
        '''            if self.path == "/api/status":
                self._send_json(200, build_api_payload(host_manager, settings))
                return''',
        '''            if self.path == "/api/status":
                self._send_json(200, build_api_payload(host_manager, settings, incident_log))
                return'''
    ),

    # ──── 10. start_web_server: accept and pass incident_log
    (
        '''def start_web_server(host_manager, settings, config_path, port, stop_event):
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(host_manager, settings, config_path))''',
        '''def start_web_server(host_manager, settings, config_path, port, stop_event, incident_log=None):
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(host_manager, settings, config_path, incident_log))'''
    ),

    # ──── 11. main(): pass incident_log to web server thread
    (
        '''        wt = threading.Thread(
            target=start_web_server,
            args=(host_manager, web_settings, config_path, args.port, stop_event),
            daemon=True
        )''',
        '''        wt = threading.Thread(
            target=start_web_server,
            args=(host_manager, web_settings, config_path, args.port, stop_event, incident_log),
            daemon=True
        )'''
    ),

    # ──── 12. Bump dashboard footer version label to 3.1
    (
        '<span>netwatch v3.0 - raspberry pi</span>',
        '<span>netwatch v3.1 - raspberry pi</span>'
    ),
]


def main():
    if not os.path.isfile(TARGET):
        print(f"ERROR: {TARGET} not found in current directory.")
        sys.exit(1)

    content = open(TARGET).read()

    if SENTINEL in content:
        print(f"NOTE: '{SENTINEL}' present -- patch already applied. Exiting.")
        sys.exit(0)

    # Pre-flight: confirm dashboard v3 is present
    if "view-topology" not in content:
        print("ERROR: This patch requires dashboard v3 (patch_dashboard_v3.py) first.")
        print("Apply that patch before running this one.")
        sys.exit(1)

    shutil.copy2(TARGET, BACKUP)
    print(f"[OK] Backed up {TARGET} -> {BACKUP}")

    applied = 0
    for i, (old, new) in enumerate(PATCHES, 1):
        count = content.count(old)
        if count == 0:
            print(f"[WARN] Patch #{i}: target not found, skipping.")
            continue
        if count > 1:
            print(f"[FAIL] Patch #{i}: target matches {count}x - aborting.")
            shutil.copy2(BACKUP, TARGET)
            sys.exit(1)
        content = content.replace(old, new, 1)
        applied += 1

    open(TARGET, "w").write(content)

    import ast
    try:
        ast.parse(content)
        print("[OK] Resulting Python is valid")
    except SyntaxError as e:
        print(f"[FAIL] Resulting Python has a syntax error: {e}")
        shutil.copy2(BACKUP, TARGET)
        sys.exit(1)

    print(f"[OK] Applied {applied} patches.")
    print()
    print("Next steps:")
    print("  1. sudo systemctl restart netwatch")
    print("  2. Refresh the dashboard")
    print("  3. The Events tab will populate as hosts go down/recover")
    print("     (will be empty initially - that's expected)")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
