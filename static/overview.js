/* ── Home helpers ─────────────────────────────────────────────────────────────
   Pure functions (no DOM, no fetch) so tests/test_home_js.py can extract and run them under
   node. They depend only on escapeHtml (utils.js) and, for hmFreePorts, cxLivePortMaps
   (connections.js). */

function hmHostClass(status){
  var map = {UP:'up', DOWN:'down', IDLE:'idle', DEGRADED:'degraded', MAINTENANCE:'idle'};
  return 'topo-status-' + (map[String(status || '').toUpperCase()] || 'unknown');
}

function hmIsNotUp(h){
  return ['DOWN', 'DEGRADED', 'MAINTENANCE'].indexOf(String((h && h.status) || '').toUpperCase()) >= 0;
}

function hmGroupHosts(hosts){
  var order = [], by = {};
  (hosts || []).forEach(function(h){
    var g = h.group || 'Other';
    if(!by[g]){ by[g] = {name: g, hosts: [], up: 0}; order.push(g); }
    by[g].hosts.push(h);
    if(h.is_up) by[g].up++;
  });
  return order.map(function(g){
    return {name: g, hosts: by[g].hosts, up: by[g].up, total: by[g].hosts.length};
  });
}

function hmAvgWatts(history){
  var v = (history || []).map(function(d){ return d && d.watts; })
    .filter(function(w){ return typeof w === 'number' && isFinite(w); });
  if(!v.length) return null;
  return Math.round(v.reduce(function(a, b){ return a + b; }, 0) / v.length);
}

function hmItemHref(link){
  if(!link || typeof link.page !== 'string') return null;
  var paths = {home: '/', monitor: '/monitor', lab: '/lab', infra: '/infra', links: '/links'};
  var base = paths[link.page];
  if(!base) return null;
  if(link.subview && /^[a-z]+$/.test(link.subview)) base = (base === '/' ? '' : base) + '/' + link.subview;
  var params = link.params || {};
  var qs = Object.keys(params)
    .filter(function(k){ return params[k] !== null && params[k] !== undefined; })
    .map(function(k){ return encodeURIComponent(k) + '=' + encodeURIComponent(params[k]); })
    .join('&');
  return base + (qs ? '?' + qs : '');
}

function hmAgo(seconds){
  if(typeof seconds !== 'number' || !isFinite(seconds)) return '';
  var s = Math.max(0, Math.floor(seconds));
  if(s < 60) return s + 's';
  var m = Math.floor(s / 60);
  if(m < 60) return m + 'm';
  var h = Math.floor(m / 60);
  if(h < 48) return h + 'h';
  return Math.floor(h / 24) + 'd';
}

function hmStatsLine(s, watts, checkedAgo){
  var p = [];
  if(s && s.total > 0) p.push(s.up + '/' + s.total + ' up');
  if(s && typeof s.avgLat === 'number') p.push(s.avgLat.toFixed(1) + ' ms');
  if(s && typeof s.avgUpt === 'number') p.push(s.avgUpt.toFixed(1) + '% uptime');
  if(typeof watts === 'number' && isFinite(watts)) p.push(Math.round(watts) + ' W');
  if(typeof checkedAgo === 'number' && isFinite(checkedAgo)) p.push('checked ' + hmAgo(checkedAgo) + ' ago');
  return p.join(' · ');
}

function hmVerdictLevelClass(level, stale){
  if(stale) return 'hm-led-stale';
  return {ok: 'hm-led-ok', warn: 'hm-led-warn', down: 'hm-led-down'}[level] || 'hm-led-stale';
}

function hmHeartbeatBackground(states){
  if(!Array.isArray(states) || !states.length) return 'none';
  var colors = {1: 'var(--green)', 0: 'var(--red)', 2: 'var(--amber)'};
  var n = states.length;
  var stops = states.map(function(st, i){
    var c = (st === 0 || st === 1 || st === 2) ? colors[st] : 'var(--border)';
    var a = +(i * 100 / n).toFixed(3), b = +((i + 1) * 100 / n).toFixed(3);
    return c + ' ' + a + '% ' + b + '%';
  });
  return 'linear-gradient(90deg, ' + stops.join(', ') + ')';
}

function hmHeartbeatLabel(states){
  if(!Array.isArray(states)) return '24h: no data';
  var known = states.filter(function(s){ return s === 0 || s === 1 || s === 2; });
  if(!known.length) return '24h: no data';
  var up = known.filter(function(s){ return s === 1; }).length;
  return '24h: ' + up + ' of ' + known.length + ' periods fully up';
}

function hmAttentionRowHtml(item, isAdmin){
  var sev = {critical: 'crit', warning: 'warn', info: 'info'}[item.severity] || 'info';
  var href = hmItemHref(item.link);
  var badge = (item.kind === 'host_down' && item.affected && item.affected.length)
    ? '<span class="hm-badge hm-badge-dn">' + item.affected.length + ' affected</span>' : '';
  var open = href ? '<a class="hm-go" href="' + escapeHtml(href) + '">Open →</a>' : '';
  var dismiss = (isAdmin && item.kind === 'poller_condition')
    ? '<button type="button" class="hm-dismiss" data-dismiss="' + escapeHtml(item.id) + '">Dismiss</button>' : '';
  return '<div class="hm-arow">'
    + '<span class="hm-aico hm-aico-' + sev + '" aria-hidden="true"></span>'
    + '<div class="hm-at"><b>' + escapeHtml(item.title) + '</b>' + badge
    + '<div class="hm-ad">' + escapeHtml(item.detail || '') + '</div></div>'
    + '<div class="hm-aact">' + open + dismiss + '</div></div>';
}

function hmHostTileHtml(h, states){
  var type = /^[a-z]+$/.test(h.device_type || '') ? h.device_type : 'host';
  var label = (h.name || h.ip) + ' · ' + String(h.status || '').toLowerCase();
  return '<a class="hm-h3 ' + hmHostClass(h.status) + '" href="/monitor/hosts?host=' + encodeURIComponent(h.ip)
    + '" title="' + escapeHtml(label) + '" aria-label="' + escapeHtml(label) + '">'
    + '<svg class="hm-ic topo-node-icon" aria-hidden="true"><use href="#topo-icon-' + type + '"/></svg>'
    + '<span class="hm-hb" role="img" aria-label="' + escapeHtml(hmHeartbeatLabel(states)) + '" style="background:'
    + hmHeartbeatBackground(states) + '"></span></a>';
}

function hmNotUpLineHtml(h){
  var st = String(h.status || '').toLowerCase();
  var ago = (typeof h.last_seen_up_seconds === 'number') ? ' ' + hmAgo(h.last_seen_up_seconds) : '';
  var word = st === 'down' ? 'down' + ago : st;
  return '<div class="hm-nu"><span class="hm-nu-name">' + escapeHtml(h.name || h.ip) + '</span>'
    + '<span class="hm-nu-meta">' + escapeHtml(word) + '</span></div>';
}

function hmGroupHtml(g, hb){
  var tiles = g.hosts.map(function(h){ return hmHostTileHtml(h, hb && hb[h.ip]); }).join('');
  var nu = g.hosts.filter(hmIsNotUp).map(hmNotUpLineHtml).join('');
  return '<div class="hm-group"><div class="hm-grow"><div class="hm-gl"><span>' + escapeHtml(g.name)
    + '</span><em>' + g.up + '/' + g.total + '</em></div><div class="hm-gi">' + tiles + '</div></div>'
    + (nu ? '<div class="hm-nulist">' + nu + '</div>' : '') + '</div>';
}

function hmExplainMessage(status, data){
  data = data || {};
  if(status === 200 && typeof data.explanation === 'string' && data.explanation){
    return {ok: true, text: data.explanation, note: data.stale ? 'cached' : ''};
  }
  if(status === 404 && data.error === 'ai_not_configured'){
    return {ok: false, text: 'Add an OpenRouter key in Settings to enable explanations.'};
  }
  if(status === 429) return {ok: false, text: 'Try again in a moment.'};
  return {ok: false, text: "Couldn't generate an explanation right now."};
}

function hmUpsText(live){
  if(!live) return 'unknown';
  var flags = String(live.status || '').toUpperCase().split(/\s+/);
  var word = flags.indexOf('LB') >= 0 ? 'low battery'
    : flags.indexOf('OB') >= 0 ? 'on battery'
    : flags.indexOf('OL') >= 0 ? 'on line' : 'unknown';
  return typeof live.charge_percent === 'number'
    ? word + ' · ' + Math.round(live.charge_percent) + '%' : word;
}

function hmPoolPct(pool){
  var total = pool && pool.capacity_total_bytes;
  if(!total) return null;
  return Math.round((pool.capacity_used_bytes || 0) / total * 100);
}

function hmFreePorts(maps){
  var free = 0, total = 0;
  cxLivePortMaps(maps).forEach(function(m){
    m.data.ports.forEach(function(p){
      total++;
      if(!p.up && !(p.occupants && p.occupants.length)) free++;
    });
  });
  return {free: free, total: total};
}

function hmSafeUrl(url){
  var u = String(url == null ? '' : url).trim();
  return /^https?:\/\//i.test(u) ? u : '#';
}

/* Overview tab — read-only glance across every other tab. Pulls from data the
   other modules already fetch (or the server already caches); no new endpoints. */
(function () {
  'use strict';

  var INV_TYPE_COLORS = { host: 'var(--blue)', vm: '#7c3aed', network: '#0891b2',
    ups: 'var(--amber)', disk: '#059669', peripheral: '#6b7280',
    tablet: '#0d9488', phone: '#a21caf', printer: '#92400e' };

  var _mounted = { proxmox: null, nas: null, inventory: null, briefs: null, ports: null };

  window.initOverviewTab = function () {
    _renderShell();
    if (typeof updateAuthUI === 'function') updateAuthUI();
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
    _renderPorts();
    if (typeof cxLoadPortMaps === 'function') {
      cxLoadPortMaps().then(function (maps) { _mounted.ports = maps; _renderPorts(); }).catch(function () {});
    }
    fetch('/api/brief').then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) { _mounted.briefs = d; _renderBrief(); } }).catch(function () {});
    if (typeof mountQuickLinksCard === 'function') mountQuickLinksCard();
  };

  // Called by the nwStatus subscriber (bottom of file) on every poll while the tab is active.
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

    _renderPower();
  };

  // Static illustration: deliberately status-free (no red/green), links to the Lab.
  var TOPO_PLACEHOLDER =
    '<a class="ov-topo-box" href="/lab/topology" aria-label="Open the topology map" style="display:block">'
    + '<svg viewBox="0 0 260 150" width="100%" height="100%" style="color:var(--hint)" aria-hidden="true">'
    + '<g stroke="var(--border)" stroke-width="1.4" fill="none">'
    + '<path d="M130 75L64 34M130 75L200 30M130 75L214 108M130 75L60 116M64 34L26 60M60 116L24 128M200 30L238 52"/></g>'
    + '<use href="#topo-icon-host" x="48" y="18" width="32" height="32"/>'
    + '<use href="#topo-icon-host" x="184" y="14" width="32" height="32"/>'
    + '<use href="#topo-icon-ups" x="198" y="92" width="32" height="32"/>'
    + '<use href="#topo-icon-disk" x="44" y="100" width="32" height="32"/>'
    + '<use href="#topo-icon-network" x="10" y="44" width="30" height="30"/>'
    + '<use href="#topo-icon-vm" x="8" y="116" width="26" height="26"/>'
    + '<use href="#topo-icon-phone" x="226" y="38" width="26" height="26"/>'
    + '<use href="#topo-icon-network" x="112" y="57" width="36" height="36"/></svg></a>';

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
      + _card('topology', 'Topology', 'topology', '', TOPO_PLACEHOLDER)
      + _card('ports', 'Switch ports', 'connections', 'ov-span2', '<div id="ov-ports-body"></div>')
      + _card('servers', 'Servers', 'servers', '', '<div id="ov-servers-list" class="ov-rows"></div>')
      + _card('events', 'Events', 'events', '', '<div id="ov-events-list" class="ov-rows"></div>')
      + _card('inventory', 'Inventory', 'inventory', '',
        '<div class="ov-big" id="ov-inv-count">-</div><div class="ov-chips" id="ov-inv-chips"></div>')
      + _card('briefs', 'Latest brief', 'briefs', '', '<div id="ov-brief-body" class="ov-empty">No briefs yet</div>')
      + _card('quicklinks', 'Quick Links', 'quicklinks', '',
        '<div class="ov-big" id="ov-ql-count">-</div><div class="ov-big-sub">quick links</div>');
  }

  function _card (id, title, tab, extraCls, body, headerControl) {
    var link = headerControl || (tab ? '<button class="ov-viewall" onclick="setTab(\'' + tab + '\')">View all →</button>' : '');
    return '<div class="ov-card ' + extraCls + '" id="ov-card-' + id + '">'
      + '<div class="ov-card-hdr"><span class="ov-card-title">' + title + '</span>' + link + '</div>'
      + body + '</div>';
  }

  // Copy of the Connections tab's port map; hidden until a switch is found.
  function _renderPorts () {
    var card = document.getElementById('ov-card-ports');
    if (!card) return;
    var maps = _mounted.ports || [];
    var has = typeof cxLivePortMaps === 'function' && cxLivePortMaps(maps).length > 0;
    card.style.display = has ? '' : 'none';
    if (has) document.getElementById('ov-ports-body').innerHTML = cxPortFacesHtml(maps);
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

nwStatus.subscribe(function (data) { if (window.renderOverviewLive) window.renderOverviewLive(data); });
nwOnReady(function () { if (window.initOverviewTab) window.initOverviewTab(); });
