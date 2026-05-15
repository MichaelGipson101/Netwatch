#!/usr/bin/env python3
"""
netwatch patch: fix inventory editor form styling.

The "New inventory record" / "Edit inventory record" modal had broken layout:
labels rendered inline with their inputs (rather than stacked above), inputs
had no styling, and the .full class had no effect outside the row-extra
context where it was originally defined.

Fix: scope a small set of styles to .inv-edit-form (a new class on the
form's grid container) so the inventory editor matches the rest of the UI.

Must be applied AFTER patch_inventory.py.

Run once from ~/netwatch/:
    python3 patch_inventory_styling.py
    sudo systemctl restart netwatch

Backup saved to monitor.py.bak_invstyling.
Idempotent - safe to re-run.
"""

import os
import shutil
import sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_invstyling"
SENTINEL = "inv-edit-form"  # presence means already patched


PATCHES = [
    # ──── 1. Add CSS for inventory editor form. We anchor on the existing
    # inventory CSS section (the one starting with /* Inventory */).
    (
        '''/* Inventory drawer specs grid */''',
        '''/* Inventory editor form */
.inv-edit-form{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.inv-edit-form label{display:flex;flex-direction:column;gap:5px;font-size:11px;font-family:'DM Mono',monospace;letter-spacing:.05em;color:var(--hint);text-transform:uppercase}
.inv-edit-form label.full{grid-column:1 / -1}
.inv-edit-form input,.inv-edit-form textarea{width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:13px;background:var(--surface);color:var(--text);text-transform:none;letter-spacing:0;box-sizing:border-box}
.inv-edit-form input:focus,.inv-edit-form textarea:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px var(--blue-bg)}
.inv-edit-form textarea{resize:vertical;min-height:60px;font-family:'DM Sans',sans-serif}
.inv-edit-form input::placeholder,.inv-edit-form textarea::placeholder{color:var(--hint);text-transform:none;letter-spacing:0;font-family:'DM Sans',sans-serif}

/* Inventory drawer specs grid */'''
    ),

    # ──── 2. Apply .inv-edit-form class to the editor's grid div.
    # Replace the inline-styled <div style="display:grid;..."> with the class.
    (
        '''      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
        <label class="full" style="grid-column:1/-1">System name <span style="color:var(--red)">*</span><input type="text" class="inv-f-system" required></label>''',
        '''      <div class="inv-edit-form">
        <label class="full">System name <span style="color:var(--red);text-transform:none;letter-spacing:0">*</span><input type="text" class="inv-f-system" required></label>'''
    ),

    # ──── 3. Drop the redundant inline style on the Notes label
    (
        '''        <label class="full" style="grid-column:1/-1">Notes<textarea class="inv-f-notes" placeholder="Anything else worth remembering"></textarea></label>''',
        '''        <label class="full">Notes<textarea class="inv-f-notes" placeholder="Anything else worth remembering"></textarea></label>'''
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

    if "class InventoryDB" not in content:
        print("ERROR: This patch requires patch_inventory first.")
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
    print("  2. Click 'Inventory' tab -> '+ Add' to verify form looks right.")
    print("  3. Click any existing inventory row's 'Edit record' button to")
    print("     confirm it works for editing too.")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
