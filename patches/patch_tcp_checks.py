#!/usr/bin/env python3
"""
netwatch patch: TCP service checks.

Adds optional TCP port checks per host. For each configured service:
  - A TCP connect is attempted on each poll cycle
  - Connect succeeds = service is up. Connect fails / times out = service is down.
  - Results are reflected in the dashboard alongside ping status.

Per-host configuration (in hosts.yaml or via the editor):
    services:
      - port: 8123
        name: Web UI
      - port: 22
        name: SSH
    strict: true     # when true, a service failure makes host status DEGRADED
                     # even if ping succeeds. when false (default), service
                     # failures are shown but don't change the host's main status.

New status state: DEGRADED (amber)
  - Shown when a strict host has ping=up but at least one service down
  - Distinct from UP / DOWN / IDLE / WAIT
  - In the topology view: amber pill, amber dot
  - In the hosts table: amber row tint, amber badge
  - In the drawer: amber header dot + new "Services" section listing each
    port and its current state

Backward compatibility:
  - Hosts without a `services` entry behave identically to before
  - All existing dashboard logic works unchanged

Must be applied AFTER patch_sqlite_history.py.

Run once from ~/netwatch/:
    python3 patch_tcp_checks.py
    sudo systemctl restart netwatch

Backup saved to monitor.py.bak_tcp.
Idempotent - safe to re-run.
"""

import os
import shutil
import sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_tcp"
SENTINEL = "check_tcp_service"  # presence means already patched


PATCHES = [
    # ──── 1. Add `services` and `strict` fields to HostState dataclass.
    # `service_results` is the in-memory live state. `services` is the config list.
    (
        '''@dataclass
class HostState:
    name: str
    ip: str
    group: str
    interval: int
    always_on: bool = True
    specs: dict = field(default_factory=dict)
    notes: str = ""
    links: dict = field(default_factory=dict)
    history: deque = field(default_factory=lambda: deque(maxlen=100))''',
        '''@dataclass
class HostState:
    name: str
    ip: str
    group: str
    interval: int
    always_on: bool = True
    specs: dict = field(default_factory=dict)
    notes: str = ""
    links: dict = field(default_factory=dict)
    services: list = field(default_factory=list)
    strict: bool = False
    service_results: dict = field(default_factory=dict)
    history: deque = field(default_factory=lambda: deque(maxlen=100))'''
    ),

    # ──── 2. Update status_str to handle DEGRADED.
    # A strict host with at least one failing service goes DEGRADED if ping is up.
    (
        '''    @property
    def status_str(self):
        if not self.last_checked:
            return "WAIT"
        if self.is_up:
            return "UP"
        return "DOWN" if self.always_on else "IDLE"''',
        '''    @property
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
        return "DOWN" if self.always_on else "IDLE"'''
    ),

    # ──── 3. Include services + strict + service_results in to_dict.
    (
        '''            return {
                "name":         self.name,
                "ip":           self.ip,
                "group":        self.group,
                "interval":     self.interval,
                "always_on":    self.always_on,
                "specs":        dict(self.specs) if self.specs else {},
                "notes":        self.notes,
                "links":        {"primary": primary_url, "extras": extra_links},
                "is_pi":        _is_local_ip(self.ip),
                "status":       self.status_str,''',
        '''            services_out = []
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
                "status":       self.status_str,'''
    ),

    # ──── 4. Add a check_tcp_service helper near ping_host.
    (
        '''def ping_host(ip, timeout):''',
        '''def check_tcp_service(ip, port, timeout=2):
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


def ping_host(ip, timeout):'''
    ),

    # ──── 5. poll_host: after the ping, run service checks and store results.
    (
        '''        # Persist to DB if available
        if history_db is not None:
            try:
                history_db.record_ping(host.ip, is_up, latency)
            except Exception as e:
                logging.warning(f"HistoryDB record_ping failed: {e}")''',
        '''        # Persist to DB if available
        if history_db is not None:
            try:
                history_db.record_ping(host.ip, is_up, latency)
            except Exception as e:
                logging.warning(f"HistoryDB record_ping failed: {e}")

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
                    }'''
    ),

    # ──── 6. _spawn: accept services + strict
    (
        '''    def _spawn(self, name, ip, group, interval, always_on=True, specs=None, notes="", links=None):
        history_window = self.history_window
        host = HostState(
            name=name, ip=ip, group=group, interval=interval,
            always_on=always_on,
            specs=specs or {},
            notes=notes or "",
            links=links or {},
            history=deque(maxlen=history_window),
            stop_event=threading.Event(),
        )''',
        '''    def _spawn(self, name, ip, group, interval, always_on=True, specs=None, notes="", links=None, services=None, strict=False):
        history_window = self.history_window
        host = HostState(
            name=name, ip=ip, group=group, interval=interval,
            always_on=always_on,
            specs=specs or {},
            notes=notes or "",
            links=links or {},
            services=services or [],
            strict=bool(strict),
            history=deque(maxlen=history_window),
            stop_event=threading.Event(),
        )'''
    ),

    # ──── 7. load_initial: pass services + strict
    (
        '''    def load_initial(self, raw_hosts, default_interval):
        with self.lock:
            for h in raw_hosts:
                self.hosts.append(self._spawn(
                    h["name"], h["ip"], h.get("group", "General"),
                    h.get("interval", default_interval),
                    bool(h.get("always_on", True)),
                    h.get("specs") if isinstance(h.get("specs"), dict) else None,
                    h.get("notes", "") if isinstance(h.get("notes"), str) else "",
                    h.get("links") if isinstance(h.get("links"), dict) else None
                ))''',
        '''    def load_initial(self, raw_hosts, default_interval):
        with self.lock:
            for h in raw_hosts:
                self.hosts.append(self._spawn(
                    h["name"], h["ip"], h.get("group", "General"),
                    h.get("interval", default_interval),
                    bool(h.get("always_on", True)),
                    h.get("specs") if isinstance(h.get("specs"), dict) else None,
                    h.get("notes", "") if isinstance(h.get("notes"), str) else "",
                    h.get("links") if isinstance(h.get("links"), dict) else None,
                    h.get("services") if isinstance(h.get("services"), list) else None,
                    bool(h.get("strict", False))
                ))'''
    ),

    # ──── 8. reload_from_config: handle services + strict on existing hosts
    (
        '''                links = h.get("links") if isinstance(h.get("links"), dict) else {}
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
                    if name_changed and self.incident_log:
                        self.incident_log.update_host_info(existing)
                    rebuilt.append(existing)
                else:
                    rebuilt.append(self._spawn(name, ip, group, interval, always_on, specs, notes, links))''',
        '''                links = h.get("links") if isinstance(h.get("links"), dict) else {}
                services = h.get("services") if isinstance(h.get("services"), list) else []
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
                    rebuilt.append(self._spawn(name, ip, group, interval, always_on, specs, notes, links, services, strict))'''
    ),

    # ──── 9. validate_hosts_config: validate services list and strict flag
    (
        '''        if "links" in h:
            if not isinstance(h["links"], dict):
                return False, f"Host '{h['name']}': links must be a mapping."''',
        '''        if "strict" in h and not isinstance(h["strict"], bool):
            return False, f"Host '{h['name']}': strict must be true or false."
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
                return False, f"Host '{h['name']}': links must be a mapping."'''
    ),
]


# ─── Frontend changes ──────────────────────────────────────────────────────────

FRONTEND_PATCHES = [
    # ──── F1. CSS: amber DEGRADED styling for table rows, badges, dots, nodes
    (
        '''.dot-up{background:var(--green);box-shadow:0 0 0 3px var(--green-bg)}
.dot-dn{background:var(--red);box-shadow:0 0 0 3px var(--red-bg)}
.dot-wt{background:var(--amber);box-shadow:0 0 0 3px var(--amber-bg)}
.dot-idle{background:var(--hint);box-shadow:0 0 0 3px var(--subtle)}''',
        '''.dot-up{background:var(--green);box-shadow:0 0 0 3px var(--green-bg)}
.dot-dn{background:var(--red);box-shadow:0 0 0 3px var(--red-bg)}
.dot-wt{background:var(--amber);box-shadow:0 0 0 3px var(--amber-bg)}
.dot-degraded{background:var(--amber);box-shadow:0 0 0 3px var(--amber-bg)}
.dot-idle{background:var(--hint);box-shadow:0 0 0 3px var(--subtle)}'''
    ),

    (
        '''.badge-up{background:var(--green-bg);color:var(--green-text)}
.badge-dn{background:var(--red-bg);color:var(--red-text)}
.badge-wt{background:var(--amber-bg);color:var(--amber-text)}
.badge-idle{background:var(--subtle);color:var(--muted);border:1px solid var(--border)}''',
        '''.badge-up{background:var(--green-bg);color:var(--green-text)}
.badge-dn{background:var(--red-bg);color:var(--red-text)}
.badge-wt{background:var(--amber-bg);color:var(--amber-text)}
.badge-degraded{background:var(--amber-bg);color:var(--amber-text)}
.badge-idle{background:var(--subtle);color:var(--muted);border:1px solid var(--border)}'''
    ),

    # Topology nodes: degraded variant
    (
        '''.node.up{background:var(--green-soft);border-color:var(--green-bg)}
.node.down{background:var(--red-bg);border-color:var(--red);color:var(--red-text)}
.node.idle{background:var(--subtle);border-color:var(--border);color:var(--muted)}
.node.wait{background:var(--amber-bg);border-color:var(--amber);color:var(--amber-text)}''',
        '''.node.up{background:var(--green-soft);border-color:var(--green-bg)}
.node.down{background:var(--red-bg);border-color:var(--red);color:var(--red-text)}
.node.idle{background:var(--subtle);border-color:var(--border);color:var(--muted)}
.node.wait{background:var(--amber-bg);border-color:var(--amber);color:var(--amber-text)}
.node.degraded{background:var(--amber-bg);border-color:var(--amber);color:var(--amber-text)}'''
    ),

    (
        '''.node.up .node-dot{background:var(--green);box-shadow:0 0 0 2.5px var(--green-bg)}
.node.down .node-dot{background:var(--red);box-shadow:0 0 0 2.5px var(--red-bg)}
.node.idle .node-dot{background:var(--hint)}
.node.wait .node-dot{background:var(--amber);box-shadow:0 0 0 2.5px var(--amber-bg)}''',
        '''.node.up .node-dot{background:var(--green);box-shadow:0 0 0 2.5px var(--green-bg)}
.node.down .node-dot{background:var(--red);box-shadow:0 0 0 2.5px var(--red-bg)}
.node.idle .node-dot{background:var(--hint)}
.node.wait .node-dot{background:var(--amber);box-shadow:0 0 0 2.5px var(--amber-bg)}
.node.degraded .node-dot{background:var(--amber);box-shadow:0 0 0 2.5px var(--amber-bg)}'''
    ),

    # Hosts table: amber tint for degraded rows
    (
        '''.row.down-row{background:var(--red-bg)}
.row.down-row:hover{background:var(--red-bg);filter:brightness(1.05)}''',
        '''.row.down-row{background:var(--red-bg)}
.row.down-row:hover{background:var(--red-bg);filter:brightness(1.05)}
.row.degraded-row{background:var(--amber-bg)}
.row.degraded-row:hover{background:var(--amber-bg);filter:brightness(1.05)}'''
    ),

    # Drawer header dot
    (
        '''.drawer-name-dot.up{background:var(--green);box-shadow:0 0 0 3px var(--green-bg)}
.drawer-name-dot.down{background:var(--red);box-shadow:0 0 0 3px var(--red-bg)}
.drawer-name-dot.idle{background:var(--hint)}
.drawer-name-dot.wait{background:var(--amber);box-shadow:0 0 0 3px var(--amber-bg)}''',
        '''.drawer-name-dot.up{background:var(--green);box-shadow:0 0 0 3px var(--green-bg)}
.drawer-name-dot.down{background:var(--red);box-shadow:0 0 0 3px var(--red-bg)}
.drawer-name-dot.idle{background:var(--hint)}
.drawer-name-dot.wait{background:var(--amber);box-shadow:0 0 0 3px var(--amber-bg)}
.drawer-name-dot.degraded{background:var(--amber);box-shadow:0 0 0 3px var(--amber-bg)}'''
    ),

    # ──── F2. CSS for the Services section in the drawer + editor service rows
    (
        '''/* Pi system health */''',
        '''/* Services list (drawer) */
.d-services{background:var(--subtle);border:1px solid var(--border-light);border-radius:8px;overflow:hidden}
.d-svc{display:grid;grid-template-columns:14px 1fr 80px 80px;gap:10px;padding:9px 12px;border-bottom:1px solid var(--border-light);align-items:center;font-size:13px}
.d-svc:last-child{border-bottom:none}
.d-svc-dot{width:8px;height:8px;border-radius:50%}
.d-svc-dot.ok{background:var(--green);box-shadow:0 0 0 2px var(--green-bg)}
.d-svc-dot.fail{background:var(--red);box-shadow:0 0 0 2px var(--red-bg)}
.d-svc-dot.unknown{background:var(--hint)}
.d-svc-name{font-weight:500}
.d-svc-port{font-family:'DM Mono',monospace;color:var(--muted);font-size:11px;margin-left:6px}
.d-svc-state{font-family:'DM Mono',monospace;font-size:11px;text-align:right}
.d-svc-state.ok{color:var(--green-text)}
.d-svc-state.fail{color:var(--red-text)}
.d-svc-state.unknown{color:var(--hint)}
.d-svc-checked{font-family:'DM Mono',monospace;font-size:10px;color:var(--hint);text-align:right}
.d-svc-strict-note{font-family:'DM Mono',monospace;font-size:10px;color:var(--amber-text);background:var(--amber-bg);padding:2px 6px;border-radius:3px;margin-left:5px}

/* Editor: services rows */
.svc-wrap{display:flex;flex-direction:column;gap:6px;margin-bottom:6px}
.svc-row{display:grid;grid-template-columns:80px 1fr 30px;gap:6px;align-items:center}
.svc-row input{width:100%;padding:6px 9px;border:1px solid var(--border);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:12px;background:var(--surface);color:var(--text)}
.svc-row input:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px var(--blue-bg)}
.svc-row input.invalid{border-color:var(--red);background:var(--red-bg)}
.svc-row .del-btn{background:transparent;border:none;color:var(--hint);cursor:pointer;padding:4px;border-radius:4px;font-size:12px;line-height:1}
.svc-row .del-btn:hover{background:var(--red-bg);color:var(--red)}
.row-extra .add-svc-btn{margin-top:0;font-size:12px;padding:6px 10px;border:1px dashed var(--border);background:transparent;border-radius:6px;color:var(--muted);cursor:pointer;width:auto}
.row-extra .add-svc-btn:hover{border-color:var(--hint);background:var(--surface);color:var(--text)}
.svc-strict-toggle{display:inline-flex;align-items:center;gap:7px;font-family:'DM Sans',sans-serif;font-size:11px;color:var(--muted);text-transform:none;letter-spacing:0;font-weight:400;margin-top:6px}
.svc-strict-toggle input{width:14px;height:14px;margin:0;cursor:pointer}

/* Pi system health */'''
    ),

    # ──── F3. JavaScript: extend renderHost() to handle DEGRADED status
    (
        '''function renderHost(h){
  const isIdle = h.status === 'IDLE';
  const dotCls = h.status === 'WAIT' ? 'dot-wt' : h.is_up ? 'dot-up' : (isIdle ? 'dot-idle' : 'dot-dn');
  const badgeCls = h.status === 'WAIT' ? 'badge-wt' : h.is_up ? 'badge-up' : (isIdle ? 'badge-idle' : 'badge-dn');
  const nameStyle = h.is_up || h.status === 'WAIT' || isIdle ? '' : 'style="color:var(--red)"';
  const uPct = h.uptime_pct;
  const uColor = isIdle ? 'var(--hint)' : uptimeColor(uPct);
  const uBarColor = isIdle ? 'var(--border)' : uColor;
  const uBarW = uPct !== null ? uPct.toFixed(1) : 0;
  const uLabel = uPct !== null ? uPct.toFixed(1) + '%' : '-%';
  const rowCls = h.is_up || h.status === 'WAIT' || isIdle ? '' : ' down-row';''',
        '''function renderHost(h){
  const isIdle = h.status === 'IDLE';
  const isDegraded = h.status === 'DEGRADED';
  const dotCls = h.status === 'WAIT' ? 'dot-wt' : isDegraded ? 'dot-degraded' : h.is_up ? 'dot-up' : (isIdle ? 'dot-idle' : 'dot-dn');
  const badgeCls = h.status === 'WAIT' ? 'badge-wt' : isDegraded ? 'badge-degraded' : h.is_up ? 'badge-up' : (isIdle ? 'badge-idle' : 'badge-dn');
  const nameStyle = h.is_up || h.status === 'WAIT' || isIdle || isDegraded ? '' : 'style="color:var(--red)"';
  const uPct = h.uptime_pct;
  const uColor = isIdle ? 'var(--hint)' : uptimeColor(uPct);
  const uBarColor = isIdle ? 'var(--border)' : uColor;
  const uBarW = uPct !== null ? uPct.toFixed(1) : 0;
  const uLabel = uPct !== null ? uPct.toFixed(1) + '%' : '-%';
  const rowCls = isDegraded ? ' degraded-row' : (h.is_up || h.status === 'WAIT' || isIdle ? '' : ' down-row');'''
    ),

    # ──── F4. Topology node: handle DEGRADED
    (
        '''function renderTopologyNode(h){
  const isIdle = h.status === 'IDLE';
  const cls = h.status === 'WAIT' ? 'wait' : h.is_up ? 'up' : (isIdle ? 'idle' : 'down');
  let lat;
  if(isIdle) lat = 'idle';
  else if(h.status === 'WAIT') lat = '...';
  else if(h.is_up && h.latency_ms !== null) lat = h.latency_ms.toFixed(1) + 'ms';
  else lat = 'offline';''',
        '''function renderTopologyNode(h){
  const isIdle = h.status === 'IDLE';
  const isDegraded = h.status === 'DEGRADED';
  const cls = h.status === 'WAIT' ? 'wait' : isDegraded ? 'degraded' : h.is_up ? 'up' : (isIdle ? 'idle' : 'down');
  let lat;
  if(isIdle) lat = 'idle';
  else if(h.status === 'WAIT') lat = '...';
  else if(isDegraded) lat = 'degraded';
  else if(h.is_up && h.latency_ms !== null) lat = h.latency_ms.toFixed(1) + 'ms';
  else lat = 'offline';'''
    ),

    # ──── F5. Drawer header dot: handle DEGRADED
    (
        '''  dotEl.className = 'drawer-name-dot ' + (h.status === 'WAIT' ? 'wait' : h.is_up ? 'up' : (h.status === 'IDLE' ? 'idle' : 'down'));
  document.getElementById('d-name').textContent = h.name;
  const badgeCls = h.status === 'WAIT' ? 'badge-wt' : h.is_up ? 'badge-up' : (h.status === 'IDLE' ? 'badge-idle' : 'badge-dn');''',
        '''  dotEl.className = 'drawer-name-dot ' + (h.status === 'WAIT' ? 'wait' : h.status === 'DEGRADED' ? 'degraded' : h.is_up ? 'up' : (h.status === 'IDLE' ? 'idle' : 'down'));
  document.getElementById('d-name').textContent = h.name;
  const badgeCls = h.status === 'WAIT' ? 'badge-wt' : h.status === 'DEGRADED' ? 'badge-degraded' : h.is_up ? 'badge-up' : (h.status === 'IDLE' ? 'badge-idle' : 'badge-dn');'''
    ),

    # ──── F6. updateDrawerStats: keep header dot and badge in sync with DEGRADED
    (
        '''  // Refresh the header status badge since the host might have changed state
  const meta = document.getElementById('d-meta');
  if(meta){
    const badgeCls = h.status === 'WAIT' ? 'badge-wt' : h.is_up ? 'badge-up' : (h.status === 'IDLE' ? 'badge-idle' : 'badge-dn');
    meta.innerHTML =
      '<span>' + escapeHtml(h.ip) + '</span><span>·</span><span>' + escapeHtml(h.group) + '</span>'
      + '<span class="badge ' + badgeCls + '">' + h.status + '</span>';
  }
  const dotEl = document.getElementById('d-dot');
  if(dotEl){
    dotEl.className = 'drawer-name-dot ' + (h.status === 'WAIT' ? 'wait' : h.is_up ? 'up' : (h.status === 'IDLE' ? 'idle' : 'down'));
  }
}''',
        '''  // Refresh the header status badge since the host might have changed state
  const meta = document.getElementById('d-meta');
  if(meta){
    const badgeCls = h.status === 'WAIT' ? 'badge-wt' : h.status === 'DEGRADED' ? 'badge-degraded' : h.is_up ? 'badge-up' : (h.status === 'IDLE' ? 'badge-idle' : 'badge-dn');
    meta.innerHTML =
      '<span>' + escapeHtml(h.ip) + '</span><span>·</span><span>' + escapeHtml(h.group) + '</span>'
      + '<span class="badge ' + badgeCls + '">' + h.status + '</span>';
  }
  const dotEl = document.getElementById('d-dot');
  if(dotEl){
    dotEl.className = 'drawer-name-dot ' + (h.status === 'WAIT' ? 'wait' : h.status === 'DEGRADED' ? 'degraded' : h.is_up ? 'up' : (h.status === 'IDLE' ? 'idle' : 'down'));
  }

  // If the services section is present, update each row in place so users
  // can watch service state change live without the section flickering.
  const svcContainer = document.querySelector('#drawer-body .d-services');
  if(svcContainer && h.services){
    h.services.forEach(svc => {
      const row = svcContainer.querySelector('[data-svc-port="' + svc.port + '"]');
      if(!row) return;
      const dot = row.querySelector('.d-svc-dot');
      const state = row.querySelector('.d-svc-state');
      const checked = row.querySelector('.d-svc-checked');
      const stateClass = svc.ok === true ? 'ok' : svc.ok === false ? 'fail' : 'unknown';
      if(dot) dot.className = 'd-svc-dot ' + stateClass;
      if(state){
        state.className = 'd-svc-state ' + stateClass;
        state.textContent = svc.ok === true ? 'OK' : svc.ok === false ? (svc.error || 'fail') : '...';
      }
      if(checked) checked.textContent = svc.checked || '';
    });
  }
}'''
    ),

    # ──── F7. Drawer: insert Services section into renderDrawer output.
    # We add it right after the Pi health section.
    (
        '''  let piHtml = '';
  if(h.is_pi){''',
        '''  // Services section (only if host has any configured)
  let svcHtml = '';
  if(h.services && h.services.length){
    const strictNote = h.strict ? '<span class="d-svc-strict-note">strict</span>' : '';
    svcHtml = '<div class="d-section"><div class="d-section-hdr"><span>Services ' + strictNote + '</span></div>'
      + '<div class="d-services">'
      + h.services.map(svc => {
        const stateClass = svc.ok === true ? 'ok' : svc.ok === false ? 'fail' : 'unknown';
        const stateTxt = svc.ok === true ? 'OK' : svc.ok === false ? (svc.error || 'fail') : '...';
        return '<div class="d-svc" data-svc-port="' + svc.port + '">'
          + '<span class="d-svc-dot ' + stateClass + '"></span>'
          + '<div><span class="d-svc-name">' + escapeHtml(svc.name) + '</span><span class="d-svc-port">:' + svc.port + '</span></div>'
          + '<span class="d-svc-state ' + stateClass + '">' + escapeHtml(stateTxt) + '</span>'
          + '<span class="d-svc-checked">' + escapeHtml(svc.checked || '') + '</span>'
          + '</div>';
      }).join('')
      + '</div></div>';
  }

  let piHtml = '';
  if(h.is_pi){'''
    ),

    # ──── F8. Inject svcHtml into the drawer body output (right after links, before pi-health)
    (
        '''    drawerBody.innerHTML = statsHtml + linksHtml + piHtml + sparkHtml + specsHtml + notesHtml + incHtml + actionsHtml;''',
        '''    drawerBody.innerHTML = statsHtml + linksHtml + svcHtml + piHtml + sparkHtml + specsHtml + notesHtml + incHtml + actionsHtml;'''
    ),

    # ──── F9. Editor: add Services + Strict UI inside the row-extra block
    # We insert the new fields right after the MAC address line (which we
    # anchor on for uniqueness).
    (
        """    + '<label class="full">MAC address<input type="text" class="f-mac" placeholder="aa:bb:cc:dd:ee:ff (required for Wake-on-LAN)" value="' + escapeHtml(specs.mac || '') + '"></label>'""",
        """    + '<label class="full">MAC address<input type="text" class="f-mac" placeholder="aa:bb:cc:dd:ee:ff (required for Wake-on-LAN)" value="' + escapeHtml(specs.mac || '') + '"></label>'
    + '<label class="full">Services (TCP port checks)<div class="svc-wrap"></div><button type="button" class="add-svc-btn">+ Add service</button><label class="svc-strict-toggle"><input type="checkbox" class="f-strict" ' + ((h && h.strict) ? 'checked' : '') + '>Strict mode (mark host DEGRADED if any service fails)</label></label>'"""
    ),

    # ──── F10. addRow: populate services list + wire up "Add service"
    (
        '''  // Populate extras + wire up "+ Add link"
  const extrasWrap = row.querySelector('.extras-wrap');
  const initialExtras = (h && h.links && Array.isArray(h.links.extras)) ? h.links.extras : [];
  initialExtras.forEach(e => addExtraLinkRow(extrasWrap, e.name, e.url));
  row.querySelector('.add-extra-btn').addEventListener('click', () => addExtraLinkRow(extrasWrap, '', ''));
}''',
        '''  // Populate extras + wire up "+ Add link"
  const extrasWrap = row.querySelector('.extras-wrap');
  const initialExtras = (h && h.links && Array.isArray(h.links.extras)) ? h.links.extras : [];
  initialExtras.forEach(e => addExtraLinkRow(extrasWrap, e.name, e.url));
  row.querySelector('.add-extra-btn').addEventListener('click', () => addExtraLinkRow(extrasWrap, '', ''));

  // Populate services + wire up "+ Add service"
  const svcWrap = row.querySelector('.svc-wrap');
  const initialServices = (h && Array.isArray(h.services)) ? h.services : [];
  initialServices.forEach(s => addServiceRow(svcWrap, s.port, s.name));
  row.querySelector('.add-svc-btn').addEventListener('click', () => addServiceRow(svcWrap, '', ''));
}

function addServiceRow(container, port, name){
  const div = document.createElement('div');
  div.className = 'svc-row';
  div.innerHTML =
    '<input type="number" class="f-svc-port" placeholder="80" min="1" max="65535" value="' + escapeHtml(port !== undefined && port !== null ? String(port) : '') + '">'
    + '<input type="text" class="f-svc-name" placeholder="e.g. Web UI, SSH" value="' + escapeHtml(name || '') + '">'
    + '<button type="button" class="del-btn" title="Remove">X</button>';
  container.appendChild(div);
  div.querySelector('.del-btn').addEventListener('click', () => div.remove());
}'''
    ),

    # ──── F11. saveHosts: collect services + strict flag from the editor
    (
        '''    if(extras.length) links.extras = extras;
    if(Object.keys(links).length) entry.links = links;''',
        '''    if(extras.length) links.extras = extras;
    if(Object.keys(links).length) entry.links = links;

    // Services
    const services = [];
    row.querySelectorAll('.svc-row').forEach(svcRow => {
      const portEl = svcRow.querySelector('.f-svc-port');
      const nameEl = svcRow.querySelector('.f-svc-name');
      portEl.classList.remove('invalid');
      const portRaw = portEl.value.trim();
      const svcName = nameEl.value.trim();
      if(!portRaw && !svcName) return;
      const portNum = parseInt(portRaw);
      if(isNaN(portNum) || portNum < 1 || portNum > 65535){
        portEl.classList.add('invalid'); hasError = true; return;
      }
      services.push({ port: portNum, name: svcName || ('port ' + portNum) });
    });
    if(services.length) entry.services = services;
    const strictEl = row.querySelector('.f-strict');
    if(strictEl && strictEl.checked) entry.strict = true;'''
    ),

    # ──── F12. Update hasData detection in addRow so the more-btn dot appears
    # when a host has services configured.
    (
        '''  const hasLinks = !!(h && h.links && (h.links.primary || (h.links.extras && h.links.extras.length)));
  const hasData = !!(specs.cpu || specs.ram || specs.storage || specs.os || specs.mac || notes || hasLinks);''',
        '''  const hasLinks = !!(h && h.links && (h.links.primary || (h.links.extras && h.links.extras.length)));
  const hasServices = !!(h && Array.isArray(h.services) && h.services.length);
  const hasData = !!(specs.cpu || specs.ram || specs.storage || specs.os || specs.mac || notes || hasLinks || hasServices);'''
    ),

    # ──── F13. Summary "down" count: include DEGRADED in the offline count?
    # No - we keep DOWN as the offline count, but we want the summary card to
    # tell the user "1 down, 2 degraded" if both exist. Adjust the renderer.
    (
        '''  const up = data.hosts.filter(h => h.is_up).length;
  const total = data.hosts.length;
  const down = data.hosts.filter(h => !h.is_up && h.status === 'DOWN').length;''',
        '''  const up = data.hosts.filter(h => h.is_up).length;
  const total = data.hosts.length;
  const down = data.hosts.filter(h => !h.is_up && h.status === 'DOWN').length;
  const degraded = data.hosts.filter(h => h.status === 'DEGRADED').length;'''
    ),

    (
        '''  upEl.style.color = down > 0 ? 'var(--red)' : 'var(--green)';
  document.getElementById('s-up-sub').textContent = down > 0 ? down + ' host' + (down>1?'s':'') + ' offline' : 'all hosts online';''',
        '''  upEl.style.color = down > 0 ? 'var(--red)' : (degraded > 0 ? 'var(--amber)' : 'var(--green)');
  let subTxt;
  if(down > 0 && degraded > 0) subTxt = down + ' offline, ' + degraded + ' degraded';
  else if(down > 0) subTxt = down + ' host' + (down>1?'s':'') + ' offline';
  else if(degraded > 0) subTxt = degraded + ' service issue' + (degraded>1?'s':'');
  else subTxt = 'all hosts online';
  document.getElementById('s-up-sub').textContent = subTxt;'''
    ),

    # ──── F14. Bump version
    (
        '<span>netwatch v3.6 - raspberry pi</span>',
        '<span>netwatch v3.7 - raspberry pi</span>'
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

    if "class HistoryDB" not in content:
        print("ERROR: This patch requires patch_sqlite_history first.")
        sys.exit(1)

    shutil.copy2(TARGET, BACKUP)
    print(f"[OK] Backed up {TARGET} -> {BACKUP}")

    applied = 0
    for i, (old, new) in enumerate(PATCHES, 1):
        count = content.count(old)
        if count == 0:
            print(f"[FAIL] Backend patch #{i}: target not found")
            shutil.copy2(BACKUP, TARGET); sys.exit(1)
        if count > 1:
            print(f"[FAIL] Backend patch #{i}: matches {count}x")
            shutil.copy2(BACKUP, TARGET); sys.exit(1)
        content = content.replace(old, new, 1)
        applied += 1

    for i, (old, new) in enumerate(FRONTEND_PATCHES, 1):
        count = content.count(old)
        if count == 0:
            print(f"[FAIL] Frontend patch #{i}: target not found")
            shutil.copy2(BACKUP, TARGET); sys.exit(1)
        if count > 1:
            print(f"[FAIL] Frontend patch #{i}: matches {count}x")
            shutil.copy2(BACKUP, TARGET); sys.exit(1)
        content = content.replace(old, new, 1)
        applied += 1

    if 'VERSION = "3.6"' in content:
        content = content.replace('VERSION = "3.6"', 'VERSION = "3.7"', 1)

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
    print("  1. sudo systemctl restart netwatch")
    print("  2. Click 'Edit hosts' -> click ... on any row -> scroll to 'Services'")
    print("  3. Add port + label (e.g. 8123 / Web UI for HomeAssistant)")
    print("  4. Optionally enable 'Strict mode' on hosts where service")
    print("     failure should mark the host DEGRADED")
    print("  5. Save - dashboard immediately starts checking ports each cycle")
    print()
    print("Examples for your homelab:")
    print("  HomeAssistant: 8123 / Web UI")
    print("  Proxmox:       8006 / Web UI, 22 / SSH")
    print("  TrueNAS:       443  / Web UI, 22 / SSH, 445 / SMB")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
