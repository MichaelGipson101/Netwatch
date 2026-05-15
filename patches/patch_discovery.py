#!/usr/bin/env python3
"""
netwatch patch: network discovery via nmap.

Adds a "+ Discover" button to the host editor that scans your subnet for
live devices and lets you check off which ones to add as monitored hosts.

Behaviour:
  - Subnet is auto-detected from the Pi's primary interface (already used
    for WoL). User cannot supply an arbitrary range - this is a safety
    measure so the Pi only ever scans its own LAN.
  - Uses nmap's -sn (ping scan) which combines ARP, ICMP, and TCP probes.
    Handles /22 subnets gracefully in a few seconds.
  - Returns IP, MAC, hostname (reverse DNS), and vendor (from MAC OUI lookup).
  - Hosts already in your config are flagged with "already monitored" so you
    don't double-add.
  - Selected hosts are POSTed to the existing /api/hosts endpoint with
    sensible defaults (group=Discovered, always_on=true).

nmap install:
  - The patch checks if nmap is installed.
  - If not, it tries `sudo apt-get install -y nmap` (Debian/Ubuntu/Pi OS).
  - If install fails, the patch still applies, but the Discover button
    will show an error message about needing nmap.

Must be applied AFTER patch_tcp_checks.py.

Run once from ~/netwatch/:
    python3 patch_discovery.py
    sudo systemctl restart netwatch

Backup saved to monitor.py.bak_discovery.
Idempotent - safe to re-run.
"""

import os
import shutil
import subprocess
import sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_discovery"
SENTINEL = "/api/discover"  # presence means already patched


# ─── Step 0: ensure nmap is installed ─────────────────────────────────────────

def ensure_nmap():
    """Best-effort install of nmap. Returns (installed, message)."""
    # Already installed?
    r = subprocess.run(["which", "nmap"], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        return True, f"nmap already at {r.stdout.strip()}"

    # Try apt-get install
    print("  nmap not found - attempting install via apt-get...")
    try:
        r = subprocess.run(
            ["sudo", "apt-get", "install", "-y", "nmap"],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0:
            r2 = subprocess.run(["which", "nmap"], capture_output=True, text=True)
            if r2.returncode == 0 and r2.stdout.strip():
                return True, f"nmap installed at {r2.stdout.strip()}"
        return False, f"apt-get install failed: {r.stderr.strip()[:200]}"
    except subprocess.TimeoutExpired:
        return False, "apt-get install timed out"
    except FileNotFoundError:
        return False, "apt-get not found (not a Debian-family system?)"


# ─── New backend code: discovery module ───────────────────────────────────────

NEW_DISCOVERY_MODULE = '''# ============================================================================
# Network discovery (nmap-based)
# ============================================================================

# Cache the most recent scan result. Scans take a few seconds, so we run
# them in a background thread and let the client poll for results.
_DISCOVERY_LOCK = threading.Lock()
_DISCOVERY_STATE = {
    "running":   False,
    "started":   None,    # unix epoch
    "finished":  None,    # unix epoch
    "subnet":    None,
    "results":   [],
    "error":     None,
}


def _check_nmap_available():
    """Return (ok, path_or_error)."""
    try:
        r = subprocess.run(
            ["which", "nmap"], capture_output=True, text=True, timeout=2
        )
        if r.returncode == 0 and r.stdout.strip():
            return True, r.stdout.strip()
        return False, "nmap not installed (try: sudo apt-get install nmap)"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return False, f"could not check for nmap: {e}"


def _detect_subnet_cidr():
    """Detect the Pi's primary subnet in CIDR form, e.g. '192.168.0.0/22'.
    Reuses the same logic as _detect_broadcast_address."""
    import ipaddress
    try:
        # Find the outgoing interface
        result = subprocess.run(
            ["ip", "-o", "route", "get", "1.1.1.1"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            return None
        parts = result.stdout.split()
        if "dev" not in parts:
            return None
        iface = parts[parts.index("dev") + 1]

        # Get the CIDR for that interface
        result = subprocess.run(
            ["ip", "-o", "-f", "inet", "addr", "show", iface],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            return None
        line_parts = result.stdout.split()
        if "inet" not in line_parts:
            return None
        cidr = line_parts[line_parts.index("inet") + 1]
        net = ipaddress.IPv4Network(cidr, strict=False)
        return str(net)
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError, OSError):
        return None


def _run_nmap_scan(subnet):
    """Run `nmap -sn -oX -` against the subnet. Parses XML output and returns
    a list of {ip, mac, hostname, vendor} dicts. Returns (results, error)."""
    import xml.etree.ElementTree as ET

    try:
        # -sn = ping scan (no ports). -oX - = XML to stdout.
        # -T4 = aggressive timing for faster scans on local networks.
        # -n  = no DNS resolution by nmap (we'll do reverse DNS ourselves
        #       per host below for hostname enrichment).
        result = subprocess.run(
            ["nmap", "-sn", "-T4", "-n", "-oX", "-", subnet],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return [], "scan timed out (>2 minutes)"
    except FileNotFoundError:
        return [], "nmap not installed"
    except OSError as e:
        return [], f"could not run nmap: {e}"

    if result.returncode != 0:
        return [], f"nmap exited with code {result.returncode}: {result.stderr.strip()[:200]}"

    try:
        root = ET.fromstring(result.stdout)
    except ET.ParseError as e:
        return [], f"could not parse nmap output: {e}"

    hosts = []
    for host_el in root.findall("host"):
        # Skip down hosts (status="down")
        status_el = host_el.find("status")
        if status_el is None or status_el.get("state") != "up":
            continue

        ip = mac = vendor = None
        for addr in host_el.findall("address"):
            addrtype = addr.get("addrtype")
            if addrtype == "ipv4":
                ip = addr.get("addr")
            elif addrtype == "mac":
                mac = addr.get("addr", "").lower()
                vendor = addr.get("vendor")

        if not ip:
            continue

        # Reverse DNS lookup
        hostname = None
        try:
            import socket
            hostname = socket.gethostbyaddr(ip)[0]
        except (socket.herror, socket.gaierror, OSError):
            pass

        hosts.append({
            "ip":       ip,
            "mac":      mac,
            "hostname": hostname,
            "vendor":   vendor,
        })

    # Sort by IP for stable display
    def _ip_key(h):
        try:
            return tuple(int(x) for x in h["ip"].split("."))
        except (ValueError, AttributeError):
            return (999, 999, 999, 999)
    hosts.sort(key=_ip_key)
    return hosts, None


def start_discovery_scan():
    """Kick off a scan in a background thread if one isn't already running.
    Returns (started, message)."""
    with _DISCOVERY_LOCK:
        if _DISCOVERY_STATE["running"]:
            return False, "scan already in progress"

        ok, info = _check_nmap_available()
        if not ok:
            return False, info

        subnet = _detect_subnet_cidr()
        if not subnet:
            return False, "could not auto-detect subnet"

        _DISCOVERY_STATE.update({
            "running":  True,
            "started":  int(time.time()),
            "finished": None,
            "subnet":   subnet,
            "results":  [],
            "error":    None,
        })

    def _worker():
        results, error = _run_nmap_scan(subnet)
        with _DISCOVERY_LOCK:
            _DISCOVERY_STATE.update({
                "running":  False,
                "finished": int(time.time()),
                "results":  results,
                "error":    error,
            })
        if error:
            logging.warning(f"Discovery scan failed: {error}")
        else:
            logging.info(f"Discovery scan finished: {len(results)} hosts found in {subnet}")

    threading.Thread(target=_worker, daemon=True, name="discovery-scan").start()
    return True, f"started scan on {subnet}"


def get_discovery_state():
    """Return a snapshot of current scan state for the API."""
    with _DISCOVERY_LOCK:
        return dict(_DISCOVERY_STATE)


# ============================================================================
# Dashboard HTML (served inline)
# ============================================================================'''


# ─── Patches ──────────────────────────────────────────────────────────────────

def main():
    if not os.path.isfile(TARGET):
        print(f"ERROR: {TARGET} not found.")
        sys.exit(1)

    content = open(TARGET).read()

    if SENTINEL in content:
        print(f"NOTE: '{SENTINEL}' found - patch already applied.")
        sys.exit(0)

    if "check_tcp_service" not in content:
        print("ERROR: This patch requires patch_tcp_checks first.")
        sys.exit(1)

    # ── Step 0: try to install nmap ────────────────────────────────────────
    print("Checking for nmap...")
    ok, msg = ensure_nmap()
    print(f"  {msg}")
    if not ok:
        print("  WARNING: nmap is not available. The patch will still be applied,")
        print("  but the Discover button will show an error until you install nmap")
        print("  manually:  sudo apt-get install nmap")

    shutil.copy2(TARGET, BACKUP)
    print(f"[OK] Backed up {TARGET} -> {BACKUP}")

    # ── 1. Insert discovery module before the dashboard HTML section ──────
    OLD = '''# ============================================================================
# Dashboard HTML (served inline)
# ============================================================================'''
    if content.count(OLD) != 1:
        print(f"[FAIL] Dashboard HTML anchor not unique: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW_DISCOVERY_MODULE, 1)
    print("[OK] Inserted discovery module")

    # ── 2. Add /api/discover endpoints (GET=poll, POST=start) ──────────────
    OLD = '''            if self.path == "/api/pi-health":
                try:
                    self._send_json(200, read_pi_health())
                except Exception as e:
                    logging.exception("Error reading Pi health")
                    self._send_json(500, {"error": str(e)})
                return

            self.send_response(404)
            self.end_headers()'''

    NEW = '''            if self.path == "/api/pi-health":
                try:
                    self._send_json(200, read_pi_health())
                except Exception as e:
                    logging.exception("Error reading Pi health")
                    self._send_json(500, {"error": str(e)})
                return

            if self.path == "/api/discover":
                # Return current scan state
                try:
                    state = get_discovery_state()
                    # Annotate each result with whether the IP is already in hosts.yaml
                    cfg = load_yaml(config_path) or {}
                    known_ips = {h.get("ip") for h in cfg.get("hosts", []) if isinstance(h, dict)}
                    annotated = []
                    for r in state.get("results", []):
                        annotated.append({**r, "already_monitored": r["ip"] in known_ips})
                    state["results"] = annotated
                    self._send_json(200, state)
                except Exception as e:
                    logging.exception("Error reading discovery state")
                    self._send_json(500, {"error": str(e)})
                return

            self.send_response(404)
            self.end_headers()'''
    if content.count(OLD) != 1:
        print(f"[FAIL] GET /api/pi-health anchor not unique: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Added GET /api/discover endpoint")

    # ── 3. Add POST /api/discover (kicks off a scan)
    OLD = '''        def do_POST(self):
            if self.path == "/api/wake":'''
    NEW = '''        def do_POST(self):
            if self.path == "/api/discover":
                try:
                    started, msg = start_discovery_scan()
                    if started:
                        self._send_json(200, {"ok": True, "message": msg})
                    else:
                        self._send_json(400, {"error": msg})
                except Exception as e:
                    logging.exception("Error starting discovery scan")
                    self._send_json(500, {"error": str(e)})
                return

            if self.path == "/api/wake":'''
    if content.count(OLD) != 1:
        print(f"[FAIL] POST anchor not unique: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Added POST /api/discover endpoint")

    # ── 4. Frontend: add "+ Discover" button next to "+ Add host"
    OLD = '''      <button class="add-row-btn" onclick="addRow()">+ Add host</button>'''
    NEW = '''      <div style="display:flex;gap:8px;margin-top:10px">
        <button class="add-row-btn" onclick="addRow()" style="margin-top:0;flex:1">+ Add host</button>
        <button class="add-row-btn" onclick="openDiscover()" style="margin-top:0;flex:1">+ Discover</button>
      </div>'''
    if content.count(OLD) != 1:
        print(f"[FAIL] Add-host button anchor not unique: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Added Discover button to editor")

    # ── 5. Frontend: discovery modal HTML (separate overlay so it can stack
    # above the editor modal)
    OLD = '''<script>
(function initTheme(){'''
    NEW = '''<div class="modal-overlay" id="discover-overlay">
  <div class="modal" style="max-width:720px">
    <div class="modal-hdr">
      <div class="modal-title">Network discovery</div>
      <button class="btn btn-ghost" onclick="closeDiscover()">X</button>
    </div>
    <div class="modal-body">
      <div id="discover-status" style="font-family:'DM Mono',monospace;font-size:12px;color:var(--muted);margin-bottom:14px">Detecting subnet...</div>
      <div id="discover-results" style="display:none">
        <div style="font-family:'DM Mono',monospace;font-size:10px;letter-spacing:.07em;color:var(--hint);text-transform:uppercase;margin-bottom:8px">Devices found</div>
        <div id="discover-list" style="background:var(--subtle);border:1px solid var(--border);border-radius:8px;overflow:hidden"></div>
      </div>
    </div>
    <div class="modal-foot">
      <button class="btn" onclick="startDiscover()" id="discover-scan-btn">Scan now</button>
      <div style="display:flex;gap:8px">
        <button class="btn" onclick="closeDiscover()">Cancel</button>
        <button class="btn btn-primary" onclick="addDiscovered()" id="discover-add-btn" disabled>Add selected</button>
      </div>
    </div>
  </div>
</div>

<style>
.disc-row{display:grid;grid-template-columns:24px 130px 1fr 110px;gap:10px;padding:10px 12px;align-items:center;border-bottom:1px solid var(--border-light);font-size:13px}
.disc-row:last-child{border-bottom:none}
.disc-row.known{opacity:.6}
.disc-row input[type="checkbox"]{margin:0;cursor:pointer;width:16px;height:16px}
.disc-ip{font-family:'DM Mono',monospace;font-size:12px}
.disc-name{display:flex;flex-direction:column;gap:2px}
.disc-hostname{font-weight:500}
.disc-vendor{font-size:11px;color:var(--muted)}
.disc-mac{font-family:'DM Mono',monospace;font-size:10px;color:var(--hint);text-align:right}
.disc-known-tag{display:inline-block;font-family:'DM Mono',monospace;font-size:9px;background:var(--subtle);color:var(--muted);padding:2px 6px;border-radius:3px;letter-spacing:.04em;margin-left:6px;border:1px solid var(--border)}
</style>

<script>
(function initTheme(){'''
    if content.count(OLD) != 1:
        print(f"[FAIL] script anchor not unique: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Added discovery modal HTML")

    # ── 6. Frontend: the discovery JS functions. Insert just before saveHosts.
    OLD = '''async function saveHosts(){'''
    NEW = '''let _discoverPollTimer = null;

function openDiscover(){
  document.getElementById('discover-overlay').classList.add('open');
  document.getElementById('discover-status').textContent = 'Click "Scan now" to discover devices on your network.';
  document.getElementById('discover-results').style.display = 'none';
  document.getElementById('discover-list').innerHTML = '';
  document.getElementById('discover-add-btn').disabled = true;
  document.getElementById('discover-scan-btn').disabled = false;
  // Pre-check current state so we don't restart a finished scan
  refreshDiscoverState(false);
}
function closeDiscover(){
  document.getElementById('discover-overlay').classList.remove('open');
  if(_discoverPollTimer){ clearTimeout(_discoverPollTimer); _discoverPollTimer = null; }
}

async function startDiscover(){
  const btn = document.getElementById('discover-scan-btn');
  const statusEl = document.getElementById('discover-status');
  btn.disabled = true;
  statusEl.textContent = 'Starting scan...';
  document.getElementById('discover-list').innerHTML = '';
  document.getElementById('discover-results').style.display = 'none';
  try {
    const res = await fetch('/api/discover', { method: 'POST' });
    const data = await res.json();
    if(!res.ok){
      statusEl.textContent = 'Error: ' + (data.error || 'could not start scan');
      btn.disabled = false;
      return;
    }
    statusEl.textContent = data.message || 'Scan started. This may take a few seconds...';
    pollDiscover();
  } catch(e){
    statusEl.textContent = 'Network error starting scan.';
    btn.disabled = false;
  }
}

function pollDiscover(){
  refreshDiscoverState(true);
}

async function refreshDiscoverState(continuePolling){
  try {
    const res = await fetch('/api/discover');
    if(!res.ok) throw new Error('bad');
    const state = await res.json();
    const statusEl = document.getElementById('discover-status');
    const btn = document.getElementById('discover-scan-btn');

    if(state.running){
      statusEl.textContent = 'Scanning ' + (state.subnet || 'network') + '...';
      btn.disabled = true;
      if(continuePolling){
        _discoverPollTimer = setTimeout(pollDiscover, 1500);
      }
      return;
    }
    if(state.error){
      statusEl.textContent = 'Scan failed: ' + state.error;
      btn.disabled = false;
      return;
    }
    if(state.finished && state.results){
      const total = state.results.length;
      const newOnes = state.results.filter(r => !r.already_monitored).length;
      statusEl.textContent = 'Scan of ' + state.subnet + ' complete - ' + total + ' devices found, ' + newOnes + ' new.';
      renderDiscoverResults(state.results);
      btn.disabled = false;
      btn.textContent = 'Scan again';
      return;
    }
    // No previous scan
    statusEl.textContent = 'Click "Scan now" to discover devices on ' + (state.subnet || 'your network') + '.';
    btn.disabled = false;
  } catch(e){
    document.getElementById('discover-status').textContent = 'Could not reach netwatch.';
  }
}

function renderDiscoverResults(results){
  const wrap = document.getElementById('discover-results');
  const list = document.getElementById('discover-list');
  if(!results.length){
    wrap.style.display = 'none';
    return;
  }
  wrap.style.display = '';
  list.innerHTML = results.map(r => {
    const knownTag = r.already_monitored ? '<span class="disc-known-tag">already monitored</span>' : '';
    const hostnameDisplay = r.hostname || (r.vendor ? '(' + r.vendor + ')' : '(unknown)');
    const vendorLine = (r.hostname && r.vendor) ? '<span class="disc-vendor">' + escapeHtml(r.vendor) + '</span>' : '';
    const checkbox = r.already_monitored ? '' : '<input type="checkbox" class="disc-check" data-ip="' + escapeHtml(r.ip) + '" data-name="' + escapeHtml(r.hostname || r.vendor || ('Host ' + r.ip)) + '" data-mac="' + escapeHtml(r.mac || '') + '">';
    return '<div class="disc-row' + (r.already_monitored ? ' known' : '') + '">'
      + '<div>' + checkbox + '</div>'
      + '<div class="disc-ip">' + escapeHtml(r.ip) + '</div>'
      + '<div class="disc-name"><span class="disc-hostname">' + escapeHtml(hostnameDisplay) + knownTag + '</span>' + vendorLine + '</div>'
      + '<div class="disc-mac">' + escapeHtml(r.mac || '') + '</div>'
      + '</div>';
  }).join('');
  // Wire up checkboxes to enable/disable the Add button
  const update = () => {
    const any = list.querySelectorAll('.disc-check:checked').length > 0;
    document.getElementById('discover-add-btn').disabled = !any;
  };
  list.querySelectorAll('.disc-check').forEach(cb => cb.addEventListener('change', update));
  update();
}

async function addDiscovered(){
  const checked = document.querySelectorAll('.disc-check:checked');
  if(!checked.length) return;
  // Fetch the existing host list so we can append to it (rather than replace)
  let existing = [];
  try {
    const res = await fetch('/api/hosts');
    const data = await res.json();
    existing = data.hosts || [];
  } catch(e){
    document.getElementById('discover-status').textContent = 'Could not load existing hosts.';
    return;
  }
  // Build new entries
  const additions = [];
  checked.forEach(cb => {
    const ip = cb.dataset.ip;
    const name = cb.dataset.name || ('Host ' + ip);
    const mac = cb.dataset.mac;
    const entry = { name, ip, group: 'Discovered' };
    if(mac){ entry.specs = { mac }; }
    additions.push(entry);
  });
  const merged = existing.concat(additions);
  const res = await fetch('/api/hosts', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ hosts: merged })
  });
  const data = await res.json();
  if(!res.ok){
    document.getElementById('discover-status').textContent = 'Save failed: ' + (data.error || 'unknown');
    return;
  }
  document.getElementById('discover-status').textContent = 'Added ' + additions.length + ' host' + (additions.length===1?'':'s') + '. Closing...';
  setTimeout(() => {
    closeDiscover();
    closeEditor();
    refresh();
  }, 800);
}

async function saveHosts(){'''
    if content.count(OLD) != 1:
        print(f"[FAIL] saveHosts anchor not unique: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Added discovery JS functions")

    # ── 7. Bump version
    if 'VERSION = "3.7"' in content:
        content = content.replace('VERSION = "3.7"', 'VERSION = "3.8"', 1)
    content = content.replace('netwatch v3.7 - raspberry pi', 'netwatch v3.8 - raspberry pi', 1)
    print("[OK] Version bumped to 3.8")

    open(TARGET, "w").write(content)

    import ast
    try:
        ast.parse(open(TARGET).read())
        print("[OK] Resulting Python is valid")
    except SyntaxError as e:
        print(f"[FAIL] Syntax error: {e}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)

    print()
    print("Next steps:")
    print("  1. sudo systemctl restart netwatch")
    print("  2. Click 'Edit hosts' in the dashboard")
    print("  3. Click '+ Discover' (next to '+ Add host')")
    print("  4. Click 'Scan now' - takes a few seconds for a /22 subnet")
    print("  5. Check the boxes for devices you want to monitor")
    print("  6. Click 'Add selected' - they're appended to your config")
    print()
    if not ok:
        print("  IMPORTANT: nmap was not installed automatically. To enable")
        print("  discovery, run on the Pi:")
        print("    sudo apt-get install nmap")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
