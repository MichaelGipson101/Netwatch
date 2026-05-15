#!/usr/bin/env python3
"""
netwatch patch: add always_on flag support.

Adds per-host `always_on: true/false` configuration. Hosts with
`always_on: false` are treated as intermittent:
  - Offline -> shown as gray IDLE instead of red DOWN
  - Excluded from "hosts down" and "avg uptime" summary metrics
  - Gray uptime % (shown as availability, not a warning)
  - Editable via the web UI with an "Always on" checkbox

Defaults to always_on = True so existing hosts behave identically.

Run this once from inside ~/netwatch/ on the Pi:

    cd ~/netwatch
    python3 patch_always_on.py
    sudo systemctl restart netwatch

A backup of monitor.py is made at monitor.py.bak before anything changes.
The patch is idempotent - safe to run again, will detect if already applied.
"""

import os
import shutil
import sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak"

SENTINEL = "always_on"  # presence of this word in the file means already patched


# ── Replacement pairs: (exact_old_text, new_text) ──────────────────────────────
# Each OLD must appear EXACTLY ONCE in monitor.py. Each pair is applied in order.

PATCHES = [
    # ──── 1. Version bump ────
    (
        'VERSION = "2.2"',
        'VERSION = "2.3"'
    ),

    # ──── 2. Add always_on field to HostState dataclass ────
    (
        '''@dataclass
class HostState:
    name: str
    ip: str
    group: str
    interval: int
    history: deque = field(default_factory=lambda: deque(maxlen=100))''',
        '''@dataclass
class HostState:
    name: str
    ip: str
    group: str
    interval: int
    always_on: bool = True
    history: deque = field(default_factory=lambda: deque(maxlen=100))'''
    ),

    # ──── 3. Update status_str to return IDLE for always_on=False ────
    (
        '''    @property
    def status_str(self):
        if not self.last_checked:
            return "WAIT"
        return "UP" if self.is_up else "DOWN"''',
        '''    @property
    def status_str(self):
        if not self.last_checked:
            return "WAIT"
        if self.is_up:
            return "UP"
        return "DOWN" if self.always_on else "IDLE"'''
    ),

    # ──── 4. Include always_on in to_dict for the API ────
    (
        '''            return {
                "name":         self.name,
                "ip":           self.ip,
                "group":        self.group,
                "interval":     self.interval,
                "status":       self.status_str,''',
        '''            return {
                "name":         self.name,
                "ip":           self.ip,
                "group":        self.group,
                "interval":     self.interval,
                "always_on":    self.always_on,
                "status":       self.status_str,'''
    ),

    # ──── 5. HostManager._spawn: accept always_on ────
    (
        '''    def _spawn(self, name, ip, group, interval):
        host = HostState(
            name=name, ip=ip, group=group, interval=interval,
            history=deque(maxlen=self.history_window),
            stop_event=threading.Event(),
        )''',
        '''    def _spawn(self, name, ip, group, interval, always_on=True):
        host = HostState(
            name=name, ip=ip, group=group, interval=interval,
            always_on=always_on,
            history=deque(maxlen=self.history_window),
            stop_event=threading.Event(),
        )'''
    ),

    # ──── 6. load_initial: pass always_on ────
    (
        '''    def load_initial(self, raw_hosts, default_interval):
        with self.lock:
            for h in raw_hosts:
                self.hosts.append(self._spawn(
                    h["name"], h["ip"], h.get("group", "General"),
                    h.get("interval", default_interval)
                ))''',
        '''    def load_initial(self, raw_hosts, default_interval):
        with self.lock:
            for h in raw_hosts:
                self.hosts.append(self._spawn(
                    h["name"], h["ip"], h.get("group", "General"),
                    h.get("interval", default_interval),
                    bool(h.get("always_on", True))
                ))'''
    ),

    # ──── 7. reload_from_config: pass always_on on create + update on existing ────
    (
        '''            for h in new_hosts_config:
                ip = h["ip"]
                new_ips.add(ip)
                interval = h.get("interval", default_interval)
                group = h.get("group", "General")
                name = h["name"]
                if ip in current_by_ip:
                    existing = current_by_ip[ip]
                    existing.name = name
                    existing.group = group
                    existing.interval = interval
                    rebuilt.append(existing)
                else:
                    rebuilt.append(self._spawn(name, ip, group, interval))''',
        '''            for h in new_hosts_config:
                ip = h["ip"]
                new_ips.add(ip)
                interval = h.get("interval", default_interval)
                group = h.get("group", "General")
                name = h["name"]
                always_on = bool(h.get("always_on", True))
                if ip in current_by_ip:
                    existing = current_by_ip[ip]
                    existing.name = name
                    existing.group = group
                    existing.interval = interval
                    existing.always_on = always_on
                    rebuilt.append(existing)
                else:
                    rebuilt.append(self._spawn(name, ip, group, interval, always_on))'''
    ),

    # ──── 8. Summary payload: exclude non-always_on from "down" count ────
    (
        '''        "summary": {
            "total":   len(hosts),
            "up":      sum(1 for h in hosts if h.is_up),
            "down":    sum(1 for h in hosts if not h.is_up and h.last_checked),
            "pending": sum(1 for h in hosts if not h.last_checked),
        },''',
        '''        "summary": {
            "total":   len(hosts),
            "up":      sum(1 for h in hosts if h.is_up),
            "down":    sum(1 for h in hosts if not h.is_up and h.last_checked and h.always_on),
            "idle":    sum(1 for h in hosts if not h.is_up and h.last_checked and not h.always_on),
            "pending": sum(1 for h in hosts if not h.last_checked),
        },'''
    ),

    # ──── 9. Save hosts: preserve always_on when writing back to YAML ────
    # This is inside the POST /api/hosts handler. We rely on the fact that the
    # web UI sends always_on in each host entry, so save_hosts_config already
    # passes it through as-is. Nothing to change in Python for save path.
    #
    # But we DO need to validate its type - add that to validate_hosts_config.
    (
        '''        if "interval" in h:
            try:
                iv = int(h["interval"])
                if iv < 5:
                    return False, f"Host '{h['name']}': interval must be >= 5 seconds."
            except (ValueError, TypeError):
                return False, f"Host '{h['name']}': interval must be an integer."
    return True, None''',
        '''        if "interval" in h:
            try:
                iv = int(h["interval"])
                if iv < 5:
                    return False, f"Host '{h['name']}': interval must be >= 5 seconds."
            except (ValueError, TypeError):
                return False, f"Host '{h['name']}': interval must be an integer."
        if "always_on" in h and not isinstance(h["always_on"], bool):
            return False, f"Host '{h['name']}': always_on must be true or false."
    return True, None'''
    ),

    # ──── 10. Dashboard JS: renderHost - handle IDLE status ────
    (
        '''function renderHost(h){
  const dotCls = h.status === 'WAIT' ? 'dot-wt' : h.is_up ? 'dot-up' : 'dot-dn';
  const badgeCls = h.status === 'WAIT' ? 'badge-wt' : h.is_up ? 'badge-up' : 'badge-dn';
  const nameStyle = h.is_up || h.status === 'WAIT' ? '' : 'style="color:var(--red)"';
  const uPct = h.uptime_pct;
  const uColor = uptimeColor(uPct);
  const uBarW = uPct !== null ? uPct.toFixed(1) : 0;
  const uLabel = uPct !== null ? uPct.toFixed(1) + '%' : '-%';
  const rowCls = h.is_up || h.status === 'WAIT' ? '' : ' down-row';''',
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
  const rowCls = h.is_up || h.status === 'WAIT' || isIdle ? '' : ' down-row';'''
    ),

    # ──── 11. Use uBarColor in the rendered bar (small tweak to host row html) ────
    (
        "+ '<div class=\"uptime-cell\"><div class=\"uptime-track\"><div class=\"uptime-fill\" style=\"width:' + uBarW + '%;background:' + uColor + '\"></div></div><span class=\"uptime-pct\" style=\"color:' + uColor + '\">' + uLabel + '</span></div>'",
        "+ '<div class=\"uptime-cell\"><div class=\"uptime-track\"><div class=\"uptime-fill\" style=\"width:' + uBarW + '%;background:' + uBarColor + '\"></div></div><span class=\"uptime-pct\" style=\"color:' + uColor + '\">' + uLabel + '</span></div>'"
    ),

    # ──── 12. CSS: add IDLE badge + dot styles ────
    (
        '''.nw-dot-dn{background:#dc2626;box-shadow:0 0 0 3px #fee2e2}''',
        '''.nw-dot-dn{background:#dc2626;box-shadow:0 0 0 3px #fee2e2}'''  # placeholder -- replaced below
    ),

    # (actually replace the real css in the real monitor - the classes are named differently)
    (
        '''.dot-up{background:var(--green);box-shadow:0 0 0 3px #dcfce7}
.dot-dn{background:var(--red);box-shadow:0 0 0 3px #fee2e2}
.dot-wt{background:var(--amber);box-shadow:0 0 0 3px #fef3c7}''',
        '''.dot-up{background:var(--green);box-shadow:0 0 0 3px #dcfce7}
.dot-dn{background:var(--red);box-shadow:0 0 0 3px #fee2e2}
.dot-wt{background:var(--amber);box-shadow:0 0 0 3px #fef3c7}
.dot-idle{background:var(--hint);box-shadow:0 0 0 3px #f0efe9}'''
    ),

    (
        '''.badge-up{background:var(--green-bg);color:var(--green-text)}
.badge-dn{background:var(--red-bg);color:var(--red-text)}
.badge-wt{background:var(--amber-bg);color:var(--amber-text)}''',
        '''.badge-up{background:var(--green-bg);color:var(--green-text)}
.badge-dn{background:var(--red-bg);color:var(--red-text)}
.badge-wt{background:var(--amber-bg);color:var(--amber-text)}
.badge-idle{background:var(--subtle);color:var(--muted);border:1px solid var(--border)}'''
    ),

    # ──── 13. Summary renderer: exclude IDLE from "offline" counting ────
    (
        '''  const up = data.hosts.filter(h => h.is_up).length;
  const total = data.hosts.length;
  const down = data.hosts.filter(h => !h.is_up && h.status !== 'WAIT').length;
  const lats = data.hosts.filter(h => h.latency_ms !== null).map(h => h.latency_ms);
  const avgLat = lats.length ? (lats.reduce((a,b)=>a+b,0)/lats.length) : null;
  const upts = data.hosts.filter(h => h.uptime_pct !== null).map(h => h.uptime_pct);
  const avgUpt = upts.length ? (upts.reduce((a,b)=>a+b,0)/upts.length) : null;''',
        '''  const up = data.hosts.filter(h => h.is_up).length;
  const total = data.hosts.length;
  const down = data.hosts.filter(h => !h.is_up && h.status === 'DOWN').length;
  const idle = data.hosts.filter(h => h.status === 'IDLE').length;
  const lats = data.hosts.filter(h => h.latency_ms !== null).map(h => h.latency_ms);
  const avgLat = lats.length ? (lats.reduce((a,b)=>a+b,0)/lats.length) : null;
  const alwaysOnUpts = data.hosts.filter(h => h.always_on !== false && h.uptime_pct !== null).map(h => h.uptime_pct);
  const avgUpt = alwaysOnUpts.length ? (alwaysOnUpts.reduce((a,b)=>a+b,0)/alwaysOnUpts.length) : null;'''
    ),

    # ──── 14. Editor: add Always-on column header ────
    (
        '''      <div class="edit-row hdr">
        <div>Name</div><div>IP address</div><div>Group</div><div>Interval (s)</div><div></div>
      </div>''',
        '''      <div class="edit-row hdr">
        <div>Name</div><div>IP address</div><div>Group</div><div>Interval</div><div>Always on</div><div></div>
      </div>'''
    ),

    # ──── 15. Editor: update row grid columns to fit the new checkbox ────
    (
        '''.edit-row{display:grid;grid-template-columns:1fr 1fr 1fr 80px 32px;gap:8px;padding:8px 0;align-items:center;border-bottom:1px solid var(--border-light)}''',
        '''.edit-row{display:grid;grid-template-columns:1fr 1fr 1fr 70px 70px 32px;gap:8px;padding:8px 0;align-items:center;border-bottom:1px solid var(--border-light)}
.edit-row .ao-cell{display:flex;align-items:center;justify-content:center}
.edit-row .ao-cell input{width:18px;height:18px;margin:0;cursor:pointer}'''
    ),

    # ──── 16. Editor: add Always-on checkbox to each row ────
    (
        '''function addRow(h){
  const row = document.createElement('div');
  row.className = 'edit-row';
  row.innerHTML =
    '<input type="text" placeholder="My device" class="f-name" value="' + (h ? escapeHtml(h.name) : '') + '">'
    + '<input type="text" placeholder="192.168.1.1" class="f-ip" value="' + (h ? escapeHtml(h.ip) : '') + '">'
    + '<input type="text" placeholder="Network" class="f-group" value="' + (h ? escapeHtml(h.group || 'General') : 'General') + '">'
    + '<input type="number" min="5" placeholder="30" class="f-interval" value="' + (h && h.interval ? h.interval : '') + '">'
    + '<button class="del-btn" title="Remove" onclick="this.parentElement.remove()">X</button>';
  document.getElementById('edit-rows').appendChild(row);
}''',
        '''function addRow(h){
  const row = document.createElement('div');
  row.className = 'edit-row';
  const alwaysOn = !h || h.always_on !== false;
  row.innerHTML =
    '<input type="text" placeholder="My device" class="f-name" value="' + (h ? escapeHtml(h.name) : '') + '">'
    + '<input type="text" placeholder="192.168.1.1" class="f-ip" value="' + (h ? escapeHtml(h.ip) : '') + '">'
    + '<input type="text" placeholder="Network" class="f-group" value="' + (h ? escapeHtml(h.group || 'General') : 'General') + '">'
    + '<input type="number" min="5" placeholder="30" class="f-interval" value="' + (h && h.interval ? h.interval : '') + '">'
    + '<div class="ao-cell"><input type="checkbox" class="f-alwayson" title="Always on? Uncheck for laptops/phones/etc." ' + (alwaysOn ? 'checked' : '') + '></div>'
    + '<button class="del-btn" title="Remove" onclick="this.parentElement.remove()">X</button>';
  document.getElementById('edit-rows').appendChild(row);
}'''
    ),

    # ──── 17. Editor: include always_on when saving ────
    (
        '''    const entry = { name, ip, group };
    if(intervalRaw){
      const iv = parseInt(intervalRaw);
      if(!isNaN(iv) && iv >= 5) entry.interval = iv;
    }
    hosts.push(entry);''',
        '''    const entry = { name, ip, group };
    if(intervalRaw){
      const iv = parseInt(intervalRaw);
      if(!isNaN(iv) && iv >= 5) entry.interval = iv;
    }
    const alwaysOnEl = row.querySelector('.f-alwayson');
    entry.always_on = alwaysOnEl ? alwaysOnEl.checked : true;
    hosts.push(entry);'''
    ),

    # ──── 18. TUI: also show IDLE status in gray in the terminal ────
    (
        '''                if pend:
                    ind_attr = C_WAIT; st_attr = C_WAIT; nm_attr = C_TEXT
                elif is_up:
                    ind_attr = C_UP;   st_attr = C_UP;   nm_attr = C_TEXT
                else:
                    ind_attr = C_DOWN; st_attr = C_DOWN; nm_attr = C_DOWN''',
        '''                if pend:
                    ind_attr = C_WAIT; st_attr = C_WAIT; nm_attr = C_TEXT
                elif is_up:
                    ind_attr = C_UP;   st_attr = C_UP;   nm_attr = C_TEXT
                elif status == "IDLE":
                    ind_attr = C_MUTED; st_attr = C_MUTED; nm_attr = C_MUTED
                else:
                    ind_attr = C_DOWN; st_attr = C_DOWN; nm_attr = C_DOWN'''
    ),

    # ──── 19. TUI: status column is 7 chars but we also grab 'status' from host ──
    # Need to widen the status slot so "IDLE" fits cleanly (it does - IDLE is 4 chars)
    # Already fine. Just need to make sure we pass the status string through to the
    # conditional. Grab it outside the `with host.lock` block wait - it's already
    # being grabbed into `status`. Confirmed no change needed here.
]


def main():
    if not os.path.isfile(TARGET):
        print(f"ERROR: {TARGET} not found in current directory.")
        print("Run this from your ~/netwatch/ directory.")
        sys.exit(1)

    with open(TARGET, "r") as f:
        content = f.read()

    if SENTINEL in content:
        print(f"NOTE: {TARGET} already contains '{SENTINEL}' - patch appears to be applied already.")
        print("Exiting without changes.")
        sys.exit(0)

    # Make a backup
    shutil.copy2(TARGET, BACKUP)
    print(f"[OK] Backed up {TARGET} -> {BACKUP}")

    # Apply patches
    applied = 0
    for i, (old, new) in enumerate(PATCHES, 1):
        if old == new:
            continue  # placeholder skip
        count = content.count(old)
        if count == 0:
            print(f"[SKIP] Patch #{i}: target text not found (may already be partially applied). Skipping.")
            continue
        if count > 1:
            print(f"[FAIL] Patch #{i}: target text matches {count} times -- aborting for safety.")
            print("       Restoring backup.")
            shutil.copy2(BACKUP, TARGET)
            sys.exit(1)
        content = content.replace(old, new, 1)
        applied += 1

    with open(TARGET, "w") as f:
        f.write(content)

    print(f"[OK] Applied {applied} patches.")
    print()
    print("Next steps:")
    print("  1. Restart the service:  sudo systemctl restart netwatch")
    print("  2. Check it started OK:  sudo systemctl status netwatch")
    print("  3. Refresh the dashboard in your browser")
    print("  4. Click 'Edit hosts' - you'll see a new 'Always on' checkbox column")
    print()
    print(f"To roll back if anything goes wrong:")
    print(f"  cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
