#!/usr/bin/env python3
"""
netwatch patch: final polish pass.

A focused set of small wins across the surfaces I hadn't audited
during prior polish sessions. Theme: consistency. Lots of small
inconsistencies have accumulated as the app grew, and this round
cleans them up.

WHAT'S INCLUDED:

  1. Modal close buttons: 5 modals (login, setup, inventory editor,
     import XLSX, discover hosts) used a bare 'X' letter for close.
     Other surfaces (drawer, fullscreen, connection delete) use
     a proper multiplication sign (×) U+00D7. Now consistent: × everywhere.

  2. Modal close button styling: bumped to match the drawer close we
     already polished. Bigger touch target (34x34), proper hover state,
     consistent treatment.

  3. The host drawer's 'Open' primary link rendered '->>' as plain
     ASCII characters meant to look like an arrow. Replaced with
     a proper → character.

  4. The 'Quick links' extras section had the same '->>' issue baked
     into a different code path. Same fix.

  5. The hosts toolbar had an empty `<div></div>` on the left side
     serving no purpose. Removed.

  6. File input in the import modal was using browser-default styling
     which looks rough especially in dark mode. Added a styled wrapper
     so the file picker matches the app's overall aesthetic.

  7. Discover modal button layout: 'Scan now' was left-aligned while
     'Cancel' and 'Add selected' were right-aligned, which felt off.
     Now 'Scan now' is grouped with the action buttons on the right.

WHAT I CONSIDERED BUT DEFERRED:

  - Form input styling consistency (lots of inline styles, big scope)
  - Events tab filtering (real feature, separate session)
  - Theme toggle redesign (risky)
  - Inv editor uppercase label intensity (subjective)

Must be applied AFTER patch_legend_polish.py.

Run once from ~/netwatch/:
    python3 patch_final_polish.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_finalpolish.
Idempotent - safe to re-run.
"""

import os, shutil, sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_finalpolish"
SENTINEL = "modal-close-btn"


PATCHES = [
    # ──────────────────────────────────────────────────────────────────────
    # 1-5. Replace the 5 modal "X" close buttons with proper × character
    # AND give them a unified class so styling is consistent.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''      <button type="button" class="btn btn-ghost" onclick="closeLogin()">X</button>''',
        '''      <button type="button" class="modal-close-btn" onclick="closeLogin()" aria-label="Close">\u00D7</button>'''
    ),
    (
        '''      <button type="button" class="btn btn-ghost" onclick="closeInventoryEditor()">X</button>''',
        '''      <button type="button" class="modal-close-btn" onclick="closeInventoryEditor()" aria-label="Close">\u00D7</button>'''
    ),
    (
        '''      <button type="button" class="btn btn-ghost" onclick="closeImportModal()">X</button>''',
        '''      <button type="button" class="modal-close-btn" onclick="closeImportModal()" aria-label="Close">\u00D7</button>'''
    ),
    (
        '''      <button class="btn btn-ghost" onclick="closeEditor()">X</button>''',
        '''      <button class="modal-close-btn" onclick="closeEditor()" aria-label="Close">\u00D7</button>'''
    ),
    (
        '''      <button class="btn btn-ghost" onclick="closeDiscover()">X</button>''',
        '''      <button class="modal-close-btn" onclick="closeDiscover()" aria-label="Close">\u00D7</button>'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 6. Add the modal-close-btn CSS. Anchor on the existing drawer-close
    # CSS for proximity (they're conceptually paired).
    # ──────────────────────────────────────────────────────────────────────
    (
        '''.drawer-close{background:transparent;border:none;color:var(--hint);cursor:pointer;font-size:20px;padding:6px 12px;border-radius:6px;line-height:1;font-family:\'DM Sans\',sans-serif;min-width:34px;min-height:34px;display:flex;align-items:center;justify-content:center;transition:all .15s}
.drawer-close:hover{background:var(--subtle);color:var(--text)}
.drawer-close:active{transform:scale(.94)}''',
        '''.drawer-close{background:transparent;border:none;color:var(--hint);cursor:pointer;font-size:20px;padding:6px 12px;border-radius:6px;line-height:1;font-family:\'DM Sans\',sans-serif;min-width:34px;min-height:34px;display:flex;align-items:center;justify-content:center;transition:all .15s}
.drawer-close:hover{background:var(--subtle);color:var(--text)}
.drawer-close:active{transform:scale(.94)}

/* Modal close button - paired with drawer-close styling for consistency */
.modal-close-btn{background:transparent;border:none;color:var(--hint);cursor:pointer;font-size:20px;padding:6px 12px;border-radius:6px;line-height:1;font-family:\'DM Sans\',sans-serif;min-width:34px;min-height:34px;display:flex;align-items:center;justify-content:center;transition:all .15s}
.modal-close-btn:hover{background:var(--subtle);color:var(--text)}
.modal-close-btn:active{transform:scale(.94)}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 7. Host drawer "Open" primary link arrow: replace ->> with proper →
    # ──────────────────────────────────────────────────────────────────────
    (
        '''+ \'<span class="d-link-arrow">->></span></a>\';''',
        '''+ \'<span class="d-link-arrow">\u2192</span></a>\';'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 8. Same arrow fix in the extras list rendering
    # ──────────────────────────────────────────────────────────────────────
    (
        '''      + \'<span class="d-link-arrow" style="color:var(--hint);font-family:DM Mono,monospace">->></span>\'''',
        '''      + \'<span class="d-link-arrow" style="color:var(--hint);font-family:DM Mono,monospace">\u2192</span>\''''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 8b. Same arrow fix in the "Edit record" button (different class)
    # ──────────────────────────────────────────────────────────────────────
    (
        '''+ \'<span>Edit record</span><span class="arrow">->></span></button>\';''',
        '''+ \'<span>Edit record</span><span class="arrow">\u2192</span></button>\';'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 9. Hosts toolbar empty div - just remove it
    # ──────────────────────────────────────────────────────────────────────
    (
        '''    <div class="hosts-toolbar">
      <div></div>
      <div class="toolbar-right">
        <label class="compact-toggle"><input type="checkbox" id="compact-mode"> Compact</label>
      </div>
    </div>''',
        '''    <div class="hosts-toolbar">
      <div class="toolbar-right">
        <label class="compact-toggle"><input type="checkbox" id="compact-mode"> Compact</label>
      </div>
    </div>'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 10. File input styling - replace browser default with custom look.
    # We anchor on the existing import modal file input.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''      <input type="file" id="import-file" accept=".xlsx,.xlsm">''',
        '''      <label class="file-input-wrap">
        <input type="file" id="import-file" accept=".xlsx,.xlsm">
        <span class="file-input-btn">Choose file</span>
        <span class="file-input-name" id="import-file-name">No file selected</span>
      </label>'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 11. Wire up the file input to display the selected filename. We
    # anchor on the existing submitImport function and add a setup
    # near it. Actually simpler: just use an inline addEventListener
    # via onchange on the input itself.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''        <input type="file" id="import-file" accept=".xlsx,.xlsm">''',
        '''        <input type="file" id="import-file" accept=".xlsx,.xlsm" onchange="document.getElementById(\'import-file-name\').textContent = this.files.length ? this.files[0].name : \'No file selected\'">'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 12. CSS for the file input styled wrapper. Anchor on the import
    # modal CSS for proximity.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''.problem-banner.show{display:block;animation:slideIn .25s ease-out}''',
        '''.problem-banner.show{display:block;animation:slideIn .25s ease-out}

/* Styled file input - hides browser default, shows a clean button + filename */
.file-input-wrap{display:flex;align-items:center;gap:10px;padding:0;cursor:pointer;margin:8px 0}
.file-input-wrap input[type="file"]{position:absolute;left:-9999px;opacity:0;pointer-events:none}
.file-input-btn{display:inline-flex;align-items:center;padding:7px 14px;background:var(--surface);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px;font-weight:500;cursor:pointer;transition:all .15s}
.file-input-wrap:hover .file-input-btn{border-color:var(--hint);background:var(--subtle)}
.file-input-name{color:var(--muted);font-size:12px;font-family:\'DM Mono\',monospace;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 13. Discover modal: regroup buttons so 'Scan now' sits with the
    # action buttons on the right.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''    <div class="modal-foot">
      <button class="btn" onclick="startDiscover()" id="discover-scan-btn">Scan now</button>
      <div style="display:flex;gap:8px">
        <button class="btn" onclick="closeDiscover()">Cancel</button>
        <button class="btn btn-primary" onclick="addDiscovered()" id="discover-add-btn" disabled>Add selected</button>
      </div>
    </div>''',
        '''    <div class="modal-foot">
      <button class="btn" onclick="closeDiscover()">Cancel</button>
      <div style="display:flex;gap:8px;margin-left:auto">
        <button class="btn" onclick="startDiscover()" id="discover-scan-btn">Scan now</button>
        <button class="btn btn-primary" onclick="addDiscovered()" id="discover-add-btn" disabled>Add selected</button>
      </div>
    </div>'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 14. Bump version
    # ──────────────────────────────────────────────────────────────────────
    (
        'netwatch v3.35 - raspberry pi',
        'netwatch v3.36 - raspberry pi'
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

    if "topo-legend-svg-line" not in content:
        print("ERROR: This patch requires patch_legend_polish first.")
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

    if 'VERSION = "3.35"' in content:
        content = content.replace('VERSION = "3.35"', 'VERSION = "3.36"', 1)

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
    print("Polish landed across these surfaces:")
    print("  - All modals: consistent \u00D7 close button (was: bare 'X' letter)")
    print("  - Modal close buttons: proper touch targets, hover states")
    print("  - Host drawer 'Open' link: \u2192 instead of '->>'")
    print("  - Hosts toolbar: removed empty placeholder div")
    print("  - Import modal file input: proper styled button + filename")
    print("  - Discover modal: 'Scan now' grouped with action buttons")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
