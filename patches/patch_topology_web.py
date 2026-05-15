#!/usr/bin/env python3
"""
netwatch patch: web topology view (force-directed graph).

Adds a Cards/Web toggle to the Topology tab. Clicking "Web" switches the
view from grouped status cards to a D3-powered force-directed graph
showing your inventory devices as nodes and your recorded connections
as edges. Status colors update live; nodes pulse on status changes;
edges have flowing dot animations; hover highlights connected paths;
drag to reposition (positions persist in localStorage).

What you see:
  - Hosts as circles
  - Network devices (switches/routers) as larger rounded rectangles
  - UPSes as distinctive rounded rectangles (yellow accent)
  - Disks as small rounded squares
  - Peripherals as small circles
  - Edges color-coded by connection type (ethernet/fiber/power/usb/console)
  - Live status borders (green/amber/red/neutral) on every node
  - Smooth pulse animation when a node's status transitions

Interactions:
  - Click a node -> opens that inventory record's drawer
  - Hover -> highlights node and its direct neighbors
  - Drag -> reposition, position pinned (saved to localStorage)
  - Mouse wheel -> zoom (10% to 400%)
  - Drag empty space -> pan
  - "Reset positions" button clears pinned positions and re-runs the layout

Endpoints:
  GET /api/topology  -> {nodes: [...], edges: [...]} bundling inventory
                        records + connections + linked-host status in
                        a single round trip

Backend dep: none new (D3 is loaded from cdnjs in the existing CDN
allowlist).

Must be applied AFTER patch_inv_connections.py.

Run once from ~/netwatch/:
    python3 patch_topology_web.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_topoweb.
Idempotent - safe to re-run.
"""

import os, shutil, sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_topoweb"
SENTINEL = "renderTopologyWeb"


# ─── Backend: bundled topology endpoint ──────────────────────────────────────

NEW_TOPOLOGY_HELPER = '''def build_topology_payload(inventory_db, host_manager):
    """Bundle inventory records + connections + linked-host status into a
    single payload for the topology view. Doing this server-side cuts the
    frontend from 3 round trips to 1 and lets us join MAC -> host status
    without serialising the full host list."""
    if not inventory_db:
        return {"nodes": [], "edges": []}

    # Build a MAC -> host status lookup
    host_by_mac = {}
    if host_manager:
        for h in host_manager.list_hosts():
            d = h.to_dict()
            mac = (d.get("specs") or {}).get("mac")
            norm = InventoryDB.normalize_mac(mac) if mac else ""
            if norm:
                host_by_mac[norm] = {
                    "name":   d.get("name"),
                    "ip":     d.get("ip"),
                    "is_up":  d.get("is_up"),
                    "status": d.get("status"),
                }

    nodes = []
    for rec in inventory_db.list_all():
        norm_mac = InventoryDB.normalize_mac(rec.get("mac")) if rec.get("mac") else ""
        linked = host_by_mac.get(norm_mac) if norm_mac else None
        nodes.append({
            "id":          rec["id"],
            "name":        rec.get("system") or "(unnamed)",
            "category":    rec.get("category"),
            "device_type": rec.get("device_type") or "host",
            "linked_host": linked,
            # Status inherits from linked host. Devices without a linked
            # monitored host (peripherals, switches we don\'t monitor) show
            # as UNKNOWN which renders as a neutral border.
            "status":      (linked["status"] if linked else "UNKNOWN"),
            "is_up":       (linked["is_up"] if linked else None),
            "ip":          rec.get("ip"),
            "mac":         rec.get("mac"),
        })

    edges = []
    for c in inventory_db.list_all_connections():
        edges.append({
            "id":              c["id"],
            "source":          c["from_device_id"],
            "target":          c["to_device_id"],
            "from_port":       c["from_port"],
            "to_port":         c["to_port"],
            "connection_type": c["connection_type"],
        })

    return {"nodes": nodes, "edges": edges}


'''


# ─── Frontend: D3 graph rendering ────────────────────────────────────────────

NEW_FRONTEND_JS = r'''// =============================================================
// Topology web view (D3 force-directed graph)
// =============================================================

let _topoView = localStorage.getItem('nw-topo-view') || 'cards';
let _topoSimulation = null;
let _topoSvg = null;
let _topoData = { nodes: [], edges: [] };
let _topoZoom = null;
let _topoIncludeUnconnected = false;
let _topoLastStatus = {};  // id -> status (for change detection / pulse)
let _topoD3Loaded = false;
let _topoD3Loading = null;  // Promise during load
const TOPO_POSITIONS_KEY = 'nw-topo-positions';

function loadTopoPositions(){
  try { return JSON.parse(localStorage.getItem(TOPO_POSITIONS_KEY) || '{}'); }
  catch(e){ return {}; }
}
function saveTopoPosition(id, x, y){
  const all = loadTopoPositions();
  all[id] = { x, y };
  localStorage.setItem(TOPO_POSITIONS_KEY, JSON.stringify(all));
}
function clearTopoPositions(){
  localStorage.removeItem(TOPO_POSITIONS_KEY);
}

function ensureD3(){
  if(_topoD3Loaded) return Promise.resolve();
  if(_topoD3Loading) return _topoD3Loading;
  _topoD3Loading = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = 'https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js';
    s.async = true;
    s.onload = () => { _topoD3Loaded = true; resolve(); };
    s.onerror = () => reject(new Error('Failed to load D3 from CDN'));
    document.head.appendChild(s);
  });
  return _topoD3Loading;
}

function setTopoView(view){
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
}

async function initTopologyWeb(){
  const container = document.getElementById('topo-web-svg-host');
  if(!container) return;
  // Show a loading message while D3 loads + data fetches
  container.innerHTML = '<div class="topo-web-loading">Loading topology...</div>';
  try {
    await ensureD3();
  } catch(e){
    container.innerHTML = '<div class="topo-web-loading topo-web-error">Could not load D3 from CDN: '
      + escapeHtml(e.message) + '</div>';
    return;
  }
  await fetchAndRenderTopologyWeb();
}

async function fetchAndRenderTopologyWeb(){
  try {
    const res = await fetch('/api/topology');
    if(!res.ok){
      const c = document.getElementById('topo-web-svg-host');
      if(c) c.innerHTML = '<div class="topo-web-loading topo-web-error">Failed to load topology data.</div>';
      return;
    }
    _topoData = await res.json();
    renderTopologyWeb();
  } catch(e){
    const c = document.getElementById('topo-web-svg-host');
    if(c) c.innerHTML = '<div class="topo-web-loading topo-web-error">Network error: '
      + escapeHtml(e.message) + '</div>';
  }
}

function renderTopologyWeb(){
  const container = document.getElementById('topo-web-svg-host');
  if(!container) return;

  // Filter nodes by connection presence (unless "include all" is on)
  const connectedIds = new Set();
  _topoData.edges.forEach(e => {
    connectedIds.add(typeof e.source === 'object' ? e.source.id : e.source);
    connectedIds.add(typeof e.target === 'object' ? e.target.id : e.target);
  });
  let nodes = _topoData.nodes;
  let edges = _topoData.edges;
  if(!_topoIncludeUnconnected){
    nodes = nodes.filter(n => connectedIds.has(n.id));
  }
  // Update unconnected count badge
  const unconCount = _topoData.nodes.length - connectedIds.size;
  const unconLabel = document.getElementById('topo-uncon-count');
  if(unconLabel) unconLabel.textContent = unconCount > 0 ? '(' + unconCount + ')' : '';

  if(nodes.length === 0){
    container.innerHTML = '<div class="topo-web-loading">No connections recorded yet. Open an inventory record to add connections, then come back here.</div>';
    return;
  }

  // Stable copies + restore pinned positions from localStorage
  const positions = loadTopoPositions();
  const nodeMap = {};
  const renderNodes = nodes.map(n => {
    const copy = Object.assign({}, n);
    const seed = seedPosition(copy, container.clientWidth || 800, container.clientHeight || 600);
    if(positions[copy.id]){
      copy.fx = positions[copy.id].x;
      copy.fy = positions[copy.id].y;
      copy.x  = positions[copy.id].x;
      copy.y  = positions[copy.id].y;
    } else {
      copy.x = seed.x;
      copy.y = seed.y;
    }
    nodeMap[copy.id] = copy;
    return copy;
  });
  // d3 mutates link source/target into refs; we need fresh objects each render
  const renderEdges = edges
    .filter(e => nodeMap[e.source] && nodeMap[e.target])
    .map(e => Object.assign({}, e));

  container.innerHTML = '';
  const width  = container.clientWidth  || 800;
  const height = container.clientHeight || 600;

  const svg = d3.select(container).append('svg')
    .attr('class', 'topo-web-svg')
    .attr('viewBox', '0 0 ' + width + ' ' + height)
    .attr('preserveAspectRatio', 'xMidYMid meet')
    .style('width',  '100%')
    .style('height', '100%');
  _topoSvg = svg;

  // Zoomable wrapper
  const zoomG = svg.append('g').attr('class', 'topo-zoom');
  _topoZoom = d3.zoom()
    .scaleExtent([0.1, 4])
    .on('zoom', (ev) => zoomG.attr('transform', ev.transform));
  svg.call(_topoZoom);

  // Background dot pattern
  const defs = svg.append('defs');
  defs.append('pattern')
    .attr('id', 'topo-dot-grid')
    .attr('width', 24).attr('height', 24)
    .attr('patternUnits', 'userSpaceOnUse')
    .append('circle')
      .attr('cx', 1).attr('cy', 1).attr('r', 1)
      .attr('class', 'topo-grid-dot');
  zoomG.append('rect')
    .attr('x', -2000).attr('y', -2000)
    .attr('width', 4000).attr('height', 4000)
    .attr('fill', 'url(#topo-dot-grid)')
    .style('pointer-events', 'none');

  // Force simulation
  const sim = d3.forceSimulation(renderNodes)
    .force('link', d3.forceLink(renderEdges).id(d => d.id).distance(110).strength(0.5))
    .force('charge', d3.forceManyBody().strength(-450))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('collide', d3.forceCollide().radius(d => nodeRadiusFor(d) + 10));
  _topoSimulation = sim;

  // Edges
  const edgeG = zoomG.append('g').attr('class', 'topo-edges');
  const edgeSel = edgeG.selectAll('g.topo-edge').data(renderEdges).join('g')
    .attr('class', d => 'topo-edge topo-edge-' + (d.connection_type || 'ethernet'));
  edgeSel.append('path').attr('class', 'topo-edge-line');
  // Animated flow dot
  edgeSel.append('circle').attr('class', 'topo-edge-flow').attr('r', 2);

  // Nodes
  const nodeG = zoomG.append('g').attr('class', 'topo-nodes');
  const nodeSel = nodeG.selectAll('g.topo-node').data(renderNodes).join('g')
    .attr('class', d => 'topo-node topo-node-' + d.device_type
      + ' topo-status-' + (d.status || 'UNKNOWN').toLowerCase())
    .attr('data-id', d => d.id)
    .on('click', (ev, d) => {
      // Don't open drawer if we just dragged
      if(ev.defaultPrevented) return;
      openInventoryDrawer(d.id);
    })
    .on('mouseenter', (ev, d) => highlightNode(d, true))
    .on('mouseleave', () => highlightNode(null, false))
    .call(d3.drag()
      .on('start', dragStart)
      .on('drag',  dragMove)
      .on('end',   dragEnd));

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
  });

  // Tooltip
  const tip = d3.select(container).append('div').attr('class', 'topo-tip').style('display', 'none');

  nodeSel.on('mousemove', function(ev, d){
    const rect = container.getBoundingClientRect();
    tip.style('left', (ev.clientX - rect.left + 12) + 'px')
       .style('top',  (ev.clientY - rect.top  + 12) + 'px')
       .style('display', 'block')
       .html(buildNodeTip(d));
  }).on('mouseleave.tip', () => tip.style('display', 'none'));

  // Tick handler: update positions + curved edges + flowing dot animation
  let tickFrame = 0;
  sim.on('tick', () => {
    tickFrame++;
    nodeSel.attr('transform', d => 'translate(' + d.x + ',' + d.y + ')');
    edgeSel.select('path.topo-edge-line').attr('d', d => {
      const dx = d.target.x - d.source.x;
      const dy = d.target.y - d.source.y;
      const dr = Math.sqrt(dx*dx + dy*dy) * 1.8;
      return 'M' + d.source.x + ',' + d.source.y
        + 'A' + dr + ',' + dr + ' 0 0,1 ' + d.target.x + ',' + d.target.y;
    });
    // Animated flow dot: position it along the path based on tick frame
    edgeSel.select('circle.topo-edge-flow').each(function(d){
      const path = this.parentNode.querySelector('path.topo-edge-line');
      if(!path) return;
      const len = path.getTotalLength();
      if(!len) return;
      // Speed varies by connection type
      let speed = 0.3;
      if(d.connection_type === 'fiber')   speed = 0.5;
      if(d.connection_type === 'power')   speed = 0.15;
      if(d.connection_type === 'usb')     speed = 0.4;
      if(d.connection_type === 'console') speed = 0.2;
      const t = ((tickFrame * speed) % 100) / 100;
      const pt = path.getPointAtLength(t * len);
      d3.select(this).attr('cx', pt.x).attr('cy', pt.y);
    });
  });

  // Cool the simulation gradually
  sim.alpha(1).restart();
  setTimeout(() => sim.alphaTarget(0.02).restart(), 4000);

  // Capture _topoLastStatus for change detection
  _topoLastStatus = {};
  renderNodes.forEach(n => { _topoLastStatus[n.id] = n.status; });

  function dragStart(ev, d){
    if(!ev.active) sim.alphaTarget(0.3).restart();
    d.fx = d.x; d.fy = d.y;
  }
  function dragMove(ev, d){
    d.fx = ev.x; d.fy = ev.y;
  }
  function dragEnd(ev, d){
    if(!ev.active) sim.alphaTarget(0.02);
    // Persist the pinned position so it survives reloads
    saveTopoPosition(d.id, d.fx, d.fy);
  }

  function highlightNode(target, on){
    const id = target ? target.id : null;
    const linked = new Set();
    if(id !== null){
      linked.add(id);
      renderEdges.forEach(e => {
        const sId = typeof e.source === 'object' ? e.source.id : e.source;
        const tId = typeof e.target === 'object' ? e.target.id : e.target;
        if(sId === id) linked.add(tId);
        if(tId === id) linked.add(sId);
      });
    }
    nodeSel.classed('dim',   on && id !== null && !linked.has(id));
    nodeSel.classed('focus', on && id !== null);
    nodeSel.each(function(n){
      d3.select(this).classed('dim',   on && !linked.has(n.id));
      d3.select(this).classed('focus', on && linked.has(n.id));
    });
    edgeSel.each(function(e){
      const sId = typeof e.source === 'object' ? e.source.id : e.source;
      const tId = typeof e.target === 'object' ? e.target.id : e.target;
      const inv = on && !(sId === id || tId === id);
      d3.select(this).classed('dim',   inv);
      d3.select(this).classed('focus', on && (sId === id || tId === id));
    });
  }
}

function nodeRadiusFor(d){
  if(d.device_type === 'network' || d.device_type === 'ups') return 60;
  if(d.device_type === 'disk') return 22;
  if(d.device_type === 'peripheral') return 16;
  return 24; // host
}

function seedPosition(node, w, h){
  // Initial guess based on type. Force layout will refine this.
  const cx = w / 2, cy = h / 2;
  const t = node.device_type || 'host';
  if(t === 'network')    return { x: cx + (Math.random() - 0.5) * 60, y: cy + (Math.random() - 0.5) * 60 };
  if(t === 'ups')        return { x: cx + (Math.random() - 0.5) * 100, y: cy + 130 + (Math.random() - 0.5) * 50 };
  if(t === 'disk')       return { x: cx - 200 + (Math.random() - 0.5) * 80, y: cy + 100 + (Math.random() - 0.5) * 50 };
  if(t === 'peripheral') return { x: cx + 200 + (Math.random() - 0.5) * 80, y: cy - 100 + (Math.random() - 0.5) * 50 };
  // host: ring around the center
  const angle = Math.random() * Math.PI * 2;
  const r = 180 + Math.random() * 40;
  return { x: cx + Math.cos(angle) * r, y: cy + Math.sin(angle) * r };
}

function truncateLabel(s, max){
  if(!s) return '';
  return s.length > max ? s.substring(0, max - 1) + '\u2026' : s;
}

function buildNodeTip(d){
  const typeLabel = {host:'Host', network:'Network', ups:'UPS', disk:'Disk', peripheral:'Peripheral'}[d.device_type] || d.device_type;
  let html = '<div class="topo-tip-name">' + escapeHtml(d.name) + '</div>'
    + '<div class="topo-tip-meta">' + escapeHtml(typeLabel)
    + (d.category ? ' &middot; ' + escapeHtml(d.category) : '') + '</div>';
  if(d.linked_host){
    html += '<div class="topo-tip-row">'
      + '<span class="topo-tip-status topo-status-' + (d.status || '').toLowerCase() + '">'
      + escapeHtml(d.status) + '</span>'
      + ' <span class="topo-tip-ip">' + escapeHtml(d.linked_host.ip) + '</span></div>';
  } else if(d.ip){
    html += '<div class="topo-tip-row"><span class="topo-tip-ip">' + escapeHtml(d.ip) + '</span></div>';
  }
  return html;
}

// Live status update: called from the existing 5s refresh cycle. Only
// updates classes if status changed; pulses on transition.
function updateTopologyWebStatus(statusData){
  if(_topoView !== 'web' || !_topoSvg) return;
  if(!statusData || !statusData.hosts) return;
  // Build MAC -> status map from hosts
  const macStatus = {};
  statusData.hosts.forEach(h => {
    const m = ((h.specs || {}).mac || '').replace(/[^0-9a-f]/gi, '').toLowerCase();
    if(m) macStatus[m] = { status: h.status, is_up: h.is_up };
  });
  // For each node in our cached data, compare new vs previous status
  _topoData.nodes.forEach(n => {
    const m = (n.mac || '').replace(/[^0-9a-f]/gi, '').toLowerCase();
    const newStatus = macStatus[m] ? macStatus[m].status : (n.linked_host ? n.status : 'UNKNOWN');
    const prev = _topoLastStatus[n.id];
    if(prev !== undefined && prev !== newStatus){
      // Status changed - pulse the node
      const sel = _topoSvg.select('g.topo-node[data-id="' + n.id + '"]');
      if(!sel.empty()){
        sel.classed('topo-status-' + (prev || 'unknown').toLowerCase(), false);
        sel.classed('topo-status-' + (newStatus || 'unknown').toLowerCase(), true);
        sel.classed('topo-pulsing', true);
        setTimeout(() => sel.classed('topo-pulsing', false), 1600);
      }
      n.status = newStatus;
    }
    _topoLastStatus[n.id] = newStatus;
  });
}

function topologyResetPositions(){
  if(!confirm('Reset all node positions and re-run the layout?')) return;
  clearTopoPositions();
  // Force a fresh render
  fetchAndRenderTopologyWeb();
}

function topologyToggleUnconnected(checked){
  _topoIncludeUnconnected = checked;
  if(_topoView === 'web') renderTopologyWeb();
}

'''


PATCHES = [
    # ──────────────────────────────────────────────────────────────────────
    # 1. Insert build_topology_payload helper near other helper functions.
    # We anchor before the existing build_api_payload function.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''def build_api_payload(host_manager, settings, incident_log=None):''',
        NEW_TOPOLOGY_HELPER + '''def build_api_payload(host_manager, settings, incident_log=None):'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 2. Add the GET /api/topology endpoint. Insert before /api/connections
    # in the do_GET handler so both topology endpoints sit together.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''            if self.path == "/api/connections":
                # All-connections list (used by the topology viz)
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"})
                    return
                try:
                    self._send_json(200, {"items": inventory_db.list_all_connections()})
                except Exception as e:
                    logging.exception("connections list error")
                    self._send_json(500, {"error": str(e)})
                return''',
        '''            if self.path == "/api/topology":
                # Bundled inventory + connections + linked-host status,
                # for the topology web view. Open to viewers (matches
                # the regular inventory list endpoint).
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"})
                    return
                try:
                    payload = build_topology_payload(inventory_db, host_manager)
                    self._send_json(200, payload)
                except Exception as e:
                    logging.exception("topology fetch error")
                    self._send_json(500, {"error": str(e)})
                return

            if self.path == "/api/connections":
                # All-connections list (used by the topology viz)
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"})
                    return
                try:
                    self._send_json(200, {"items": inventory_db.list_all_connections()})
                except Exception as e:
                    logging.exception("connections list error")
                    self._send_json(500, {"error": str(e)})
                return'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 3. HTML: replace the topology view with a layout that has both the
    # cards container AND the new web container, plus a Cards/Web toggle.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  <div class="view" id="view-topology">
    <div class="problem-banner" id="problem-banner">
      <div class="problem-banner-hdr">
        <div class="problem-banner-icon">!</div>
        <div class="problem-banner-title" id="problem-banner-title">Hosts offline</div>
      </div>
      <div class="problem-banner-list" id="problem-banner-list"></div>
    </div>
    <div class="topo-grid" id="topo-grid"></div>
  </div>''',
        '''  <div class="view" id="view-topology">
    <div class="problem-banner" id="problem-banner">
      <div class="problem-banner-hdr">
        <div class="problem-banner-icon">!</div>
        <div class="problem-banner-title" id="problem-banner-title">Hosts offline</div>
      </div>
      <div class="problem-banner-list" id="problem-banner-list"></div>
    </div>
    <div class="topo-view-toolbar">
      <div class="topo-view-toggle">
        <button class="topo-view-btn" id="topo-view-cards" onclick="setTopoView('cards')">Cards</button>
        <button class="topo-view-btn" id="topo-view-web" onclick="setTopoView('web')">Web</button>
      </div>
      <div class="topo-web-controls" id="topo-web-controls">
        <label class="topo-web-toggle">
          <input type="checkbox" onchange="topologyToggleUnconnected(this.checked)">
          Include unconnected <span id="topo-uncon-count" class="topo-uncon-count"></span>
        </label>
        <button class="topo-view-btn topo-view-btn-ghost" onclick="topologyResetPositions()">Reset positions</button>
      </div>
    </div>
    <div class="topo-grid" id="topo-grid"></div>
    <div class="topo-web" id="topo-web" style="display:none">
      <div class="topo-web-svg-host" id="topo-web-svg-host"></div>
    </div>
  </div>'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 4. Insert the new D3 frontend code. Anchor on a stable later JS
    # function so the new code is in scope of escapeHtml etc.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''function renderTopology(data){''',
        NEW_FRONTEND_JS + '''function renderTopology(data){'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 5. Wire updateTopologyWebStatus into the existing data refresh cycle
    # so live status updates propagate. We anchor on the existing
    # renderTopology(data) call inside the dashboard refresh.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''    renderTopology(data);''',
        '''    renderTopology(data);
    updateTopologyWebStatus(data);'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 6. Initialize the toggle state on page load. We anchor on the
    # existing initialTab line inside the DOMContentLoaded handler so our
    # init runs in the same context where the buttons exist.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  const initialTab = localStorage.getItem('nw-tab') || 'topology';
  setTab(initialTab);''',
        '''  const initialTab = localStorage.getItem('nw-tab') || 'topology';
  setTab(initialTab);
  // Restore Cards/Web view preference for the topology tab
  if(typeof setTopoView === 'function') setTopoView(_topoView);'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 7. CSS. Big block - styles for the toggle, the SVG canvas, nodes,
    # edges, animations, status colors. Anchor on existing problem-banner
    # CSS which is in the same conceptual area.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''.problem-banner.show{display:block;animation:slideIn .25s ease-out}''',
        '''.problem-banner.show{display:block;animation:slideIn .25s ease-out}

/* ── Topology view toggle + web view ─────────────────────────────────── */
.topo-view-toolbar{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:14px;flex-wrap:wrap}
.topo-view-toggle{display:inline-flex;background:var(--subtle);border:1px solid var(--border);border-radius:8px;padding:3px;gap:2px}
.topo-view-btn{background:transparent;border:none;color:var(--muted);cursor:pointer;padding:6px 14px;border-radius:6px;font-family:'DM Sans',sans-serif;font-size:13px;font-weight:500;transition:all .15s}
.topo-view-btn:hover{color:var(--text)}
.topo-view-btn.active{background:var(--surface);color:var(--text);box-shadow:0 1px 2px rgba(0,0,0,.1)}
.topo-view-btn-ghost{border:1px solid var(--border);background:transparent;color:var(--muted);font-size:11px;padding:5px 10px}
.topo-view-btn-ghost:hover{background:var(--subtle);color:var(--text)}
.topo-web-controls{display:flex;align-items:center;gap:14px;font-size:12px;color:var(--muted)}
.topo-web-toggle{display:inline-flex;align-items:center;gap:6px;cursor:pointer;user-select:none}
.topo-web-toggle input{cursor:pointer}
.topo-uncon-count{font-family:'DM Mono',monospace;font-size:11px;color:var(--hint)}
@media (max-width:768px){.topo-view-toolbar{flex-direction:column;align-items:stretch}.topo-web-controls{justify-content:space-between}}

.topo-web{position:relative;background:var(--bg);border:1px solid var(--border);border-radius:12px;overflow:hidden;height:min(70vh,800px);min-height:500px}
.topo-web-svg-host{width:100%;height:100%;position:relative}
.topo-web-loading{display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:13px;font-style:italic;padding:40px;text-align:center}
.topo-web-error{color:var(--red)}
.topo-web-svg{display:block;cursor:grab}
.topo-web-svg:active{cursor:grabbing}

/* Background grid dots */
.topo-grid-dot{fill:var(--border-light);opacity:.4}

/* Edges */
.topo-edge{transition:opacity .25s}
.topo-edge.dim{opacity:.15}
.topo-edge-line{fill:none;stroke:var(--border);stroke-width:1.5;opacity:.7;transition:stroke .25s,stroke-width .25s}
.topo-edge.focus .topo-edge-line{stroke-width:2.5;opacity:1}
.topo-edge-flow{fill:var(--blue);opacity:.85}
.topo-edge-ethernet .topo-edge-line{stroke:#5b8eff}
.topo-edge-ethernet .topo-edge-flow{fill:#5b8eff}
.topo-edge-fiber    .topo-edge-line{stroke:#a872d6;stroke-dasharray:4,2}
.topo-edge-fiber    .topo-edge-flow{fill:#c89af0}
.topo-edge-power    .topo-edge-line{stroke:#f0a93b;stroke-dasharray:8,3}
.topo-edge-power    .topo-edge-flow{fill:#ffd070}
.topo-edge-usb      .topo-edge-line{stroke:#5dbb8d;stroke-dasharray:2,3}
.topo-edge-usb      .topo-edge-flow{fill:#7dd6a8}
.topo-edge-console  .topo-edge-line{stroke:#888;stroke-dasharray:1,3}
.topo-edge-console  .topo-edge-flow{fill:#aaa}
.topo-edge-other    .topo-edge-line{stroke:var(--muted)}
.topo-edge-other    .topo-edge-flow{fill:var(--muted)}

/* Nodes */
.topo-node{cursor:pointer;transition:opacity .25s}
.topo-node.dim{opacity:.25}
.topo-node-shape{fill:var(--surface);stroke:var(--border);stroke-width:2;transition:stroke .3s,stroke-width .3s,fill .3s}
.topo-node:hover .topo-node-shape,.topo-node.focus .topo-node-shape{stroke-width:3}

/* Status colors (border) */
.topo-status-up        .topo-node-shape{stroke:#5dbb8d}
.topo-status-degraded  .topo-node-shape{stroke:#f0a93b}
.topo-status-down      .topo-node-shape{stroke:#e57373}
.topo-status-idle      .topo-node-shape{stroke:#7a7a7a}
.topo-status-unknown   .topo-node-shape{stroke:var(--border-light)}

/* Type-specific fills */
.topo-node-network .topo-node-shape{fill:#1a2540}
.topo-node-ups     .topo-node-shape{fill:#3a2e15}
.topo-node-disk    .topo-node-shape{fill:#1f2a25}
.topo-node-peripheral .topo-node-shape{fill:var(--subtle)}

/* Labels */
.topo-node-label-below{text-anchor:middle;fill:var(--text);font-family:'DM Sans',sans-serif;font-size:11px;font-weight:500;pointer-events:none;paint-order:stroke;stroke:var(--bg);stroke-width:3;stroke-linejoin:round}
.topo-node-label-inside{text-anchor:middle;fill:var(--text);font-family:'DM Sans',sans-serif;font-size:11px;font-weight:500;pointer-events:none}

/* Status pulse on transition */
@keyframes topo-pulse{
  0%   {filter:drop-shadow(0 0 0 var(--blue));transform:scale(1)}
  50%  {filter:drop-shadow(0 0 12px var(--blue));transform:scale(1.15)}
  100% {filter:drop-shadow(0 0 0 var(--blue));transform:scale(1)}
}
.topo-pulsing .topo-node-shape{animation:topo-pulse 1.6s ease-in-out}
.topo-status-down.topo-pulsing .topo-node-shape{animation:topo-pulse 1.6s ease-in-out;filter:drop-shadow(0 0 12px #e57373)}
.topo-status-up.topo-pulsing   .topo-node-shape{animation:topo-pulse 1.6s ease-in-out;filter:drop-shadow(0 0 12px #5dbb8d)}

/* Tooltip */
.topo-tip{position:absolute;pointer-events:none;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:8px 11px;box-shadow:0 4px 16px rgba(0,0,0,.25);font-size:12px;z-index:50;max-width:240px}
.topo-tip-name{font-weight:600;color:var(--text);margin-bottom:2px}
.topo-tip-meta{font-size:10px;color:var(--muted);font-family:'DM Mono',monospace;letter-spacing:.04em;text-transform:uppercase}
.topo-tip-row{margin-top:5px;display:flex;align-items:center;gap:6px}
.topo-tip-status{font-family:'DM Mono',monospace;font-size:9px;letter-spacing:.07em;text-transform:uppercase;padding:1px 6px;border-radius:3px;background:var(--subtle);color:var(--muted)}
.topo-tip-status.topo-status-up{background:rgba(93,187,141,.15);color:#5dbb8d}
.topo-tip-status.topo-status-degraded{background:rgba(240,169,59,.15);color:#f0a93b}
.topo-tip-status.topo-status-down{background:rgba(229,115,115,.15);color:#e57373}
.topo-tip-ip{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted)}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 8. Bump version
    # ──────────────────────────────────────────────────────────────────────
    (
        'netwatch v3.18 - raspberry pi',
        'netwatch v3.19 - raspberry pi'
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

    if "inventory_connections" not in content:
        print("ERROR: This patch requires patch_inv_connections first.")
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

    if 'VERSION = "3.18"' in content:
        content = content.replace('VERSION = "3.18"', 'VERSION = "3.19"', 1)

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
    print("  2. Open the Topology tab. Top-right has a Cards/Web toggle.")
    print("  3. Click Web. D3 loads from CDN, then renders your inventory")
    print("     as a force-directed graph using the connections you recorded.")
    print("  4. Hover, drag, zoom, click. Drag positions persist; use")
    print("     'Reset positions' to clear them.")
    print()
    print("Tip: if the graph looks sparse, that's because not all your")
    print("inventory has connections recorded. Toggle 'Include unconnected'")
    print("to see floating loose devices, or go add more connections to")
    print("flesh out the structure.")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
