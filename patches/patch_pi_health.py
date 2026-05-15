#!/usr/bin/env python3
"""
netwatch patch: Pi self-monitoring (CPU temp, load, RAM, disk, uptime).

When you click the host that represents the Pi running netwatch, the detail
drawer now shows a "System health" section with:
  - CPU temperature (with green/amber/red color based on thresholds)
  - Load average (1m / 5m / 15m)
  - RAM usage (used / total + percentage)
  - Disk usage (used / total + percentage of root filesystem)
  - System uptime

Detection: the patch identifies the Pi as the host whose IP matches one of
the Pi's local interfaces (auto-detected on startup). No configuration needed.

Implementation notes:
  - All metrics read from /proc and /sys - no extra dependencies
  - Only computed on demand (when the Pi's drawer is open)
  - Returns None gracefully if a metric is unavailable

Must be applied AFTER patch_quick_links.py.

Run once from ~/netwatch/:
    python3 patch_pi_health.py
    sudo systemctl restart netwatch

Backup saved to monitor.py.bak_pihealth.
Idempotent - safe to re-run.
"""

import os
import shutil
import sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_pihealth"
SENTINEL = "read_pi_health"  # presence means already patched


PATCHES = [
    # ──── 1. Add the read_pi_health helper module right after _get_wol_broadcast
    # We anchor on the next major section so we land in a clean spot.
    (
        '''# ============================================================================
# Dashboard HTML (served inline)
# ============================================================================''',
        '''# ============================================================================
# Pi self-monitoring
# ============================================================================

_PI_LOCAL_IPS_CACHE = None

def _get_pi_local_ips():
    """Return the set of local IPs this Pi has, used to identify which monitored
    host represents the Pi itself. Cached on first call."""
    global _PI_LOCAL_IPS_CACHE
    if _PI_LOCAL_IPS_CACHE is not None:
        return _PI_LOCAL_IPS_CACHE
    import subprocess
    ips = set()
    try:
        result = subprocess.run(
            ["ip", "-o", "-f", "inet", "addr"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                parts = line.split()
                if "inet" in parts:
                    cidr = parts[parts.index("inet") + 1]
                    ip = cidr.split("/")[0]
                    ips.add(ip)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    # Always include loopback so 127.0.0.1 in hosts.yaml also matches the Pi
    ips.add("127.0.0.1")
    _PI_LOCAL_IPS_CACHE = ips
    return ips


def _is_local_ip(ip):
    return ip in _get_pi_local_ips()


def read_pi_health():
    """Read current Pi system metrics. Returns a dict with whatever we could
    read; missing keys mean that metric is unavailable on this system."""
    health = {}

    # CPU temperature (Pi exposes this at /sys/class/thermal/thermal_zone0/temp
    # in millidegrees C). Fall back to None if unavailable.
    for path in ("/sys/class/thermal/thermal_zone0/temp",):
        try:
            with open(path) as f:
                health["cpu_temp_c"] = round(int(f.read().strip()) / 1000.0, 1)
                break
        except (OSError, ValueError):
            continue

    # Load average
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
            health["load_1m"]  = float(parts[0])
            health["load_5m"]  = float(parts[1])
            health["load_15m"] = float(parts[2])
    except (OSError, ValueError, IndexError):
        pass

    # CPU count for normalising load average
    try:
        health["cpu_count"] = os.cpu_count() or 1
    except Exception:
        pass

    # Memory (read /proc/meminfo)
    try:
        meminfo = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                meminfo[k.strip()] = v.strip()
        def _kb(k):
            v = meminfo.get(k, "")
            try:
                return int(v.split()[0]) * 1024
            except (ValueError, IndexError):
                return None
        total = _kb("MemTotal")
        avail = _kb("MemAvailable")
        if total and avail is not None:
            used = total - avail
            health["mem_total_bytes"] = total
            health["mem_used_bytes"]  = used
            health["mem_pct"] = round(used / total * 100, 1)
    except OSError:
        pass

    # Disk usage of /
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free  = st.f_bavail * st.f_frsize
        used  = total - free
        health["disk_total_bytes"] = total
        health["disk_used_bytes"]  = used
        health["disk_pct"] = round(used / total * 100, 1) if total else None
    except OSError:
        pass

    # System uptime
    try:
        with open("/proc/uptime") as f:
            health["uptime_seconds"] = int(float(f.read().split()[0]))
    except (OSError, ValueError):
        pass

    return health


# ============================================================================
# Dashboard HTML (served inline)
# ============================================================================'''
    ),

    # ──── 2. to_dict: include is_pi flag so the frontend knows which host is
    # special. We append it just inside the existing return dict.
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
                "status":       self.status_str,''',
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
                "status":       self.status_str,'''
    ),

    # ──── 3. Add a /api/pi-health endpoint — returns the live metrics
    (
        '''            if self.path == "/api/hosts":
                try:
                    cfg = load_yaml(config_path) or {}
                    self._send_json(200, {"hosts": cfg.get("hosts", [])})
                except Exception as e:
                    self._send_json(500, {"error": f"Could not read config: {e}"})
                return
            self.send_response(404)
            self.end_headers()''',
        '''            if self.path == "/api/hosts":
                try:
                    cfg = load_yaml(config_path) or {}
                    self._send_json(200, {"hosts": cfg.get("hosts", [])})
                except Exception as e:
                    self._send_json(500, {"error": f"Could not read config: {e}"})
                return

            if self.path == "/api/pi-health":
                try:
                    self._send_json(200, read_pi_health())
                except Exception as e:
                    logging.exception("Error reading Pi health")
                    self._send_json(500, {"error": str(e)})
                return

            self.send_response(404)
            self.end_headers()'''
    ),
]


# ─── Frontend changes ──────────────────────────────────────────────────────────

FRONTEND_PATCHES = [
    # ──── F1. CSS for the system health section
    (
        '''/* Links */
.d-link-primary{display:flex;align-items:center;justify-content:space-between;background:var(--text);color:var(--surface);border-radius:8px;padding:11px 14px;text-decoration:none;font-family:'DM Sans',sans-serif;font-size:13px;font-weight:500;transition:all .15s;border:1px solid var(--text)}''',
        '''/* Pi system health */
.d-pihealth{background:var(--subtle);border:1px solid var(--border-light);border-radius:8px;overflow:hidden}
.d-pi-row{display:grid;grid-template-columns:90px 1fr 70px;padding:10px 12px;gap:10px;align-items:center;border-bottom:1px solid var(--border-light);font-size:13px}
.d-pi-row:last-child{border-bottom:none}
.d-pi-key{color:var(--hint);font-family:'DM Mono',monospace;font-size:11px;letter-spacing:.04em;text-transform:uppercase}
.d-pi-bar{height:6px;background:var(--border-light);border-radius:3px;overflow:hidden;position:relative}
.d-pi-bar-fill{height:100%;border-radius:3px;transition:width .4s}
.d-pi-val{font-family:'DM Mono',monospace;font-size:12px;text-align:right;white-space:nowrap}
.d-pi-val.green{color:var(--green-text)}
.d-pi-val.amber{color:var(--amber-text)}
.d-pi-val.red{color:var(--red-text)}
.d-pi-empty{color:var(--hint);font-size:12px;font-style:italic;padding:9px 0;text-align:center}

/* Links */
.d-link-primary{display:flex;align-items:center;justify-content:space-between;background:var(--text);color:var(--surface);border-radius:8px;padding:11px 14px;text-decoration:none;font-family:'DM Sans',sans-serif;font-size:13px;font-weight:500;transition:all .15s;border:1px solid var(--text)}'''
    ),

    # ──── F2. Insert a section that fetches and renders pi health, between
    # statsHtml and linksHtml. We extend renderDrawer so it injects the section
    # and triggers an async fetch when h.is_pi is true.
    (
        '''  document.getElementById('drawer-body').innerHTML = statsHtml + linksHtml + sparkHtml + specsHtml + notesHtml + incHtml + actionsHtml;

  const wakeBtn = document.getElementById('d-wake-btn');
  if(wakeBtn) wakeBtn.addEventListener('click', () => sendWake(wakeBtn.dataset.ip));
}''',
        '''  let piHtml = '';
  if(h.is_pi){
    piHtml = '<div class="d-section"><div class="d-section-hdr"><span>System health</span><span style="color:var(--muted)" id="d-pi-meta">loading...</span></div>'
      + '<div class="d-pihealth" id="d-pihealth"><div class="d-pi-empty">Reading metrics...</div></div></div>';
  }

  document.getElementById('drawer-body').innerHTML = statsHtml + linksHtml + piHtml + sparkHtml + specsHtml + notesHtml + incHtml + actionsHtml;

  const wakeBtn = document.getElementById('d-wake-btn');
  if(wakeBtn) wakeBtn.addEventListener('click', () => sendWake(wakeBtn.dataset.ip));

  if(h.is_pi){
    fetchPiHealth();
  }
}

function _bytesHuman(b){
  if(b === undefined || b === null) return '-';
  if(b < 1024) return b + ' B';
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = b / 1024;
  let i = 0;
  while(v >= 1024 && i < units.length - 1){ v /= 1024; i++; }
  return v.toFixed(v >= 100 ? 0 : 1) + ' ' + units[i];
}
function _uptimeHuman(s){
  if(s === undefined || s === null) return '-';
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if(d > 0) return d + 'd ' + h + 'h';
  if(h > 0) return h + 'h ' + m + 'm';
  return m + 'm';
}
function _tempColor(t){
  if(t === undefined || t === null) return 'var(--hint)';
  if(t < 60) return 'var(--green)';
  if(t < 75) return 'var(--amber)';
  return 'var(--red)';
}
function _pctColor(p){
  if(p === undefined || p === null) return 'var(--hint)';
  if(p < 70) return 'var(--green)';
  if(p < 90) return 'var(--amber)';
  return 'var(--red)';
}
function _pctClass(p){
  if(p === undefined || p === null) return '';
  if(p < 70) return 'green';
  if(p < 90) return 'amber';
  return 'red';
}
function _loadColor(load, cores){
  if(load === undefined || load === null) return 'var(--hint)';
  const ratio = cores ? (load / cores) : load;
  if(ratio < 0.7) return 'var(--green)';
  if(ratio < 1.2) return 'var(--amber)';
  return 'var(--red)';
}
function _loadClass(load, cores){
  if(load === undefined || load === null) return '';
  const ratio = cores ? (load / cores) : load;
  if(ratio < 0.7) return 'green';
  if(ratio < 1.2) return 'amber';
  return 'red';
}

async function fetchPiHealth(){
  const target = document.getElementById('d-pihealth');
  if(!target) return;
  try {
    const res = await fetch('/api/pi-health');
    if(!res.ok) throw new Error('bad response');
    const h = await res.json();
    const meta = document.getElementById('d-pi-meta');
    if(meta) meta.textContent = h.cpu_count ? h.cpu_count + ' CPU cores' : '';

    const rows = [];

    if(h.cpu_temp_c !== undefined){
      rows.push({
        key: 'CPU TEMP',
        bar_pct: Math.min(100, (h.cpu_temp_c / 90) * 100),
        bar_color: _tempColor(h.cpu_temp_c),
        val: h.cpu_temp_c.toFixed(1) + ' &deg;C',
        val_color: _tempColor(h.cpu_temp_c),
      });
    }

    if(h.load_1m !== undefined){
      const cls = _loadClass(h.load_1m, h.cpu_count);
      rows.push({
        key: 'LOAD AVG',
        bar_pct: Math.min(100, (h.load_1m / (h.cpu_count || 1)) * 100),
        bar_color: _loadColor(h.load_1m, h.cpu_count),
        val: h.load_1m.toFixed(2) + ' &middot; ' + h.load_5m.toFixed(2) + ' &middot; ' + h.load_15m.toFixed(2),
        val_class: cls,
      });
    }

    if(h.mem_pct !== undefined){
      rows.push({
        key: 'MEMORY',
        bar_pct: h.mem_pct,
        bar_color: _pctColor(h.mem_pct),
        val: _bytesHuman(h.mem_used_bytes) + ' / ' + _bytesHuman(h.mem_total_bytes),
        val_class: _pctClass(h.mem_pct),
        sub: h.mem_pct.toFixed(1) + '%',
      });
    }

    if(h.disk_pct !== undefined){
      rows.push({
        key: 'DISK',
        bar_pct: h.disk_pct,
        bar_color: _pctColor(h.disk_pct),
        val: _bytesHuman(h.disk_used_bytes) + ' / ' + _bytesHuman(h.disk_total_bytes),
        val_class: _pctClass(h.disk_pct),
        sub: h.disk_pct.toFixed(1) + '%',
      });
    }

    if(h.uptime_seconds !== undefined){
      rows.push({
        key: 'UPTIME',
        bar_pct: null,
        val: _uptimeHuman(h.uptime_seconds),
      });
    }

    if(rows.length === 0){
      target.innerHTML = '<div class="d-pi-empty">No system metrics available.</div>';
      return;
    }

    target.innerHTML = rows.map(r => {
      const barCell = r.bar_pct !== null && r.bar_pct !== undefined
        ? '<div class="d-pi-bar"><div class="d-pi-bar-fill" style="width:' + Math.max(2, r.bar_pct) + '%;background:' + r.bar_color + '"></div></div>'
        : '<div></div>';
      const valHtml = r.sub
        ? '<div><span class="d-pi-val ' + (r.val_class || '') + '">' + r.sub + '</span><div style="font-family:DM Mono,monospace;font-size:10px;color:var(--hint);text-align:right">' + r.val + '</div></div>'
        : '<span class="d-pi-val ' + (r.val_class || '') + '" style="color:' + (r.val_color || '') + '">' + r.val + '</span>';
      return '<div class="d-pi-row"><div class="d-pi-key">' + r.key + '</div>' + barCell + valHtml + '</div>';
    }).join('');
  } catch(e){
    target.innerHTML = '<div class="d-pi-empty">Could not read metrics.</div>';
  }
}'''
    ),

    # ──── F3. Auto-refresh pi health while drawer is open
    # We hook into the main refresh() function to also re-fetch metrics if the
    # Pi's drawer is open.
    (
        '''    if(openDrawerIp){
      const h = data.hosts.find(x => x.ip === openDrawerIp);
      if(h) renderDrawer(h, data);
    }''',
        '''    if(openDrawerIp){
      const h = data.hosts.find(x => x.ip === openDrawerIp);
      if(h) renderDrawer(h, data);
    }
    // Note: pi-health auto-refresh happens inside renderDrawer when h.is_pi'''
    ),

    # ──── F4. Bump version label
    (
        '<span>netwatch v3.3 - raspberry pi</span>',
        '<span>netwatch v3.4 - raspberry pi</span>'
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

    if "f-primary-url" not in content:
        print("ERROR: This patch requires patch_quick_links first.")
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

    if 'VERSION = "3.3"' in content:
        content = content.replace('VERSION = "3.3"', 'VERSION = "3.4"', 1)

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
    print("  2. The Pi (any host whose IP matches the Pi's interface) auto-detects.")
    print("     Click that host's pill -> drawer opens with a new 'System health'")
    print("     section showing CPU temp, load avg, RAM, disk, and uptime.")
    print("  3. To verify which IP the Pi recognises as itself:")
    print("       curl -s http://<pi-ip>:8080/api/status | python3 -c \"")
    print("       import json,sys; [print(h['name'], h['ip'], h['is_pi']) for h in json.load(sys.stdin)['hosts']]\"")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
