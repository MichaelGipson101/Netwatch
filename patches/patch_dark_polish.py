#!/usr/bin/env python3
"""
netwatch patch: dark mode contrast polish.

Fixes a contrast bug where the URL text and arrow inside primary-styled
buttons (the "Open" quick-link button, the "Wake this device" button) used
hardcoded semi-transparent white. That worked fine in light mode (dark
button -> white-ish text) but became invisible in dark mode (light button
-> white-ish text on light background).

Same fix for the hover state, which flipped to pure #000 in both modes
instead of just darkening relative to the current theme.

Changes:
  - Adds --text-inverse-muted / --text-inverse-hint tokens that flip
    correctly with the theme
  - Replaces all hardcoded rgba(255,255,255,...) inside primary buttons
    with these tokens
  - Replaces background:#000 hover with a theme-aware "darker" variant

Must be applied AFTER patch_auth.py.

Run once from ~/netwatch/:
    python3 patch_dark_polish.py
    sudo systemctl restart netwatch

Backup saved to monitor.py.bak_darkpolish.
Idempotent - safe to re-run.
"""

import os
import shutil
import sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_darkpolish"
SENTINEL = "--text-inverse-muted"  # presence means already patched


PATCHES = [
    # ──── 1. Add new theme tokens to :root (light mode)
    (
        '''  --green:#16a34a;--green-bg:#dcfce7;--green-text:#15803d;--green-soft:#ecfdf5;
  --red:#dc2626;--red-bg:#fee2e2;--red-text:#b91c1c;--red-soft:#fef2f2;
  --amber:#d97706;--amber-bg:#fef3c7;--amber-text:#b45309;
  --blue:#2563eb;--blue-bg:#dbeafe;
}
[data-theme="dark"]{''',
        '''  --green:#16a34a;--green-bg:#dcfce7;--green-text:#15803d;--green-soft:#ecfdf5;
  --red:#dc2626;--red-bg:#fee2e2;--red-text:#b91c1c;--red-soft:#fef2f2;
  --amber:#d97706;--amber-bg:#fef3c7;--amber-text:#b45309;
  --blue:#2563eb;--blue-bg:#dbeafe;
  --primary-hover:#000;
  --text-inverse-muted:rgba(255,255,255,.7);
  --text-inverse-hint:rgba(255,255,255,.55);
  --text-inverse-bg:rgba(255,255,255,.2);
}
[data-theme="dark"]{'''
    ),

    # ──── 2. Add the dark-theme variants of those tokens (data-theme="dark")
    (
        '''  --amber:#f59e0b;--amber-bg:#2a1f0a;--amber-text:#fbbf24;
  --blue:#3b82f6;--blue-bg:#0f1a2e;
}
@media (prefers-color-scheme: dark){
  [data-theme="auto"]{''',
        '''  --amber:#f59e0b;--amber-bg:#2a1f0a;--amber-text:#fbbf24;
  --blue:#3b82f6;--blue-bg:#0f1a2e;
  --primary-hover:#fff;
  --text-inverse-muted:rgba(15,14,13,.7);
  --text-inverse-hint:rgba(15,14,13,.55);
  --text-inverse-bg:rgba(15,14,13,.2);
}
@media (prefers-color-scheme: dark){
  [data-theme="auto"]{'''
    ),

    # ──── 3. Same in the auto-dark @media block
    (
        '''    --amber:#f59e0b;--amber-bg:#2a1f0a;--amber-text:#fbbf24;
    --blue:#3b82f6;--blue-bg:#0f1a2e;
  }
}''',
        '''    --amber:#f59e0b;--amber-bg:#2a1f0a;--amber-text:#fbbf24;
    --blue:#3b82f6;--blue-bg:#0f1a2e;
    --primary-hover:#fff;
    --text-inverse-muted:rgba(15,14,13,.7);
    --text-inverse-hint:rgba(15,14,13,.55);
    --text-inverse-bg:rgba(15,14,13,.2);
  }
}'''
    ),

    # ──── 4. .btn-primary hover
    (
        '''.btn-primary:hover{background:#000;border-color:#000}''',
        '''.btn-primary:hover{background:var(--primary-hover);border-color:var(--primary-hover)}'''
    ),

    # ──── 5. .d-action-btn hover
    (
        '''.d-action-btn:hover{background:#000;border-color:#000}''',
        '''.d-action-btn:hover{background:var(--primary-hover);border-color:var(--primary-hover)}'''
    ),

    # ──── 6. .d-action-btn arrow
    (
        '''.d-action-btn .arrow{color:rgba(255,255,255,.6);font-size:14px}''',
        '''.d-action-btn .arrow{color:var(--text-inverse-hint);font-size:14px}'''
    ),

    # ──── 7. .d-link-primary hover
    (
        '''.d-link-primary:hover{background:#000;border-color:#000}''',
        '''.d-link-primary:hover{background:var(--primary-hover);border-color:var(--primary-hover)}'''
    ),

    # ──── 8. .d-link-primary URL text (the buggy one in your screenshot)
    (
        '''.d-link-primary .d-link-url{font-family:'DM Mono',monospace;font-size:11px;font-weight:400;color:rgba(255,255,255,.7);margin-left:8px}''',
        '''.d-link-primary .d-link-url{font-family:'DM Mono',monospace;font-size:11px;font-weight:400;color:var(--text-inverse-muted);margin-left:8px}'''
    ),

    # ──── 9. .d-link-primary arrow
    (
        '''.d-link-primary .d-link-arrow{color:rgba(255,255,255,.6);font-size:14px;font-family:'DM Mono',monospace}''',
        '''.d-link-primary .d-link-arrow{color:var(--text-inverse-hint);font-size:14px;font-family:'DM Mono',monospace}'''
    ),

    # ──── 9b. .tab.active .tab-count: the tiny count badge on the active tab.
    # Same bug pattern - white-ish bg works on dark tab (light mode) but is
    # unreadable on light tab (dark mode). Use a theme-aware token.
    (
        '''.tab.active .tab-count{background:rgba(255,255,255,.2);color:var(--surface)}''',
        '''.tab.active .tab-count{background:var(--text-inverse-bg);color:var(--surface)}'''
    ),

    # ──── 10. Footer version bump
    (
        '<span>netwatch v3.9 - raspberry pi</span>',
        '<span>netwatch v3.10 - raspberry pi</span>'
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

    if "class AuthManager" not in content:
        print("ERROR: This patch requires patch_auth first.")
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

    if 'VERSION = "3.9"' in content:
        content = content.replace('VERSION = "3.9"', 'VERSION = "3.10"', 1)

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
    print("  2. Click any host - the 'Open' button URL is now readable in dark mode")
    print("  3. Hover the Open button - it darkens (light mode) or lightens (dark mode)")
    print("     instead of jarringly flipping to pure black")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
