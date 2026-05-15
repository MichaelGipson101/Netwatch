#!/usr/bin/env python3
"""
netwatch patch: fullscreen kiosk mode for the web view.

Adds a fullscreen toggle button next to the Cards/Web switcher. Click
it, and the topology web view expands to fill the entire browser
window: header, tabs, all dashboard chrome hidden. A Netwatch wordmark
appears centered at the top, just enough branding to know what you\'re
looking at without competing with the graph.

Designed for kiosk use - point a monitor at your homelab dashboard, hit
fullscreen, walk away. The graph fills the screen with maximum visual
impact.

Exit: click the X button in the top-right corner, or press Esc.

The fullscreen state does NOT persist across page loads (localStorage).
Refreshing the page brings you back to the normal layout. This is
deliberate - fullscreen should be a deliberate active choice, not
something that surprises you on a fresh load.

Implementation: CSS-only fullscreen (the topo-web container becomes
position:fixed covering the viewport). NOT the browser fullscreen API,
which has UX quirks. If you want OS-level true fullscreen too, your
browser's F11 still works on top of this.

Must be applied AFTER patch_vm_type_followup.py.

Run once from ~/netwatch/:
    python3 patch_fullscreen_kiosk.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_fullscreen.
Idempotent - safe to re-run.
"""

import os, shutil, sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_fullscreen"
SENTINEL = "topo-fullscreen-active"


PATCHES = [
    # ──────────────────────────────────────────────────────────────────────
    # 1. Add the fullscreen button to the toolbar. Sits inside the
    # topo-view-toggle group so it visually anchors next to Cards/Web.
    # The icon is a simple unicode "expand" arrow set; we wrap it in
    # a span so the title attribute provides hover-tooltip context.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''      <div class="topo-view-toggle">
        <button class="topo-view-btn" id="topo-view-cards" onclick="setTopoView('cards')">Cards</button>
        <button class="topo-view-btn" id="topo-view-web" onclick="setTopoView('web')">Web</button>
      </div>''',
        '''      <div class="topo-view-toggle">
        <button class="topo-view-btn" id="topo-view-cards" onclick="setTopoView('cards')">Cards</button>
        <button class="topo-view-btn" id="topo-view-web" onclick="setTopoView('web')">Web</button>
      </div>
      <button class="topo-view-btn topo-view-btn-ghost topo-fullscreen-btn"
              id="topo-fullscreen-btn"
              title="Enter fullscreen kiosk view (Esc to exit)"
              onclick="enterTopologyFullscreen()">
        <span class="topo-fs-icon">\u26F6</span>
        <span class="topo-fs-label">Fullscreen</span>
      </button>'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 2. Add the fullscreen overlay elements inside the topo-web container.
    # The wordmark sits at the top, the close button in the top-right.
    # Both render only when the .topo-fullscreen-active class is on the body.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''    <div class="topo-web" id="topo-web" style="display:none">
      <div class="topo-web-svg-host" id="topo-web-svg-host"></div>''',
        '''    <div class="topo-web" id="topo-web" style="display:none">
      <!-- Fullscreen overlay elements - only visible when topo-fullscreen-active
           class is on the body. The wordmark + close button float over the SVG. -->
      <div class="topo-fs-wordmark" aria-hidden="true">netwatch</div>
      <button class="topo-fs-close"
              title="Exit fullscreen (Esc)"
              onclick="exitTopologyFullscreen()">\u00D7</button>
      <div class="topo-web-svg-host" id="topo-web-svg-host"></div>'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 3. The JS: enter/exit functions. Bind Esc to exit. Resize SVG/sim
    # on transition. Also: hide the fullscreen button when not in web
    # mode (it doesn\'t apply to cards mode).
    #
    # We anchor on the existing topologyToggleUnconnected which is the
    # last function in the topology-web JS section.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''function topologyToggleUnconnected(checked){
  _topoIncludeUnconnected = checked;
  if(_topoView === 'web') renderTopologyWeb();
}''',
        '''function topologyToggleUnconnected(checked){
  _topoIncludeUnconnected = checked;
  if(_topoView === 'web') renderTopologyWeb();
}

// ── Fullscreen kiosk mode ──────────────────────────────────────────────
let _topoFullscreen = false;

function enterTopologyFullscreen(){
  if(_topoFullscreen) return;
  // Make sure we\'re actually in web mode - otherwise the button shouldn\'t
  // be active, but defend against edge cases anyway
  if(_topoView !== \'web\') setTopoView(\'web\');
  _topoFullscreen = true;
  document.body.classList.add(\'topo-fullscreen-active\');
  // Re-render so the SVG picks up the new container dimensions and the
  // force simulation re-centers around the larger viewport
  setTimeout(() => {
    if(typeof renderTopologyWeb === \'function\') renderTopologyWeb();
  }, 50);
}

function exitTopologyFullscreen(){
  if(!_topoFullscreen) return;
  _topoFullscreen = false;
  document.body.classList.remove(\'topo-fullscreen-active\');
  // Re-render so SVG resizes back down + sim recenters
  setTimeout(() => {
    if(typeof renderTopologyWeb === \'function\') renderTopologyWeb();
  }, 50);
}

// Esc to exit fullscreen. Ignored if any modal is open or if we\'re
// not actually in fullscreen.
document.addEventListener(\'keydown\', (ev) => {
  if(ev.key === \'Escape\' && _topoFullscreen){
    // Only intercept Esc if no modal is currently open. If a modal IS
    // open the Esc should close it, not the fullscreen.
    const anyModalOpen = document.querySelector(\'.modal-overlay.open, .drawer.open\');
    if(!anyModalOpen){
      exitTopologyFullscreen();
    }
  }
});'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 4. setTopoView: hide the fullscreen button when not in web mode.
    # Also: if user toggles AWAY from web mode while in fullscreen, exit
    # fullscreen automatically.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''function setTopoView(view){
  _topoView = view;
  localStorage.setItem('nw-topo-view', view);
  const cardsBtn = document.getElementById('topo-view-cards');
  const webBtn   = document.getElementById('topo-view-web');
  if(cardsBtn) cardsBtn.classList.toggle('active', view === 'cards');
  if(webBtn)   webBtn.classList.toggle('active', view === 'web');''',
        '''function setTopoView(view){
  _topoView = view;
  localStorage.setItem('nw-topo-view', view);
  const cardsBtn = document.getElementById('topo-view-cards');
  const webBtn   = document.getElementById('topo-view-web');
  if(cardsBtn) cardsBtn.classList.toggle('active', view === 'cards');
  if(webBtn)   webBtn.classList.toggle('active', view === 'web');
  // Fullscreen button only makes sense in web mode
  const fsBtn = document.getElementById('topo-fullscreen-btn');
  if(fsBtn) fsBtn.style.display = (view === 'web') ? '' : 'none';
  // If switching away from web mode while in fullscreen, drop fullscreen
  if(view !== 'web' && _topoFullscreen){
    exitTopologyFullscreen();
  }'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 5. CSS for the fullscreen state. Anchor on the existing topo-web
    # block so the new rules sit logically together.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''.topo-web{position:relative;background:var(--bg);border:1px solid var(--border);border-radius:12px;overflow:hidden;height:min(78vh,1000px);min-height:540px;transition:height .3s cubic-bezier(.4,0,.2,1)}''',
        '''.topo-web{position:relative;background:var(--bg);border:1px solid var(--border);border-radius:12px;overflow:hidden;height:min(78vh,1000px);min-height:540px;transition:height .3s cubic-bezier(.4,0,.2,1)}

/* Fullscreen button: minor tweak so the icon and label sit nicely */
.topo-fullscreen-btn{display:inline-flex;align-items:center;gap:6px;font-family:\'DM Sans\',sans-serif}
.topo-fs-icon{font-family:\'DM Mono\',monospace;font-size:13px;line-height:1}
.topo-fs-label{font-size:11px}
@media (max-width:600px){.topo-fs-label{display:none}}

/* Fullscreen overlay elements: hidden by default */
.topo-fs-wordmark, .topo-fs-close{display:none}

/* When fullscreen-active body class is on, the topo-web container
   covers the viewport. Everything else fades out. */
body.topo-fullscreen-active{overflow:hidden}
body.topo-fullscreen-active .topo-web{
  position:fixed;
  top:0;left:0;right:0;bottom:0;
  width:100vw;height:100vh;
  max-width:none;min-height:0;
  border-radius:0;border:none;
  z-index:9999;
  background:var(--bg);
}
/* Hide everything except the topo-web container during fullscreen.
   We use sibling selectors and hide the major page chrome. */
body.topo-fullscreen-active > nav,
body.topo-fullscreen-active .tabs,
body.topo-fullscreen-active .summary,
body.topo-fullscreen-active .topo-view-toolbar,
body.topo-fullscreen-active .problem-banner,
body.topo-fullscreen-active footer,
body.topo-fullscreen-active .nav-strip,
body.topo-fullscreen-active .header,
body.topo-fullscreen-active .view:not(#view-topology),
body.topo-fullscreen-active #view-topology > *:not(.topo-web){
  display:none !important;
}

/* Show the wordmark + close button only in fullscreen */
body.topo-fullscreen-active .topo-fs-wordmark{
  display:block;
  position:absolute;
  top:18px;
  left:50%;
  transform:translateX(-50%);
  font-family:\'DM Mono\',monospace;
  font-size:12px;
  letter-spacing:.18em;
  text-transform:lowercase;
  color:var(--muted);
  opacity:.7;
  z-index:10001;
  pointer-events:none;
  user-select:none;
}
body.topo-fullscreen-active .topo-fs-close{
  display:flex;
  align-items:center;justify-content:center;
  position:absolute;
  top:14px;right:14px;
  width:32px;height:32px;
  background:rgba(20,22,28,.65);
  backdrop-filter:blur(8px);
  -webkit-backdrop-filter:blur(8px);
  border:1px solid rgba(255,255,255,.08);
  border-radius:6px;
  color:var(--muted);
  font-size:18px;line-height:1;
  cursor:pointer;
  z-index:10001;
  transition:all .15s;
  font-family:\'DM Sans\',sans-serif;
}
body.topo-fullscreen-active .topo-fs-close:hover{
  background:rgba(40,42,50,.85);
  color:var(--text);
}

/* In fullscreen, the overlay metrics float over the larger canvas */
body.topo-fullscreen-active .topo-web-overlay-tl{top:50px;left:24px}
body.topo-fullscreen-active .topo-web-overlay-tr{top:50px;right:60px}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 6. Bump version
    # ──────────────────────────────────────────────────────────────────────
    (
        'netwatch v3.27 - raspberry pi',
        'netwatch v3.28 - raspberry pi'
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

    if '<option value="vm">' not in content:
        print("ERROR: This patch requires patch_vm_type_followup first.")
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

    if 'VERSION = "3.27"' in content:
        content = content.replace('VERSION = "3.27"', 'VERSION = "3.28"', 1)

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
    print("  2. Topology tab > Web mode. Look for the new 'Fullscreen' button")
    print("     next to the Cards/Web toggle.")
    print("  3. Click it. The graph fills the entire window. The 'netwatch'")
    print("     wordmark appears centered at the top. The X button at top-right")
    print("     exits, as does pressing Esc.")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
