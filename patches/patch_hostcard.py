#!/usr/bin/env python3
"""
netwatch patch: clickable linked-host card with inline quick actions.

The "Linked monitored host" card in the inventory drawer was already
technically clickable (had cursor:pointer + onclick handler) but the
user experience was confusing:

  - No visual affordance to suggest it was clickable
  - Clicking left you on the topology tab with the host drawer overlaid,
    rather than navigating you to the Hosts tab properly
  - You had to switch tabs anyway to use Wake-on-LAN, even though the
    inventory drawer already showed the host's MAC

This patch makes three improvements:

1. The linked-host card now has a clear "->" arrow affordance and
   matching hover state, making it obviously interactive.

2. Clicking it now properly switches to the Hosts tab AND opens the
   drawer there, so the user lands somewhere coherent rather than
   confused.

3. NEW: Inline Wake-on-LAN button directly in the inventory drawer's
   linked-host section. The most common action you'd switch tabs for
   is now one click away, no tab switch needed. The button is admin-
   gated (matching the host-drawer wake button) and shows live status
   feedback inline.

Must be applied AFTER patch_web_focal_polish.py.

Run once from ~/netwatch/:
    python3 patch_linked_host_actions.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_linkhost.
Idempotent - safe to re-run.
"""

import os, shutil, sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_linkhost"
SENTINEL = "sendWakeFromInventory"  # presence means already patched


PATCHES = [
    # ──────────────────────────────────────────────────────────────────────
    # 1. Replace the linked-host card render with one that includes:
    #    - a clear arrow affordance
    #    - an inline "Wake" button if the host has a MAC
    #    - status feedback area for the wake action
    # ──────────────────────────────────────────────────────────────────────
    (
        '''    linkHtml = '<div class="d-section"><div class="d-section-hdr"><span>Linked monitored host</span></div>'
      + '<div class="inv-link-host-card" onclick="openHostDrawerByIp(' + JSON.stringify(lh.ip) + ')">'
      + '<div><div style="font-weight:500">' + escapeHtml(lh.name) + '</div>'
      + '<div class="inv-mono" style="font-size:11px;color:var(--muted);margin-top:2px">' + escapeHtml(lh.ip) + '</div></div>'
      + '<span class="inv-link-pill ' + cls + '">' + escapeHtml(lh.status) + '</span>'
      + '</div></div>';''',
        '''    // The card itself navigates to the host drawer; the inline buttons
    // below offer common actions (Wake) without requiring tab switch.
    // We use stopPropagation on the buttons so clicking them doesn't
    // also trigger the card's navigation onclick.
    const macForWake = (rec.mac || '').trim();
    const wakeBtnHtml = macForWake
      ? '<button class="inv-link-action-btn" '
        + 'onclick="event.stopPropagation();sendWakeFromInventory(' + JSON.stringify(lh.ip) + ', this)">'
        + 'Wake</button>'
      : '';
    linkHtml = '<div class="d-section"><div class="d-section-hdr"><span>Linked monitored host</span></div>'
      + '<div class="inv-link-host-card" onclick="navigateToHostDrawer(' + JSON.stringify(lh.ip) + ')">'
      + '<div class="inv-link-host-info">'
        + '<div style="font-weight:500">' + escapeHtml(lh.name) + '</div>'
        + '<div class="inv-mono" style="font-size:11px;color:var(--muted);margin-top:2px">' + escapeHtml(lh.ip) + '</div>'
      + '</div>'
      + '<div class="inv-link-host-right">'
        + '<span class="inv-link-pill ' + cls + '">' + escapeHtml(lh.status) + '</span>'
        + wakeBtnHtml
        + '<span class="inv-link-arrow">\u2192</span>'
      + '</div>'
      + '</div>'
      + '<div class="inv-link-action-status" id="inv-link-action-status"></div>'
      + '</div>';'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 2. Add the new navigation + wake-from-inventory functions. We
    # anchor on the existing openHostDrawerByIp definition.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''function openHostDrawerByIp(ip){
  // openDrawer takes an IP and looks up the host in lastData itself
  closeDrawer();
  openDrawer(ip);
}''',
        '''function openHostDrawerByIp(ip){
  // openDrawer takes an IP and looks up the host in lastData itself
  closeDrawer();
  openDrawer(ip);
}

function navigateToHostDrawer(ip){
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
}

async function sendWakeFromInventory(ip, btn){
  // Wake-on-LAN from the inventory drawer's linked-host card. Same
  // backend endpoint as the host-drawer's Wake button, just with
  // inline status feedback in the inventory drawer instead.
  if(!_authState.logged_in){
    if(_authState.setup_required) openSetup();
    else openLogin(() => sendWakeFromInventory(ip, btn));
    return;
  }
  const status = document.getElementById('inv-link-action-status');
  if(btn) btn.disabled = true;
  if(status){
    status.className = 'inv-link-action-status';
    status.textContent = 'Sending magic packet...';
  }
  try {
    const res = await fetch('/api/wake', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ip })
    });
    const data = await res.json();
    if(!res.ok){
      if(status){
        status.className = 'inv-link-action-status error';
        status.textContent = data.error || 'Wake failed';
      }
    } else {
      if(status){
        status.className = 'inv-link-action-status success';
        status.textContent = 'Magic packet sent at ' + new Date().toLocaleTimeString();
      }
    }
  } catch(e){
    if(status){
      status.className = 'inv-link-action-status error';
      status.textContent = 'Network error';
    }
  } finally {
    if(btn) btn.disabled = false;
  }
}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 3. CSS for the improved card layout. Anchor on the existing
    # inv-link-host-card style block.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''.inv-link-host-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:11px 13px;cursor:pointer;transition:all .15s;display:flex;align-items:center;justify-content:space-between}
.inv-link-host-card:hover{border-color:var(--hint);background:var(--subtle)}''',
        '''.inv-link-host-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:11px 13px;cursor:pointer;transition:all .15s;display:flex;align-items:center;justify-content:space-between;gap:10px}
.inv-link-host-card:hover{border-color:var(--blue);background:var(--subtle)}
.inv-link-host-card:hover .inv-link-arrow{color:var(--blue);transform:translateX(2px)}
.inv-link-host-info{flex:1;min-width:0}
.inv-link-host-right{display:flex;align-items:center;gap:8px;flex-shrink:0}
.inv-link-arrow{color:var(--hint);font-size:14px;font-family:\'DM Mono\',monospace;transition:all .15s}
.inv-link-action-btn{background:var(--surface);border:1px solid var(--border);color:var(--text);font-family:\'DM Sans\',sans-serif;font-size:11px;font-weight:500;padding:5px 10px;border-radius:5px;cursor:pointer;transition:all .15s}
.inv-link-action-btn:hover:not(:disabled){background:var(--blue-bg);border-color:var(--blue);color:var(--blue)}
.inv-link-action-btn:disabled{opacity:.5;cursor:wait}
.inv-link-action-status{font-size:11px;font-family:\'DM Mono\',monospace;color:var(--muted);margin-top:6px;min-height:14px;padding:0 2px;transition:color .2s}
.inv-link-action-status.success{color:var(--green)}
.inv-link-action-status.error{color:var(--red)}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 4. Bump version
    # ──────────────────────────────────────────────────────────────────────
    (
        'netwatch v3.23 - raspberry pi',
        'netwatch v3.24 - raspberry pi'
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

    if 'VERSION = "3.23"' in content:
        content = content.replace('VERSION = "3.23"', 'VERSION = "3.24"', 1)

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
    print("  2. Open any inventory record that has a linked monitored host.")
    print("     The linked-host card now shows a -> arrow on the right.")
    print("  3. Click the card -> switches to the Hosts tab and opens the")
    print("     host drawer cleanly.")
    print("  4. NEW: if the inventory record has a MAC, a 'Wake' button")
    print("     appears next to the status pill. Click it to fire WoL")
    print("     directly without navigating away.")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
