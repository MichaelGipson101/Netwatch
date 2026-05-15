#!/usr/bin/env python3
"""
netwatch patch: bidirectional edge flow + status-aware edge styling.

Two related fixes for the topology web view based on user feedback:

1. **Edges now animate in both directions.** Previously, flow dots only
   traveled from source to target along each edge - which meant for a
   "Desktop -> Switch" connection, dots went away from the switch even
   though network traffic obviously flows both ways. Now each edge has
   two flow dots traveling in opposite directions simultaneously,
   accurately representing bidirectional traffic. Looks better too.

2. **Edges grey out when either endpoint is offline.** Previously, an
   edge between an offline desktop and the switch would keep animating
   cheerfully as if the desktop was up. Now:
     - If either endpoint is DOWN or IDLE -> edge is muted, no flow
     - If either endpoint is DEGRADED (and neither is DOWN/IDLE) -> edge
       stays animated but with reduced opacity
     - If both ends are UP, or one UP + one UNKNOWN (peripheral with
       no monitoring) -> full color + flow

   This makes status visible at a glance across the whole graph - one
   glance tells you what's reachable and what's not.

Must be applied AFTER patch_wifi_and_form_fix.py.

Run once from ~/netwatch/:
    python3 patch_edge_flow_status.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_edgeflow.
Idempotent - safe to re-run.
"""

import os, shutil, sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_edgeflow"
SENTINEL = "topo-edge-dead"


PATCHES = [
    # ──────────────────────────────────────────────────────────────────────
    # 1. Edge creation: add a second flow circle (reverse direction) and
    # tag each circle with a direction marker so the tick handler knows
    # which way to animate it.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  edgeSel.append('path').attr('class', 'topo-edge-line');
  // Animated flow dot
  edgeSel.append('circle').attr('class', 'topo-edge-flow').attr('r', 2);''',
        '''  edgeSel.append('path').attr('class', 'topo-edge-line');
  // Two flow dots - one each direction - to represent bidirectional traffic.
  // The "fwd" dot animates source -> target; the "rev" dot animates
  // target -> source. They travel at the same speed but offset by 0.5
  // along the path so they don't overlap visually.
  edgeSel.append('circle').attr('class', 'topo-edge-flow topo-edge-flow-fwd').attr('r', 2);
  edgeSel.append('circle').attr('class', 'topo-edge-flow topo-edge-flow-rev').attr('r', 2);'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 2. Tick handler: animate both flow dots in opposite directions.
    # Also: hide both dots entirely when the edge is "dead" (either
    # endpoint DOWN/IDLE).
    # ──────────────────────────────────────────────────────────────────────
    (
        '''    // Animated flow dot: position it along the path based on tick frame
    edgeSel.select('circle.topo-edge-flow').each(function(d){
      const path = this.parentNode.querySelector('path.topo-edge-line');
      if(!path) return;
      const len = path.getTotalLength();
      if(!len) return;
      // Speed varies by connection type
      let speed = 0.3;
      if(d.connection_type === 'fiber')   speed = 0.5;
      if(d.connection_type === 'wifi')    speed = 0.18;
      if(d.connection_type === 'power')   speed = 0.15;
      if(d.connection_type === 'usb')     speed = 0.4;
      if(d.connection_type === 'console') speed = 0.2;
      const t = ((tickFrame * speed) % 100) / 100;
      const pt = path.getPointAtLength(t * len);
      d3.select(this).attr('cx', pt.x).attr('cy', pt.y);
    });''',
        '''    // Position both flow dots along the path. Forward dot goes 0 -> 1,
    // reverse dot goes 1 -> 0, offset so they don\'t collide visually.
    edgeSel.each(function(d){
      const path = this.querySelector('path.topo-edge-line');
      if(!path) return;
      const len = path.getTotalLength();
      if(!len) return;
      const fwd = this.querySelector('circle.topo-edge-flow-fwd');
      const rev = this.querySelector('circle.topo-edge-flow-rev');
      // Speed varies by connection type
      let speed = 0.3;
      if(d.connection_type === 'fiber')   speed = 0.5;
      if(d.connection_type === 'wifi')    speed = 0.18;
      if(d.connection_type === 'power')   speed = 0.15;
      if(d.connection_type === 'usb')     speed = 0.4;
      if(d.connection_type === 'console') speed = 0.2;
      const t = ((tickFrame * speed) % 100) / 100;
      const tFwd = t;
      const tRev = (t + 0.5) % 1;  // offset by half so they don\'t overlap
      if(fwd){
        const pt = path.getPointAtLength(tFwd * len);
        fwd.setAttribute('cx', pt.x);
        fwd.setAttribute('cy', pt.y);
      }
      if(rev){
        // Travel from end -> start
        const pt = path.getPointAtLength((1 - tRev) * len);
        rev.setAttribute('cx', pt.x);
        rev.setAttribute('cy', pt.y);
      }
    });''',
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 3. Edge class assignment: enrich the class string with a
    # status-aware "edge state" (alive/degraded/dead) so the CSS can
    # respond. This requires looking up both endpoints' statuses.
    #
    # We replace the existing edge class line. The new version computes
    # the edge state from the source/target node statuses.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  const edgeSel = edgeG.selectAll('g.topo-edge').data(renderEdges).join('g')
    .attr('class', d => 'topo-edge topo-edge-' + (d.connection_type || 'ethernet'));''',
        '''  // Helper: classify an edge based on its endpoints\' current statuses.
  // Returns one of: \'alive\' (both up or up+unknown), \'degraded\' (at least
  // one degraded but no down/idle), or \'dead\' (at least one down/idle).
  function edgeState(edge){
    const s = nodeMap[typeof edge.source === \'object\' ? edge.source.id : edge.source];
    const t = nodeMap[typeof edge.target === \'object\' ? edge.target.id : edge.target];
    const ss = (s && s.status) || \'UNKNOWN\';
    const ts = (t && t.status) || \'UNKNOWN\';
    if(ss === \'DOWN\' || ss === \'IDLE\' || ts === \'DOWN\' || ts === \'IDLE\') return \'dead\';
    if(ss === \'DEGRADED\' || ts === \'DEGRADED\') return \'degraded\';
    return \'alive\';
  }
  const edgeSel = edgeG.selectAll(\'g.topo-edge\').data(renderEdges).join(\'g\')
    .attr(\'class\', d => \'topo-edge topo-edge-\' + (d.connection_type || \'ethernet\')
      + \' topo-edge-\' + edgeState(d));'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 4. Status-update handler: also recompute edge state when statuses
    # change, so edges grey/un-grey live without a full re-render.
    # We anchor on the existing updateTopologyWebStatus function.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''    _topoLastStatus[n.id] = newStatus;
  });
}''',
        '''    _topoLastStatus[n.id] = newStatus;
  });
  // Recompute edge classes based on the new statuses. We rebuild the
  // node lookup from the live data, then walk every edge group and
  // update its alive/degraded/dead class.
  if(!_topoSvg) return;
  const liveNodeMap = {};
  _topoData.nodes.forEach(n => { liveNodeMap[n.id] = n; });
  _topoSvg.selectAll(\'g.topo-edge\').each(function(d){
    const sId = typeof d.source === \'object\' ? d.source.id : d.source;
    const tId = typeof d.target === \'object\' ? d.target.id : d.target;
    const s = liveNodeMap[sId];
    const t = liveNodeMap[tId];
    const ss = (s && s.status) || \'UNKNOWN\';
    const ts = (t && t.status) || \'UNKNOWN\';
    let state;
    if(ss === \'DOWN\' || ss === \'IDLE\' || ts === \'DOWN\' || ts === \'IDLE\') state = \'dead\';
    else if(ss === \'DEGRADED\' || ts === \'DEGRADED\') state = \'degraded\';
    else state = \'alive\';
    const node = d3.select(this);
    node.classed(\'topo-edge-alive\',    state === \'alive\');
    node.classed(\'topo-edge-degraded\', state === \'degraded\');
    node.classed(\'topo-edge-dead\',     state === \'dead\');
  });
}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 5. CSS: define the three edge states. Dead edges grey out the line
    # AND hide the flow dots (set opacity:0 instead of removing - keeps
    # the SVG structure stable so transitions look smooth).
    # We anchor on the existing topo-edge-other CSS line.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''.topo-edge-other    .topo-edge-line{stroke:var(--muted)}
.topo-edge-other    .topo-edge-flow{fill:var(--muted)}''',
        '''.topo-edge-other    .topo-edge-line{stroke:var(--muted)}
.topo-edge-other    .topo-edge-flow{fill:var(--muted)}

/* Status-aware edge states */
.topo-edge-alive    .topo-edge-line{opacity:.85}
.topo-edge-alive    .topo-edge-flow{opacity:.95}
.topo-edge-degraded .topo-edge-line{opacity:.5}
.topo-edge-degraded .topo-edge-flow{opacity:.6}
.topo-edge-dead     .topo-edge-line{stroke:var(--border-light) !important;opacity:.35;stroke-dasharray:none !important;transition:stroke .3s,opacity .3s}
.topo-edge-dead     .topo-edge-flow{opacity:0;transition:opacity .3s}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 6. Bump version
    # ──────────────────────────────────────────────────────────────────────
    (
        'netwatch v3.20 - raspberry pi',
        'netwatch v3.21 - raspberry pi'
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

    if "topo-edge-wifi" not in content:
        print("ERROR: This patch requires patch_wifi_and_form_fix first.")
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

    if 'VERSION = "3.20"' in content:
        content = content.replace('VERSION = "3.20"', 'VERSION = "3.21"', 1)

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
    print("  2. Open Topology -> Web. Edges connecting to UP devices")
    print("     animate full-color in BOTH directions. Edges to DOWN or")
    print("     IDLE devices grey out and stop animating. Power off your")
    print("     desktop and watch its edge to the switch fade.")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
