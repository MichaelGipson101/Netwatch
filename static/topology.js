// =============================================================
// Topology web view (D3 force-directed graph)
// =============================================================

// Default view: web for new visitors. Existing localStorage preference
// (if set) wins, so anyone who explicitly chose cards keeps cards.
let _topoView = localStorage.getItem('nw-topo-view') || 'web';

let _topoSimulation = null;

let _topoSvg = null;

let _topoResizeObserver = null;

let _topoData = { nodes: [], edges: [] };

let _topoZoom = null;

let _topoIncludeUnconnected = false;

let _topoLastStatus = {};  // id -> status (for change detection / pulse)

let _topoUserAdjusted = false;   // true once the user pans/zooms/drags

let _flowRaf = null;             // requestAnimationFrame id for flow dots

let _topoEdgeSel = null;         // d3 selection of edge groups; set by renderTopologyWeb

const _reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

// ── Layout state (plan 5) ───────────────────────────────────────────────────
const TOPO_LAYOUT_KEY = 'nw-topo-layout';

const TOPO_COLLAPSED_KEY = 'nw-topo-collapsed';

const TOPO_GHOSTS_KEY = 'nw-topo-ghosts';

let _topoLayout = localStorage.getItem(TOPO_LAYOUT_KEY) === 'tree' ? 'tree' : 'force';

let _topoShowGhosts = localStorage.getItem(TOPO_GHOSTS_KEY) !== '0';

let _topoTreeOrient = null;

let _topoRelayoutTimer = null;

// ── Pure helpers (unit-tested in node; keep brackets balanced in literals) ──
// guestCollapseMin: more than 6 guests under one parent starts collapsed as
// "+N guests". spacing: [sibling, level] px per orientation.
const TOPO_TREE_RULES = {guestCollapseMin: 7, spacing: {down: [84, 130], right: [90, 190]}};

function topoPortLabel(port){
  if(port === null || port === undefined) return '';
  const s = String(port).trim();
  if(!s) return '';
  const m = /^(?:port\s*)?0*([0-9]+)$/i.exec(s);
  return ':' + (m ? m[1] : s);
}

function topoTreeOrientation(w, h){
  return (w || 0) >= (h || 0) ? 'down' : 'right';
}

function topoParseCollapsed(raw){
  // {id: true|false}. A legacy array means "these ids are collapsed".
  try {
    const v = JSON.parse(raw || '{}');
    if(Array.isArray(v)){
      const o = {};
      v.forEach(id => { o[String(id)] = true; });
      return o;
    }
    return (v && typeof v === 'object') ? v : {};
  } catch(e){ return {}; }
}

function topoIsCollapsed(id, guestKids, prefs){
  const k = String(id);
  if(prefs && Object.prototype.hasOwnProperty.call(prefs, k)) return !!prefs[k];
  return guestKids >= TOPO_TREE_RULES.guestCollapseMin;
}

function topoScene(data, opts){
  const nodes = (data && data.nodes) || [];
  const edges = (data && data.edges) || [];
  const nodeIds = new Set(nodes.map(n => n.id));
  const ghostsAll = ((opts && opts.showGhosts && data && data.suggested_edges) || [])
    .filter(g => nodeIds.has(g.source) && nodeIds.has(g.target));
  const idOf = v => (v !== null && typeof v === 'object') ? v.id : v;
  const connected = new Set();
  edges.forEach(e => { connected.add(idOf(e.source)); connected.add(idOf(e.target)); });
  ghostsAll.forEach(g => { connected.add(g.source); connected.add(g.target); });
  const shown = (opts && opts.includeUnconnected) ? nodes : nodes.filter(n => connected.has(n.id));
  const ids = new Set(shown.map(n => n.id));
  return {
    nodes: shown,
    edges: edges.filter(e => ids.has(idOf(e.source)) && ids.has(idOf(e.target))),
    ghosts: ghostsAll.filter(g => ids.has(g.source) && ids.has(g.target)),
    unconnected: nodes.filter(n => !connected.has(n.id)).length,
  };
}

function topoBuildForest(nodes, edges, prefs){
  prefs = prefs || {};
  const items = {};
  nodes.forEach(n => {
    items[n.id] = {id: n.id, name: n.name || '', gateway: n.network_role === 'gateway', kids: []};
  });
  const primaryType = {};
  (edges || []).forEach(e => {
    const s = (e.source !== null && typeof e.source === 'object') ? e.source.id : e.source;
    if(e.is_primary) primaryType[s] = e.connection_type;
  });
  const hasParent = {};
  nodes.forEach(n => {
    const p = n.primary_parent_id;
    if(p !== null && p !== undefined && items[p] && p !== n.id){
      items[p].kids.push(items[n.id]);
      hasParent[n.id] = true;
    }
  });
  Object.keys(items).forEach(k => items[k].kids.sort((a, b) => a.name.localeCompare(b.name)));
  const sizeSeen = {};
  function size(it){
    if(sizeSeen[it.id]) return 0;
    sizeSeen[it.id] = true;
    return 1 + it.kids.reduce((acc, c) => acc + size(c), 0);
  }
  // anchor: hidden id -> the collapsed, visible ancestor it's folded into.
  const seen = {}, visible = [], info = {}, anchor = {};
  function countHidden(it, into){
    if(seen[it.id]) return 0;
    seen[it.id] = true;
    anchor[it.id] = into;
    return 1 + it.kids.reduce((acc, c) => acc + countHidden(c, into), 0);
  }
  function build(it){
    seen[it.id] = true;
    visible.push(it.id);
    const kids = it.kids.filter(c => !seen[c.id]);
    const guests = kids.filter(c => primaryType[c.id] === 'virtual').length;
    const collapsed = kids.length > 0 && topoIsCollapsed(it.id, guests, prefs);
    const out = {id: it.id, children: []};
    const meta = {collapsible: kids.length > 0, collapsed: collapsed, hidden: 0, kids: kids.length,
                  guestPill: kids.length > 0 && guests === kids.length};
    if(collapsed) kids.forEach(c => { meta.hidden += countHidden(c, it.id); });
    else out.children = kids.map(build);
    info[it.id] = meta;
    return out;
  }
  function rootOf(it){
    const guard = {};
    let cur = it;
    while(!guard[cur.id]){
      guard[cur.id] = true;
      const n = nodes.find(x => x.id === cur.id);
      const p = n ? n.primary_parent_id : null;
      if(p === null || p === undefined || !items[p] || !hasParent[cur.id]) return cur;
      cur = items[p];
    }
    return cur;
  }
  const gw = Object.keys(items).map(k => items[k]).find(it => it.gateway);
  const gwRoot = gw ? rootOf(gw) : null;
  const rootIds = nodes.filter(n => !hasParent[n.id]).map(n => n.id);
  const roots = rootIds.map(id => items[id]);
  const sizes = {};
  roots.forEach(r => { sizes[r.id] = size(r); });
  roots.sort((a, b) => {
    if(gwRoot && a.id === gwRoot.id) return -1;
    if(gwRoot && b.id === gwRoot.id) return 1;
    return (sizes[b.id] - sizes[a.id]) || a.name.localeCompare(b.name);
  });
  const trees = roots.map(build);
  // Anything unreachable (a cycle in bad data) becomes its own root.
  nodes.forEach(n => { if(!seen[n.id]) trees.push(build(items[n.id])); });
  return {trees: trees, visible: visible, info: info, anchor: anchor};
}

// Tree layout: a ghost whose end is folded into a collapsed subtree is drawn
// to that collapsed node instead of vanishing (real_* keep the true ends for
// the tooltip). A ghost folded entirely into one node isn't drawn.
function topoAnchorGhosts(ghosts, forest){
  const vis = new Set(forest.visible);
  const at = id => vis.has(id) ? id : forest.anchor[id];
  const out = [];
  (ghosts || []).forEach(g => {
    const s = at(g.source), t = at(g.target);
    if(s === undefined || t === undefined || s === t) return;
    if(s === g.source && t === g.target){ out.push(g); return; }
    out.push(Object.assign({}, g, {source: s, target: t, real_source: g.source, real_target: g.target}));
  });
  return out;
}

// "+N guests" counts only the guests themselves (anything under them is
// still hidden, but isn't a guest); a mixed subtree counts everything hidden.
function topoPillLabel(meta){
  if(!meta || !meta.collapsed || !(meta.hidden > 0)) return '';
  return meta.guestPill ? '+' + meta.kids + (meta.kids === 1 ? ' guest' : ' guests') : '+' + meta.hidden;
}

// Force layout: VMs tuck behind their host. A guest is the child of a primary
// virtual edge whose host isn't itself a guest; a guest with anything under
// it stays a normal node. Returns {hostOf: {guest: host}, guestsOf: {host: [guests]}}.
function topoGuestSplit(nodes, edges){
  const idOf = v => (v !== null && typeof v === 'object') ? v.id : v;
  const ids = new Set((nodes || []).map(n => n.id));
  const parents = new Set();
  (edges || []).forEach(e => parents.add(idOf(e.target)));
  const cand = {};
  (edges || []).forEach(e => {
    if(e.connection_type !== 'virtual' || e.is_primary === false) return;
    const g = idOf(e.source), h = idOf(e.target);
    if(ids.has(g) && ids.has(h) && g !== h && !parents.has(g) && cand[g] === undefined) cand[g] = h;
  });
  const hostOf = {}, guestsOf = {};
  Object.keys(cand).forEach(k => {
    const h = cand[k];
    if(cand[h] !== undefined) return;            // nested: the host is a guest too
    const g = isNaN(Number(k)) ? k : Number(k);
    hostOf[g] = h;
    (guestsOf[h] = guestsOf[h] || []).push(g);
  });
  return {hostOf: hostOf, guestsOf: guestsOf};
}

// n points on a ring around (cx, cy), starting at 12 o'clock; the radius
// grows so neighbours stay ~minGap apart.
function topoFanPositions(cx, cy, n, minR, minGap){
  const r = Math.max(minR || 85, ((minGap || 58) * n) / (2 * Math.PI));
  const out = [];
  for(let i = 0; i < n; i++){
    const a = -Math.PI / 2 + (2 * Math.PI * i) / Math.max(n, 1);
    out.push({x: cx + r * Math.cos(a), y: cy + r * Math.sin(a)});
  }
  return out;
}

// "+N" badge: red when a tucked-away guest is DOWN (only always-on hosts go
// DOWN; quiet ones show IDLE), so collapsing never hides an outage.
function topoGuestBadge(guestIds, nodeById){
  const down = (guestIds || []).some(id => ((nodeById[id] || {}).status || '').toUpperCase() === 'DOWN');
  return {label: '+' + (guestIds || []).length, down: down};
}

function topoTreePositions(trees, orient){
  const spacing = TOPO_TREE_RULES.spacing[orient] || TOPO_TREE_RULES.spacing.down;
  const sib = spacing[0], lvl = spacing[1];
  const pos = {};
  let offset = 0;
  trees.forEach(tree => {
    const root = d3.hierarchy(tree, t => t.children);
    d3.tree().nodeSize([sib, lvl])(root);
    let min = Infinity, max = -Infinity;
    root.each(nd => { min = Math.min(min, nd.x); max = Math.max(max, nd.x); });
    const shift = offset - min;
    root.each(nd => {
      const along = nd.x + shift;
      pos[nd.data.id] = orient === 'down' ? {x: along, y: nd.y} : {x: nd.y, y: along};
    });
    offset += (max - min) + sib * 1.5;
  });
  return pos;
}

// Flow-dot speeds (fraction of path per second) per connection type.
// Defined at module level so the flow loop can be restarted without a full re-render.
const _FLOW_SPEEDS = {ethernet:.10, fiber:.16, wifi:.06, virtual:.07, power:.05, usb:.13, console:.065, other:.10};

function _flowFrame(ts){
  if(!_topoEdgeSel) return;
  _topoEdgeSel.each(function(d){
    const len = this._flowLen;
    if(!len) return;
    const path = this.querySelector('path.topo-edge-line');
    const speed = _FLOW_SPEEDS[d.connection_type] || .10;
    const t = (ts / 1000 * speed) % 1;
    const fwd = this.querySelector('.topo-edge-flow-fwd');
    const rev = this.querySelector('.topo-edge-flow-rev');
    if(fwd){ const p = path.getPointAtLength(t * len); fwd.setAttribute('cx', p.x); fwd.setAttribute('cy', p.y); }
    if(rev){ const p = path.getPointAtLength((1 - (t + .5) % 1) * len); rev.setAttribute('cx', p.x); rev.setAttribute('cy', p.y); }
  });
  _flowRaf = requestAnimationFrame(_flowFrame);
}

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

const TOPO_LAST_LAYOUT_KEY = 'nw-topo-last-layout';

function loadTopoLastLayout(){
  try { return JSON.parse(localStorage.getItem(TOPO_LAST_LAYOUT_KEY) || '{}'); }
  catch(e){ return {}; }
}

function saveTopoLastLayout(nodes){
  const snapshot = {};
  nodes.forEach(n => { snapshot[n.id] = { x: n.x, y: n.y }; });
  localStorage.setItem(TOPO_LAST_LAYOUT_KEY, JSON.stringify(snapshot));
}

function setTopoView(view){
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
  }
  const grid = document.getElementById('topo-grid');
  const web  = document.getElementById('topo-web');
  // Body class lets CSS reposition the main metrics row when web is active.
  // Only applied when the topology sub-view itself is active - the other Lab sub-views use
  // the metrics normally. (Otherwise restoring a saved 'web' view on boot
  // would hide the summary cards even on non-topology sub-views.)
  const topoTabActive = nwCurrentSubview() === 'topology';
  document.body.classList.toggle('nw-topo-web', view === 'web' && topoTabActive);
  if(view === 'web'){
    if(grid) grid.style.display = 'none';
    if(web)  web.style.display  = 'block';
    initTopologyWeb();
  } else {
    if(_flowRaf){ cancelAnimationFrame(_flowRaf); _flowRaf = null; }
    if(web)  web.style.display  = 'none';
    if(grid) grid.style.display = '';
  }
}

async function initTopologyWeb(){
  syncTopoLayoutControls();
  const gt = document.getElementById('topo-ghost-toggle');
  if(gt) gt.checked = _topoShowGhosts;
  const container = document.getElementById('topo-web-svg-host');
  if(!container) return;
  // Show a loading message while D3 loads + data fetches
  container.innerHTML = '<div class="topo-web-loading">Loading topology...</div>';
  try {
    await ensureD3();
  } catch(e){
    container.innerHTML = '<div class="topo-web-loading topo-web-error">Could not load the graph library: '
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
  const scene = topoScene(_topoData, {includeUnconnected: _topoIncludeUnconnected,
                                      showGhosts: _topoShowGhosts});
  const unconLabel = document.getElementById('topo-uncon-count');
  if(unconLabel) unconLabel.textContent = scene.unconnected > 0 ? '(' + scene.unconnected + ')' : '';
  if(_topoSimulation){ _topoSimulation.stop(); _topoSimulation = null; }
  if(scene.nodes.length === 0){
    container.innerHTML = '<div class="topo-web-loading">No connections recorded yet. '
      + '<a href="#" class="topo-empty-link" onclick="setTab(\'connections\');return false;">'
      + 'Open Connections</a> to add some.</div>';
    return;
  }
  _topoUserAdjusted = false;
  let nodes = scene.nodes, forest = null, ghosts = scene.ghosts;
  if(_topoLayout === 'tree'){
    forest = topoBuildForest(nodes, scene.edges, topoLoadCollapsed());
    const vis = new Set(forest.visible);
    nodes = nodes.filter(n => vis.has(n.id));   // collapsed subtrees aren't drawn
    ghosts = topoAnchorGhosts(ghosts, forest);
  }
  let split = null;
  if(!forest){
    split = topoGuestSplit(nodes, scene.edges);
    ghosts = topoAnchorGhosts(ghosts, {
      visible: nodes.filter(n => split.hostOf[n.id] === undefined).map(n => n.id),
      anchor: split.hostOf});
  }
  const ctx = _topoBuildScene(container, nodes, scene.edges, ghosts);
  ctx.forest = forest;
  ctx.split = split;
  if(forest) _layoutTree(ctx); else _layoutForce(ctx);
  _topoLastStatus = {};
  ctx.renderNodes.forEach(n => { _topoLastStatus[n.id] = n.status; });
}

function _topoBuildScene(container, nodes, edges, ghosts){
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
  // Fresh edge objects each render, with ends resolved to the node copies
  // (d3.forceLink accepts object ends as-is; the tree layout needs them too).
  const idOf = v => (v !== null && typeof v === 'object') ? v.id : v;
  const renderEdges = edges
    .filter(e => nodeMap[idOf(e.source)] && nodeMap[idOf(e.target)])
    .map(e => Object.assign({}, e, {source: nodeMap[idOf(e.source)], target: nodeMap[idOf(e.target)]}));
  const ghostEdges = (ghosts || [])
    .filter(g => nodeMap[g.source] && nodeMap[g.target])
    .map(g => Object.assign({}, g, {source: nodeMap[g.source], target: nodeMap[g.target]}));

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
    .on('zoom', (ev) => {
      if(ev.sourceEvent) _topoUserAdjusted = true;   // ignore programmatic fits
      zoomG.attr('transform', ev.transform);
    });
  svg.call(_topoZoom);

  // Background dot pattern + status glow filters
  const defs = svg.append('defs');
  defs.append('pattern')
    .attr('id', 'topo-dot-grid')
    .attr('width', 24).attr('height', 24)
    .attr('patternUnits', 'userSpaceOnUse')
    .append('circle')
      .attr('cx', 1).attr('cy', 1).attr('r', 1)
      .attr('class', 'topo-grid-dot');

  // Two vignettes; CSS picks the right one per theme.
  [['topo-vignette-dark','rgba(0,0,0,0.4)'],['topo-vignette-light','rgba(15,18,24,0.07)']].forEach(([id,edge]) => {
    const g = defs.append('radialGradient').attr('id', id)
      .attr('cx','50%').attr('cy','50%').attr('r','70%');
    g.append('stop').attr('offset','60%').attr('stop-color','transparent');
    g.append('stop').attr('offset','100%').attr('stop-color', edge);
  });
  zoomG.append('rect')
    .attr('x', -2000).attr('y', -2000)
    .attr('width', 4000).attr('height', 4000)
    .attr('fill', 'url(#topo-dot-grid)')
    .attr('class', 'topo-grid-bg')
    .style('pointer-events', 'none');
  // Vignette overlay - sits ABOVE the zoom group so it stays anchored to
  // the viewport rather than zooming/panning with content.
  svg.append('rect')
    .attr('class', 'topo-vignette-rect')
    .attr('x', 0).attr('y', 0)
    .attr('width', '100%').attr('height', '100%')
    .style('pointer-events', 'none');

  // Edges
  _topoEdgeSel = null;  // clear until rebuilt below
  const edgeG = zoomG.append('g').attr('class', 'topo-edges');
  // Classify an edge by its endpoints' current statuses: 'alive' (both up
  // or up+unknown), 'degraded' (one degraded, none down/idle), 'dead'.
  function edgeState(edge){
    const ss = (edge.source && edge.source.status) || 'UNKNOWN';
    const ts = (edge.target && edge.target.status) || 'UNKNOWN';
    if(ss === 'DOWN' || ss === 'IDLE' || ts === 'DOWN' || ts === 'IDLE') return 'dead';
    if(ss === 'DEGRADED' || ts === 'DEGRADED' || ss === 'MAINTENANCE' || ts === 'MAINTENANCE') return 'degraded';
    return 'alive';
  }
  const edgeSel = edgeG.selectAll('g.topo-edge').data(renderEdges).join('g')
    .attr('class', d => 'topo-edge topo-edge-' + (d.connection_type || 'ethernet')
      + ' topo-edge-' + edgeState(d));
  _topoEdgeSel = edgeSel;  // expose for flow-loop restart without full re-render
  // Wider invisible hit-area path so hover/click on the edge is generous
  edgeSel.append('path').attr('class', 'topo-edge-hit');
  edgeSel.append('path').attr('class', 'topo-edge-line');
  // Two flow dots - one each direction - to represent bidirectional traffic.
  edgeSel.append('circle').attr('class', 'topo-edge-flow topo-edge-flow-fwd').attr('r', 2);
  edgeSel.append('circle').attr('class', 'topo-edge-flow topo-edge-flow-rev').attr('r', 2);
  // Parent-side port label (":11"), both layouts (spec §6.3)
  edgeSel.filter(d => !!topoPortLabel(d.to_port)).append('text')
    .attr('class', 'topo-edge-port').text(d => topoPortLabel(d.to_port));

  // Ghost (suggested) edges: dashed + "?", never part of any layout (spec §6.4).
  const ghostG = zoomG.append('g').attr('class', 'topo-ghosts');
  const ghostSel = ghostG.selectAll('g.topo-ghost').data(ghostEdges).join('g')
    .attr('class', 'topo-ghost')
    .on('click', (ev, g) => { ev.stopPropagation(); topologyOpenSuggestion(g.suggestion_id); });
  ghostSel.append('path').attr('class', 'topo-ghost-hit');
  ghostSel.append('path').attr('class', 'topo-ghost-line');
  ghostSel.append('text').attr('class', 'topo-ghost-q').attr('dy', '0.35em').text('?');

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
    .on('mouseleave', () => highlightNode(null, false));

  // A node is a VM if its device_type is 'vm' or (legacy) it's the child
  // end of a virtual edge.
  const vmIds = new Set();
  renderNodes.forEach(n => { if(n.device_type === 'vm') vmIds.add(n.id); });
  renderEdges.forEach(e => { if(e.connection_type === 'virtual') vmIds.add(e.source.id); });

  // Render the node body as a dimensional icon; status is conveyed by the
  // parent .topo-status-* class plus the CSS breathing/pulse animations.
  nodeSel.each(function(d){
    const sel = d3.select(this);
    let iconSize, hitR;
    if(d.device_type === 'network'){
      iconSize = 64; hitR = 30;
    } else if(d.device_type === 'ups'){
      iconSize = 56; hitR = 28;
    } else if(d.device_type === 'host'){
      iconSize = 52; hitR = 26;
    } else if(d.device_type === 'disk'){
      iconSize = 48; hitR = 24;
    } else if(d.device_type === 'vm'){
      iconSize = 44; hitR = 22;
    } else if(d.device_type === 'printer'){
      iconSize = 44; hitR = 22;
    } else {
      iconSize = 40; hitR = 20;
    }
    const iconHref = '#topo-icon-' + (d.device_type || 'host');
    sel.append('circle').attr('class', 'topo-node-hit').attr('r', hitR);
    sel.append('use')
      .attr('class', 'topo-node-icon')
      .attr('href', iconHref)
      .attr('x', -iconSize/2).attr('y', -iconSize/2)
      .attr('width', iconSize).attr('height', iconSize);
    sel.append('text')
      .attr('class', 'topo-node-label-below')
      .attr('y', iconSize/2 + 14)
      .text(truncateLabel(d.name, 20));
    if(vmIds.has(d.id)) sel.classed('topo-node-vm', true);
    // Stagger the ambient breathing so the network doesn't pulse in unison.
    const iconEl = sel.select('.topo-node-icon');
    if(!iconEl.empty()){
      const delay = ((d.id * 1.7) % 4).toFixed(2);
      iconEl.style('animation-delay', delay + 's');
    }
  });

  // Tooltip (shared positioning for nodes and edges)
  const tip = d3.select(container).append('div').attr('class', 'topo-tip').style('display', 'none');
  function placeTip(ev, html){
    const rect = container.getBoundingClientRect();
    tip.style('display', 'block').html(html);
    const tipNode = tip.node();
    const tipW = tipNode ? tipNode.offsetWidth : 240;
    const tipH = tipNode ? tipNode.offsetHeight : 60;
    const cx = ev.clientX - rect.left;
    const cy = ev.clientY - rect.top;
    const margin = 14;
    let x = cx + 12, y = cy + 12;
    if(x + tipW + margin > rect.width)  x = cx - tipW - 12;   // flip left
    if(y + tipH + margin > rect.height) y = cy - tipH - 12;   // flip above
    x = Math.max(8, x); y = Math.max(8, y);
    tip.style('left', x + 'px').style('top', y + 'px');
  }
  nodeSel.on('mousemove', function(ev, d){ placeTip(ev, buildNodeTip(d)); })
    .on('mouseleave.tip', () => tip.style('display', 'none'));
  edgeSel.on('mousemove', function(ev, e){
    placeTip(ev, buildEdgeTip(e, nodeMap));
    d3.select(this).classed('topo-edge-hovered', true);
  }).on('mouseleave.tip', function(){
    tip.style('display', 'none');
    d3.select(this).classed('topo-edge-hovered', false);
  });
  ghostSel.on('mousemove', function(ev, g){ placeTip(ev, buildGhostTip(g)); })
    .on('mouseleave.tip', () => tip.style('display', 'none'));

  function highlightNode(target, on){
    const id = target ? target.id : null;
    const linked = new Set();
    if(id !== null){
      linked.add(id);
      renderEdges.forEach(e => {
        if(e.source.id === id) linked.add(e.target.id);
        if(e.target.id === id) linked.add(e.source.id);
      });
    }
    nodeSel.each(function(n){
      d3.select(this).classed('dim',   on && !linked.has(n.id));
      d3.select(this).classed('focus', on && linked.has(n.id));
    });
    edgeSel.each(function(e){
      const touches = e.source.id === id || e.target.id === id;
      d3.select(this).classed('dim',   on && !touches);
      d3.select(this).classed('focus', on && touches);
    });
  }

  return {container: container, svg: svg, zoomG: zoomG, width: width, height: height,
          renderNodes: renderNodes, renderEdges: renderEdges, ghostEdges: ghostEdges,
          nodeMap: nodeMap, nodeSel: nodeSel, edgeSel: edgeSel, ghostSel: ghostSel,
          tip: tip, placeTip: placeTip, edgePath: _topoArcPath};
}

function _topoPositionAll(ctx){
  ctx.nodeSel.attr('transform', d => 'translate(' + d.x + ',' + d.y + ')');
  ctx.edgeSel.select('path.topo-edge-line').attr('d', ctx.edgePath);
  ctx.edgeSel.select('path.topo-edge-hit').attr('d', ctx.edgePath);
  ctx.edgeSel.select('text.topo-edge-port')
    .attr('x', d => d.source.x + (d.target.x - d.source.x) * 0.72)
    .attr('y', d => d.source.y + (d.target.y - d.source.y) * 0.72 - 4);
  ctx.edgeSel.each(function(){
    const path = this.querySelector('path.topo-edge-line');
    this._flowLen = path ? path.getTotalLength() : 0;   // cache while geometry changes
  });
  const ghostPath = d => 'M' + d.source.x + ',' + d.source.y + 'L' + d.target.x + ',' + d.target.y;
  ctx.ghostSel.select('path.topo-ghost-line').attr('d', ghostPath);
  ctx.ghostSel.select('path.topo-ghost-hit').attr('d', ghostPath);
  ctx.ghostSel.select('text.topo-ghost-q')
    .attr('x', d => (d.source.x + d.target.x) / 2)
    .attr('y', d => (d.source.y + d.target.y) / 2);
}

function _topoStartFlow(){
  // Time-based rAF loop; pauses when the tab is hidden, off under reduced motion.
  if(_flowRaf){ cancelAnimationFrame(_flowRaf); _flowRaf = null; }
  if(!_reducedMotion.matches) _flowRaf = requestAnimationFrame(_flowFrame);
}

function _topoObserveResize(ctx, onResize){
  // Keeps the SVG viewBox matching the container (fullscreen toggle, window
  // resize...). Re-created every render, so disconnect the previous one.
  if(_topoResizeObserver){
    try { _topoResizeObserver.disconnect(); } catch(e){}
  }
  if(typeof ResizeObserver === 'undefined') return;
  _topoResizeObserver = new ResizeObserver(entries => {
    for(const entry of entries){
      const newW = entry.contentRect.width;
      const newH = entry.contentRect.height;
      if(newW <= 0 || newH <= 0) continue;
      ctx.svg.attr('viewBox', '0 0 ' + newW + ' ' + newH);
      onResize(newW, newH);
    }
  });
  _topoResizeObserver.observe(ctx.container);
}

function _layoutForce(ctx){
  const guests = _topoSetupGuests(ctx);
  const sim = d3.forceSimulation(guests.simNodes)
    .force('link', d3.forceLink(guests.simEdges).id(d => d.id)
      .distance(d => d.connection_type === 'virtual' ? 50 : 110)
      .strength(d => d.connection_type === 'virtual' ? 0.9 : 0.5))
    .force('charge', d3.forceManyBody().strength(-450))
    .force('center', d3.forceCenter(ctx.width / 2, ctx.height / 2))
    .force('collide', d3.forceCollide().radius(d => nodeRadiusFor(d) + 10));
  _topoSimulation = sim;
  sim.on('end', () => saveTopoLastLayout(guests.snapshot()));
  ctx.edgePath = _topoArcPath;
  sim.on('tick', () => { guests.place(); _topoPositionAll(ctx); });

  _topoObserveResize(ctx, (newW, newH) => {
    // Re-centre the simulation and warm it gently so nodes ease over.
    if(!_topoSimulation) return;
    const cf = _topoSimulation.force('center');
    if(cf){
      cf.x(newW / 2).y(newH / 2);
      _topoSimulation.alphaTarget(0.05).restart();
      setTimeout(() => { if(_topoSimulation) _topoSimulation.alphaTarget(0); }, 800);
    }
  });

  ctx.nodeSel.filter(d => !guests.isGuest(d)).call(d3.drag()
    .on('start', (ev, d) => {
      if(!ev.active) sim.alphaTarget(0.3).restart();
      d.fx = d.x; d.fy = d.y;
      _topoUserAdjusted = true;
      if(_topoSvg) _topoSvg.classed('topo-dragging', true);
    })
    .on('drag', (ev, d) => { d.fx = ev.x; d.fy = ev.y; })
    .on('end', (ev, d) => {
      if(!ev.active) sim.alphaTarget(0);
      saveTopoPosition(d.id, d.fx, d.fy);   // survives reloads
      setTimeout(() => spreadOverlappingLabels(ctx.nodeSel), 600);
      if(_topoSvg) _topoSvg.classed('topo-dragging', false);
    }));

  _topoStartFlow();
  // Cool the simulation gradually, then frame it and untangle labels.
  sim.alpha(1).restart();
  setTimeout(() => {
    sim.alphaTarget(0);
    if(_topoView === 'web' && !_topoUserAdjusted) fitTopologyToView();
  }, 4000);
  setTimeout(() => spreadOverlappingLabels(guests.hostSel), 4500);
  setTimeout(() => spreadOverlappingLabels(guests.hostSel), 6500);
}

// Guests (VMs) are drawn but tucked onto their host: hover the host to fan
// them out, tap its "+N" badge to pin them open. They never join the
// simulation or the saved Force positions.
let _topoFanOpen = new Set(), _topoFanPinned = new Set(), _topoFanTimers = {};

function _topoSetupGuests(ctx){
  const split = ctx.split || {hostOf: {}, guestsOf: {}};
  const isGuest = d => split.hostOf[d.id] !== undefined;
  const nodeById = {};
  ctx.renderNodes.forEach(n => { nodeById[n.id] = n; });
  const openNow = new Set([..._topoFanPinned].filter(h => split.guestsOf[h]));
  _topoFanOpen = openNow;
  _topoFanPinned = new Set(openNow);
  const progress = {};                       // host -> 0 (tucked) .. 1 (fanned)
  Object.keys(split.guestsOf).forEach(h => { progress[h] = openNow.has(Number(h)) ? 1 : 0; });

  function place(){
    Object.keys(split.guestsOf).forEach(h => {
      const host = ctx.nodeMap[h];
      if(!host) return;
      const list = split.guestsOf[h];
      const t = progress[h] || 0;
      const pts = topoFanPositions(host.x, host.y, list.length);
      list.forEach((gid, i) => {
        const g = ctx.nodeMap[gid];
        if(!g) return;
        g.x = host.x + (pts[i].x - host.x) * t;
        g.y = host.y + (pts[i].y - host.y) * t;
      });
    });
  }

  function sync(hostId){
    const open = _topoFanOpen.has(hostId);
    ctx.nodeSel.filter(d => split.hostOf[d.id] === hostId).classed('topo-guest-shown', open);
    ctx.edgeSel.filter(e => split.hostOf[e.source.id] === hostId || split.hostOf[e.target.id] === hostId)
      .classed('topo-guest-shown', open);
    ctx.nodeSel.filter(d => d.id === hostId).classed('topo-fan-open', open);
  }

  function animate(hostId){
    const key = String(hostId);
    const from = progress[key] || 0, to = _topoFanOpen.has(hostId) ? 1 : 0;
    if(from === to) return;
    const dur = _reducedMotion.matches ? 0 : 220, start = performance.now();
    function step(now){
      const k = dur ? Math.min(1, (now - start) / dur) : 1;
      const e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
      progress[key] = from + (to - from) * e;
      place();
      _topoPositionAll(ctx);
      if(k < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  function setOpen(hostId, open){
    if(!split.guestsOf[hostId]) return;
    if(open === _topoFanOpen.has(hostId)) return;
    if(open) _topoFanOpen.add(hostId); else _topoFanOpen.delete(hostId);
    sync(hostId);
    animate(hostId);
  }
  function cancelClose(hostId){
    clearTimeout(_topoFanTimers[hostId]);
    delete _topoFanTimers[hostId];
  }
  function scheduleClose(hostId){
    cancelClose(hostId);
    if(_topoFanPinned.has(hostId)) return;
    // Grace period: long enough to move the pointer onto a VM.
    _topoFanTimers[hostId] = setTimeout(() => setOpen(hostId, false), 350);
  }

  ctx.nodeSel.classed('topo-guest', isGuest);
  ctx.edgeSel.classed('topo-guest-edge', e => isGuest(e.source) || isGuest(e.target));
  const hostSel = ctx.nodeSel.filter(d => !isGuest(d));
  hostSel.filter(d => !!split.guestsOf[d.id])
    .on('mouseenter.fan', (ev, d) => { cancelClose(d.id); setOpen(d.id, true); })
    .on('mouseleave.fan', (ev, d) => scheduleClose(d.id))
    .each(function(d){
      const b = topoGuestBadge(split.guestsOf[d.id], nodeById);
      const icon = this.querySelector('.topo-node-icon');
      const half = icon ? (parseFloat(icon.getAttribute('width')) || 44) / 2 : 22;
      const badge = d3.select(this).append('g')
        .attr('class', 'topo-guest-badge' + (b.down ? ' down' : ''))
        .attr('transform', 'translate(' + (half - 4) + ',' + (-half + 4) + ')')
        .attr('role', 'button')
        .attr('aria-label', (split.guestsOf[d.id].length) + ' VMs' + (b.down ? ', one or more down' : '') + ': show or hide')
        .on('click', ev => {
          ev.stopPropagation();          // the host itself still opens the drawer
          if(_topoFanPinned.has(d.id)){ _topoFanPinned.delete(d.id); setOpen(d.id, false); }
          else { _topoFanPinned.add(d.id); cancelClose(d.id); setOpen(d.id, true); }
        });
      badge.append('circle').attr('class', 'topo-guest-badge-hit').attr('r', 15);
      badge.append('circle').attr('class', 'topo-guest-badge-bg').attr('r', 10);
      badge.append('text').attr('dy', '0.35em').text(b.label);
    });
  ctx.nodeSel.filter(isGuest)
    .on('mouseenter.fan', (ev, d) => cancelClose(split.hostOf[d.id]))
    .on('mouseleave.fan', (ev, d) => scheduleClose(split.hostOf[d.id]));
  // Tapping empty canvas closes every fan (a phone never fires mouse-out).
  ctx.svg.on('click.fan', ev => {
    if(ev.target && ev.target.closest && ev.target.closest('.topo-node, .topo-ghost, .topo-edge')) return;
    _topoFanPinned.clear();
    [..._topoFanOpen].forEach(h => setOpen(h, false));
  });
  Object.keys(split.guestsOf).forEach(h => sync(Number(h)));

  return {
    isGuest: isGuest, hostSel: hostSel, place: place,
    simNodes: ctx.renderNodes.filter(d => !isGuest(d)),
    simEdges: ctx.renderEdges.filter(e => !isGuest(e.source) && !isGuest(e.target)),
    // Saved for the Overview preview: VMs in a tight ring around their host.
    snapshot: () => {
      const out = ctx.renderNodes.filter(d => !isGuest(d)).map(d => ({id: d.id, x: d.x, y: d.y}));
      Object.keys(split.guestsOf).forEach(h => {
        const host = ctx.nodeMap[h];
        if(!host) return;
        const pts = topoFanPositions(host.x, host.y, split.guestsOf[h].length, 30, 14);
        split.guestsOf[h].forEach((gid, i) => out.push({id: gid, x: pts[i].x, y: pts[i].y}));
      });
      return out;
    },
  };
}

function _topoArcPath(d){
  const dx = d.target.x - d.source.x;
  const dy = d.target.y - d.source.y;
  const dr = Math.sqrt(dx*dx + dy*dy) * 1.8;
  return 'M' + d.source.x + ',' + d.source.y
    + 'A' + dr + ',' + dr + ' 0 0,1 ' + d.target.x + ',' + d.target.y;
}

function topoLoadCollapsed(){
  return topoParseCollapsed(localStorage.getItem(TOPO_COLLAPSED_KEY));
}

function topologyToggleCollapse(id, collapsed){
  const prefs = topoLoadCollapsed();
  prefs[String(id)] = !!collapsed;
  localStorage.setItem(TOPO_COLLAPSED_KEY, JSON.stringify(prefs));
  renderTopologyWeb();
}

function setTopoLayout(layout){
  _topoLayout = layout === 'tree' ? 'tree' : 'force';
  localStorage.setItem(TOPO_LAYOUT_KEY, _topoLayout);
  syncTopoLayoutControls();
  if(_topoView === 'web') renderTopologyWeb();
}

function syncTopoLayoutControls(){
  const f = document.getElementById('topo-layout-force');
  const t = document.getElementById('topo-layout-tree');
  if(f){ f.classList.toggle('active', _topoLayout === 'force'); f.setAttribute('aria-pressed', String(_topoLayout === 'force')); }
  if(t){ t.classList.toggle('active', _topoLayout === 'tree');  t.setAttribute('aria-pressed', String(_topoLayout === 'tree')); }
  const reset = document.getElementById('topo-reset-btn');
  if(reset) reset.style.display = _topoLayout === 'tree' ? 'none' : '';   // pinning is Force-only
}

function _layoutTree(ctx){
  const orient = topoTreeOrientation(ctx.width, ctx.height);
  _topoTreeOrient = orient;
  const pos = topoTreePositions(ctx.forest.trees, orient);
  ctx.renderNodes.forEach(n => {
    const p = pos[n.id];
    if(p){ n.x = p.x; n.y = p.y; }
    n.fx = null; n.fy = null;           // Force pins don't apply here (and aren't touched)
  });
  ctx.edgeSel.classed('topo-edge-cross', d => !d.is_primary);
  ctx.edgePath = _topoTreePath(orient);
  _topoAddCollapseControls(ctx);
  _topoPositionAll(ctx);
  _topoStartFlow();
  _topoObserveResize(ctx, (newW, newH) => {
    if(topoTreeOrientation(newW, newH) !== _topoTreeOrient){
      clearTimeout(_topoRelayoutTimer);   // phone rotated: lay the tree out again
      _topoRelayoutTimer = setTimeout(() => { if(_topoView === 'web') renderTopologyWeb(); }, 250);
    } else if(!_topoUserAdjusted){
      fitTopologyToView();
    }
  });
  requestAnimationFrame(() => { if(!_topoUserAdjusted) fitTopologyToView(); });
  setTimeout(() => spreadOverlappingLabels(ctx.nodeSel), 120);
}

function _topoTreePath(orient){
  // Primary edges curve along the tree's flow; cross-links (non-primary)
  // are straight and styled faint/dashed via .topo-edge-cross.
  return d => {
    const s = d.source, t = d.target;
    if(!d.is_primary) return 'M' + s.x + ',' + s.y + 'L' + t.x + ',' + t.y;
    if(orient === 'down'){
      const my = (s.y + t.y) / 2;
      return 'M' + s.x + ',' + s.y + 'C' + s.x + ',' + my + ' ' + t.x + ',' + my + ' ' + t.x + ',' + t.y;
    }
    const mx = (s.x + t.x) / 2;
    return 'M' + s.x + ',' + s.y + 'C' + mx + ',' + s.y + ' ' + mx + ',' + t.y + ' ' + t.x + ',' + t.y;
  };
}

function _topoAddCollapseControls(ctx){
  const info = ctx.forest.info;
  ctx.nodeSel.each(function(d){
    const f = info[d.id];
    if(!f || !f.collapsible) return;
    const sel = d3.select(this);
    const r = nodeRadiusFor(d);
    const btn = sel.append('g')
      .attr('class', 'topo-collapse-btn')
      .attr('transform', 'translate(' + (r - 2) + ',' + (-r + 2) + ')')
      .attr('role', 'button')
      .attr('aria-label', f.collapsed ? 'Expand' : 'Collapse')
      .on('click', ev => { ev.stopPropagation(); topologyToggleCollapse(d.id, !f.collapsed); });
    btn.append('circle').attr('r', 9);
    btn.append('text').attr('dy', '0.35em').text(f.collapsed ? '⊞' : '⊟');
    const label = topoPillLabel(f);
    if(label){
      const pill = sel.append('g')
        .attr('class', 'topo-hidden-pill')
        .attr('transform', 'translate(0,' + (r + 34) + ')')
        .on('click', ev => { ev.stopPropagation(); topologyToggleCollapse(d.id, false); });
      const w = 12 + label.length * 6.2;
      pill.append('rect').attr('x', -w / 2).attr('y', -9).attr('width', w).attr('height', 18).attr('rx', 9);
      pill.append('text').attr('dy', '0.35em').text(label);
    }
  });
}

function nodeRadiusFor(d){
  if(d.device_type === 'network') return 34;
  if(d.device_type === 'ups')     return 32;
  if(d.device_type === 'disk')    return 28;
  if(d.device_type === 'vm')      return 26;
  if(d.device_type === 'printer') return 26;
  if(d.device_type === 'peripheral' || d.device_type === 'tablet'
     || d.device_type === 'phone') return 24;
  return 30; // host
}

function seedPosition(node, w, h){
  // Initial guess based on type. Force layout will refine this.
  const cx = w / 2, cy = h / 2;
  const t = node.device_type || 'host';
  if(t === 'network')    return { x: cx + (Math.random() - 0.5) * 60, y: cy + (Math.random() - 0.5) * 60 };
  if(t === 'ups')        return { x: cx + (Math.random() - 0.5) * 100, y: cy + 130 + (Math.random() - 0.5) * 50 };
  if(t === 'disk')       return { x: cx - 200 + (Math.random() - 0.5) * 80, y: cy + 100 + (Math.random() - 0.5) * 50 };
  if(t === 'peripheral' || t === 'tablet' || t === 'phone' || t === 'printer')
    return { x: cx + 200 + (Math.random() - 0.5) * 80, y: cy - 100 + (Math.random() - 0.5) * 50 };
  if(t === 'vm'){
    // VMs seed near the center so they're close to their host once
    // the virtual edge force pulls them in.
    return { x: cx + (Math.random() - 0.5) * 80, y: cy + (Math.random() - 0.5) * 80 };
  }
  // host: ring around the center
  const angle = Math.random() * Math.PI * 2;
  const r = 180 + Math.random() * 40;
  return { x: cx + Math.cos(angle) * r, y: cy + Math.sin(angle) * r };
}

function truncateLabel(s, max){
  if(!s) return '';
  if(s.length <= max) return s;
  // Prefer breaking at a space boundary if one exists in the back half
  // of the cut. This avoids ugly mid-word cuts like "TP Link 24-po..."
  // which become "TP Link 24-port..." or just "TP Link..." instead.
  const halfBack = Math.floor(max * 0.6);
  const lastSpace = s.lastIndexOf(' ', max - 1);
  if(lastSpace >= halfBack){
    return s.substring(0, lastSpace) + '\u2026';
  }
  return s.substring(0, max - 1) + '\u2026';
}

function buildNodeTip(d){
  const typeLabel = {host:'Host', vm:'VM', network:'Network', ups:'UPS', disk:'Disk',
    peripheral:'Peripheral', tablet:'Tablet', phone:'Phone', printer:'Printer'}[d.device_type] || d.device_type;
  // Check if the SVG group for this node has the VM class
  const isVm = !!(_topoSvg && !_topoSvg.select('g.topo-node[data-id="' + d.id + '"].topo-node-vm').empty());
  let html = '<div class="topo-tip-name">' + escapeHtml(d.name)
    + (isVm ? ' <span class="topo-tip-vm">VM</span>' : '') + '</div>'
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
  // Build MAC -> status and IP -> status maps for matching inventory nodes.
  const macStatus = {}, ipStatus = {};
  statusData.hosts.forEach(h => {
    const m = ((h.specs || {}).mac || '').replace(/[^0-9a-f]/gi, '').toLowerCase();
    if(m) macStatus[m] = { status: h.status, is_up: h.is_up };
    if(h.ip) ipStatus[h.ip] = { status: h.status, is_up: h.is_up };
  });
  // For each node: try MAC first, then linked_host IP, then keep/fallback.
  // This ensures VMs and peripherals with no MAC still get live status updates.
  _topoData.nodes.forEach(n => {
    const m = (n.mac || '').replace(/[^0-9a-f]/gi, '').toLowerCase();
    const match = macStatus[m] || (n.linked_host && n.linked_host.ip ? ipStatus[n.linked_host.ip] : null);
    const newStatus = match ? match.status : (n.linked_host ? n.status : 'UNKNOWN');
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
  // Recompute edge classes based on the new statuses. We rebuild the
  // node lookup from the live data, then walk every edge group and
  // update its alive/degraded/dead class.
  if(!_topoSvg) return;
  const liveNodeMap = {};
  _topoData.nodes.forEach(n => { liveNodeMap[n.id] = n; });
  _topoSvg.selectAll('g.topo-edge').each(function(d){
    const sId = typeof d.source === 'object' ? d.source.id : d.source;
    const tId = typeof d.target === 'object' ? d.target.id : d.target;
    const s = liveNodeMap[sId];
    const t = liveNodeMap[tId];
    const ss = (s && s.status) || 'UNKNOWN';
    const ts = (t && t.status) || 'UNKNOWN';
    let state;
    if(ss === 'DOWN' || ss === 'IDLE' || ts === 'DOWN' || ts === 'IDLE') state = 'dead';
    else if(ss === 'DEGRADED' || ts === 'DEGRADED' || ss === 'MAINTENANCE' || ts === 'MAINTENANCE') state = 'degraded';
    else state = 'alive';
    const node = d3.select(this);
    node.classed('topo-edge-alive',    state === 'alive');
    node.classed('topo-edge-degraded', state === 'degraded');
    node.classed('topo-edge-dead',     state === 'dead');
  });
}

let _resetArmTimer = null;

function topologyResetPositions(){
  const btn = document.getElementById('topo-reset-btn');
  if(!btn) return;
  if(btn.dataset.armed !== '1'){
    btn.dataset.armed = '1';
    btn.dataset.label = btn.textContent;
    btn.textContent = 'Confirm reset?';
    btn.style.color = 'var(--red-text)';
    _resetArmTimer = setTimeout(() => disarmReset(btn), 3000);
    return;
  }
  clearTimeout(_resetArmTimer);
  disarmReset(btn);
  clearTopoPositions();
  fetchAndRenderTopologyWeb();
}

function disarmReset(btn){
  btn.dataset.armed = '';
  if(btn.dataset.label) btn.textContent = btn.dataset.label;
  btn.style.color = '';
}

function topologyToggleUnconnected(checked){
  _topoIncludeUnconnected = checked;
  if(_topoView === 'web') renderTopologyWeb();
}

function topologyToggleGhosts(on){
  _topoShowGhosts = !!on;
  localStorage.setItem(TOPO_GHOSTS_KEY, on ? '1' : '0');
  if(_topoView === 'web') renderTopologyWeb();
}

function topologyOpenSuggestion(id){
  if(_topoFullscreen) exitTopologyFullscreen();
  // cxHighlightSuggestion switches tab itself, capturing the refresh seq
  // first - switching here too would just fire a second refresh.
  if(typeof cxHighlightSuggestion === 'function') cxHighlightSuggestion(id);
  else setTab('connections');
}

function buildGhostTip(g){
  const src = (typeof CX_SOURCE_LABELS !== 'undefined' && CX_SOURCE_LABELS[g.origin]) || g.origin || '';
  // Re-anchored onto a collapsed node: name the real (hidden) ends.
  const nameOf = (id, fallback) => {
    if(id === undefined) return fallback.name;
    const n = ((_topoData && _topoData.nodes) || []).find(x => x.id === id);
    return n ? n.name : fallback.name;
  };
  let html = '<div class="topo-tip-edge-type">Suggested' + (src ? ' by ' + escapeHtml(src) : '') + '</div>'
    + '<div class="topo-tip-name">' + escapeHtml(nameOf(g.real_source, g.source)) + ' <span class="topo-tip-arrow">→</span> '
    + escapeHtml(nameOf(g.real_target, g.target)) + '</div>';
  if(g.real_source !== undefined || g.real_target !== undefined){
    html += '<div class="topo-tip-meta">inside a collapsed group</div>';
  }
  if(g.parent_port) html += '<div class="topo-tip-meta">port ' + escapeHtml(g.parent_port) + '</div>';
  return html + '<div class="topo-tip-meta">Tap to review in Connections</div>';
}

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
  if(e.notes){
    html += '<div class="topo-tip-meta topo-tip-notes">' + escapeHtml(e.notes) + '</div>';
  }
  return html;
}

// Push apart overlapping labels. Runs after simulation cooldown and
// after node drag-end. Detects bounding-box collisions between label
// text elements and shifts colliding labels vertically (one up, one
// down from default position) until they no longer overlap.
function spreadOverlappingLabels(nodeSel){
  if(!nodeSel || nodeSel.empty()) return;
  // Collect all labels with their current positions and bounding boxes
  const labels = [];
  nodeSel.each(function(d){
    const labelEl = this.querySelector('.topo-node-label-below');
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
    else if(n.device_type === 'peripheral' || n.device_type === 'tablet'
            || n.device_type === 'phone' || n.device_type === 'printer') r = 28;
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

  // Get the SVG's actual rendered size from its DOM node
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
  // Clamp scale to the zoom's configured extent (0.1 to 4)
  scale = Math.max(0.15, Math.min(scale, 3));

  // Translate so the bbox center maps to the viewport center
  const tx = vpW / 2 - bboxCx * scale;
  const ty = vpH / 2 - bboxCy * scale;

  // Apply with a smooth transition. Use d3.zoom's transform helper.
  const d3Identity = d3.zoomIdentity.translate(tx, ty).scale(scale);
  _topoSvg.transition()
    .duration(550)
    .ease(d3.easeCubicInOut)
    .call(_topoZoom.transform, d3Identity);
}

// ── Fullscreen kiosk mode ──────────────────────────────────────────────
let _topoFullscreen = false;

function enterTopologyFullscreen(){
  if(_topoFullscreen) return;
  // Make sure we're actually in web mode - otherwise the button shouldn't
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
}

function exitTopologyFullscreen(){
  if(!_topoFullscreen) return;
  _topoFullscreen = false;
  document.body.classList.remove('topo-fullscreen-active');
  // The resize observer handles the container shrinking. We auto-fit
  // shortly after the transition so the graph re-frames nicely in the
  // smaller viewport.
  setTimeout(() => {
    if(typeof fitTopologyToView === 'function') fitTopologyToView();
  }, 450);
}

// Esc to exit fullscreen. Ignored if any modal is open or if we're
// not actually in fullscreen.
document.addEventListener('keydown', (ev) => {
  if(ev.key === 'Escape' && _topoFullscreen){
    // Only intercept Esc if no modal is currently open. If a modal IS
    // open the Esc should close it, not the fullscreen.
    const anyModalOpen = document.querySelector('.modal-overlay.open, .drawer.open');
    if(!anyModalOpen){
      exitTopologyFullscreen();
    }
  }
});

// Pause the flow loop when the browser tab is hidden; restart it on return.
// We only restart the RAF (not the full simulation) since node positions and
// the D3 graph are still intact — there is no need to blow away the layout.
document.addEventListener('visibilitychange', () => {
  if(document.hidden){
    if(_flowRaf){ cancelAnimationFrame(_flowRaf); _flowRaf = null; }
  } else if(_topoView === 'web' && _topoEdgeSel && !_reducedMotion.matches){
    if(!_flowRaf) _flowRaf = requestAnimationFrame(_flowFrame);
  }
});

nwStatus.subscribe(function(data){ updateTopologyWebStatus(data); });
nwOnReady(function(){ if(typeof setTopoView === 'function') setTopoView(_topoView); });

// Web-overlay metrics only apply on the topology sub-view in web mode; re-fetch when returning to it
// (initial load is handled by setTopoView on page boot).
['topology', 'connections', 'inventory'].forEach(function(name){
  nwOnSubview(name, function(){
    document.body.classList.toggle('nw-topo-web', name === 'topology' && _topoView === 'web');
    if(name === 'topology' && _topoD3Loaded && typeof fetchAndRenderTopologyWeb === 'function') fetchAndRenderTopologyWeb();
  });
});
