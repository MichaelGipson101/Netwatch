#!/usr/bin/env python3
"""
netwatch patch: make idle/dead edges actually visible.

The dead edge styling (for connections involving an offline or idle
device) was using --border-light (#232220 in dark mode) at 0.35 opacity.
Against the #0f0e0d background, that's effectively invisible -
especially on bright displays or in kiosk lighting conditions.

The intent was right (de-emphasize dead edges so active ones stand out)
but the execution went too far. You couldn't actually FIND the connection
between an idle laptop and the router it's associated with.

The fix:
  - Use --muted color instead of --border-light. Much more visible
    while still clearly muted vs. active edges.
  - Bump opacity from 0.35 to 0.45.
  - Restore a dashed pattern (3,3) so the line reads as an intentional
    connection rather than a faint smudge. Visually distinct from
    active solid lines, AND from the active dashed types (wifi, fiber,
    power) which use different dash patterns.

Active edges still have full color + flow animation. Dead edges still
have no flow dots and reduced opacity. The hierarchy is preserved -
they're just findable now.

Must be applied AFTER patch_polish_drawer_topo.py.

Run once from ~/netwatch/:
    python3 patch_dead_edge_visibility.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_deadedge.
Idempotent - safe to re-run.
"""

import os, shutil, sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_deadedge"
SENTINEL = "/* Dead edge: muted but findable"


PATCHES = [
    # ──────────────────────────────────────────────────────────────────────
    # Replace the dead edge rule. Brighter color, moderate opacity,
    # subtle dash pattern.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''.topo-edge-dead     .topo-edge-line{stroke:var(--border-light) !important;opacity:.35;stroke-dasharray:none !important;transition:stroke .3s,opacity .3s}
.topo-edge-dead     .topo-edge-flow{opacity:0;transition:opacity .3s}''',
        '''/* Dead edge: muted but findable. Previously used border-light at .35
   opacity which was nearly invisible on dark backgrounds. Now uses
   muted-color with a short dash pattern so the line reads as an
   intentional muted connection rather than a faint smudge. */
.topo-edge-dead     .topo-edge-line{stroke:var(--muted) !important;opacity:.45;stroke-dasharray:3,3 !important;transition:stroke .3s,opacity .3s}
.topo-edge-dead     .topo-edge-flow{opacity:0;transition:opacity .3s}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # Bump version
    # ──────────────────────────────────────────────────────────────────────
    (
        'netwatch v3.32 - raspberry pi',
        'netwatch v3.33 - raspberry pi'
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

    if "topo-loading-pulse" not in content:
        print("ERROR: This patch requires patch_polish_drawer_topo first.")
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

    if 'VERSION = "3.32"' in content:
        content = content.replace('VERSION = "3.32"', 'VERSION = "3.33"', 1)

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
    print("Idle/dead edges should now be clearly findable:")
    print("  - Brighter color (muted instead of border-light)")
    print("  - Moderate opacity (.45 instead of .35)")
    print("  - Subtle dash pattern so they read as intentional connections")
    print()
    print("Active edges still have full color + animation, so the visual")
    print("hierarchy of 'this is alive, this is offline' stays clear.")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
