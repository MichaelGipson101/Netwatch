#!/usr/bin/env python3
"""
netwatch patch: ARP-based MAC auto-detection.

Adds two ways to discover host MAC addresses automatically:

  1. After every successful ping, the ARP cache is consulted. If the host
     has no MAC configured, the detected MAC is saved to hosts.yaml and
     marked as auto-detected. If the host has a configured MAC that
     matches, no-op. If the configured MAC differs from the detected one,
     a warning is logged but the configured value is preserved.

  2. A "Detect" button next to the MAC field in the host editor lets you
     query the ARP cache on demand. Useful when adding a new host.

Auto-detected MACs get a 'mac_auto: true' flag in hosts.yaml and a small
"auto" tag in the editor UI, so it's clear which MACs you typed vs which
the system filled in. Manually editing the field clears the flag.

ARP-based detection only works for hosts on the same broadcast domain
(your LAN segment). Hosts behind a router/VPN won't have ARP entries.
For your /22 homelab, this covers everything.

Must be applied AFTER patch_inv_link_wizard.py.

Run once from ~/netwatch/:
    python3 patch_arp_detect.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_arpdetect.
Idempotent - safe to re-run.
"""

import os
import shutil
import sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_arpdetect"
SENTINEL = "_detect_mac_for_ip"  # presence means already patched


# ─── New backend module: ARP detection helpers ────────────────────────────────

NEW_ARP_MODULE = '''# ============================================================================
# ARP-based MAC detection
# ============================================================================

# Lock for serializing writes to hosts.yaml from the ping thread.
# Uses the same lock the host_manager uses for config reloads, but we need
# our own here since this module-level helper can't reach into HostManager.
_ARP_WRITE_LOCK = threading.Lock()

# Track which IPs we've already auto-saved a MAC for in this session.
# Prevents rewriting hosts.yaml on every successful ping.
_ARP_SAVED_THIS_SESSION = set()


def _detect_mac_for_ip(ip):
    """Read /proc/net/arp and return the MAC for ip, or None if not present
    or not yet resolved.

    Format is 6 fixed-position columns:
        IP address       HW type     Flags       HW address            Mask     Device

    Flags 0x2 (ATF_COM) means the entry is complete & resolved. Other values
    mean it\'s incomplete (no response yet) or has an error.
    """
    try:
        with open("/proc/net/arp") as f:
            lines = f.readlines()
    except OSError:
        return None
    # Skip header line
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        arp_ip, hw_type, flags, mac = parts[0], parts[1], parts[2], parts[3]
        if arp_ip != ip:
            continue
        # Only return resolved entries
        try:
            flag_int = int(flags, 16)
        except ValueError:
            continue
        if not (flag_int & 0x2):
            continue
        if mac == "00:00:00:00:00:00" or not mac:
            continue
        return mac.lower()
    return None


def _normalise_mac(s):
    """Normalise a MAC string for comparison. Returns lowercase colon form."""
    if not s:
        return ""
    clean = "".join(c for c in str(s).lower() if c in "0123456789abcdef")
    if len(clean) != 12:
        return str(s).lower().strip()
    return ":".join(clean[i:i+2] for i in range(0, 12, 2))


def _save_detected_mac(config_path, host_ip, detected_mac):
    """Write back to hosts.yaml: set specs.mac and specs.mac_auto for the
    host with the given IP. Atomic via temp-file-then-rename. Returns True
    on success.

    Race-aware: we re-read the config under our write lock so we don\'t
    clobber concurrent edits from POST /api/hosts. The HTTP handler also
    uses load_yaml + save under its own lock, so we coordinate through
    the file itself - last writer wins, but neither corrupts.
    """
    with _ARP_WRITE_LOCK:
        try:
            cfg = load_yaml(config_path) or {}
        except Exception as e:
            logging.warning(f"ARP detect: could not read {config_path}: {e}")
            return False
        hosts = cfg.get("hosts", []) or []
        changed = False
        for h in hosts:
            if not isinstance(h, dict):
                continue
            if h.get("ip") != host_ip:
                continue
            specs = h.get("specs")
            if not isinstance(specs, dict):
                specs = {}
                h["specs"] = specs
            existing_mac = specs.get("mac")
            if existing_mac:
                # Don\'t overwrite a user-set MAC. Caller already decided
                # whether to call us; this is just defensive.
                return False
            specs["mac"] = detected_mac
            specs["mac_auto"] = True
            changed = True
            break
        if not changed:
            return False
        # Atomic write
        tmp = config_path + ".tmp"
        try:
            import yaml
            with open(tmp, "w") as f:
                yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
            os.replace(tmp, config_path)
            logging.info(f"ARP detect: saved MAC {detected_mac} for {host_ip}")
            return True
        except Exception as e:
            try: os.unlink(tmp)
            except OSError: pass
            logging.warning(f"ARP detect: could not write {config_path}: {e}")
            return False


# ============================================================================
# Wake-on-LAN
# ============================================================================'''


# ─── Patches ──────────────────────────────────────────────────────────────────

PATCHES = [
    # ──── 1. Hook into poll_host: after a successful ping, look up ARP and
    # decide whether to auto-save or warn.
    #
    # Anchor: the existing block where we record_ping to history_db. We add
    # the ARP logic right after that.
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
                    # Mismatch - log a warning but keep the user\'s value.
                    # We add to the session set so we don\'t spam this every
                    # ping cycle.
                    logging.warning(
                        f"ARP detect: configured MAC {configured} for {host.ip} "
                        f"({host.name}) differs from network-detected MAC {detected}. "
                        f"Keeping configured value. If the host\'s NIC was changed, "
                        f"clear the MAC in the editor and let it re-detect."
                    )
                    _ARP_SAVED_THIS_SESSION.add(host.ip)
                else:
                    # Match - no need to keep checking this session
                    _ARP_SAVED_THIS_SESSION.add(host.ip)'''
    ),

    # ──── 2. poll_host needs access to config_path so it can save back. We
    # thread it through via the function signature.
    (
        '''def poll_host(host, timeout, global_stop, incident_log=None, history_db=None):''',
        '''def poll_host(host, timeout, global_stop, incident_log=None, history_db=None, config_path_for_arp=None):'''
    ),

    # ──── 3. _spawn passes config_path through to the thread. We thread
    # config_path through HostManager too.
    (
        '''        host.thread = threading.Thread(
            target=poll_host,
            args=(host, self.ping_timeout, self.global_stop, self.incident_log, self.history_db),
            daemon=True, name=f"ping-{name}",
        )''',
        '''        host.thread = threading.Thread(
            target=poll_host,
            args=(host, self.ping_timeout, self.global_stop, self.incident_log, self.history_db, self.config_path),
            daemon=True, name=f"ping-{name}",
        )'''
    ),

    # ──── 4. Add /api/detect-mac POST endpoint. Anchor on the existing
    # /api/wake POST.
    (
        '''            if self.path == "/api/wake":
                if not self._require_auth():
                    return''',
        '''            if self.path == "/api/detect-mac":
                # On-demand ARP lookup for an IP. Used by the "Detect" button
                # in the host editor. Auth required since this is editor-side.
                if not self._require_auth():
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length).decode()
                    data = json.loads(body)
                    ip = (data.get("ip") or "").strip()
                    if not ip:
                        self._send_json(400, {"error": "ip required"})
                        return
                    mac = _detect_mac_for_ip(ip)
                    if mac:
                        self._send_json(200, {"ok": True, "mac": mac})
                    else:
                        self._send_json(404, {"error": "not in ARP cache (host may be offline or not yet pinged)"})
                except json.JSONDecodeError:
                    self._send_json(400, {"error": "invalid JSON"})
                except Exception as e:
                    logging.exception("detect-mac error")
                    self._send_json(500, {"error": str(e)})
                return

            if self.path == "/api/wake":
                if not self._require_auth():
                    return'''
    ),

    # ──── 5. Frontend: add "Detect" button + auto tag next to the MAC field
    # in the host editor. Replace the existing MAC label.
    (
        '''    + \'<label class="full">MAC address<input type="text" class="f-mac" placeholder="aa:bb:cc:dd:ee:ff (required for Wake-on-LAN)" value="\' + escapeHtml(specs.mac || \'\') + \'"></label>\'''',
        '''    + \'<label class="full">MAC address<div class="mac-row"><input type="text" class="f-mac" placeholder="aa:bb:cc:dd:ee:ff (required for Wake-on-LAN)" value="\' + escapeHtml(specs.mac || \'\') + \'" data-auto="\' + (specs.mac_auto ? \'1\' : \'\') + \'"><button type="button" class="mac-detect-btn" onclick="detectMac(this)">Detect</button>\' + (specs.mac_auto ? \'<span class="mac-auto-tag" title="This MAC was auto-detected from the network">auto</span>\' : \'\') + \'</div></label>\'''',
    ),

    # ──── 6. Frontend: when the user types in the MAC field, clear the
    # "auto" tag (since they\'re now editing manually). Add a delegated
    # listener for this. We anchor on the existing inv-search input
    # listener for proximity.
    (
        '''document.addEventListener('input', e => {
  if(e.target.id === 'inv-search'){
    _inventoryFilter.search = e.target.value;
    renderInventoryRows();
  }''',
        '''document.addEventListener('input', e => {
  if(e.target.id === 'inv-search'){
    _inventoryFilter.search = e.target.value;
    renderInventoryRows();
  }
  // If user manually edits a MAC field, clear the "auto" flag visually
  if(e.target.classList.contains('f-mac')){
    e.target.dataset.auto = '';
    const tag = e.target.parentElement.querySelector('.mac-auto-tag');
    if(tag) tag.remove();
  }'''
    ),

    # ──── 7. Frontend: detectMac() function + add to saveHosts so mac_auto
    # is preserved/cleared correctly on save.
    # We add detectMac() near the other host-editor helpers. Anchor on the
    # existing addExtraLinkRow function.
    (
        '''function addExtraLinkRow(container, name, url){''',
        '''async function detectMac(btn){
  const row = btn.closest('.edit-row');
  if(!row) return;
  const ipEl = row.querySelector('.f-ip');
  const macEl = row.querySelector('.f-mac');
  if(!ipEl || !macEl) return;
  const ip = ipEl.value.trim();
  if(!ip){ alert('Set the IP first, then try Detect.'); return; }
  const origText = btn.textContent;
  btn.disabled = true; btn.textContent = '...';
  try {
    const res = await fetch('/api/detect-mac', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ip })
    });
    const data = await res.json();
    if(!res.ok){
      alert(data.error || 'Could not detect MAC');
      return;
    }
    macEl.value = data.mac;
    // Mark as auto-detected (will be saved as mac_auto: true)
    macEl.dataset.auto = '1';
    // Add the visual tag if not already there
    const parent = macEl.parentElement;
    let tag = parent.querySelector('.mac-auto-tag');
    if(!tag){
      tag = document.createElement('span');
      tag.className = 'mac-auto-tag';
      tag.title = 'This MAC was auto-detected from the network';
      tag.textContent = 'auto';
      parent.appendChild(tag);
    }
  } catch(e){
    alert('Network error during MAC detection');
  } finally {
    btn.disabled = false; btn.textContent = origText;
  }
}

function addExtraLinkRow(container, name, url){'''
    ),

    # ──── 8. saveHosts: include mac_auto in the saved specs when the field
    # was auto-detected (and the user hasn\'t edited it since).
    # NOTE: macEl is already declared earlier in saveHosts for validation,
    # so we reuse it rather than redeclare.
    (
        '''    const specs = {};
    [\'cpu\',\'ram\',\'storage\',\'os\',\'mac\'].forEach(k => {
      const el = row.querySelector(\'.f-\' + k);
      if(el && el.value.trim()) specs[k] = el.value.trim();
    });
    if(Object.keys(specs).length) entry.specs = specs;''',
        '''    const specs = {};
    [\'cpu\',\'ram\',\'storage\',\'os\',\'mac\'].forEach(k => {
      const el = row.querySelector(\'.f-\' + k);
      if(el && el.value.trim()) specs[k] = el.value.trim();
    });
    // Preserve mac_auto flag if the MAC field still has its auto marker
    // (macEl is the one already grabbed earlier in this function for validation)
    if(macEl && macEl.dataset.auto === \'1\' && macEl.value.trim()){
      specs.mac_auto = true;
    }
    if(Object.keys(specs).length) entry.specs = specs;'''
    ),

    # ──── 9. CSS for the MAC row layout (input + button + tag)
    (
        '''/* Inventory editor form */''',
        '''/* MAC row in host editor (input + Detect button + auto tag) */
.mac-row{display:flex;align-items:center;gap:6px}
.mac-row input{flex:1;padding:7px 10px;border:1px solid var(--border);border-radius:6px;font-family:'DM Mono',monospace;font-size:12px;background:var(--surface);color:var(--text);text-transform:none;letter-spacing:0}
.mac-row input:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px var(--blue-bg)}
.mac-detect-btn{padding:6px 11px;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--muted);font-family:\'DM Sans\',sans-serif;font-size:11px;cursor:pointer;white-space:nowrap;transition:all .15s}
.mac-detect-btn:hover{border-color:var(--hint);color:var(--text);background:var(--subtle)}
.mac-detect-btn:disabled{opacity:.5;cursor:not-allowed}
.mac-auto-tag{display:inline-block;padding:2px 7px;border-radius:3px;font-size:10px;font-family:\'DM Mono\',monospace;background:var(--blue-bg);color:var(--blue);text-transform:lowercase;letter-spacing:.04em}

/* Inventory editor form */'''
    ),

    # ──── 10. (No-op) Validator already accepts arbitrary keys in specs since
    # it only enforces MAC format. mac_auto will pass through correctly.
]


def main():
    if not os.path.isfile(TARGET):
        print(f"ERROR: {TARGET} not found.")
        sys.exit(1)

    content = open(TARGET).read()

    if SENTINEL in content:
        print(f"NOTE: '{SENTINEL}' found - patch already applied.")
        sys.exit(0)

    if "renderInvLinkPicker" not in content:
        print("ERROR: This patch requires patch_inv_link_wizard first.")
        sys.exit(1)

    shutil.copy2(TARGET, BACKUP)
    print(f"[OK] Backed up {TARGET} -> {BACKUP}")

    # ── 1. Insert ARP module before "# Wake-on-LAN" anchor
    OLD = '''# ============================================================================
# Wake-on-LAN
# ============================================================================'''
    if content.count(OLD) != 1:
        print(f"[FAIL] WoL anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW_ARP_MODULE, 1)
    print("[OK] Inserted ARP module")

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
    print("  2. Wait ~30 seconds for the first ping cycle of each host.")
    print("  3. Open Edit hosts. Hosts that had no MAC should now have one,")
    print("     marked with a small blue 'auto' tag.")
    print("  4. New hosts: type the IP, click 'Detect' button next to MAC.")
    print()
    print("Limitations to know:")
    print("  - ARP only works for hosts on your LAN segment (your /22 covers")
    print("    everything for you, so this isn't a problem)")
    print("  - Hosts that are offline can't be detected until they come up")
    print("  - If a host's existing MAC differs from what ARP detects, the")
    print("    configured value is kept and a warning is logged. Check with:")
    print("      journalctl -u netwatch -n 50 | grep 'ARP detect'")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
