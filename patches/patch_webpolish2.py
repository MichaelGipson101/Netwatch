#!/usr/bin/env python3
"""
netwatch patch: legend bug fixes + small visual polish.

Catches two real bugs from the topology polish bundle and adds a few
small touches:

  1. Legend label "Virtual (VM \\u2192 host)" was rendering as literal
     escape characters instead of an arrow. The Python r-string in the
     prior patch escaped the backslash, sending the literal sequence
     to the browser. Fix: use the actual unicode arrow character (or
     HTML entity) in the legend HTML.

  2. Legend's connection-type swatches were supposed to show example
     dashed lines for WiFi/Fiber/Power/Virtual/Offline. The CSS
     linear-gradient approach worked but the dashes are too subtle
     to read at 24px width. Replaced with proper inline SVG paths
     that mirror exactly what the actual graph edges use - same
     stroke-dasharray patterns, same colors. Visually correct and
     semantically meaningful.

  3. Legend "?" button stays visible behind the open legend panel,
     creating a small visual conflict. Now hidden while the legend
     is open, restored when closed.

  4. Node drag now shows the proper grabbing cursor while dragging,
     a small but pleasing affordance.

Must be applied AFTER patch_topo_polish_bundle.py.

Run once from ~/netwatch/:
    python3 patch_legend_polish.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_legpolish.
Idempotent - safe to re-run.
"""

import os, shutil, sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_legpolish"
SENTINEL = "topo-legend-svg-line"


# ─── New legend HTML using inline SVG for proper dash rendering ──────────────
# Using HTML entity &#8594; for the right arrow (no backslash escapes,
# no unicode interpretation issues).

# SVG mini-line component generator (helper - kept inline so the patch
# is self-contained):
def svg_line(color, dash=None, opacity=1.0, width=2):
    da = f' stroke-dasharray="{dash}"' if dash else ''
    op = f' opacity="{opacity}"' if opacity < 1.0 else ''
    return (f'<svg class="topo-legend-svg-line" width="28" height="6" viewBox="0 0 28 6">'
            f'<line x1="2" y1="3" x2="26" y2="3" stroke="{color}" '
            f'stroke-width="{width}"{da}{op} stroke-linecap="round"/></svg>')


NEW_LEGEND_CONNECTIONS_BLOCK = (
    '''<div class="topo-legend-section">
          <div class="topo-legend-title">Connections</div>
          <div class="topo-legend-row">''' + svg_line('#5b8eff') + '''Ethernet</div>
          <div class="topo-legend-row">''' + svg_line('#3dc7c0', dash='1,3', opacity=0.85) + '''WiFi</div>
          <div class="topo-legend-row">''' + svg_line('#a872d6', dash='4,2') + '''Fiber</div>
          <div class="topo-legend-row">''' + svg_line('#f0a93b', dash='8,3') + '''Power</div>
          <div class="topo-legend-row">''' + svg_line('#b07cd6', dash='3,2', width=1.5) + '''Virtual (VM &#8594; host)</div>
          <div class="topo-legend-row">''' + svg_line('#9b998f', dash='3,3', opacity=0.6) + '''Offline / idle endpoint</div>
        </div>'''
)


PATCHES = [
    # ──────────────────────────────────────────────────────────────────────
    # 1. Replace the broken Connections legend section with one that uses
    # inline SVG mini-lines for correct dashed rendering. This also fixes
    # the literal '\u2192' bug since we use the HTML entity directly.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''<div class="topo-legend-section">
          <div class="topo-legend-title">Connections</div>
          <div class="topo-legend-row"><span class="topo-legend-line topo-leg-eth"></span>Ethernet</div>
          <div class="topo-legend-row"><span class="topo-legend-line topo-leg-wifi"></span>WiFi</div>
          <div class="topo-legend-row"><span class="topo-legend-line topo-leg-fiber"></span>Fiber</div>
          <div class="topo-legend-row"><span class="topo-legend-line topo-leg-power"></span>Power</div>
          <div class="topo-legend-row"><span class="topo-legend-line topo-leg-virtual"></span>Virtual (VM \\u2192 host)</div>
          <div class="topo-legend-row"><span class="topo-legend-line topo-leg-dead"></span>Offline / idle endpoint</div>
        </div>''',
        NEW_LEGEND_CONNECTIONS_BLOCK
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 2. CSS for the inline SVG legend lines + drag cursor + hide-button-
    # while-legend-open. Anchor on the existing topo-legend-line CSS
    # block (which becomes mostly dead code, but we leave it for any
    # future use).
    # ──────────────────────────────────────────────────────────────────────
    (
        '''.topo-legend-line{display:inline-block;width:24px;height:2px;flex-shrink:0;background:var(--muted);border-radius:1px}''',
        '''.topo-legend-line{display:inline-block;width:24px;height:2px;flex-shrink:0;background:var(--muted);border-radius:1px}
/* Inline SVG legend lines - show actual dash patterns matching the graph */
.topo-legend-svg-line{flex-shrink:0;display:block}

/* Hide the ? button while the legend is open to reduce visual clutter */
.topo-legend.open ~ .topo-legend-btn,
.topo-web:has(.topo-legend.open) .topo-legend-btn{opacity:0;pointer-events:none}

/* Grabbing cursor while dragging a node */
.topo-node:active{cursor:grabbing}
.topo-web-svg.topo-dragging,
.topo-web-svg.topo-dragging *{cursor:grabbing !important}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 3. Drag handlers: toggle a `topo-dragging` class on the SVG so the
    # CSS cursor rule above kicks in. We anchor on the existing dragStart
    # function.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  function dragStart(ev, d){
    if(!ev.active) sim.alphaTarget(0.3).restart();
    d.fx = d.x; d.fy = d.y;
  }''',
        '''  function dragStart(ev, d){
    if(!ev.active) sim.alphaTarget(0.3).restart();
    d.fx = d.x; d.fy = d.y;
    if(_topoSvg) _topoSvg.classed('topo-dragging', true);
  }'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 4. Drag end: remove the dragging class
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  function dragEnd(ev, d){
    if(!ev.active) sim.alphaTarget(0.02);
    // Persist the pinned position so it survives reloads
    saveTopoPosition(d.id, d.fx, d.fy);
    // Re-run label collision since the dragged node's neighborhood changed
    setTimeout(() => spreadOverlappingLabels(nodeSel), 600);
  }''',
        '''  function dragEnd(ev, d){
    if(!ev.active) sim.alphaTarget(0.02);
    // Persist the pinned position so it survives reloads
    saveTopoPosition(d.id, d.fx, d.fy);
    // Re-run label collision since the dragged node's neighborhood changed
    setTimeout(() => spreadOverlappingLabels(nodeSel), 600);
    if(_topoSvg) _topoSvg.classed('topo-dragging', false);
  }'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 5. Bump version
    # ──────────────────────────────────────────────────────────────────────
    (
        'netwatch v3.34 - raspberry pi',
        'netwatch v3.35 - raspberry pi'
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

    if "buildEdgeTip" not in content:
        print("ERROR: This patch requires patch_topo_polish_bundle first.")
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

    if 'VERSION = "3.34"' in content:
        content = content.replace('VERSION = "3.34"', 'VERSION = "3.35"', 1)

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
    print("Legend fixes + polish:")
    print("  - Connection legend now uses real SVG dashed lines that")
    print("    match the actual graph rendering. WiFi shows tiny dots,")
    print("    Fiber shows medium dashes, Power shows long dashes, etc.")
    print("  - 'VM \u2192 host' arrow renders correctly (was literal text)")
    print("  - The '?' button hides while the legend is open")
    print("  - Dragging a node now shows the grabbing cursor")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
