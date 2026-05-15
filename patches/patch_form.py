#!/usr/bin/env python3
"""
netwatch patch: WiFi connection type + drawer form clipping fix.

Two small but important fixes:

1. **WiFi as a first-class connection type.** Until now you had to use
   "other" for wireless connections (e.g., Eero mesh backhaul, devices
   associated with an AP). WiFi is too common in a homelab to be a
   second-class citizen, so it gets its own type with:
     - Backend: included in CONNECTION_TYPES validation
     - Add-form dropdown: WiFi appears as an option
     - Topology web view: distinct teal color + dashed line + slow flow
       animation (suggesting wireless propagation rather than wired data)

2. **Drawer form labels were being clipped.** The connection add-form
   used a 2-column grid (1fr 1fr) with a media-query fallback at 768px
   viewport. But the drawer itself is only 440px wide regardless of
   viewport, so the media query never fired on desktop. Long uppercase
   labels like "From port (this device, optional)" got truncated.

   Fix: drop the 2-column grid. Stack form fields vertically. Saves
   horizontal space, eliminates clipping, looks tidier in a narrow
   drawer.

Must be applied AFTER patch_topology_web.py.

Run once from ~/netwatch/:
    python3 patch_wifi_and_form_fix.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_wifi.
Idempotent - safe to re-run.
"""

import os, shutil, sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_wifi"
SENTINEL = '"wifi"'  # presence in CONNECTION_TYPES means already patched


PATCHES = [
    # ──────────────────────────────────────────────────────────────────────
    # 1. Backend: add "wifi" to CONNECTION_TYPES
    # ──────────────────────────────────────────────────────────────────────
    (
        '''    CONNECTION_TYPES = ("ethernet", "fiber", "power", "usb", "console", "other")''',
        '''    CONNECTION_TYPES = ("ethernet", "fiber", "wifi", "power", "usb", "console", "other")'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 2. Frontend: add WiFi to the connection-type dropdown in the
    # inventory drawer's add-connection form. Inserted right after Fiber.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''+ \'<label>Type<select class="conn-f-type"><option value="ethernet">Ethernet</option><option value="fiber">Fiber</option><option value="power">Power</option><option value="usb">USB</option><option value="console">Console</option><option value="other">Other</option></select></label>\'''',
        '''+ \'<label>Type<select class="conn-f-type"><option value="ethernet">Ethernet</option><option value="fiber">Fiber</option><option value="wifi">WiFi</option><option value="power">Power</option><option value="usb">USB</option><option value="console">Console</option><option value="other">Other</option></select></label>\''''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 3. Frontend (drawer connection list): add WiFi to the icon map.
    # Uses the radio-tower / antenna glyph that's available in basic Unicode.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  const typeIcon = {
    ethernet: \'\u2500\u2500\',  // box drawing horizontal
    fiber:    \'\u2248\',        // wave (suggestive of light/optical)
    power:    \'\u26a1\',        // lightning bolt
    usb:      \'\u21cc\',        // double arrow
    console:  \'\u2192\',        // arrow
    other:    \'\u00b7\',
  };''',
        '''  const typeIcon = {
    ethernet: \'\u2500\u2500\',  // box drawing horizontal
    fiber:    \'\u2248\',        // wave (suggestive of light/optical)
    wifi:     \'\u29B0\',        // empty set / signal indicator
    power:    \'\u26a1\',        // lightning bolt
    usb:      \'\u21cc\',        // double arrow
    console:  \'\u2192\',        // arrow
    other:    \'\u00b7\',
  };'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 4. Topology web view: add WiFi to the speed-by-type logic in
    # the edge animation tick handler. WiFi animates slowly (suggesting
    # variable signal) at speed=0.18, similar to console.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''      // Speed varies by connection type
      let speed = 0.3;
      if(d.connection_type === \'fiber\')   speed = 0.5;
      if(d.connection_type === \'power\')   speed = 0.15;
      if(d.connection_type === \'usb\')     speed = 0.4;
      if(d.connection_type === \'console\') speed = 0.2;''',
        '''      // Speed varies by connection type
      let speed = 0.3;
      if(d.connection_type === \'fiber\')   speed = 0.5;
      if(d.connection_type === \'wifi\')    speed = 0.18;
      if(d.connection_type === \'power\')   speed = 0.15;
      if(d.connection_type === \'usb\')     speed = 0.4;
      if(d.connection_type === \'console\') speed = 0.2;'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 5. CSS: add WiFi edge styling. Teal color, dashed pattern that
    # suggests intermittent wireless propagation. Anchor on existing
    # fiber edge style so wifi sits next to its conceptual cousin.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''.topo-edge-fiber    .topo-edge-line{stroke:#a872d6;stroke-dasharray:4,2}
.topo-edge-fiber    .topo-edge-flow{fill:#c89af0}''',
        '''.topo-edge-fiber    .topo-edge-line{stroke:#a872d6;stroke-dasharray:4,2}
.topo-edge-fiber    .topo-edge-flow{fill:#c89af0}
.topo-edge-wifi     .topo-edge-line{stroke:#3dc7c0;stroke-dasharray:1,4;opacity:.6}
.topo-edge-wifi     .topo-edge-flow{fill:#5fe0d8;opacity:.9}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 6. Drawer form fix: drop the 2-column grid that was clipping labels.
    # Single-column stack works in any drawer width. Also remove the dead
    # media query.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''.conn-form-row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.conn-form label{display:flex;flex-direction:column;gap:4px;font-family:\'DM Mono\',monospace;font-size:10px;letter-spacing:.07em;text-transform:uppercase;color:var(--hint)}
.conn-form input,.conn-form select{padding:7px 9px;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--text);font-size:13px;font-family:inherit;text-transform:none;letter-spacing:0}
.conn-form input:focus,.conn-form select:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 2px var(--blue-bg)}
.conn-form-actions{display:flex;gap:8px;justify-content:flex-end}
@media (max-width:768px){.conn-form-row{grid-template-columns:1fr}}''',
        '''.conn-form-row{display:flex;flex-direction:column;gap:10px}
.conn-form label{display:flex;flex-direction:column;gap:4px;font-family:\'DM Mono\',monospace;font-size:10px;letter-spacing:.07em;text-transform:uppercase;color:var(--hint);min-width:0}
.conn-form input,.conn-form select{padding:7px 9px;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--text);font-size:13px;font-family:inherit;text-transform:none;letter-spacing:0;width:100%;box-sizing:border-box}
.conn-form input:focus,.conn-form select:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 2px var(--blue-bg)}
.conn-form-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:4px}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 7. Bump version
    # ──────────────────────────────────────────────────────────────────────
    (
        'netwatch v3.19 - raspberry pi',
        'netwatch v3.20 - raspberry pi'
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

    if "renderTopologyWeb" not in content:
        print("ERROR: This patch requires patch_topology_web first.")
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

    if 'VERSION = "3.19"' in content:
        content = content.replace('VERSION = "3.19"', 'VERSION = "3.20"', 1)

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
    print("  2. Open any inventory drawer's connection form. Labels are")
    print("     no longer clipped (form fields stack vertically instead")
    print("     of squeezing into 2 columns).")
    print("  3. The Type dropdown now includes WiFi between Fiber and Power.")
    print("     Use it for things like Eero mesh links, wireless devices,")
    print("     access point connections, etc.")
    print("  4. WiFi connections in the topology web view render with a")
    print("     dashed teal line and slow-pulsing animation, distinguishing")
    print("     them from wired Ethernet at a glance.")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
