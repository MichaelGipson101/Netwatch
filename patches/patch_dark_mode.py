#!/usr/bin/env python3
"""
netwatch patch: dark mode toggle.

Adds a small theme toggle in the top-right nav with three states:
  - auto  (follow browser/OS preference)
  - light (force light)
  - dark  (force dark)

Preference is saved to localStorage and persists across page loads.
No server changes - pure frontend.

Run once from ~/netwatch/:
    python3 patch_dark_mode.py
    sudo systemctl restart netwatch

Backup saved to monitor.py.bak_darkmode.
Idempotent - safe to re-run.
"""

import os
import shutil
import sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_darkmode"
SENTINEL = "data-theme"  # presence means already patched


PATCHES = [
    # ──── 1. Version bump ────
    (
        'VERSION = "2.3"',
        'VERSION = "2.4"'
    ),

    # ──── 2. Extend :root CSS with dark-mode variable set ────
    (
        ''':root{
  --bg:#f5f4f1;--surface:#fff;--border:#e5e4e0;--border-light:#f0efe9;
  --text:#1a1a1a;--muted:#6b7280;--hint:#9ca3af;--subtle:#fafaf8;
  --green:#16a34a;--green-bg:#dcfce7;--green-text:#15803d;
  --red:#dc2626;--red-bg:#fee2e2;--red-text:#b91c1c;
  --amber:#d97706;--amber-bg:#fef3c7;--amber-text:#b45309;
  --blue:#2563eb;--blue-bg:#dbeafe;
}''',
        ''':root{
  --bg:#f5f4f1;--surface:#fff;--border:#e5e4e0;--border-light:#f0efe9;
  --text:#1a1a1a;--muted:#6b7280;--hint:#9ca3af;--subtle:#fafaf8;
  --green:#16a34a;--green-bg:#dcfce7;--green-text:#15803d;
  --red:#dc2626;--red-bg:#fee2e2;--red-text:#b91c1c;
  --amber:#d97706;--amber-bg:#fef3c7;--amber-text:#b45309;
  --blue:#2563eb;--blue-bg:#dbeafe;
}
[data-theme="dark"]{
  --bg:#0f0e0d;--surface:#1a1917;--border:#2a2825;--border-light:#232220;
  --text:#e8e6e0;--muted:#9b998f;--hint:#6b6962;--subtle:#1f1d1b;
  --green:#22c55e;--green-bg:#0c2518;--green-text:#4ade80;
  --red:#ef4444;--red-bg:#2a1515;--red-text:#f87171;
  --amber:#f59e0b;--amber-bg:#2a1f0a;--amber-text:#fbbf24;
  --blue:#3b82f6;--blue-bg:#0f1a2e;
}
@media (prefers-color-scheme: dark){
  [data-theme="auto"]{
    --bg:#0f0e0d;--surface:#1a1917;--border:#2a2825;--border-light:#232220;
    --text:#e8e6e0;--muted:#9b998f;--hint:#6b6962;--subtle:#1f1d1b;
    --green:#22c55e;--green-bg:#0c2518;--green-text:#4ade80;
    --red:#ef4444;--red-bg:#2a1515;--red-text:#f87171;
    --amber:#f59e0b;--amber-bg:#2a1f0a;--amber-text:#fbbf24;
    --blue:#3b82f6;--blue-bg:#0f1a2e;
  }
}'''
    ),

    # ──── 3. body background uses var(--bg) instead of fixed ────
    # This is already the case via the CSS above, but we need the <html>/<body>
    # element to actually have data-theme attached. The script block will do it.

    # ──── 4. Adjust the status dot glow colors (they use hardcoded hex) ────
    (
        '''.dot-up{background:var(--green);box-shadow:0 0 0 3px #dcfce7}
.dot-dn{background:var(--red);box-shadow:0 0 0 3px #fee2e2}
.dot-wt{background:var(--amber);box-shadow:0 0 0 3px #fef3c7}
.dot-idle{background:var(--hint);box-shadow:0 0 0 3px #f0efe9}''',
        '''.dot-up{background:var(--green);box-shadow:0 0 0 3px var(--green-bg)}
.dot-dn{background:var(--red);box-shadow:0 0 0 3px var(--red-bg)}
.dot-wt{background:var(--amber);box-shadow:0 0 0 3px var(--amber-bg)}
.dot-idle{background:var(--hint);box-shadow:0 0 0 3px var(--subtle)}'''
    ),

    # ──── 5. down-row tint - use a subtle token that adapts ────
    (
        '''.row.down-row{background:#fffbfb}
.row.down-row:hover{background:#fff5f5}''',
        '''.row.down-row{background:var(--red-bg)}
.row.down-row:hover{background:var(--red-bg);filter:brightness(1.05)}'''
    ),

    # ──── 6. err-banner: already uses vars ────
    # Modal overlay: check dark mode works
    (
        '''.modal-overlay{position:fixed;inset:0;background:rgba(26,26,26,.5);display:none;align-items:flex-start;justify-content:center;z-index:50;overflow-y:auto;padding:40px 16px}''',
        '''.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:flex-start;justify-content:center;z-index:50;overflow-y:auto;padding:40px 16px}'''
    ),

    # ──── 7. Invalid input tint - adapt via red-bg ────
    (
        '''.edit-row input.invalid{border-color:var(--red);background:#fff5f5}''',
        '''.edit-row input.invalid{border-color:var(--red);background:var(--red-bg)}'''
    ),

    # ──── 8. Add toggle button styles ────
    (
        '''.btn-ghost{border-color:transparent;background:transparent}
.btn-ghost:hover{background:var(--subtle)}''',
        '''.btn-ghost{border-color:transparent;background:transparent}
.btn-ghost:hover{background:var(--subtle)}
.theme-toggle{display:inline-flex;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:2px;gap:0}
.theme-toggle button{background:transparent;border:none;padding:4px 8px;border-radius:6px;cursor:pointer;color:var(--hint);display:inline-flex;align-items:center;justify-content:center;transition:all .15s;font-family:inherit}
.theme-toggle button:hover{color:var(--text)}
.theme-toggle button.active{background:var(--subtle);color:var(--text)}
.theme-toggle svg{width:14px;height:14px;display:block}'''
    ),

    # ──── 9. Inject the toggle into the nav ────
    (
        '''    <div class="nav-right">
      <button class="btn" onclick="openEditor()">Edit hosts</button>
      <div class="live-pip" id="pip"><span class="live-dot"></span><span>live</span></div>
      <span id="clock">-</span>
    </div>''',
        '''    <div class="nav-right">
      <div class="theme-toggle" id="theme-toggle" role="group" aria-label="Theme">
        <button data-theme-btn="light" title="Light" aria-label="Light theme"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg></button>
        <button data-theme-btn="auto" title="Auto" aria-label="Auto theme"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 3v18" fill="currentColor"/><path d="M12 3a9 9 0 0 1 0 18" fill="currentColor" stroke="none"/></svg></button>
        <button data-theme-btn="dark" title="Dark" aria-label="Dark theme"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg></button>
      </div>
      <button class="btn" onclick="openEditor()">Edit hosts</button>
      <div class="live-pip" id="pip"><span class="live-dot"></span><span>live</span></div>
      <span id="clock">-</span>
    </div>'''
    ),

    # ──── 10. Theme init + toggle script at the very start of <script> ────
    (
        '''<script>
const REFRESH = 5000;
let lastOk = true;''',
        '''<script>
(function initTheme(){
  const saved = localStorage.getItem('nw-theme') || 'auto';
  document.documentElement.setAttribute('data-theme', saved);
})();
function setTheme(mode){
  document.documentElement.setAttribute('data-theme', mode);
  localStorage.setItem('nw-theme', mode);
  document.querySelectorAll('#theme-toggle button').forEach(b => {
    b.classList.toggle('active', b.dataset.themeBtn === mode);
  });
}
document.addEventListener('DOMContentLoaded', () => {
  const current = localStorage.getItem('nw-theme') || 'auto';
  document.querySelectorAll('#theme-toggle button').forEach(b => {
    b.classList.toggle('active', b.dataset.themeBtn === current);
    b.addEventListener('click', () => setTheme(b.dataset.themeBtn));
  });
});
const REFRESH = 5000;
let lastOk = true;'''
    ),

    # ──── 11. body style: add data-theme attribute + adapt base background ────
    # The <body> tag doesn't have explicit bg; the body selector sets it
    (
        '''body{font-family:'DM Sans',sans-serif;background:#f5f4f1;color:#1a1a1a;font-size:14px;line-height:1.5}''',
        '''html{background:var(--bg)}
body{font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text);font-size:14px;line-height:1.5;transition:background .2s,color .2s}'''
    ),
]


def main():
    if not os.path.isfile(TARGET):
        print(f"ERROR: {TARGET} not found in current directory.")
        print("Run this from your ~/netwatch/ directory.")
        sys.exit(1)

    content = open(TARGET).read()

    if SENTINEL in content:
        print(f"NOTE: '{SENTINEL}' found in {TARGET} -- dark mode patch already applied.")
        print("Exiting without changes.")
        sys.exit(0)

    shutil.copy2(TARGET, BACKUP)
    print(f"[OK] Backed up {TARGET} -> {BACKUP}")

    applied = 0
    for i, (old, new) in enumerate(PATCHES, 1):
        count = content.count(old)
        if count == 0:
            print(f"[WARN] Patch #{i}: target not found, skipping.")
            continue
        if count > 1:
            print(f"[FAIL] Patch #{i}: target matches {count}x - aborting.")
            shutil.copy2(BACKUP, TARGET)
            sys.exit(1)
        content = content.replace(old, new, 1)
        applied += 1

    open(TARGET, "w").write(content)
    print(f"[OK] Applied {applied} patches.")
    print()
    print("Next: sudo systemctl restart netwatch")
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
