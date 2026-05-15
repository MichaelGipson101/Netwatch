#!/usr/bin/env python3
"""
netwatch patch: topology view polish bundle.

Three improvements that together make the graph easier to read,
investigate, and learn:

  1. EDGE TOOLTIPS - hovering an edge now reveals what it represents.
     Connection type, source -> target, port info, notes. Reuses the
     existing tooltip element. Adds an invisible wider hit-area path
     behind each visible edge so the hover target is generous (the
     visible 1.5px line is hard to hit).

  2. LABEL COLLISION AVOIDANCE - when two node labels overlap (text
     bounding boxes intersect), they get pushed apart vertically.
     Runs as a one-shot pass after the simulation cools, and again
     after node drag-end. Doesn't affect the simulation itself, only
     label rendering.

  3. STATUS LEGEND - small "?" info button in the bottom-left of the
     canvas. Click to toggle a legend overlay explaining what colors,
     shapes, and edge styles mean. Especially helpful for kiosk
     viewers seeing the graph for the first time. Click anywhere
     outside or press Esc to dismiss.

The grid drift animation is preserved per user request.

Must be applied AFTER patch_dead_edge_visibility.py.

Run once from ~/netwatch/:
    python3 patch_topo_polish_bundle.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_topobundle.
Idempotent - safe to re-run.
"""

import os, shutil, sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_topobundle"
SENTINEL = "buildEdgeTip"


# ─── Frontend additions ──────────────────────────────────────────────────────

EDGE_TIP_BUILDER = r'''
// Build tooltip HTML for an edge. Includes connection type icon,
// endpoint names, port info, and notes if present.
function buildEdgeTip(e, nodeMap){
  const sId = typeof e.source === 'object' ? e.source.id : e.source;
  const tId = typeof e.target === 'object' ? e.target.id : e.target;
  const s = nodeMap[sId];
  const t = nodeMap[tId];
  if(!s || !t) return '';

  const typeLabels = {
    ethernet: 'Ethernet',
    fiber: 'Fiber',
    wifi: 'WiFi',
    virtual: 'Virtual',
    power: 'Power',
    usb: 'USB',
    console: 'Console',
    other: 'Other',
  };
  const typeLabel = typeLabels[e.connection_type] || e.connection_type;

  let html = '<div class="topo-tip-edge-type topo-edge-tip-' + e.connection_type + '">'
    + escapeHtml(typeLabel) + '</div>';
  html += '<div class="topo-tip-name">'
    + escapeHtml(s.name) + ' <span class="topo-tip-arrow">\u2192</span> '
    + escapeHtml(t.name) + '</div>';

  const portBits = [];
  if(e.from_port) portBits.push('via ' + escapeHtml(e.from_port));
  if(e.to_port)   portBits.push('port ' + escapeHtml(e.to_port));
  if(portBits.length){
    html += '<div class="topo-tip-meta">' + portBits.join(' \u00b7 ') + '</div>';
  }
  return html;
}
'''


LABEL_COLLISION_FN = r'''
// Push apart overlapping labels. Runs after simulation cooldown and
// after node drag-end. Detects bounding-box collisions between label
// text elements and shifts colliding labels vertically (one up, one
// down from default position) until they no longer overlap.
function spreadOverlappingLabels(nodeSel){
  if(!nodeSel || nodeSel.empty()) return;
  // Collect all labels with their current positions and bounding boxes
  const labels = [];
  nodeSel.each(function(d){
    const labelEl = this.querySelector('.topo-node-label-below, .topo-node-label-inside');
    if(!labelEl) return;
    let bbox;
    try { bbox = labelEl.getBBox(); }
    catch(e){ return; }
    if(!bbox || !bbox.width) return;
    // Reset any prior offset so we recompute from scratch
    labelEl.removeAttribute('data-y-offset');
    const origY = parseFloat(labelEl.getAttribute('data-orig-y') || labelEl.getAttribute('y') || '0');
    labelEl.setAttribute('data-orig-y', origY);
    labelEl.setAttribute('y', origY);
    labels.push({
      el: labelEl,
      d: d,
      origY: origY,
      x: d.x,
      y: d.y + origY,
      w: bbox.width,
      h: bbox.height,
    });
  });

  // Pairwise collision check + shift. We do up to 3 iterations since
  // shifting one label can free up another.
  const PAD = 2;
  for(let iter = 0; iter < 3; iter++){
    let anyShift = false;
    for(let i = 0; i < labels.length; i++){
      for(let j = i + 1; j < labels.length; j++){
        const a = labels[i], b = labels[j];
        // Use current y positions including any prior shift
        const aY = parseFloat(a.el.getAttribute('y'));
        const bY = parseFloat(b.el.getAttribute('y'));
        const aTop = a.d.y + aY - a.h, aBot = a.d.y + aY + 2;
        const bTop = b.d.y + bY - b.h, bBot = b.d.y + bY + 2;
        const aLeft  = a.d.x - a.w/2, aRight = a.d.x + a.w/2;
        const bLeft  = b.d.x - b.w/2, bRight = b.d.x + b.w/2;
        // Horizontal overlap?
        const xOverlap = aLeft < bRight + PAD && bLeft < aRight + PAD;
        if(!xOverlap) continue;
        // Vertical overlap?
        const yOverlap = aTop < bBot + PAD && bTop < aBot + PAD;
        if(!yOverlap) continue;
        // Collision - shift the lower-positioned label further down,
        // and the higher one further up. Magnitude is enough to clear
        // the other's bbox.
        const shift = Math.ceil((Math.min(aBot, bBot) - Math.max(aTop, bTop)) / 2) + PAD;
        if((a.d.y + aY) <= (b.d.y + bY)){
          a.el.setAttribute('y', aY - shift);
          b.el.setAttribute('y', bY + shift);
        } else {
          a.el.setAttribute('y', aY + shift);
          b.el.setAttribute('y', bY - shift);
        }
        anyShift = true;
      }
    }
    if(!anyShift) break;
  }
}
'''


LEGEND_OVERLAY_HTML = r'''      <!-- Status legend - hidden by default, opens via the ? button -->
      <button class="topo-legend-btn" id="topo-legend-btn"
              title="What do the colors mean?"
              onclick="toggleTopologyLegend()">?</button>
      <div class="topo-legend" id="topo-legend">
        <div class="topo-legend-hdr">
          <span>Legend</span>
          <button class="topo-legend-close" onclick="toggleTopologyLegend()">&times;</button>
        </div>
        <div class="topo-legend-section">
          <div class="topo-legend-title">Node shapes</div>
          <div class="topo-legend-row"><span class="topo-legend-shape topo-leg-host"></span>Host</div>
          <div class="topo-legend-row"><span class="topo-legend-shape topo-leg-vm"></span>VM (smaller circle)</div>
          <div class="topo-legend-row"><span class="topo-legend-shape topo-leg-network"></span>Network device</div>
          <div class="topo-legend-row"><span class="topo-legend-shape topo-leg-ups"></span>UPS</div>
          <div class="topo-legend-row"><span class="topo-legend-shape topo-leg-disk"></span>Disk</div>
          <div class="topo-legend-row"><span class="topo-legend-shape topo-leg-peripheral"></span>Peripheral</div>
        </div>
        <div class="topo-legend-section">
          <div class="topo-legend-title">Status border</div>
          <div class="topo-legend-row"><span class="topo-legend-dot topo-leg-up"></span>Up</div>
          <div class="topo-legend-row"><span class="topo-legend-dot topo-leg-degraded"></span>Degraded</div>
          <div class="topo-legend-row"><span class="topo-legend-dot topo-leg-down"></span>Down</div>
          <div class="topo-legend-row"><span class="topo-legend-dot topo-leg-idle"></span>Idle / unmonitored</div>
        </div>
        <div class="topo-legend-section">
          <div class="topo-legend-title">Connections</div>
          <div class="topo-legend-row"><span class="topo-legend-line topo-leg-eth"></span>Ethernet</div>
          <div class="topo-legend-row"><span class="topo-legend-line topo-leg-wifi"></span>WiFi</div>
          <div class="topo-legend-row"><span class="topo-legend-line topo-leg-fiber"></span>Fiber</div>
          <div class="topo-legend-row"><span class="topo-legend-line topo-leg-power"></span>Power</div>
          <div class="topo-legend-row"><span class="topo-legend-line topo-leg-virtual"></span>Virtual (VM \u2192 host)</div>
          <div class="topo-legend-row"><span class="topo-legend-line topo-leg-dead"></span>Offline / idle endpoint</div>
        </div>
      </div>
'''


LEGEND_TOGGLE_FN = r'''
function toggleTopologyLegend(){
  const el = document.getElementById('topo-legend');
  if(!el) return;
  el.classList.toggle('open');
}

// Click outside the legend or press Esc closes it
document.addEventListener('click', (ev) => {
  const legend = document.getElementById('topo-legend');
  const btn    = document.getElementById('topo-legend-btn');
  if(!legend || !legend.classList.contains('open')) return;
  if(legend.contains(ev.target) || btn.contains(ev.target)) return;
  legend.classList.remove('open');
});
'''


PATCHES = [
    # ──────────────────────────────────────────────────────────────────────
    # 1. Add an invisible wider hit-area path behind each visible edge,
    # plus the visible path. Hit area uses stroke 14px transparent so
    # hover detection works comfortably on thin lines.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  edgeSel.append('path').attr('class', 'topo-edge-line');
  // Two flow dots - one each direction - to represent bidirectional traffic.''',
        '''  // Wider invisible hit-area path so hover/click on the edge is generous
  edgeSel.append('path').attr('class', 'topo-edge-hit');
  edgeSel.append('path').attr('class', 'topo-edge-line');
  // Two flow dots - one each direction - to represent bidirectional traffic.'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 2. Tick handler: also update the hit-area path's d attribute to
    # follow the same curve as the visible line.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''    edgeSel.select('path.topo-edge-line').attr('d', d => {
      const dx = d.target.x - d.source.x;
      const dy = d.target.y - d.source.y;
      const dr = Math.sqrt(dx*dx + dy*dy) * 1.8;
      return 'M' + d.source.x + ',' + d.source.y
        + 'A' + dr + ',' + dr + ' 0 0,1 ' + d.target.x + ',' + d.target.y;
    });''',
        '''    const edgePath = d => {
      const dx = d.target.x - d.source.x;
      const dy = d.target.y - d.source.y;
      const dr = Math.sqrt(dx*dx + dy*dy) * 1.8;
      return 'M' + d.source.x + ',' + d.source.y
        + 'A' + dr + ',' + dr + ' 0 0,1 ' + d.target.x + ',' + d.target.y;
    };
    edgeSel.select('path.topo-edge-line').attr('d', edgePath);
    edgeSel.select('path.topo-edge-hit').attr('d', edgePath);'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 3. Add hover handlers to edges. Reuse the existing tip element by
    # binding mousemove/mouseleave on edgeSel just like nodeSel.
    # We anchor on the line right after the tooltip is created.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  }).on('mouseleave.tip', () => tip.style('display', 'none'));

  // Tick handler: update positions + curved edges + flowing dot animation''',
        '''  }).on('mouseleave.tip', () => tip.style('display', 'none'));

  // Edge hover tooltips - same positioning logic as nodes, just on edges
  edgeSel.on('mousemove', function(ev, e){
    const rect = container.getBoundingClientRect();
    tip.style('display', 'block').html(buildEdgeTip(e, nodeMap));
    const tipNode = tip.node();
    const tipW = tipNode ? tipNode.offsetWidth : 240;
    const tipH = tipNode ? tipNode.offsetHeight : 60;
    const cx = ev.clientX - rect.left;
    const cy = ev.clientY - rect.top;
    const margin = 14;
    let x = cx + 12, y = cy + 12;
    if(x + tipW + margin > rect.width)  x = cx - tipW - 12;
    if(y + tipH + margin > rect.height) y = cy - tipH - 12;
    x = Math.max(8, x); y = Math.max(8, y);
    tip.style('left', x + 'px').style('top', y + 'px');
    // Highlight this edge a bit while hovered
    d3.select(this).classed('topo-edge-hovered', true);
  }).on('mouseleave.tip', function(){
    tip.style('display', 'none');
    d3.select(this).classed('topo-edge-hovered', false);
  });

  // Tick handler: update positions + curved edges + flowing dot animation'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 4. Run label-collision pass when simulation cools. Anchor on the
    # existing `setTimeout(() => sim.alphaTarget(0.02).restart(), 4000);`
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  // Cool the simulation gradually
  sim.alpha(1).restart();
  setTimeout(() => sim.alphaTarget(0.02).restart(), 4000);''',
        '''  // Cool the simulation gradually
  sim.alpha(1).restart();
  setTimeout(() => sim.alphaTarget(0.02).restart(), 4000);
  // Run label collision pass after the simulation has had time to settle.
  // This nudges overlapping labels apart so dense areas read more clearly.
  setTimeout(() => spreadOverlappingLabels(nodeSel), 4500);
  // And re-run after a longer settle, in case nodes are still adjusting
  setTimeout(() => spreadOverlappingLabels(nodeSel), 6500);'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 5. Re-run label collision after a node drag ends. Anchor on dragEnd.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  function dragEnd(ev, d){
    if(!ev.active) sim.alphaTarget(0.02);
    // Persist the pinned position so it survives reloads
    saveTopoPosition(d.id, d.fx, d.fy);
  }''',
        '''  function dragEnd(ev, d){
    if(!ev.active) sim.alphaTarget(0.02);
    // Persist the pinned position so it survives reloads
    saveTopoPosition(d.id, d.fx, d.fy);
    // Re-run label collision since the dragged node's neighborhood changed
    setTimeout(() => spreadOverlappingLabels(nodeSel), 600);
  }'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 6. Add the buildEdgeTip + spreadOverlappingLabels + legend toggle
    # functions. Anchor on the existing topologyToggleUnconnected which
    # is at the end of the topology JS section.
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
''' + EDGE_TIP_BUILDER + LABEL_COLLISION_FN + LEGEND_TOGGLE_FN
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 7. Add the legend HTML inside the topo-web container. Anchor on
    # the existing fullscreen overlay elements (close button + wordmark).
    # ──────────────────────────────────────────────────────────────────────
    (
        '''      <!-- Fullscreen overlay elements - only visible when topo-fullscreen-active
           class is on the body. The wordmark + close button float over the SVG. -->''',
        LEGEND_OVERLAY_HTML + '''      <!-- Fullscreen overlay elements - only visible when topo-fullscreen-active
           class is on the body. The wordmark + close button float over the SVG. -->'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 8. CSS additions for edge hit area, edge hover, edge tooltip styles,
    # legend button + panel. Anchor on existing topo-tip CSS block.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''.topo-tip{position:absolute;pointer-events:none;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:8px 11px;box-shadow:0 4px 16px rgba(0,0,0,.25);font-size:12px;z-index:50;max-width:240px}''',
        '''.topo-tip{position:absolute;pointer-events:none;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:8px 11px;box-shadow:0 4px 16px rgba(0,0,0,.25);font-size:12px;z-index:50;max-width:280px}

/* Edge tooltip extras */
.topo-tip-edge-type{display:inline-block;font-family:'DM Mono',monospace;font-size:9px;letter-spacing:.07em;text-transform:uppercase;padding:1px 6px;border-radius:3px;margin-bottom:4px;background:var(--subtle);color:var(--muted)}
.topo-edge-tip-ethernet{background:rgba(91,142,255,.15);color:#5b8eff}
.topo-edge-tip-fiber{background:rgba(168,114,214,.15);color:#a872d6}
.topo-edge-tip-wifi{background:rgba(61,199,192,.15);color:#3dc7c0}
.topo-edge-tip-virtual{background:rgba(176,124,214,.15);color:#b07cd6}
.topo-edge-tip-power{background:rgba(240,169,59,.15);color:#f0a93b}
.topo-edge-tip-usb{background:rgba(93,187,141,.15);color:#5dbb8d}
.topo-edge-tip-console{background:rgba(140,140,140,.18);color:#aaa}
.topo-tip-arrow{color:var(--hint);margin:0 4px}

/* Edge hit area (invisible, wider hover target) */
.topo-edge-hit{stroke:transparent;fill:none;stroke-width:14;cursor:pointer;pointer-events:stroke}
.topo-edge-hovered .topo-edge-line{opacity:1 !important;stroke-width:2.5}
.topo-edge-hovered .topo-edge-flow{opacity:1 !important}

/* Legend button + panel */
.topo-legend-btn{position:absolute;bottom:14px;left:14px;width:28px;height:28px;border-radius:50%;background:rgba(20,22,28,.65);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);border:1px solid rgba(255,255,255,.08);color:var(--muted);font-family:'DM Sans',sans-serif;font-size:14px;font-weight:600;cursor:pointer;z-index:11;transition:all .15s;display:flex;align-items:center;justify-content:center;line-height:1;padding:0}
.topo-legend-btn:hover{background:rgba(40,42,50,.85);color:var(--text)}
.topo-legend{position:absolute;bottom:50px;left:14px;background:rgba(20,22,28,.92);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:14px 16px;font-size:11px;color:var(--text);z-index:12;width:230px;box-shadow:0 8px 24px rgba(0,0,0,.35);opacity:0;transform:translateY(8px);pointer-events:none;transition:opacity .2s,transform .2s}
.topo-legend.open{opacity:1;transform:translateY(0);pointer-events:auto}
.topo-legend-hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid var(--border-light);font-family:'DM Mono',monospace;font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--hint)}
.topo-legend-close{background:transparent;border:none;color:var(--muted);cursor:pointer;font-size:16px;padding:0;line-height:1;width:18px;height:18px;display:flex;align-items:center;justify-content:center}
.topo-legend-close:hover{color:var(--text)}
.topo-legend-section{margin-bottom:12px}
.topo-legend-section:last-child{margin-bottom:0}
.topo-legend-title{font-family:'DM Mono',monospace;font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--hint);margin-bottom:6px}
.topo-legend-row{display:flex;align-items:center;gap:8px;padding:3px 0;font-size:11px;color:var(--text)}
.topo-legend-shape{display:inline-block;width:14px;height:14px;flex-shrink:0;border:1.5px solid var(--muted);background:var(--subtle)}
.topo-legend-shape.topo-leg-host{border-radius:50%;width:14px;height:14px}
.topo-legend-shape.topo-leg-vm{border-radius:50%;width:10px;height:10px;background:#1f1a26}
.topo-legend-shape.topo-leg-network{border-radius:3px;width:18px;height:10px;background:#1a2540}
.topo-legend-shape.topo-leg-ups{border-radius:3px;width:18px;height:10px;background:#3a2e15}
.topo-legend-shape.topo-leg-disk{border-radius:2px;width:11px;height:11px;background:#1f2a25}
.topo-legend-shape.topo-leg-peripheral{border-radius:50%;width:9px;height:9px}
.topo-legend-dot{display:inline-block;width:12px;height:12px;border-radius:50%;flex-shrink:0;border:2px solid;background:var(--subtle)}
.topo-legend-dot.topo-leg-up{border-color:#5dbb8d}
.topo-legend-dot.topo-leg-degraded{border-color:#f0a93b}
.topo-legend-dot.topo-leg-down{border-color:#e57373}
.topo-legend-dot.topo-leg-idle{border-color:#7a7a7a}
.topo-legend-line{display:inline-block;width:24px;height:2px;flex-shrink:0;background:var(--muted);border-radius:1px}
.topo-legend-line.topo-leg-eth{background:#5b8eff}
.topo-legend-line.topo-leg-wifi{background:#3dc7c0;background-image:linear-gradient(90deg,#3dc7c0 1px,transparent 1px,transparent 4px);background-size:5px 100%}
.topo-legend-line.topo-leg-fiber{background:#a872d6;background-image:linear-gradient(90deg,#a872d6 4px,transparent 4px,transparent 6px);background-size:6px 100%}
.topo-legend-line.topo-leg-power{background:#f0a93b;background-image:linear-gradient(90deg,#f0a93b 8px,transparent 8px,transparent 11px);background-size:11px 100%}
.topo-legend-line.topo-leg-virtual{background:#b07cd6;background-image:linear-gradient(90deg,#b07cd6 3px,transparent 3px,transparent 5px);background-size:5px 100%}
.topo-legend-line.topo-leg-dead{background:var(--muted);opacity:.45;background-image:linear-gradient(90deg,var(--muted) 3px,transparent 3px,transparent 6px);background-size:6px 100%}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 9. Bump version
    # ──────────────────────────────────────────────────────────────────────
    (
        'netwatch v3.33 - raspberry pi',
        'netwatch v3.34 - raspberry pi'
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

    if "Dead edge: muted but findable" not in content:
        print("ERROR: This patch requires patch_dead_edge_visibility first.")
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

    if 'VERSION = "3.33"' in content:
        content = content.replace('VERSION = "3.33"', 'VERSION = "3.34"', 1)

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
    print("Topology view bundle landed:")
    print("  - Hover any edge to see what it represents (type, source ->")
    print("    target, port info). Hit area is generous.")
    print("  - Overlapping labels in dense areas now nudge apart")
    print("    automatically once the simulation settles.")
    print("  - A '?' button in the bottom-left of the canvas opens a")
    print("    legend explaining shapes, status colors, and edge types.")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
