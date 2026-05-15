#!/usr/bin/env python3
"""
netwatch patch: virtual connection type for VM <-> host relationships.

VMs run inside their physical hosts - they're not really "connected" to
the host the way a desktop is connected to a switch. This patch adds a
new connection type that captures that hosting relationship truthfully:

  - New connection type: 'virtual'
  - Direction convention: VM -> connects to -> host (the physical machine)
  - In the topology web view: virtual edges render in muted purple with
    a short-dashed pattern (distinct from network edges) but keep flow
    animation since VMs do exchange real network traffic
  - VM nodes get a small "VM" badge in the corner. Detection is implicit:
    any node that's the SOURCE of a virtual edge is treated as a VM.
    No schema changes required.

How to use it:
  1. Open the inventory drawer for a VM (e.g., one of your TrueNAS or
     Proxmox guests)
  2. Click "+ Add connection"
  3. Pick the physical host as the target, set Type to "Virtual"
  4. Save. The VM now visually clusters around its host in the web view.

Multiple VMs on the same host all cluster naturally (the force layout
pulls them in close to their host since virtual edges have shorter link
distances than physical Ethernet).

Must be applied AFTER patch_edge_flow_status.py.

Run once from ~/netwatch/:
    python3 patch_virtual_vm.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_virtual.
Idempotent - safe to re-run.
"""

import os, shutil, sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_virtual"
SENTINEL = '"virtual"'  # presence in CONNECTION_TYPES means already patched


PATCHES = [
    # ──────────────────────────────────────────────────────────────────────
    # 1. Backend: add "virtual" to CONNECTION_TYPES
    # ──────────────────────────────────────────────────────────────────────
    (
        '''    CONNECTION_TYPES = ("ethernet", "fiber", "wifi", "power", "usb", "console", "other")''',
        '''    CONNECTION_TYPES = ("ethernet", "fiber", "wifi", "virtual", "power", "usb", "console", "other")'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 2. Drawer add-form dropdown: add Virtual option after WiFi.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''+ \'<label>Type<select class="conn-f-type"><option value="ethernet">Ethernet</option><option value="fiber">Fiber</option><option value="wifi">WiFi</option><option value="power">Power</option><option value="usb">USB</option><option value="console">Console</option><option value="other">Other</option></select></label>\'''',
        '''+ \'<label>Type<select class="conn-f-type"><option value="ethernet">Ethernet</option><option value="fiber">Fiber</option><option value="wifi">WiFi</option><option value="virtual">Virtual (VM \u2192 host)</option><option value="power">Power</option><option value="usb">USB</option><option value="console">Console</option><option value="other">Other</option></select></label>\''''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 3. Drawer connection list: add icon for virtual type.
    # Uses a stacked-rectangles glyph that suggests virtualization.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  const typeIcon = {
    ethernet: \'\u2500\u2500\',  // box drawing horizontal
    fiber:    \'\u2248\',        // wave (suggestive of light/optical)
    wifi:     \'\u29B0\',        // empty set / signal indicator
    power:    \'\u26a1\',        // lightning bolt
    usb:      \'\u21cc\',        // double arrow
    console:  \'\u2192\',        // arrow
    other:    \'\u00b7\',
  };''',
        '''  const typeIcon = {
    ethernet: \'\u2500\u2500\',  // box drawing horizontal
    fiber:    \'\u2248\',        // wave (suggestive of light/optical)
    wifi:     \'\u29B0\',        // empty set / signal indicator
    virtual:  \'\u25C8\',        // diamond (suggests "container/inside")
    power:    \'\u26a1\',        // lightning bolt
    usb:      \'\u21cc\',        // double arrow
    console:  \'\u2192\',        // arrow
    other:    \'\u00b7\',
  };'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 4. Topology: add virtual to the speed-by-type logic. Slightly
    # slower than ethernet (0.22) - VM traffic is real but feels softer.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''      // Speed varies by connection type
      let speed = 0.3;
      if(d.connection_type === \'fiber\')   speed = 0.5;
      if(d.connection_type === \'wifi\')    speed = 0.18;
      if(d.connection_type === \'power\')   speed = 0.15;
      if(d.connection_type === \'usb\')     speed = 0.4;
      if(d.connection_type === \'console\') speed = 0.2;''',
        '''      // Speed varies by connection type
      let speed = 0.3;
      if(d.connection_type === \'fiber\')   speed = 0.5;
      if(d.connection_type === \'wifi\')    speed = 0.18;
      if(d.connection_type === \'virtual\') speed = 0.22;
      if(d.connection_type === \'power\')   speed = 0.15;
      if(d.connection_type === \'usb\')     speed = 0.4;
      if(d.connection_type === \'console\') speed = 0.2;'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 5. Topology force layout: shorten the link distance for virtual edges.
    # VMs should cluster CLOSE to their host, not at the same distance as
    # physical Ethernet runs. We replace the existing forceLink config.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  const sim = d3.forceSimulation(renderNodes)
    .force('link', d3.forceLink(renderEdges).id(d => d.id).distance(110).strength(0.5))''',
        '''  const sim = d3.forceSimulation(renderNodes)
    .force('link', d3.forceLink(renderEdges).id(d => d.id)
      .distance(d => d.connection_type === 'virtual' ? 50 : 110)
      .strength(d => d.connection_type === 'virtual' ? 0.9 : 0.5))'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 6. VM detection + badge rendering. We compute the set of VM IDs
    # from the edges (any node that's the source of a virtual edge is a
    # VM). Then we add a badge to those nodes during the per-node render.
    # We anchor on the .each() block that renders node shapes.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  // Render the node body shape based on type
  nodeSel.each(function(d){
    const sel = d3.select(this);
    if(d.device_type === 'network' || d.device_type === 'ups'){
      const w = 110, h = 38;
      sel.append('rect')
        .attr('class', 'topo-node-shape')
        .attr('x', -w/2).attr('y', -h/2)
        .attr('width', w).attr('height', h)
        .attr('rx', 8).attr('ry', 8);
      sel.append('text').attr('class', 'topo-node-label-inside')
        .attr('y', 5).text(truncateLabel(d.name, 14));
    } else if(d.device_type === 'disk'){
      const s = 32;
      sel.append('rect')
        .attr('class', 'topo-node-shape')
        .attr('x', -s/2).attr('y', -s/2)
        .attr('width', s).attr('height', s)
        .attr('rx', 5).attr('ry', 5);
      sel.append('text').attr('class', 'topo-node-label-below')
        .attr('y', s/2 + 14).text(truncateLabel(d.name, 18));
    } else if(d.device_type === 'peripheral'){
      sel.append('circle').attr('class', 'topo-node-shape').attr('r', 14);
      sel.append('text').attr('class', 'topo-node-label-below')
        .attr('y', 14 + 14).text(truncateLabel(d.name, 18));
    } else {
      // host (default)
      sel.append('circle').attr('class', 'topo-node-shape').attr('r', 22);
      sel.append('text').attr('class', 'topo-node-label-below')
        .attr('y', 22 + 14).text(truncateLabel(d.name, 18));
    }
  });''',
        '''  // Compute the set of VM node IDs: any node that is the SOURCE of a
  // 'virtual' edge is treated as a VM. This is implicit detection - no
  // schema changes needed - and updates automatically when virtual
  // connections are added or removed.
  const vmIds = new Set();
  renderEdges.forEach(e => {
    if(e.connection_type === 'virtual'){
      vmIds.add(typeof e.source === 'object' ? e.source.id : e.source);
    }
  });

  // Helper: append a small "VM" badge to a node's group. Position is
  // type-dependent so it sits at the top-right of whatever shape we drew.
  function appendVmBadge(sel, dtype){
    let bx, by;
    if(dtype === 'network' || dtype === 'ups'){
      bx = 110/2 - 8;  by = -38/2 - 4;  // top-right of rectangle
    } else if(dtype === 'disk'){
      bx = 32/2 - 4;   by = -32/2 - 4;  // top-right of square
    } else if(dtype === 'peripheral'){
      bx = 14 - 4;     by = -14 - 4;    // top-right of small circle
    } else {
      bx = 22 - 4;     by = -22 - 4;    // top-right of host circle
    }
    const pillW = 22, pillH = 12;
    const g = sel.append('g').attr('class', 'topo-vm-badge')
      .attr('transform', 'translate(' + bx + ',' + by + ')');
    g.append('rect')
      .attr('x', -pillW/2).attr('y', -pillH/2)
      .attr('width', pillW).attr('height', pillH)
      .attr('rx', 4).attr('ry', 4);
    g.append('text')
      .attr('text-anchor', 'middle')
      .attr('y', 3.5)
      .text('VM');
  }

  // Render the node body shape based on type
  nodeSel.each(function(d){
    const sel = d3.select(this);
    if(d.device_type === 'network' || d.device_type === 'ups'){
      const w = 110, h = 38;
      sel.append('rect')
        .attr('class', 'topo-node-shape')
        .attr('x', -w/2).attr('y', -h/2)
        .attr('width', w).attr('height', h)
        .attr('rx', 8).attr('ry', 8);
      sel.append('text').attr('class', 'topo-node-label-inside')
        .attr('y', 5).text(truncateLabel(d.name, 14));
    } else if(d.device_type === 'disk'){
      const s = 32;
      sel.append('rect')
        .attr('class', 'topo-node-shape')
        .attr('x', -s/2).attr('y', -s/2)
        .attr('width', s).attr('height', s)
        .attr('rx', 5).attr('ry', 5);
      sel.append('text').attr('class', 'topo-node-label-below')
        .attr('y', s/2 + 14).text(truncateLabel(d.name, 18));
    } else if(d.device_type === 'peripheral'){
      sel.append('circle').attr('class', 'topo-node-shape').attr('r', 14);
      sel.append('text').attr('class', 'topo-node-label-below')
        .attr('y', 14 + 14).text(truncateLabel(d.name, 18));
    } else {
      // host (default)
      sel.append('circle').attr('class', 'topo-node-shape').attr('r', 22);
      sel.append('text').attr('class', 'topo-node-label-below')
        .attr('y', 22 + 14).text(truncateLabel(d.name, 18));
    }
    // VM badge if this node hosts a virtual outbound connection
    if(vmIds.has(d.id)){
      appendVmBadge(sel, d.device_type || 'host');
      sel.classed('topo-node-vm', true);
    }
  });'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 7. Tooltip enrichment: show "Virtual machine" line if this is a VM.
    # We need to read the same vmIds set, but buildNodeTip is a separate
    # function and runs at hover time. Easiest: stash vmIds on the data
    # object during render, then read it in the tip builder.
    # We modify buildNodeTip to check the existing topo-node-vm class
    # on the SVG node element, which is reliably set by render time.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''function buildNodeTip(d){
  const typeLabel = {host:'Host', network:'Network', ups:'UPS', disk:'Disk', peripheral:'Peripheral'}[d.device_type] || d.device_type;
  let html = '<div class="topo-tip-name">' + escapeHtml(d.name) + '</div>'
    + '<div class="topo-tip-meta">' + escapeHtml(typeLabel)
    + (d.category ? ' &middot; ' + escapeHtml(d.category) : '') + '</div>';''',
        '''function buildNodeTip(d){
  const typeLabel = {host:'Host', network:'Network', ups:'UPS', disk:'Disk', peripheral:'Peripheral'}[d.device_type] || d.device_type;
  // Check if the SVG group for this node has the VM class
  const isVm = !!(_topoSvg && !_topoSvg.select('g.topo-node[data-id="' + d.id + '"].topo-node-vm').empty());
  let html = '<div class="topo-tip-name">' + escapeHtml(d.name)
    + (isVm ? ' <span class="topo-tip-vm">VM</span>' : '') + '</div>'
    + '<div class="topo-tip-meta">' + escapeHtml(typeLabel)
    + (d.category ? ' &middot; ' + escapeHtml(d.category) : '') + '</div>';'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 8. CSS: virtual edge styling + VM badge styling. We anchor on the
    # existing wifi edge styles since virtual is its conceptual sibling.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''.topo-edge-wifi     .topo-edge-line{stroke:#3dc7c0;stroke-dasharray:1,4;opacity:.6}
.topo-edge-wifi     .topo-edge-flow{fill:#5fe0d8;opacity:.9}''',
        '''.topo-edge-wifi     .topo-edge-line{stroke:#3dc7c0;stroke-dasharray:1,4;opacity:.6}
.topo-edge-wifi     .topo-edge-flow{fill:#5fe0d8;opacity:.9}
.topo-edge-virtual  .topo-edge-line{stroke:#b07cd6;stroke-dasharray:3,2;opacity:.7;stroke-width:1.2}
.topo-edge-virtual  .topo-edge-flow{fill:#cfa0e8;opacity:.85}

/* VM badge */
.topo-vm-badge rect{fill:#b07cd6;stroke:#8a5ab2;stroke-width:.5}
.topo-vm-badge text{fill:#fff;font-family:\'DM Mono\',monospace;font-size:8px;font-weight:600;letter-spacing:.5px;pointer-events:none}
.topo-tip-vm{display:inline-block;background:rgba(176,124,214,.2);color:#b07cd6;font-family:\'DM Mono\',monospace;font-size:9px;letter-spacing:.05em;padding:1px 5px;border-radius:3px;vertical-align:middle;margin-left:4px}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 9. Bump version
    # ──────────────────────────────────────────────────────────────────────
    (
        'netwatch v3.21 - raspberry pi',
        'netwatch v3.22 - raspberry pi'
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

    if "topo-edge-dead" not in content:
        print("ERROR: This patch requires patch_edge_flow_status first.")
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

    if 'VERSION = "3.21"' in content:
        content = content.replace('VERSION = "3.21"', 'VERSION = "3.22"', 1)

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
    print("  2. For each of your VMs, open its inventory drawer and add a")
    print("     connection to its physical host. Pick 'Virtual (VM \u2192 host)'")
    print("     as the type. No port info needed.")
    print("  3. The web view will cluster each VM tightly around its host,")
    print("     show a small purple 'VM' badge in the corner, and animate")
    print("     a muted dashed purple edge between them.")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
