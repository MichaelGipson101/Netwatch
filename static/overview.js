/* Overview tab — read-only glance across every other tab. Pulls from data the
   other modules already fetch (or the server already caches); no new endpoints. */
(function () {
  'use strict';

  var INV_TYPE_COLORS = { host: 'var(--blue)', vm: '#7c3aed', network: '#0891b2',
    ups: 'var(--amber)', disk: '#059669', peripheral: '#6b7280',
    tablet: '#0d9488', phone: '#a21caf', printer: '#92400e' };

  var _mounted = { proxmox: null, nas: null, inventory: null, briefs: null };

  // Overview hides the tab bar; the hamburger next to the greeting toggles it.
  window.toggleOverviewMenu = function () {
    var open = document.body.classList.toggle('nw-overview-menu-open');
    var btn = document.getElementById('ov-menu-toggle');
    if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  };

  window.initOverviewTab = function () {
    _renderShell();
    var hour = new Date().getHours();
    var greet = hour < 5 ? 'Good night.' : hour < 12 ? 'Good morning.'
              : hour < 18 ? 'Good afternoon.' : 'Good evening.';
    document.getElementById('ov-greeting-title').textContent = greet;
    if (window.nwLastData) window.renderOverviewLive(window.nwLastData);
    // Lightweight fetch-on-mount for data whose tabs may not have been opened.
    // /api/proxmox and /api/nas serve from server-side poller caches — cheap.
    fetch('/api/proxmox').then(function (r) { return r.json(); })
      .then(function (d) { _mounted.proxmox = d; _renderServers(); }).catch(function () {});
    fetch('/api/nas').then(function (r) { return r.json(); })
      .then(function (d) { _mounted.nas = d; _renderServers(); }).catch(function () {});
    fetch('/api/inventory').then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) { _mounted.inventory = d; _renderInventory(); } }).catch(function () {});
    fetch('/api/brief').then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) { _mounted.briefs = d; _renderBrief(); } }).catch(function () {});
    _mountTopoPreview();
  };

  // Called from core.js refresh() on every poll while the tab is active.
  window.renderOverviewLive = function (data) {
    if (!document.getElementById('ov-hosts-num')) return;
    var hosts = data.hosts || [];
    var up = hosts.filter(function (h) { return h.is_up; }).length;
    document.getElementById('ov-hosts-num').innerHTML =
      up + '<span class="ov-num-dim">/' + hosts.length + '</span>';
    var ongoing = (data.events || []).filter(function (e) { return e.ongoing; });
    document.getElementById('ov-hosts-down').innerHTML = ongoing.slice(0, 4).map(function (e) {
      return '<div class="ov-row"><span class="ov-dot" style="background:var(--red)"></span>'
        + '<span class="ov-row-name">' + escapeHtml(e.host_name) + '</span>'
        + '<span class="ov-row-meta">down ' + _ago(e.started_ts * 1000) + '</span></div>';
    }).join('') || '<div class="ov-empty">All hosts up</div>';

    var evs = (data.events || []).slice(0, 3);
    document.getElementById('ov-events-list').innerHTML = evs.map(function (e) {
      var color = e.ongoing ? 'var(--red)' : 'var(--green)';
      var text = escapeHtml(e.host_name) + (e.ongoing ? ' down' : ' recovered');
      return '<div class="ov-row"><span class="ov-dot" style="background:' + color + '"></span>'
        + '<span class="ov-row-name ov-trunc">' + text + '</span>'
        + '<span class="ov-row-meta">' + _ago(e.started_ts * 1000) + '</span></div>';
    }).join('') || '<div class="ov-empty">No incidents</div>';

    _updateTopoPreviewStatus(hosts);
    _renderPower();
  };

  function _renderShell () {
    var grid = document.getElementById('ov-grid');
    if (grid.childElementCount) return;   // build once
    grid.innerHTML =
      _card('hosts', 'Hosts', 'hosts', 'ov-span2',
        '<div class="ov-hosts-line"><span class="ov-big" id="ov-hosts-num">-</span>'
        + '<span class="ov-big-sub">hosts up</span></div><div id="ov-hosts-down"></div>')
      + _card('power', 'Power', null, '',
        '<div class="ov-big" id="ov-power-watts">-</div>'
        + '<svg width="100%" height="26" viewBox="0 0 100 26" preserveAspectRatio="none">'
        + '<polyline id="ov-power-spark" points="" fill="none" stroke="var(--blue)" stroke-width="1.6"/></svg>')
      + _card('topology', 'Topology', 'topology', '',
        '<div class="ov-topo-box"><svg id="ov-topo-svg" width="100%" height="100%" viewBox="0 0 200 110"></svg></div>')
      + _card('servers', 'Servers', 'servers', '', '<div id="ov-servers-list" class="ov-rows"></div>')
      + _card('events', 'Events', 'events', '', '<div id="ov-events-list" class="ov-rows"></div>')
      + _card('inventory', 'Inventory', 'inventory', '',
        '<div class="ov-big" id="ov-inv-count">-</div><div class="ov-chips" id="ov-inv-chips"></div>')
      + _card('briefs', 'Latest brief', 'briefs', '', '<div id="ov-brief-body" class="ov-empty">No briefs yet</div>');
  }

  function _card (id, title, tab, extraCls, body) {
    var link = tab ? '<button class="ov-viewall" onclick="setTab(\'' + tab + '\')">View all →</button>' : '';
    return '<div class="ov-card ' + extraCls + '" id="ov-card-' + id + '">'
      + '<div class="ov-card-hdr"><span class="ov-card-title">' + title + '</span>' + link + '</div>'
      + body + '</div>';
  }

  function _renderPower () {
    var p = window.nwLastPower;
    var card = document.getElementById('ov-card-power');
    if (!card) return;
    if (!p || !p.configured) { card.style.display = 'none'; return; }
    card.style.display = '';
    var live = p.live || {};
    document.getElementById('ov-power-watts').innerHTML =
      (live.watts != null ? live.watts.toFixed(0) : '-') + '<span class="ov-num-dim">W</span>';
    var watts = (p.history || []).filter(function (d) { return d.watts !== null; })
      .slice(-15).map(function (d) { return d.watts; });
    document.getElementById('ov-power-spark').setAttribute('points', nwSparkPoints(watts, 100, 26));
  }

  // ── Topology preview: a static mini render of the real web ─────────────
  // Same data (/api/topology), same icon sprites, same status/edge classes as
  // topology.js — just non-interactive and fitted into the card. Positions come
  // from the user's saved layout (nw-topo-positions) when present; otherwise a
  // short synchronous force simulation lays the web out.
  var _topoPreview = null;   // { nodes, edges } with resolved x/y

  // Rendered on-screen icon sizes (CSS px). The draw pass converts these to
  // viewBox units so icons stay legible however wide the layout spreads.
  var _TOPO_ICON_PX = { network: 20, ups: 18, host: 17, disk: 15, vm: 14, printer: 14 };

  function _mountTopoPreview () {
    fetch('/api/topology').then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        var connected = {};
        data.edges.forEach(function (e) { connected[e.source] = 1; connected[e.target] = 1; });
        var nodeMap = {};
        var nodes = data.nodes.filter(function (n) { return connected[n.id]; })
          .map(function (n) { var c = Object.assign({}, n); nodeMap[c.id] = c; return c; });
        var edges = data.edges.filter(function (e) { return nodeMap[e.source] && nodeMap[e.target]; })
          .map(function (e) { return Object.assign({}, e); });
        if (!nodes.length) { _renderTopoFallback(); return; }
        return ensureD3().then(function () {
          var saved = (typeof loadTopoPositions === 'function') ? loadTopoPositions() : {};
          var missing = false, pinned = [];
          nodes.forEach(function (n) {
            var p = saved[n.id];
            if (p) { n.x = p.x; n.y = p.y; n.fx = p.x; n.fy = p.y; pinned.push(n); }
            else missing = true;
          });
          if (missing) {
            // Anchor the simulation at the centroid of any user-arranged
            // (pinned) nodes so free nodes settle among them instead of
            // stretching toward the origin, far from the saved layout.
            var cx0 = 0, cy0 = 0;
            if (pinned.length) {
              pinned.forEach(function (p) { cx0 += p.fx; cy0 += p.fy; });
              cx0 /= pinned.length; cy0 /= pinned.length;
            }
            nodes.forEach(function (n, i) {
              if (n.fx === undefined) {
                var a = i * 2.4;
                n.x = cx0 + Math.cos(a) * 60;
                n.y = cy0 + Math.sin(a) * 60;
              }
            });
            var sim = d3.forceSimulation(nodes)
              .force('link', d3.forceLink(edges).id(function (d) { return d.id; })
                .distance(function (d) { return d.connection_type === 'virtual' ? 45 : 95; })
                .strength(0.6))
              .force('charge', d3.forceManyBody().strength(-350))
              .force('center', d3.forceCenter(cx0, cy0))
              // stronger y pull flattens the web to roughly the card's 2:1 aspect
              .force('x', d3.forceX(cx0).strength(0.04))
              .force('y', d3.forceY(cy0).strength(0.16))
              .force('collide', d3.forceCollide().radius(34))
              .stop();
            for (var i = 0; i < 250; i++) sim.tick();
          }
          _topoPreview = { nodes: nodes, edges: edges, nodeMap: nodeMap };
          _drawTopoPreview();
          if (window.nwLastData) _updateTopoPreviewStatus(window.nwLastData.hosts || []);
        });
      }).catch(function () {});
  }

  function _edgeEndpointId (v) { return typeof v === 'object' ? v.id : v; }

  function _edgeState (e, nodeMap) {
    var s = nodeMap[_edgeEndpointId(e.source)], t = nodeMap[_edgeEndpointId(e.target)];
    var ss = (s && s.status) || 'UNKNOWN', ts = (t && t.status) || 'UNKNOWN';
    if (ss === 'DOWN' || ss === 'IDLE' || ts === 'DOWN' || ts === 'IDLE') return 'dead';
    if (ss === 'DEGRADED' || ts === 'DEGRADED') return 'degraded';
    return 'alive';
  }

  function _drawTopoPreview () {
    var svg = document.getElementById('ov-topo-svg');
    if (!svg || !_topoPreview) return;
    var nodes = _topoPreview.nodes, edges = _topoPreview.edges, nodeMap = _topoPreview.nodeMap;
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    nodes.forEach(function (n) {
      minX = Math.min(minX, n.x); maxX = Math.max(maxX, n.x);
      minY = Math.min(minY, n.y); maxY = Math.max(maxY, n.y);
    });
    // viewBox units per CSS pixel — how much "meet" scaling will shrink the
    // layout to fit the card box. Icon/pad sizes are multiplied by this so
    // they render at a constant on-screen size regardless of layout spread.
    var box = svg.parentElement;
    var bw = (box && box.clientWidth) || 360, bh = (box && box.clientHeight) || 110;
    var unitScale = Math.max((maxX - minX) / bw, (maxY - minY) / bh, 0.2);
    var pad = 16 * unitScale;
    svg.setAttribute('viewBox',
      (minX - pad) + ' ' + (minY - pad) + ' ' +
      Math.max(maxX - minX + pad * 2, 1) + ' ' + Math.max(maxY - minY + pad * 2, 1));
    svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
    var parts = [];
    edges.forEach(function (e) {
      var s = nodeMap[_edgeEndpointId(e.source)], t = nodeMap[_edgeEndpointId(e.target)];
      if (!s || !t) return;
      parts.push('<g class="topo-edge topo-edge-' + escapeHtml(e.connection_type || 'ethernet')
        + ' topo-edge-' + _edgeState(e, nodeMap) + '" data-edge="' + e.id + '">'
        + '<path class="topo-edge-line" vector-effect="non-scaling-stroke" d="M'
        + s.x.toFixed(1) + ',' + s.y.toFixed(1)
        + 'L' + t.x.toFixed(1) + ',' + t.y.toFixed(1) + '"/></g>');
    });
    nodes.forEach(function (n) {
      var size = (_TOPO_ICON_PX[n.device_type] || 13) * unitScale;
      parts.push('<g class="topo-node topo-status-' + escapeHtml((n.status || 'UNKNOWN').toLowerCase())
        + '" data-id="' + n.id + '">'
        + '<use class="topo-node-icon" href="#topo-icon-' + escapeHtml(n.device_type || 'host')
        + '" x="' + (n.x - size / 2).toFixed(1) + '" y="' + (n.y - size / 2).toFixed(1)
        + '" width="' + size.toFixed(1) + '" height="' + size.toFixed(1) + '"/></g>');
    });
    svg.innerHTML = parts.join('');
  }

  // Live status refresh: match hosts to preview nodes by MAC, then linked-host
  // IP (same matching as updateTopologyWebStatus in topology.js), swap the
  // status classes in place, and recompute edge alive/degraded/dead classes.
  function _updateTopoPreviewStatus (hosts) {
    var svg = document.getElementById('ov-topo-svg');
    if (!svg) return;
    if (!_topoPreview) { _renderTopoFallback(hosts); return; }
    var macStatus = {}, ipStatus = {};
    (hosts || []).forEach(function (h) {
      var m = (((h.specs || {}).mac) || '').replace(/[^0-9a-f]/gi, '').toLowerCase();
      if (m) macStatus[m] = h.status;
      if (h.ip) ipStatus[h.ip] = h.status;
    });
    _topoPreview.nodes.forEach(function (n) {
      var m = (n.mac || '').replace(/[^0-9a-f]/gi, '').toLowerCase();
      var s = macStatus[m] || (n.linked_host && n.linked_host.ip ? ipStatus[n.linked_host.ip] : null);
      if (s) n.status = s;
      var g = svg.querySelector('g.topo-node[data-id="' + n.id + '"]');
      if (g) g.setAttribute('class', 'topo-node topo-status-' + (n.status || 'UNKNOWN').toLowerCase());
    });
    _topoPreview.edges.forEach(function (e) {
      var g = svg.querySelector('g.topo-edge[data-edge="' + e.id + '"]');
      if (g) g.setAttribute('class', 'topo-edge topo-edge-' + (e.connection_type || 'ethernet')
        + ' topo-edge-' + _edgeState(e, _topoPreview.nodeMap));
    });
  }

  // No inventory connections yet — fall back to the simple hosts-around-a-hub
  // sketch so the card still shows something meaningful.
  function _renderTopoFallback (hosts) {
    var svg = document.getElementById('ov-topo-svg');
    if (!svg) return;
    svg.setAttribute('viewBox', '0 0 200 110');
    var sats = (hosts || (window.nwLastData && window.nwLastData.hosts) || []).slice(0, 6);
    var cx = 100, cy = 55, r = 40;
    var parts = [];
    sats.forEach(function (h, i) {
      var a = (i / Math.max(sats.length, 1)) * 2 * Math.PI - Math.PI / 2;
      var x = (cx + r * 1.5 * Math.cos(a)).toFixed(1), y = (cy + r * 0.8 * Math.sin(a)).toFixed(1);
      var color = h.is_up ? 'var(--green)' : (h.status === 'DOWN' ? 'var(--red)' : 'var(--amber)');
      parts.push('<line x1="' + cx + '" y1="' + cy + '" x2="' + x + '" y2="' + y + '" stroke="var(--border)" stroke-width="1.2"/>');
      parts.push('<circle cx="' + x + '" cy="' + y + '" r="5" fill="' + color + '"/>');
    });
    parts.push('<circle cx="' + cx + '" cy="' + cy + '" r="8" fill="var(--text)"/>');
    svg.innerHTML = parts.join('');
  }

  function _renderServers () {
    var el = document.getElementById('ov-servers-list');
    if (!el) return;
    var rows = [];
    var pve = _mounted.proxmox;
    (pve && pve.nodes || []).forEach(function (n) {
      rows.push('<div class="ov-row"><span class="ov-row-name">' + escapeHtml(n.name) + ' CPU</span>'
        + '<span class="ov-row-meta">' + (n.cpu_percent || 0).toFixed(0) + '%</span></div>');
    });
    var nas = _mounted.nas;
    (nas && nas.pools || []).forEach(function (p) {
      var cls = p.status === 'ONLINE' ? 'nas-badge-ok' : 'nas-badge-err';
      rows.push('<div class="ov-row ov-row-top"><span class="ov-row-name">' + escapeHtml(p.name) + ' pool</span>'
        + '<span class="nas-badge ' + cls + '">' + escapeHtml(p.status) + '</span></div>');
    });
    el.innerHTML = rows.join('') || '<div class="ov-empty">No servers configured</div>';
    var card = document.getElementById('ov-card-servers');
    if (!card) return;
    if (rows.length) card.style.display = '';
    else if (!(pve && pve.reachable) && nas && !nas.reachable) card.style.display = 'none';
  }

  function _renderInventory () {
    var inv = _mounted.inventory;
    var items = (inv && inv.items) || [];
    document.getElementById('ov-inv-count').innerHTML =
      items.length + '<span class="ov-num-dim"> devices</span>';
    var counts = {};
    items.forEach(function (it) { counts[it.device_type] = (counts[it.device_type] || 0) + 1; });
    document.getElementById('ov-inv-chips').innerHTML = Object.keys(counts).map(function (t) {
      var c = INV_TYPE_COLORS[t] || 'var(--hint)';
      return '<span class="ov-chip" style="color:' + c + '">' + counts[t] + ' ' + escapeHtml(t) + '</span>';
    }).join('');
  }

  function _renderBrief () {
    var briefs = (_mounted.briefs && _mounted.briefs.briefs) || [];
    if (!briefs.length) return;
    var b = briefs[0];
    document.getElementById('ov-brief-body').outerHTML =
      '<div><div class="ov-brief-date">' + _ago(b.created_ts * 1000) + ' ago</div>'
      + '<div class="ov-brief-title">' + escapeHtml(b.subject || 'Brief') + '</div>'
      + '<p class="ov-brief-text">' + escapeHtml(String(b.narrative || '').slice(0, 180)) + '</p></div>';
  }

  function _ago (ts) {
    var t = typeof ts === 'string' ? new Date(ts).getTime() : ts;
    if (!t || isNaN(t)) return '';
    var m = Math.max(1, Math.round((Date.now() - t) / 60000));
    if (m < 60) return m + 'm';
    if (m < 1440) return Math.round(m / 60) + 'h';
    return Math.round(m / 1440) + 'd';
  }
})();
