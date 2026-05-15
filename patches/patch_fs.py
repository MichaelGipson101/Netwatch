#!/usr/bin/env python3
"""
netwatch patch: fix SVG letterboxing in fullscreen + add resize handler.

THE ACTUAL BUG (which earlier patches didn't fix):

  When the topo-web container resizes (entering fullscreen, exiting
  fullscreen, or just resizing the browser window), the SVG viewBox
  stays at its ORIGINAL dimensions. With preserveAspectRatio='xMidYMid
  meet', this causes letterboxing: the SVG content is fitted into the
  new viewport while preserving its old aspect ratio, leaving black
  bars above/below or beside the graph. When nodes simulate to positions
  outside the old viewBox, they render outside the 'fitted' area and
  appear to disappear.

THE FIX:

  1. On every render, set the SVG viewBox to actually match the
     container dimensions. No more aspect-ratio mismatch.
  2. Add a ResizeObserver that watches the topo-web container. Whenever
     it resizes (fullscreen toggle, window resize, etc.), update the
     SVG viewBox AND the force simulation's center force to use the
     new dimensions, then gently re-warm the simulation so it adapts.
  3. The fullscreen enter/exit logic stays largely the same but no
     longer needs the heavy 350ms timeout - the resize observer handles
     the dimension change cleanly. We keep a smaller 200ms delay
     before auto-fit so the resize has time to settle.

Result: the graph fills the entire fullscreen viewport. Nodes can be
dragged anywhere in the visible canvas without disappearing. Browser
window resizes also work cleanly now.

Must be applied AFTER patch_fit_to_view.py.

Run once from ~/netwatch/:
    python3 patch_fullscreen_resize.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_resize.
Idempotent - safe to re-run.
"""

import os, shutil, sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_resize"
SENTINEL = "_topoResizeObserver"


PATCHES = [
    # ──────────────────────────────────────────────────────────────────────
    # 1. Add a module-level reference for the resize observer so we can
    # disconnect/recreate it cleanly on each render.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''let _topoSimulation = null;
let _topoSvg = null;''',
        '''let _topoSimulation = null;
let _topoSvg = null;
let _topoResizeObserver = null;'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 2. After the SVG is created, set up a ResizeObserver on the
    # container. When the container resizes, update the viewBox + center
    # force + restart the simulation alpha gently.
    # We anchor on the line that creates the zoom variable.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  _topoSvg = svg;

  // Zoomable wrapper
  const zoomG = svg.append('g').attr('class', 'topo-zoom');
  _topoZoom = d3.zoom()
    .scaleExtent([0.1, 4])
    .on('zoom', (ev) => zoomG.attr('transform', ev.transform));
  svg.call(_topoZoom);''',
        '''  _topoSvg = svg;

  // Zoomable wrapper
  const zoomG = svg.append('g').attr('class', 'topo-zoom');
  _topoZoom = d3.zoom()
    .scaleExtent([0.1, 4])
    .on('zoom', (ev) => zoomG.attr('transform', ev.transform));
  svg.call(_topoZoom);

  // Resize observer: keeps SVG viewBox + simulation centered when the
  // container dimensions change (fullscreen toggle, window resize, etc).
  // Disconnect any prior observer first - we re-create on every render.
  if(_topoResizeObserver){
    try { _topoResizeObserver.disconnect(); } catch(e){}
  }
  if(typeof ResizeObserver !== \'undefined\'){
    _topoResizeObserver = new ResizeObserver(entries => {
      for(const entry of entries){
        const newW = entry.contentRect.width;
        const newH = entry.contentRect.height;
        if(newW <= 0 || newH <= 0) continue;
        // Update the SVG\'s viewBox to actually match the container.
        // This eliminates the letterboxing that preserveAspectRatio=meet
        // causes when the aspect ratio shifts.
        svg.attr(\'viewBox\', \'0 0 \' + newW + \' \' + newH);
        // Update the center force so the simulation re-balances around
        // the new midpoint, and warm the simulation gently so nodes
        // ease toward their new equilibrium without jumping.
        if(_topoSimulation){
          const cf = _topoSimulation.force(\'center\');
          if(cf){
            cf.x(newW / 2).y(newH / 2);
            _topoSimulation.alphaTarget(0.05).restart();
            // Cool back down after a moment
            setTimeout(() => {
              if(_topoSimulation) _topoSimulation.alphaTarget(0.02);
            }, 800);
          }
        }
      }
    });
    _topoResizeObserver.observe(container);
  }'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 3. Tighten the fullscreen enter timeout. The resize observer now
    # handles dimension updates automatically, so we don\'t need to wait
    # 350ms anymore. We keep a small delay before auto-fit to let the
    # resize settle and the simulation warm.
    # ──────────────────────────────────────────────────────────────────────
    (
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
}''',
        '''function enterTopologyFullscreen(){
  if(_topoFullscreen) return;
  // Make sure we\'re actually in web mode - otherwise the button shouldn\'t
  // be active, but defend against edge cases anyway
  if(_topoView !== 'web') setTopoView('web');
  _topoFullscreen = true;
  document.body.classList.add('topo-fullscreen-active');
  // The resize observer (set up in renderTopologyWeb) handles SVG
  // viewBox + simulation center updates automatically as the container
  // expands. We just need to wait a moment for the CSS transition and
  // resize-observer callbacks to settle, then auto-fit the view.
  setTimeout(() => {
    if(typeof fitTopologyToView === 'function') fitTopologyToView();
  }, 450);
}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 4. Same simplification for exit. The resize observer handles
    # the dimension change; we no longer need to manually re-render.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''function exitTopologyFullscreen(){
  if(!_topoFullscreen) return;
  _topoFullscreen = false;
  document.body.classList.remove('topo-fullscreen-active');
  // Wait for the exit transition to complete before re-rendering
  setTimeout(() => {
    if(typeof renderTopologyWeb === 'function') renderTopologyWeb();
  }, 350);
}''',
        '''function exitTopologyFullscreen(){
  if(!_topoFullscreen) return;
  _topoFullscreen = false;
  document.body.classList.remove('topo-fullscreen-active');
  // The resize observer handles the container shrinking. We auto-fit
  // shortly after the transition so the graph re-frames nicely in the
  // smaller viewport.
  setTimeout(() => {
    if(typeof fitTopologyToView === 'function') fitTopologyToView();
  }, 450);
}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 5. Bump version
    # ──────────────────────────────────────────────────────────────────────
    (
        'netwatch v3.29 - raspberry pi',
        'netwatch v3.30 - raspberry pi'
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

    if "fitTopologyToView" not in content:
        print("ERROR: This patch requires patch_fit_to_view first.")
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

    if 'VERSION = "3.29"' in content:
        content = content.replace('VERSION = "3.29"', 'VERSION = "3.30"', 1)

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
    print("  2. Topology > Web > Fullscreen. The graph should now fill the")
    print("     ENTIRE viewport edge-to-edge - no more letterboxed dark")
    print("     rectangle. Drag a node anywhere on screen and it stays")
    print("     visible (won't disappear past invisible boundaries).")
    print("  3. Resize the browser window in normal mode - graph adapts.")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
