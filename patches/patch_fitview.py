#!/usr/bin/env python3
"""
netwatch patch: fit-to-view button + fullscreen reliability fix.

Two coordinated improvements:

1. **Fit-to-view button** that auto-frames the entire graph in the
   viewport. Calculates the bounding box of all nodes and applies a
   smooth zoom transform to fit them comfortably (with margin), then
   centers the result.

   Two surfaces:
   - Toolbar button alongside "Reset positions" (visible in normal
     web mode)
   - Floating button inside the canvas (visible only in fullscreen,
     since the toolbar is hidden there)

   Both call the same fitTopologyToView() function. Animated transition
   over ~500ms - feels deliberate, not jarring.

2. **Fullscreen reliability fix.** Previously, entering fullscreen
   re-rendered after a 50ms timeout - sometimes too short for the CSS
   transition to complete, leaving the SVG sized for the old viewport.
   Bumped to 350ms with a follow-up auto-fit so the graph always frames
   itself nicely on entry.

Together: entering fullscreen now reliably resizes the canvas AND
auto-fits the graph to fill the new space - no more "graph stuck in
the upper third" bug.

Must be applied AFTER patch_fullscreen_kiosk.py.

Run once from ~/netwatch/:
    python3 patch_fit_to_view.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_fitview.
Idempotent - safe to re-run.
"""

import os, shutil, sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_fitview"
SENTINEL = "fitTopologyToView"


PATCHES = [
    # ──────────────────────────────────────────────────────────────────────
    # 1. Add the fit-to-view button to the toolbar, alongside "Reset
    # positions". Same ghost-button styling for consistency.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''        <button class="topo-view-btn topo-view-btn-ghost" onclick="topologyResetPositions()">Reset positions</button>''',
        '''        <button class="topo-view-btn topo-view-btn-ghost" onclick="fitTopologyToView()" title="Frame the graph to fit the viewport">Fit to view</button>
        <button class="topo-view-btn topo-view-btn-ghost" onclick="topologyResetPositions()">Reset positions</button>'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 2. Add the floating fit button inside the canvas (visible only in
    # fullscreen via CSS). Place it next to the X close button.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''      <button class="topo-fs-close"
              title="Exit fullscreen (Esc)"
              onclick="exitTopologyFullscreen()">\u00D7</button>''',
        '''      <button class="topo-fs-fit"
              title="Fit graph to view"
              onclick="fitTopologyToView()">\u29C7</button>
      <button class="topo-fs-close"
              title="Exit fullscreen (Esc)"
              onclick="exitTopologyFullscreen()">\u00D7</button>'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 3. Implement fitTopologyToView. Compute node bounding box, calculate
    # the zoom transform that fits it into the viewport with margin,
    # apply with smooth transition.
    # We anchor on the existing topologyToggleUnconnected function.
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

// Fit the graph to the current viewport. Computes the bounding box of
// all nodes and applies a smooth zoom transform that frames them
// comfortably with padding. Triggered by the Fit-to-view button (visible
// in toolbar AND inside the canvas during fullscreen).
function fitTopologyToView(){
  if(!_topoSvg || !_topoZoom) return;
  // Pull node positions from the simulation. We need at least one node
  // to compute a bbox.
  const nodes = _topoSvg.selectAll('g.topo-node').data();
  if(!nodes || nodes.length === 0) return;

  // Compute bounding box of node centers. We add a per-node radius
  // estimate so labels and the node shapes themselves don't get clipped.
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  nodes.forEach(n => {
    if(n.x === undefined || n.y === undefined) return;
    // Approximate radius including label space below the node
    let r = 30;
    if(n.device_type === 'network' || n.device_type === 'ups') r = 70;
    else if(n.device_type === 'disk') r = 32;
    else if(n.device_type === 'peripheral') r = 28;
    else if(n.device_type === 'vm') r = 32;
    minX = Math.min(minX, n.x - r);
    minY = Math.min(minY, n.y - r);
    maxX = Math.max(maxX, n.x + r);
    maxY = Math.max(maxY, n.y + r);
  });
  if(!isFinite(minX)) return;

  const bboxW = maxX - minX;
  const bboxH = maxY - minY;
  const bboxCx = (minX + maxX) / 2;
  const bboxCy = (minY + maxY) / 2;

  // Get the SVG\'s actual rendered size from its DOM node
  const svgNode = _topoSvg.node();
  const svgRect = svgNode.getBoundingClientRect();
  const vpW = svgRect.width;
  const vpH = svgRect.height;
  if(vpW <= 0 || vpH <= 0) return;

  // Compute scale to fit, with margin (90% of viewport)
  const margin = 0.90;
  const scaleX = (vpW * margin) / bboxW;
  const scaleY = (vpH * margin) / bboxH;
  let scale = Math.min(scaleX, scaleY);
  // Clamp scale to the zoom\'s configured extent (0.1 to 4)
  scale = Math.max(0.15, Math.min(scale, 3));

  // Translate so the bbox center maps to the viewport center
  const tx = vpW / 2 - bboxCx * scale;
  const ty = vpH / 2 - bboxCy * scale;

  // Apply with a smooth transition. Use d3.zoom\'s transform helper.
  const d3Identity = d3.zoomIdentity.translate(tx, ty).scale(scale);
  _topoSvg.transition()
    .duration(550)
    .ease(d3.easeCubicInOut)
    .call(_topoZoom.transform, d3Identity);
}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 4. Reliability fix: bump the fullscreen-enter timeout from 50ms
    # to 350ms so the CSS transition completes before re-render. Also
    # auto-call fit-to-view after the re-render so the graph fills the
    # new space.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''function enterTopologyFullscreen(){
  if(_topoFullscreen) return;
  // Make sure we\'re actually in web mode - otherwise the button shouldn\'t
  // be active, but defend against edge cases anyway
  if(_topoView !== 'web') setTopoView('web');
  _topoFullscreen = true;
  document.body.classList.add('topo-fullscreen-active');
  // Re-render so the SVG picks up the new container dimensions and the
  // force simulation re-centers around the larger viewport
  setTimeout(() => {
    if(typeof renderTopologyWeb === 'function') renderTopologyWeb();
  }, 50);
}''',
        '''function enterTopologyFullscreen(){
  if(_topoFullscreen) return;
  // Make sure we\'re actually in web mode - otherwise the button shouldn\'t
  // be active, but defend against edge cases anyway
  if(_topoView !== 'web') setTopoView('web');
  _topoFullscreen = true;
  document.body.classList.add('topo-fullscreen-active');
  // Wait for the CSS transition to fully complete before re-rendering.
  // 350ms is comfortably past the 300ms transition duration. Then auto-fit
  // so the graph frames itself nicely in the new viewport.
  setTimeout(() => {
    if(typeof renderTopologyWeb === 'function') renderTopologyWeb();
    // Auto-fit after the simulation has had a moment to settle
    setTimeout(() => {
      if(typeof fitTopologyToView === 'function') fitTopologyToView();
    }, 250);
  }, 350);
}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 5. Same reliability fix on exit - the exit transition takes ~300ms
    # too, so wait properly before re-render.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''function exitTopologyFullscreen(){
  if(!_topoFullscreen) return;
  _topoFullscreen = false;
  document.body.classList.remove('topo-fullscreen-active');
  // Re-render so SVG resizes back down + sim recenters
  setTimeout(() => {
    if(typeof renderTopologyWeb === 'function') renderTopologyWeb();
  }, 50);
}''',
        '''function exitTopologyFullscreen(){
  if(!_topoFullscreen) return;
  _topoFullscreen = false;
  document.body.classList.remove('topo-fullscreen-active');
  // Wait for the exit transition to complete before re-rendering
  setTimeout(() => {
    if(typeof renderTopologyWeb === 'function') renderTopologyWeb();
  }, 350);
}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 6. CSS for the floating fit button (visible in fullscreen). Mirrors
    # the close-button styling but positioned to its left.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''.topo-fs-wordmark, .topo-fs-close{display:none}''',
        '''.topo-fs-wordmark, .topo-fs-close, .topo-fs-fit{display:none}'''
    ),

    (
        '''body.topo-fullscreen-active .topo-fs-close{
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
}''',
        '''body.topo-fullscreen-active .topo-fs-close{
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
body.topo-fullscreen-active .topo-fs-fit{
  display:flex;
  align-items:center;justify-content:center;
  position:absolute;
  top:14px;right:54px;  /* sits 8px left of the close button */
  width:32px;height:32px;
  background:rgba(20,22,28,.65);
  backdrop-filter:blur(8px);
  -webkit-backdrop-filter:blur(8px);
  border:1px solid rgba(255,255,255,.08);
  border-radius:6px;
  color:var(--muted);
  font-size:14px;line-height:1;
  cursor:pointer;
  z-index:10001;
  transition:all .15s;
  font-family:\'DM Mono\',monospace;
}
body.topo-fullscreen-active .topo-fs-fit:hover{
  background:rgba(40,42,50,.85);
  color:var(--text);
}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 7. Bump version
    # ──────────────────────────────────────────────────────────────────────
    (
        'netwatch v3.28 - raspberry pi',
        'netwatch v3.29 - raspberry pi'
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

    if "topo-fullscreen-active" not in content:
        print("ERROR: This patch requires patch_fullscreen_kiosk first.")
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

    if 'VERSION = "3.28"' in content:
        content = content.replace('VERSION = "3.28"', 'VERSION = "3.29"', 1)

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
    print("  2. Topology > Web mode. Look for the new 'Fit to view' button")
    print("     in the toolbar next to 'Reset positions'.")
    print("  3. Click 'Fit to view'. The graph smoothly zooms/pans to frame")
    print("     all nodes in the viewport with margin.")
    print("  4. Enter fullscreen. The graph now fills the viewport on entry")
    print("     (auto-fit applied). You'll also see a small floating fit")
    print("     button next to the X in the top-right corner if you ever")
    print("     need to re-fit manually.")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
