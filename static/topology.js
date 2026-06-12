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
let _topoD3Loaded = false;
let _topoD3Loading = null;  // Promise during load
let _topoUserAdjusted = false;   // true once the user pans/zooms/drags
let _flowRaf = null;             // requestAnimationFrame id for flow dots
const _reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
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
    // Vendored copy — no {{VERSION}} templating inside JS files, and the
    // file content is immutable for this filename, so a bare URL is safe.
    s.src = '/static/d3.v7.min.js';
    s.async = true;
    s.onload = () => { _topoD3Loaded = true; resolve(); };
    s.onerror = () => reject(new Error('failed to load /static/d3.v7.min.js'));
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
  // Only applied when the topology tab itself is active - other tabs use
  // the metrics normally.
  document.body.classList.toggle('nw-topo-web', view === 'web');
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

  _topoUserAdjusted = false;

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
    .on('zoom', (ev) => {
      if(ev.sourceEvent) _topoUserAdjusted = true;   // ignore programmatic fits
      zoomG.attr('transform', ev.transform);
    });
  svg.call(_topoZoom);

  // Resize observer: keeps SVG viewBox + simulation centered when the
  // container dimensions change (fullscreen toggle, window resize, etc).
  // Disconnect any prior observer first - we re-create on every render.
  if(_topoResizeObserver){
    try { _topoResizeObserver.disconnect(); } catch(e){}
  }
  if(typeof ResizeObserver !== 'undefined'){
    _topoResizeObserver = new ResizeObserver(entries => {
      for(const entry of entries){
        const newW = entry.contentRect.width;
        const newH = entry.contentRect.height;
        if(newW <= 0 || newH <= 0) continue;
        // Update the SVG's viewBox to actually match the container.
        // This eliminates the letterboxing that preserveAspectRatio=meet
        // causes when the aspect ratio shifts.
        svg.attr('viewBox', '0 0 ' + newW + ' ' + newH);
        // Update the center force so the simulation re-balances around
        // the new midpoint, and warm the simulation gently so nodes
        // ease toward their new equilibrium without jumping.
        if(_topoSimulation){
          const cf = _topoSimulation.force('center');
          if(cf){
            cf.x(newW / 2).y(newH / 2);
            _topoSimulation.alphaTarget(0.05).restart();
            // Cool back down after a moment
            setTimeout(() => {
              if(_topoSimulation) _topoSimulation.alphaTarget(0);
            }, 800);
          }
        }
      }
    });
    _topoResizeObserver.observe(container);
  }

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
  // the viewport rather than zooming/panning with content. Added later.
  svg.append('rect')
    .attr('class', 'topo-vignette-rect')
    .attr('x', 0).attr('y', 0)
    .attr('width', '100%').attr('height', '100%')
    .style('pointer-events', 'none');

  // Force simulation
  const sim = d3.forceSimulation(renderNodes)
    .force('link', d3.forceLink(renderEdges).id(d => d.id)
      .distance(d => d.connection_type === 'virtual' ? 50 : 110)
      .strength(d => d.connection_type === 'virtual' ? 0.9 : 0.5))
    .force('charge', d3.forceManyBody().strength(-450))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('collide', d3.forceCollide().radius(d => nodeRadiusFor(d) + 10));
  _topoSimulation = sim;

  // Edges
  const edgeG = zoomG.append('g').attr('class', 'topo-edges');
  // Helper: classify an edge based on its endpoints' current statuses.
  // Returns one of: 'alive' (both up or up+unknown), 'degraded' (at least
  // one degraded but no down/idle), or 'dead' (at least one down/idle).
  function edgeState(edge){
    const s = nodeMap[typeof edge.source === 'object' ? edge.source.id : edge.source];
    const t = nodeMap[typeof edge.target === 'object' ? edge.target.id : edge.target];
    const ss = (s && s.status) || 'UNKNOWN';
    const ts = (t && t.status) || 'UNKNOWN';
    if(ss === 'DOWN' || ss === 'IDLE' || ts === 'DOWN' || ts === 'IDLE') return 'dead';
    if(ss === 'DEGRADED' || ts === 'DEGRADED') return 'degraded';
    return 'alive';
  }
  const edgeSel = edgeG.selectAll('g.topo-edge').data(renderEdges).join('g')
    .attr('class', d => 'topo-edge topo-edge-' + (d.connection_type || 'ethernet')
      + ' topo-edge-' + edgeState(d));
  // Wider invisible hit-area path so hover/click on the edge is generous
  edgeSel.append('path').attr('class', 'topo-edge-hit');
  edgeSel.append('path').attr('class', 'topo-edge-line');
  // Two flow dots - one each direction - to represent bidirectional traffic.
  // The "fwd" dot animates source -> target; the "rev" dot animates
  // target -> source. They travel at the same speed but offset by 0.5
  // along the path so they don't overlap visually.
  edgeSel.append('circle').attr('class', 'topo-edge-flow topo-edge-flow-fwd').attr('r', 2);
  edgeSel.append('circle').attr('class', 'topo-edge-flow topo-edge-flow-rev').attr('r', 2);

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

  // Compute the set of VM node IDs. A node is treated as a VM if EITHER:
  //   1. Its device_type is 'vm' (the explicit, modern way), OR
  //   2. It's the source of a virtual edge (legacy implicit detection,
  //      kept for backward compatibility with VMs you created before the
  //      vm device_type existed)
  const vmIds = new Set();
  renderNodes.forEach(n => {
    if(n.device_type === 'vm') vmIds.add(n.id);
  });
  renderEdges.forEach(e => {
    if(e.connection_type === 'virtual'){
      vmIds.add(typeof e.source === 'object' ? e.source.id : e.source);
    }
  });


  // Render the node body as a dimensional icon. There is no longer
  // a backdrop shape; status is conveyed by the parent .topo-status-*
  // class (drives both `color:` for the icon's LED and the drop-
  // shadow halo on .topo-node-icon) plus the breathing/pulse
  // animations defined in CSS.
  nodeSel.each(function(d){
    const sel = d3.select(this);
    // iconSize: rendered px width/height of the sprite. hitR: radius
    // of the invisible hit-target circle (covers the icon + a bit of
    // breathing room so drag/click still feels generous).
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
      // tablet, phone, peripheral, and any fallback
      iconSize = 40; hitR = 20;
    }
    const iconHref = '#topo-icon-' + (d.device_type || 'host');

    // Invisible hit target sits first so the icon paints over it
    sel.append('circle')
      .attr('class', 'topo-node-hit')
      .attr('r', hitR);

    // The dimensional sprite
    sel.append('use')
      .attr('class', 'topo-node-icon')
      .attr('href', iconHref)
      .attr('x', -iconSize/2).attr('y', -iconSize/2)
      .attr('width', iconSize).attr('height', iconSize);

    // Label sits below the icon for every type now
    sel.append('text')
      .attr('class', 'topo-node-label-below')
      .attr('y', iconSize/2 + 14)
      .text(truncateLabel(d.name, 20));

    // VM-class marker (still used by tooltip + isVm detection
    // elsewhere). No more pill badge — the VM icon carries its own
    // "VM" mark.
    if(vmIds.has(d.id)){
      sel.classed('topo-node-vm', true);
    }

    // Stagger the ambient breathing so the network doesn't pulse
    // in unison. Hash the node id into a delay between 0–4s.
    const iconEl = sel.select('.topo-node-icon');
    if(!iconEl.empty()){
      const delay = ((d.id * 1.7) % 4).toFixed(2);
      iconEl.style('animation-delay', delay + 's');
    }
  });

  // Tooltip
  const tip = d3.select(container).append('div').attr('class', 'topo-tip').style('display', 'none');

  nodeSel.on('mousemove', function(ev, d){
    const rect = container.getBoundingClientRect();
    // First render so we can measure
    tip.style('display', 'block').html(buildNodeTip(d));
    const tipNode = tip.node();
    const tipW = tipNode ? tipNode.offsetWidth : 240;
    const tipH = tipNode ? tipNode.offsetHeight : 60;
    const cx = ev.clientX - rect.left;
    const cy = ev.clientY - rect.top;
    const margin = 14;
    // Default: down-and-right of cursor
    let x = cx + 12;
    let y = cy + 12;
    // Flip to LEFT if would clip right edge
    if(x + tipW + margin > rect.width){
      x = cx - tipW - 12;
    }
    // Flip ABOVE if would clip bottom edge
    if(y + tipH + margin > rect.height){
      y = cy - tipH - 12;
    }
    // Final clamp so we never go off the left/top either
    x = Math.max(8, x);
    y = Math.max(8, y);
    tip.style('left', x + 'px').style('top', y + 'px');
  }).on('mouseleave.tip', () => tip.style('display', 'none'));

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

  // Tick handler: update positions + curved edges + flowing dot animation
  let tickFrame = 0;
  sim.on('tick', () => {
    tickFrame++;
    nodeSel.attr('transform', d => 'translate(' + d.x + ',' + d.y + ')');
    const edgePath = d => {
      const dx = d.target.x - d.source.x;
      const dy = d.target.y - d.source.y;
      const dr = Math.sqrt(dx*dx + dy*dy) * 1.8;
      return 'M' + d.source.x + ',' + d.source.y
        + 'A' + dr + ',' + dr + ' 0 0,1 ' + d.target.x + ',' + d.target.y;
    };
    edgeSel.select('path.topo-edge-line').attr('d', edgePath);
    edgeSel.select('path.topo-edge-hit').attr('d', edgePath);
    edgeSel.each(function(){
      const path = this.querySelector('path.topo-edge-line');
      this._flowLen = path ? path.getTotalLength() : 0;   // cache while geometry changes
    });
  });

  // Flow dots: time-based rAF loop, independent of the (now-resting)
  // simulation. Uses cached path lengths; pauses when hidden; disabled
  // under prefers-reduced-motion.
  const SPEEDS = {ethernet:.10, fiber:.16, wifi:.06, virtual:.07, power:.05, usb:.13, console:.065, other:.10};
  if(_flowRaf) cancelAnimationFrame(_flowRaf);
  function flowFrame(ts){
    edgeSel.each(function(d){
      const len = this._flowLen;
      if(!len) return;
      const path = this.querySelector('path.topo-edge-line');
      const speed = SPEEDS[d.connection_type] || .10;
      const t = (ts / 1000 * speed) % 1;
      const fwd = this.querySelector('.topo-edge-flow-fwd');
      const rev = this.querySelector('.topo-edge-flow-rev');
      if(fwd){ const p = path.getPointAtLength(t * len); fwd.setAttribute('cx', p.x); fwd.setAttribute('cy', p.y); }
      if(rev){ const p = path.getPointAtLength((1 - (t + .5) % 1) * len); rev.setAttribute('cx', p.x); rev.setAttribute('cy', p.y); }
    });
    _flowRaf = requestAnimationFrame(flowFrame);
  }
  if(!_reducedMotion.matches) _flowRaf = requestAnimationFrame(flowFrame);

  // Cool the simulation gradually
  sim.alpha(1).restart();
  setTimeout(() => {
    sim.alphaTarget(0);              // decay below alphaMin -> tick loop stops
    if(!_topoUserAdjusted) fitTopologyToView();
  }, 4000);
  // Run label collision pass after the simulation has had time to settle.
  // This nudges overlapping labels apart so dense areas read more clearly.
  setTimeout(() => spreadOverlappingLabels(nodeSel), 4500);
  // And re-run after a longer settle, in case nodes are still adjusting
  setTimeout(() => spreadOverlappingLabels(nodeSel), 6500);

  // Capture _topoLastStatus for change detection
  _topoLastStatus = {};
  renderNodes.forEach(n => { _topoLastStatus[n.id] = n.status; });

  function dragStart(ev, d){
    if(!ev.active) sim.alphaTarget(0.3).restart();
    d.fx = d.x; d.fy = d.y;
    _topoUserAdjusted = true;
    if(_topoSvg) _topoSvg.classed('topo-dragging', true);
  }
  function dragMove(ev, d){
    d.fx = ev.x; d.fy = ev.y;
  }
  function dragEnd(ev, d){
    if(!ev.active) sim.alphaTarget(0);
    // Persist the pinned position so it survives reloads
    saveTopoPosition(d.id, d.fx, d.fy);
    // Re-run label collision since the dragged node's neighborhood changed
    setTimeout(() => spreadOverlappingLabels(nodeSel), 600);
    if(_topoSvg) _topoSvg.classed('topo-dragging', false);
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
    else if(ss === 'DEGRADED' || ts === 'DEGRADED') state = 'degraded';
    else state = 'alive';
    const node = d3.select(this);
    node.classed('topo-edge-alive',    state === 'alive');
    node.classed('topo-edge-degraded', state === 'degraded');
    node.classed('topo-edge-dead',     state === 'dead');
  });
}

let _resetArmTimer = null;
function topologyResetPositions(){
  const btn = document.querySelector('.topo-web-controls .topo-view-btn-ghost:last-child')
    || document.querySelector('[onclick="topologyResetPositions()"]');
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

// Pause the flow loop when the tab is hidden or we leave web view.
document.addEventListener('visibilitychange', () => {
  if(document.hidden){
    if(_flowRaf){ cancelAnimationFrame(_flowRaf); _flowRaf = null; }
  } else if(_topoView === 'web' && _topoSvg){
    renderTopologyWeb();   // re-render restarts sim warmup + flow loop cleanly
  }
});
