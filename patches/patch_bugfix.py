#!/usr/bin/env python3
"""
netwatch patch: bug sweep + fixes (linked-host card + Wake button).

Three fixes that together restore the broken interactions in the
inventory drawer's "Linked monitored host" card.

THE PRIMARY BUG (root cause for two visible symptoms):

  The inventory drawer renders the linked-host card with this pattern:

      onclick="navigateToHostDrawer(' + JSON.stringify(lh.ip) + ')"

  JSON.stringify wraps strings in double quotes, producing:

      onclick="navigateToHostDrawer("192.168.4.89")"

  The browser parses this as: onclick="navigateToHostDrawer(" then
  treats the rest as garbage attributes. The function NEVER FIRES.
  Same problem on the Wake button. This is why clicking either does
  absolutely nothing - no JS error, no UI change, just silent breakage.

  The fix: add .replace(/"/g, '&quot;') to escape the quotes for HTML
  attribute context. This pattern is already used correctly elsewhere
  in the codebase (see line ~5615 for setInvCategoryFilter).

WAKE-ON-LAN FALLBACK:

  /api/wake looks up the host by IP in hosts.yaml, then reads the MAC
  from that monitored host's specs. If the monitored host has no MAC
  configured, WoL fails with "Host has no MAC address configured" -
  even when the matching INVENTORY record has the MAC right there.

  Fix: when the monitored host lacks a MAC, fall back to looking up
  the inventory record by IP and using its MAC. This way, you only
  have to record the MAC in one place (inventory) and WoL Just Works.

OPENDRAWER SILENT FAILURE:

  openDrawer(ip) does `if(!h) return` if the IP isn't in lastData.hosts.
  No warning, no fallback. Makes debugging impossible. Not strictly
  user-visible, but adds a console.warn so future "drawer didn't open"
  bugs surface their cause immediately.

Must be applied AFTER patch_web_focal_polish.py.

Run once from ~/netwatch/:
    python3 patch_bugfix_drawer_wol.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_bugfix_drawer.
Idempotent - safe to re-run.
"""

import os, shutil, sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_bugfix_drawer"
SENTINEL = "navigateToHostDrawerSafe"


PATCHES = [
    # ──────────────────────────────────────────────────────────────────────
    # 1. Fix the linked-host CARD onclick. Wrap JSON.stringify in
    # the same .replace(/"/g, '&quot;') pattern used elsewhere.
    # We also use a small helper function name change so we have a
    # sentinel for idempotency detection.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''+ \'<div class="inv-link-host-card" onclick="navigateToHostDrawer(\' + JSON.stringify(lh.ip) + \')">\'''',
        '''+ \'<div class="inv-link-host-card" onclick="navigateToHostDrawerSafe(\' + JSON.stringify(lh.ip).replace(/"/g, \'&quot;\') + \')">\''''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 2. Fix the WAKE BUTTON onclick. Same escaping fix.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''+ \'onclick="event.stopPropagation();sendWakeFromInventory(\' + JSON.stringify(lh.ip) + \', this)">\'''',
        '''+ \'onclick="event.stopPropagation();sendWakeFromInventory(\' + JSON.stringify(lh.ip).replace(/"/g, \'&quot;\') + \', this)">\''''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 3. Add navigateToHostDrawerSafe wrapper that warns if the host
    # isn't in lastData. Also acts as our sentinel for idempotency.
    # We anchor on the existing navigateToHostDrawer function.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''function navigateToHostDrawer(ip){
  // Switch to the Hosts tab (if not already there) and open the host
  // drawer for the given IP. This gives the user a coherent landing
  // experience: tab matches drawer content.
  closeDrawer();  // closes the inventory drawer (same #drawer element)
  if(typeof setTab === 'function'){
    const currentTab = localStorage.getItem('nw-tab');
    if(currentTab !== 'hosts'){
      setTab('hosts');
    }
  }
  // Small delay lets the tab switch settle before opening the drawer.
  // Without it, the drawer can render before the tab transition completes
  // and look like it's appearing in the wrong place.
  setTimeout(() => { openDrawer(ip); }, 60);
}''',
        '''function navigateToHostDrawer(ip){
  // Switch to the Hosts tab (if not already there) and open the host
  // drawer for the given IP. This gives the user a coherent landing
  // experience: tab matches drawer content.
  closeDrawer();  // closes the inventory drawer (same #drawer element)
  if(typeof setTab === 'function'){
    const currentTab = localStorage.getItem('nw-tab');
    if(currentTab !== 'hosts'){
      setTab('hosts');
    }
  }
  // Small delay lets the tab switch settle before opening the drawer.
  // Without it, the drawer can render before the tab transition completes
  // and look like it's appearing in the wrong place.
  setTimeout(() => { openDrawer(ip); }, 80);
}

// Wrapper that adds basic diagnostic logging if the drawer fails to open.
// Used by inline onclick handlers (the inventory drawer\'s linked-host card
// in particular). The original navigateToHostDrawer is preserved for any
// other call sites and works the same way.
function navigateToHostDrawerSafe(ip){
  if(!lastData || !lastData.hosts){
    console.warn(\'[netwatch] navigate to host drawer: no host data loaded yet\');
    return;
  }
  const exists = lastData.hosts.some(h => h.ip === ip);
  if(!exists){
    console.warn(\'[netwatch] navigate to host drawer: ip not in monitored hosts list:\', ip);
    // Soft failure UX: flash a status into the inventory drawer if it\'s open
    const status = document.getElementById(\'inv-link-action-status\');
    if(status){
      status.className = \'inv-link-action-status error\';
      status.textContent = ip + \' is not currently in the monitored hosts list.\';
      setTimeout(() => {
        if(status) { status.className = \'inv-link-action-status\'; status.textContent = \'\'; }
      }, 4000);
    }
    return;
  }
  navigateToHostDrawer(ip);
}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 4. WoL endpoint: fall back to inventory MAC when the monitored
    # host has no MAC configured. This is the "your inventory has the MAC
    # but hosts.yaml doesn\'t" case that breaks the Wake button.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''                    target_host = next((h for h in host_manager.list_hosts() if h.ip == target_ip), None)
                    if not target_host:
                        self._send_json(404, {"error": "Host not found"})
                        return
                    mac = (target_host.specs or {}).get("mac", "")
                    if not mac:
                        self._send_json(400, {"error": "Host has no MAC address configured"})
                        return''',
        '''                    target_host = next((h for h in host_manager.list_hosts() if h.ip == target_ip), None)
                    if not target_host:
                        self._send_json(404, {"error": "Host not found"})
                        return
                    mac = (target_host.specs or {}).get("mac", "")
                    if not mac and inventory_db:
                        # Fallback: the monitored host has no MAC, but the
                        # matching inventory record might. Look it up by IP.
                        try:
                            inv_records = inventory_db.list_all()
                            for rec in inv_records:
                                if rec.get("ip") == target_ip and rec.get("mac"):
                                    mac = rec["mac"]
                                    logging.info("WoL: using MAC from inventory record %s for %s",
                                                 rec.get("id"), target_ip)
                                    break
                        except Exception as e:
                            logging.warning("WoL inventory MAC lookup failed: %s", e)
                    if not mac:
                        self._send_json(400, {"error": "No MAC address configured for this host (in hosts.yaml or inventory)"})
                        return'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 5. Bump version
    # ──────────────────────────────────────────────────────────────────────
    (
        'netwatch v3.24 - raspberry pi',
        'netwatch v3.25 - raspberry pi'
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

    if "topo-web-overlay" not in content:
        print("ERROR: This patch requires patch_web_focal_polish first.")
        sys.exit(1)

    shutil.copy2(TARGET, BACKUP)
    print(f"[OK] Backed up {TARGET} -> {BACKUP}")

    applied = 0
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

    if 'VERSION = "3.24"' in content:
        content = content.replace('VERSION = "3.24"', 'VERSION = "3.25"', 1)

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
    print("  2. Open any inventory record with a linked monitored host.")
    print("     Click the card - it now correctly navigates to the host")
    print("     drawer in the Hosts tab.")
    print("  3. Click the Wake button on the same card. WoL fires.")
    print("     If your monitored host doesn't have a MAC in hosts.yaml,")
    print("     the inventory MAC is used as fallback automatically.")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
