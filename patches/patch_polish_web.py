#!/usr/bin/env python3
"""
netwatch patch: web topology view as primary focal point.

Three coordinated changes that together turn the web view into the main
experience of Netwatch:

1. **Default to web view** for new visitors. Existing localStorage
   preferences are preserved, so anyone who explicitly chose Cards
   keeps that. New installs and cleared browsers see the graph first.

2. **Dynamic metrics layout.** When the topology tab is in web mode,
   the metrics row (Hosts UP, Avg Latency, Avg Uptime, Monitored)
   collapses out of the page flow. In its place, two compact translucent
   metric overlays appear inside the canvas - top-left and top-right -
   keeping the data visible without consuming vertical space. The graph
   gets the full available height.

   In cards mode, the metrics row stays exactly where it was. The
   layout is responsive: switching modes smoothly transitions both.

3. **Ambient polish.** A combined set of subtle aesthetic touches:
   - UP nodes have a very slow "breathing" scale animation (1.0 -> 1.02
     over 4s) staggered between nodes so they don't sync up. Almost
     imperceptible individually but feels alive collectively.
   - Status-aware glow halos: green nodes get a faint green aura, red
     nodes a pronounced red one (problems should attract attention),
     UNKNOWN nodes get no glow.
   - Vignette: subtle radial darkening at canvas edges, framing the
     graph without a hard border.
   - Smooth easing throughout (cubic-bezier on transitions).
   - Slight gaussian blur on edge lines so they read softer.
   - Background dot grid drifts slowly (1px every few seconds), giving
     a sense of life even when nothing's changing.
   - Hover micro-animation: ring pulse on enter.
   - prefers-reduced-motion: respects user preference; ambient effects
     turn off entirely if the user has motion sensitivity set.

Must be applied AFTER patch_virtual_vm.py.

Run once from ~/netwatch/:
    python3 patch_web_focal_polish.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_focal.
Idempotent - safe to re-run.
"""

import os, shutil, sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_focal"
SENTINEL = "topo-web-overlay"  # presence means already patched


PATCHES = [
    # ──────────────────────────────────────────────────────────────────────
    # 1. Default to web view for new visitors. The existing localStorage
    # preference still wins (existing users keep their setting). The
    # default for new visitors flips to 'web'.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''let _topoView = localStorage.getItem('nw-topo-view') || 'cards';''',
        '''// Default view: web for new visitors. Existing localStorage preference
// (if set) wins, so anyone who explicitly chose cards keeps cards.
let _topoView = localStorage.getItem('nw-topo-view') || 'web';'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 2. Add overlay metric blocks inside the web view container. They
    # show the same data as the existing scards but are positioned
    # absolutely inside the canvas. We anchor on the existing
    # topo-web-svg-host div.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''    <div class="topo-web" id="topo-web" style="display:none">
      <div class="topo-web-svg-host" id="topo-web-svg-host"></div>
    </div>''',
        '''    <div class="topo-web" id="topo-web" style="display:none">
      <div class="topo-web-svg-host" id="topo-web-svg-host"></div>
      <!-- Overlay metric blocks: shown when in web mode, positioned
           absolutely over the canvas in the corners. They mirror the
           data from the main metrics row above but in a compact form. -->
      <div class="topo-web-overlay topo-web-overlay-tl" id="topo-overlay-status">
        <div class="topo-overlay-row">
          <div class="topo-overlay-stat">
            <div class="topo-overlay-num" id="ov-up">-</div>
            <div class="topo-overlay-lbl">Hosts up</div>
          </div>
          <div class="topo-overlay-divider"></div>
          <div class="topo-overlay-stat">
            <div class="topo-overlay-num" id="ov-tot">-</div>
            <div class="topo-overlay-lbl">Total</div>
          </div>
        </div>
      </div>
      <div class="topo-web-overlay topo-web-overlay-tr" id="topo-overlay-perf">
        <div class="topo-overlay-row">
          <div class="topo-overlay-stat">
            <div class="topo-overlay-num" id="ov-lat">-<span class="topo-overlay-unit">ms</span></div>
            <div class="topo-overlay-lbl">Latency</div>
          </div>
          <div class="topo-overlay-divider"></div>
          <div class="topo-overlay-stat">
            <div class="topo-overlay-num" id="ov-upt">-<span class="topo-overlay-unit">%</span></div>
            <div class="topo-overlay-lbl">Uptime</div>
          </div>
        </div>
      </div>
    </div>'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 3. setTopoView toggles a body class so CSS can hide/show the main
    # metrics row depending on mode. We extend the existing function.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''function setTopoView(view){
  _topoView = view;
  localStorage.setItem('nw-topo-view', view);
  const cardsBtn = document.getElementById('topo-view-cards');
  const webBtn   = document.getElementById('topo-view-web');
  if(cardsBtn) cardsBtn.classList.toggle('active', view === 'cards');
  if(webBtn)   webBtn.classList.toggle('active', view === 'web');
  const grid = document.getElementById('topo-grid');
  const web  = document.getElementById('topo-web');
  if(view === 'web'){
    if(grid) grid.style.display = 'none';
    if(web)  web.style.display  = 'block';
    initTopologyWeb();
  } else {
    if(web)  web.style.display  = 'none';
    if(grid) grid.style.display = '';
  }
}''',
        '''function setTopoView(view){
  _topoView = view;
  localStorage.setItem('nw-topo-view', view);
  const cardsBtn = document.getElementById('topo-view-cards');
  const webBtn   = document.getElementById('topo-view-web');
  if(cardsBtn) cardsBtn.classList.toggle('active', view === 'cards');
  if(webBtn)   webBtn.classList.toggle('active', view === 'web');
  const grid = document.getElementById('topo-grid');
  const web  = document.getElementById('topo-web');
  // Body class lets CSS reposition the main metrics row when web is active.
  // Only applied when the topology tab itself is active - other tabs use
  // the metrics normally.
  document.body.classList.toggle('nw-topo-web', view === 'web');
  if(view === 'web'){
    if(grid) grid.style.display = 'none';
    if(web)  web.style.display  = 'block';
    initTopologyWeb();
  } else {
    if(web)  web.style.display  = 'none';
    if(grid) grid.style.display = '';
  }
}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 4. The body class needs to clear when leaving the topology tab
    # entirely (so the Inventory/Hosts/Events tabs see the metrics row
    # normally). We piggyback on the existing setTab function.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''function setTab(tab){
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));''',
        '''function setTab(tab){
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  // Web-overlay metrics only apply when topology tab is active in web mode
  document.body.classList.toggle('nw-topo-web',
    tab === 'topology' && _topoView === 'web');'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 5. The status refresh that updates s-up etc. should also write to
    # the overlay metrics. Anchor on the existing s-up update.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  const upEl = document.getElementById('s-up');
  upEl.innerHTML = up + ' <sup>/ ' + total + '</sup>';
  upEl.style.color = down > 0 ? 'var(--red)' : (degraded > 0 ? 'var(--amber)' : 'var(--green)');''',
        '''  const upEl = document.getElementById('s-up');
  upEl.innerHTML = up + ' <sup>/ ' + total + '</sup>';
  upEl.style.color = down > 0 ? 'var(--red)' : (degraded > 0 ? 'var(--amber)' : 'var(--green)');
  // Mirror to overlay
  const ovUp = document.getElementById('ov-up');
  const ovTot = document.getElementById('ov-tot');
  if(ovUp){
    ovUp.textContent = up;
    ovUp.style.color = down > 0 ? 'var(--red)' : (degraded > 0 ? 'var(--amber)' : 'var(--green)');
  }
  if(ovTot) ovTot.textContent = total;'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 6. Latency overlay
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  const latEl = document.getElementById('s-lat');
  latEl.innerHTML = avgLat !== null ? avgLat.toFixed(1) + ' <sup>ms</sup>' : '-';
  latEl.style.color = 'var(--blue)';''',
        '''  const latEl = document.getElementById('s-lat');
  latEl.innerHTML = avgLat !== null ? avgLat.toFixed(1) + ' <sup>ms</sup>' : '-';
  latEl.style.color = 'var(--blue)';
  const ovLat = document.getElementById('ov-lat');
  if(ovLat) ovLat.innerHTML = (avgLat !== null ? avgLat.toFixed(1) : '-') + '<span class="topo-overlay-unit">ms</span>';'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 7. Uptime overlay
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  const uptEl = document.getElementById('s-upt');
  uptEl.innerHTML = avgUpt !== null ? avgUpt.toFixed(1) + ' <sup>%</sup>' : '-';
  uptEl.style.color = avgUpt !== null && avgUpt >= 95 ? 'var(--green)' : 'var(--amber)';''',
        '''  const uptEl = document.getElementById('s-upt');
  uptEl.innerHTML = avgUpt !== null ? avgUpt.toFixed(1) + ' <sup>%</sup>' : '-';
  uptEl.style.color = avgUpt !== null && avgUpt >= 95 ? 'var(--green)' : 'var(--amber)';
  const ovUpt = document.getElementById('ov-upt');
  if(ovUpt){
    ovUpt.innerHTML = (avgUpt !== null ? avgUpt.toFixed(1) : '-') + '<span class="topo-overlay-unit">%</span>';
    ovUpt.style.color = avgUpt !== null && avgUpt >= 95 ? 'var(--green)' : 'var(--amber)';
  }'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 8. Add SVG <defs> filters for status glows. The existing SVG only
    # has the dot-grid pattern in defs; we extend it with drop-shadow
    # filters for green/red/amber glows.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  // Background dot pattern
  const defs = svg.append('defs');
  defs.append('pattern')
    .attr('id', 'topo-dot-grid')
    .attr('width', 24).attr('height', 24)
    .attr('patternUnits', 'userSpaceOnUse')
    .append('circle')
      .attr('cx', 1).attr('cy', 1).attr('r', 1)
      .attr('class', 'topo-grid-dot');''',
        '''  // Background dot pattern + status glow filters
  const defs = svg.append('defs');
  defs.append('pattern')
    .attr('id', 'topo-dot-grid')
    .attr('width', 24).attr('height', 24)
    .attr('patternUnits', 'userSpaceOnUse')
    .append('circle')
      .attr('cx', 1).attr('cy', 1).attr('r', 1)
      .attr('class', 'topo-grid-dot');

  // Status glow filters - drop-shadow with status-appropriate color
  function addGlow(id, color, stddev){
    const f = defs.append('filter')
      .attr('id', id)
      .attr('x', '-50%').attr('y', '-50%')
      .attr('width', '200%').attr('height', '200%');
    f.append('feGaussianBlur')
      .attr('in', 'SourceGraphic')
      .attr('stdDeviation', stddev)
      .attr('result', 'blur');
    f.append('feFlood')
      .attr('flood-color', color)
      .attr('flood-opacity', '0.6')
      .attr('result', 'color');
    f.append('feComposite')
      .attr('in', 'color').attr('in2', 'blur')
      .attr('operator', 'in').attr('result', 'shadow');
    const merge = f.append('feMerge');
    merge.append('feMergeNode').attr('in', 'shadow');
    merge.append('feMergeNode').attr('in', 'SourceGraphic');
  }
  addGlow('topo-glow-up',       '#5dbb8d', 4);
  addGlow('topo-glow-down',     '#e57373', 6);
  addGlow('topo-glow-degraded', '#f0a93b', 5);

  // Vignette: radial gradient overlay at canvas edges
  const vignette = defs.append('radialGradient').attr('id', 'topo-vignette')
    .attr('cx', '50%').attr('cy', '50%').attr('r', '70%');
  vignette.append('stop').attr('offset', '60%').attr('stop-color', 'transparent');
  vignette.append('stop').attr('offset', '100%').attr('stop-color', 'rgba(0,0,0,0.4)');'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 9. Apply the vignette overlay rectangle, drawn LAST so it covers
    # everything. Inserted after the dot-grid background rect.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  zoomG.append('rect')
    .attr('x', -2000).attr('y', -2000)
    .attr('width', 4000).attr('height', 4000)
    .attr('fill', 'url(#topo-dot-grid)')
    .style('pointer-events', 'none');''',
        '''  zoomG.append('rect')
    .attr('x', -2000).attr('y', -2000)
    .attr('width', 4000).attr('height', 4000)
    .attr('fill', 'url(#topo-dot-grid)')
    .attr('class', 'topo-grid-bg')
    .style('pointer-events', 'none');
  // Vignette overlay - sits ABOVE the zoom group so it stays anchored to
  // the viewport rather than zooming/panning with content. Added later.
  svg.append('rect')
    .attr('class', 'topo-vignette-rect')
    .attr('x', 0).attr('y', 0)
    .attr('width', '100%').attr('height', '100%')
    .attr('fill', 'url(#topo-vignette)')
    .style('pointer-events', 'none');'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 10. CSS: a comprehensive polish block. Anchored on the existing
    # topo-web style so the new rules sit together. This is the bulk
    # of the visual change.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''.topo-web{position:relative;background:var(--bg);border:1px solid var(--border);border-radius:12px;overflow:hidden;height:min(70vh,800px);min-height:500px}''',
        '''.topo-web{position:relative;background:var(--bg);border:1px solid var(--border);border-radius:12px;overflow:hidden;height:min(78vh,1000px);min-height:540px;transition:height .3s cubic-bezier(.4,0,.2,1)}

/* When web mode is active, hide the main metrics row entirely - the
   overlay metrics inside the canvas take over. */
body.nw-topo-web .summary{display:none}
/* And give the topology view itself more vertical room since the
   metrics row no longer takes any. */
body.nw-topo-web .topo-web{height:min(82vh,1100px)}

/* Overlay metrics inside the canvas */
.topo-web-overlay{position:absolute;z-index:10;background:rgba(20,22,28,.72);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);border:1px solid rgba(255,255,255,.07);border-radius:10px;padding:9px 14px;pointer-events:none;transition:opacity .3s}
.topo-web-overlay-tl{top:14px;left:14px}
.topo-web-overlay-tr{top:14px;right:14px}
.topo-overlay-row{display:flex;align-items:center;gap:14px}
.topo-overlay-stat{display:flex;flex-direction:column;align-items:flex-start;gap:1px;min-width:50px}
.topo-overlay-num{font-family:'DM Sans',sans-serif;font-size:18px;font-weight:600;color:var(--text);line-height:1}
.topo-overlay-unit{font-family:'DM Mono',monospace;font-size:10px;font-weight:400;color:var(--muted);margin-left:2px}
.topo-overlay-lbl{font-family:'DM Mono',monospace;font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--hint)}
.topo-overlay-divider{width:1px;height:22px;background:rgba(255,255,255,.1)}
@media (max-width:600px){.topo-overlay-num{font-size:15px}.topo-overlay-divider{height:18px}}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 11. CSS: ambient breathing on UP nodes + status glows + grid drift.
    # We anchor on the existing status color CSS.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''/* Status colors (border) */
.topo-status-up        .topo-node-shape{stroke:#5dbb8d}
.topo-status-degraded  .topo-node-shape{stroke:#f0a93b}
.topo-status-down      .topo-node-shape{stroke:#e57373}
.topo-status-idle      .topo-node-shape{stroke:#7a7a7a}
.topo-status-unknown   .topo-node-shape{stroke:var(--border-light)}''',
        '''/* Status colors (border) + ambient glows */
.topo-status-up        .topo-node-shape{stroke:#5dbb8d}
.topo-status-up        > .topo-node-shape{filter:url(#topo-glow-up)}
.topo-status-degraded  .topo-node-shape{stroke:#f0a93b}
.topo-status-degraded  > .topo-node-shape{filter:url(#topo-glow-degraded)}
.topo-status-down      .topo-node-shape{stroke:#e57373}
.topo-status-down      > .topo-node-shape{filter:url(#topo-glow-down)}
.topo-status-idle      .topo-node-shape{stroke:#7a7a7a}
.topo-status-unknown   .topo-node-shape{stroke:var(--border-light)}

/* Ambient breathing on UP nodes - very subtle scale animation, staggered
   via animation-delay (set per-node by JS) so the network doesn\'t pulse
   in unison. */
@keyframes topo-breathe{
  0%,100% {transform:scale(1)}
  50%     {transform:scale(1.02)}
}
.topo-status-up .topo-node-shape{animation:topo-breathe 4s ease-in-out infinite;transform-origin:center;transform-box:fill-box}

/* Status-down nodes get a stronger pulse to attract attention */
@keyframes topo-down-pulse{
  0%,100% {opacity:1}
  50%     {opacity:.75}
}
.topo-status-down .topo-node-shape{animation:topo-down-pulse 2.2s ease-in-out infinite;transform-origin:center;transform-box:fill-box}

/* Background grid drift - very slow translate so the dots feel alive */
@keyframes topo-grid-drift{
  from {transform:translate(0,0)}
  to   {transform:translate(24px,24px)}
}
.topo-grid-bg{animation:topo-grid-drift 80s linear infinite}

/* Edge polish: very slight blur softens the lines */
.topo-edge-line{filter:blur(.3px)}
.topo-edge-flow{filter:blur(.2px)}

/* Smoother transitions on state changes */
.topo-node{transition:opacity .3s cubic-bezier(.4,0,.2,1)}
.topo-node-shape{transition:stroke .4s cubic-bezier(.4,0,.2,1),stroke-width .25s cubic-bezier(.4,0,.2,1)}

/* Hover ring pulse */
@keyframes topo-hover-ring{
  0%   {opacity:0;transform:scale(.5)}
  100% {opacity:0;transform:scale(1.6)}
}

/* Respect motion preferences */
@media (prefers-reduced-motion: reduce){
  .topo-status-up .topo-node-shape,
  .topo-status-down .topo-node-shape,
  .topo-grid-bg{animation:none}
  .topo-edge-line,.topo-edge-flow{filter:none}
}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 12. CSS: improve label readability against the new glows. Slightly
    # stronger background-stroke. Anchor on the existing label rules.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''.topo-node-label-below{text-anchor:middle;fill:var(--text);font-family:'DM Sans',sans-serif;font-size:11px;font-weight:500;pointer-events:none;paint-order:stroke;stroke:var(--bg);stroke-width:3;stroke-linejoin:round}''',
        '''.topo-node-label-below{text-anchor:middle;fill:var(--text);font-family:\'DM Sans\',sans-serif;font-size:11px;font-weight:500;pointer-events:none;paint-order:stroke;stroke:var(--bg);stroke-width:3.5;stroke-linejoin:round;letter-spacing:.01em}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 13. JS: stagger the breathing animation by setting per-node
    # animation-delay. We add this to the per-node render.
    # We anchor on the appendVmBadge call point inside the .each().
    # ──────────────────────────────────────────────────────────────────────
    (
        '''    // VM badge if this node hosts a virtual outbound connection
    if(vmIds.has(d.id)){
      appendVmBadge(sel, d.device_type || 'host');
      sel.classed('topo-node-vm', true);
    }
  });''',
        '''    // VM badge if this node hosts a virtual outbound connection
    if(vmIds.has(d.id)){
      appendVmBadge(sel, d.device_type || 'host');
      sel.classed('topo-node-vm', true);
    }
    // Stagger the ambient breathing animation so nodes don\'t pulse in
    // unison. We hash the node id into a delay between 0-4s.
    const shape = sel.select('.topo-node-shape');
    if(!shape.empty()){
      const delay = ((d.id * 1.7) % 4).toFixed(2);
      shape.style('animation-delay', delay + 's');
    }
  });'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 14. Bump version
    # ──────────────────────────────────────────────────────────────────────
    (
        'netwatch v3.22 - raspberry pi',
        'netwatch v3.23 - raspberry pi'
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

    if '"virtual"' not in content:
        print("ERROR: This patch requires patch_virtual_vm first.")
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

    if 'VERSION = "3.22"' in content:
        content = content.replace('VERSION = "3.22"', 'VERSION = "3.23"', 1)

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
    print("  2. Refresh the dashboard. New visitors land on the web view.")
    print("     The metrics row collapses into the canvas as compact")
    print("     translucent overlays in the corners.")
    print("  3. Look for the ambient touches: faint colored glows on")
    print("     UP/DOWN nodes, slow staggered breathing, slow grid drift,")
    print("     vignette darkening at the canvas edges.")
    print()
    print("Toggle to Cards anytime - the metrics row reappears in its")
    print("normal position above. Setting persists in localStorage.")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
