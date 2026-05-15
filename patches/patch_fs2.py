#!/usr/bin/env python3
"""
netwatch patch: fix fullscreen height being capped at 82vh by the
nw-topo-web CSS rule.

THE BUG (which earlier patches did not catch):

When you enter fullscreen, the body gets BOTH classes: 'nw-topo-web'
(because you're in web mode) AND 'topo-fullscreen-active'. Two CSS
rules then target .topo-web with equal specificity:

    body.topo-fullscreen-active .topo-web { height: 100vh; ... }
    body.nw-topo-web .topo-web            { height: min(82vh,1100px); }

Equal specificity means the rule that comes LATER in the stylesheet
wins. The nw-topo-web rule appears later (it was added in the focal-
point polish patch, AFTER the fullscreen patch). So in fullscreen mode,
the topo-web container is capped at 82vh - leaving an 18% dead band
at the bottom of the screen with no graph in it.

That dead band is exactly what the screenshot showed: a black strip
along the bottom of the viewport in fullscreen mode.

THE FIX:

Add !important to the fullscreen rule's height + dimensions. This is
exactly what !important is for - a more specific situation overriding
a more general one. The fullscreen rule now wins regardless of source
order or future CSS additions.

Result: in fullscreen mode, the graph fills the actual full viewport
height, edge to edge, no dead band at the bottom.

Must be applied AFTER patch_fullscreen_resize.py.

Run once from ~/netwatch/:
    python3 patch_fullscreen_height_fix.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_fsheight.
Idempotent - safe to re-run.
"""

import os, shutil, sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_fsheight"
SENTINEL = "height:100vh !important"


PATCHES = [
    # ──────────────────────────────────────────────────────────────────────
    # Override the height/width on the fullscreen rule with !important
    # so it always wins against other .topo-web rules regardless of
    # source order.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''body.topo-fullscreen-active .topo-web{
  position:fixed;
  top:0;left:0;right:0;bottom:0;
  width:100vw;height:100vh;
  max-width:none;min-height:0;
  border-radius:0;border:none;
  z-index:9999;
  background:var(--bg);
}''',
        '''body.topo-fullscreen-active .topo-web{
  position:fixed !important;
  top:0 !important;left:0 !important;right:0 !important;bottom:0 !important;
  width:100vw !important;height:100vh !important;
  max-width:none !important;min-height:0 !important;
  max-height:none !important;
  border-radius:0;border:none;
  z-index:9999;
  background:var(--bg);
}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # Bump version
    # ──────────────────────────────────────────────────────────────────────
    (
        'netwatch v3.30 - raspberry pi',
        'netwatch v3.31 - raspberry pi'
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

    if "_topoResizeObserver" not in content:
        print("ERROR: This patch requires patch_fullscreen_resize first.")
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

    if 'VERSION = "3.30"' in content:
        content = content.replace('VERSION = "3.30"', 'VERSION = "3.31"', 1)

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
    print("  2. Topology > Web > Fullscreen. The graph should now fill")
    print("     the entire viewport, edge to edge, with no dead band at")
    print("     the bottom.")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
