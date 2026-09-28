/* ── Home helpers ─────────────────────────────────────────────────────────────
   Pure functions (no DOM, no fetch) so tests/test_home_js.py can extract and run them under
   node. They depend only on escapeHtml (utils.js) and, for hmFreePorts, cxLivePortMaps
   (connections.js). */

function hmHostClass(status){
  var map = {UP:'up', DOWN:'down', IDLE:'idle', DEGRADED:'degraded', MAINTENANCE:'idle'};
  return 'topo-status-' + (map[String(status || '').toUpperCase()] || 'unknown');
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
  var icons = ['host', 'vm', 'network', 'ups', 'disk', 'peripheral', 'tablet', 'phone', 'printer'];
  var type = icons.indexOf(h.device_type) >= 0 ? h.device_type : 'host';
  var label = (h.name || h.ip) + ' · ' + String(h.status || '').toLowerCase();
  if(String(h.status || '').toUpperCase() === 'DOWN' && typeof h.last_seen_up_seconds === 'number'){
    label += ' · last seen ' + hmAgo(h.last_seen_up_seconds) + ' ago';
  }
  return '<a class="hm-h3 ' + hmHostClass(h.status) + '" href="/monitor/hosts?host=' + encodeURIComponent(h.ip)
    + '" title="' + escapeHtml(label) + '" aria-label="' + escapeHtml(label) + '">'
    + '<svg class="hm-ic topo-node-icon" aria-hidden="true"><use href="#topo-icon-' + type + '"/></svg>'
    + '<span class="hm-hb" role="img" aria-label="' + escapeHtml(hmHeartbeatLabel(states)) + '" style="background:'
    + hmHeartbeatBackground(states) + '"></span></a>';
}

function hmGroupHtml(g, hb){
  var tiles = g.hosts.map(function(h){ return hmHostTileHtml(h, hb && hb[h.ip]); }).join('');
  return '<div class="hm-group"><div class="hm-grow"><div class="hm-gl"><span>' + escapeHtml(g.name)
    + '</span><em>' + g.up + '/' + g.total + '</em></div><div class="hm-gi">' + tiles + '</div></div></div>';
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

function hmShouldPoll(now, last, everyMs, hidden){
  return !hidden && now - last >= everyMs;
}

function hmValidAttention(d){
  return !!(d && typeof d === 'object' && d.verdict && typeof d.verdict === 'object'
    && typeof d.verdict.headline === 'string' && d.verdict.headline !== '' && Array.isArray(d.items));
}

function hmSafeUrl(url){
  var u = String(url == null ? '' : url).trim();
  return /^https?:\/\//i.test(u) ? u : '#';
}

/* ── Home controller ──────────────────────────────────────────────────────────
   The glance page. Verdict text comes from the server (/api/attention); this file only formats
   and wires. Attention refreshes every 15s and the slower sources every 60s, both throttled off
   the existing 5s /api/status poll (so nothing is fetched before login or while the poll
   is stopped). Any endpoint that returns nothing/garbage leaves that section as it was and
   turns the LED to the stale state. */
(function () {
  'use strict';

  var ATTN_EVERY_MS = 15000, SLOW_EVERY_MS = 60000, STATUS_STALE_MS = 30000;
  var INV_TYPE_COLORS = { host: 'var(--blue)', vm: '#7c3aed', network: '#0891b2',
    ups: 'var(--amber)', disk: '#059669', peripheral: '#6b7280',
    tablet: '#0d9488', phone: '#a21caf', printer: '#92400e' };

  var _hb = {};                       // ip -> heartbeat buckets
  var _lastStatus = null, _statusAt = 0;
  var _lastAttn = 0, _lastSlow = 0, _attnBusy = false, _slowBusy = false;
  var _attnStale = false, _level = null, _explainBusy = false, _actionBusy = false, _attnDirty = false;
  var _srv = { proxmox: null, nas: null, ups: null };
  var _inv = null, _brief = null, _links = null, _ports = null;

  function $(id) { return document.getElementById(id); }
  function _isAdmin() { return typeof _authState !== 'undefined' && _authState.logged_in && _authState.admin; }
  function _getJson(url) {
    return fetch(url).then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; });
  }
  function _post(url, body) {
    return apiFetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body) });
  }
  function _row(name, meta, bad) {
    return '<div class="hm-row"><span class="hm-row-name">' + escapeHtml(name) + '</span>'
      + '<span class="hm-row-meta' + (bad ? ' hm-bad' : '') + '">' + escapeHtml(meta) + '</span></div>';
  }
  function _statusStale() { return !_statusAt || (Date.now() - _statusAt) > STATUS_STALE_MS; }

  // ── verdict / stats ────────────────────────────────────────────────────────
  function _renderLed() {
    var led = $('hm-led');
    if (led) led.className = 'hm-led ' + hmVerdictLevelClass(_level, _attnStale || _statusStale());
  }
  function _renderStats() {
    var el = $('hm-stats');
    if (!el || !_lastStatus) return;
    var p = window.nwLastPower;
    var watts = (p && p.configured && p.live && typeof p.live.watts === 'number') ? p.live.watts : null;
    el.textContent = hmStatsLine(nwComputeSummary(_lastStatus), watts, (Date.now() - _statusAt) / 1000);
    _renderLed();
  }

  // ── needs attention ────────────────────────────────────────────────────────
  function _renderAttention(d) {
    _level = d.verdict.level;
    var hl = $('hm-headline');
    if (hl.textContent !== d.verdict.headline) hl.textContent = d.verdict.headline;
    var items = d.items.filter(function (i) { return i && typeof i === 'object'; });
    var admin = _isAdmin();
    $('hm-attention-list').innerHTML = items.length
      ? items.map(function (i) { return hmAttentionRowHtml(i, admin); }).join('')
      : '<div class="hm-mut">Nothing needs attention.</div>';
    var dismissed = (d.verdict.counts && d.verdict.counts.dismissed) || 0;
    var dEl = $('hm-dismissed');
    if (dismissed > 0) {
      dEl.hidden = false;
      dEl.innerHTML = escapeHtml(String(dismissed)) + ' dismissed'
        + (admin ? ' · <button type="button" class="hm-link" id="hm-restore">Restore</button>' : '');
    } else { dEl.hidden = true; dEl.textContent = ''; }
    var problems = items.some(function (i) { return i.severity === 'critical' || i.severity === 'warning'; });
    $('hm-explain').hidden = !problems;
    if (!problems) {
      $('hm-explain-body').hidden = true;
      $('hm-explain-btn').setAttribute('aria-expanded', 'false');
      $('hm-explain-btn').textContent = 'Explain';
    }
    _renderLed();
  }
  function _loadAttention() {
    _attnBusy = true; _attnDirty = false;
    _getJson('/api/attention').then(function (d) {
      _attnBusy = false;
      if (_attnDirty) { _attnDirty = false; _lastAttn = 0; _tick(); return; }   // pre-action response: refetch
      if (!hmValidAttention(d)) { _attnStale = true; _renderLed(); return; }
      _attnStale = false;
      _renderAttention(d);
    });
  }
  function _msg(t) { var el = $('hm-attention-msg'); if (el) el.textContent = t; }
  function _refreshAttentionSoon() { _attnDirty = true; _lastAttn = 0; _tick(); }
  function _runAction(body, failMsg) {
    if (_actionBusy) return;
    _actionBusy = true;
    _post('/api/attention/dismiss', body).then(function (r) {
      if (!r.ok) throw new Error('action');
      _msg(''); _refreshAttentionSoon();
    }).catch(function () { _msg(failMsg); })
      .then(function () { _actionBusy = false; });
  }
  function _dismiss(id) { _runAction({ id: id }, "Couldn't dismiss that item."); }
  function _restore() { _runAction({ restore_all: true }, "Couldn't restore dismissed items."); }
  function _onExplain() {
    var btn = $('hm-explain-btn'), body = $('hm-explain-body');
    if (_explainBusy) return;
    if (!body.hidden) {
      body.hidden = true; btn.setAttribute('aria-expanded', 'false'); btn.textContent = 'Explain';
      return;
    }
    _explainBusy = true;
    btn.setAttribute('aria-disabled', 'true'); btn.setAttribute('aria-expanded', 'true'); btn.textContent = 'Hide';
    body.hidden = false; body.textContent = 'Thinking…';
    var status = 0;
    _post('/api/attention/explain', {})
      .then(function (r) { status = r.status; return r.json().catch(function () { return null; }); })
      .then(function (d) {
        var m = hmExplainMessage(status, d);
        body.textContent = m.text + (m.note ? ' (' + m.note + ')' : '');
      })
      .catch(function () { body.textContent = hmExplainMessage(0, null).text; })
      .then(function () { _explainBusy = false; btn.removeAttribute('aria-disabled'); });
  }

  // ── hosts ──────────────────────────────────────────────────────────────────
  function _renderHosts(data) {
    var body = $('hm-hosts-body');
    if (!body) return;
    var hosts = data.hosts || [];
    var groups = hmGroupHosts(hosts);
    if (!groups.length) {
      body.innerHTML = '<div class="hm-mut">Add hosts in Monitor → Edit hosts.</div>';
      $('hm-hosts-sum').textContent = '';
      return;
    }
    body.innerHTML = groups.map(function (g) { return hmGroupHtml(g, _hb); }).join('');
    var up = hosts.filter(function (h) { return h.is_up; }).length;
    $('hm-hosts-sum').textContent = up + ' of ' + hosts.length + ' up · 24h heartbeat';
  }
  function _renderRecent(data) {
    var el = $('hm-recent-body');
    if (!el) return;
    var now = Date.now() / 1000;
    el.innerHTML = (data.events || []).slice(0, 3).map(function (e) {
      return '<div class="hm-row"><span class="hm-dot ' + (e.ongoing ? 'hm-dot-dn' : 'hm-dot-up') + '"></span>'
        + '<span class="hm-row-name">' + escapeHtml(e.host_name) + (e.ongoing ? ' down' : ' recovered') + '</span>'
        + '<span class="hm-row-meta">' + escapeHtml(hmAgo(now - e.started_ts)) + '</span></div>';
    }).join('') || '<div class="hm-mut">No incidents</div>';
  }

  // ── lower sections ─────────────────────────────────────────────────────────
  function _renderServers() {
    var rows = [];
    var pve = _srv.proxmox;
    ((pve && pve.nodes) || []).forEach(function (n) {
      rows.push(_row(n.name + ' CPU', (n.cpu_percent || 0).toFixed(0) + '%'));
    });
    var nas = _srv.nas;
    ((nas && nas.pools) || []).forEach(function (p) {
      var pct = hmPoolPct(p);
      var bad = p.status && p.status !== 'ONLINE';
      rows.push(_row(p.name + ' pool', bad ? p.status : (pct == null ? '' : pct + '% used'), bad));
    });
    var ups = _srv.ups;
    if (ups && ups.configured && ups.live) rows.push(_row('UPS', hmUpsText(ups.live)));
    $('hm-servers-body').innerHTML = rows.join('');
    $('hm-servers').hidden = rows.length === 0;
  }
  function _renderPower() {
    var p = window.nwLastPower, sec = $('hm-power');
    if (!sec) return;
    if (!p || !p.configured) { sec.hidden = true; return; }
    var live = p.live || {};
    var w = typeof live.watts === 'number' ? Math.round(live.watts) : null;
    var avg = hmAvgWatts(p.history);
    $('hm-power-big').innerHTML = (w == null ? '–' : w) + '<small>W</small>';
    $('hm-power-avg').textContent = avg == null ? '' : '7-day avg ' + avg + ' W';
    var vals = (p.history || []).filter(function (d) { return d && typeof d.watts === 'number'; })
      .slice(-48).map(function (d) { return d.watts; });
    $('hm-power-spark').setAttribute('points', nwSparkPoints(vals, 100, 26));
    sec.hidden = false;
  }
  function _renderNetwork() {
    var sec = $('hm-network'), maps = _ports || [];
    var has = typeof cxLivePortMaps === 'function' && cxLivePortMaps(maps).length > 0;
    sec.hidden = !has;
    if (!has) return;
    $('hm-network-sum').textContent = hmFreePorts(maps).free + ' free';
    $('hm-network-body').innerHTML = cxPortFacesHtml(maps);
  }
  function _renderBrief() {
    var briefs = (_brief && _brief.briefs) || [];
    var sec = $('hm-brief');
    sec.hidden = !briefs.length;
    if (!briefs.length) return;
    var b = briefs[0];
    $('hm-brief-body').innerHTML =
      '<div class="hm-brief-date">' + escapeHtml(hmAgo(Date.now() / 1000 - b.created_ts)) + ' ago</div>'
      + '<div class="hm-brief-title">' + escapeHtml(b.subject || 'Brief') + '</div>'
      + '<p class="hm-brief-text">' + escapeHtml(String(b.narrative || '').slice(0, 180)) + '</p>';
  }
  function _renderInventory() {
    var items = (_inv && _inv.items) || [];
    var sec = $('hm-inventory');
    sec.hidden = !items.length;
    if (!items.length) return;
    $('hm-inv-count').innerHTML = items.length + '<small> devices</small>';
    var counts = {};
    items.forEach(function (it) { counts[it.device_type] = (counts[it.device_type] || 0) + 1; });
    $('hm-inv-chips').innerHTML = Object.keys(counts).map(function (t) {
      return '<span class="hm-chip" style="color:' + (INV_TYPE_COLORS[t] || 'var(--hint)') + '">'
        + counts[t] + ' ' + escapeHtml(t) + '</span>';
    }).join('');
  }
  function _renderLinks() {
    var links = (_links && _links.links) || [];
    var sec = $('hm-links');
    sec.hidden = !links.length;
    if (!links.length) return;
    var html = links.slice(0, 6).map(function (l) {
      return '<a class="hm-ql" href="' + escapeHtml(hmSafeUrl(l.url)) + '" target="_blank" rel="noopener noreferrer">'
        + '<span aria-hidden="true">' + escapeHtml(l.icon || '\u{1F517}') + '</span> ' + escapeHtml(l.label) + '</a>';
    }).join('');
    if (links.length > 6) html += '<a class="hm-ql hm-go" href="/links">+' + (links.length - 6) + '</a>';
    $('hm-links-body').innerHTML = html;
  }

  // ── polling ────────────────────────────────────────────────────────────────
  function _loadSlow() {
    _slowBusy = true;
    var jobs = [
      _getJson('/api/heartbeat?hours=24&buckets=48').then(function (d) {
        if (d && d.hosts && typeof d.hosts === 'object') { _hb = d.hosts; if (_lastStatus) _renderHosts(_lastStatus); }
      }),
      _getJson('/api/proxmox').then(function (d) { if (d) _srv.proxmox = d; }),
      _getJson('/api/nas').then(function (d) { if (d) _srv.nas = d; }),
      _getJson('/api/ups').then(function (d) { if (d) _srv.ups = d; }),
      _getJson('/api/inventory').then(function (d) { if (d) _inv = d; }),
      _getJson('/api/brief').then(function (d) { if (d) _brief = d; }),
      _getJson('/api/quicklinks').then(function (d) { if (d) _links = d; }),
      (typeof cxLoadPortMaps === 'function'
        ? cxLoadPortMaps().then(function (m) { _ports = m; }).catch(function () {})
        : Promise.resolve())
    ];
    Promise.all(jobs).then(function () {
      _renderServers(); _renderBrief(); _renderInventory(); _renderLinks(); _renderNetwork();
    }).catch(function () {}).then(function () { _slowBusy = false; });
  }
  function _tick() {
    if (document.hidden) return;
    var now = Date.now(), hidden = document.hidden;
    if (hmShouldPoll(now, _lastAttn, ATTN_EVERY_MS, hidden) && !_attnBusy) { _lastAttn = now; _loadAttention(); }
    if (hmShouldPoll(now, _lastSlow, SLOW_EVERY_MS, hidden) && !_slowBusy) { _lastSlow = now; _loadSlow(); }
  }
  function _onStatus(data) {
    if (!$('hm-headline')) return;
    _lastStatus = data; _statusAt = Date.now();
    _renderHosts(data); _renderRecent(data); _renderPower(); _renderStats();
    _tick();
  }

  nwStatus.subscribe(_onStatus);
  nwOnReady(function () {
    $('hm-attention-list').addEventListener('click', function (e) {
      var b = e.target.closest ? e.target.closest('[data-dismiss]') : null;
      if (b) _dismiss(b.getAttribute('data-dismiss'));
    });
    $('hm-dismissed').addEventListener('click', function (e) { if (e.target.id === 'hm-restore') _restore(); });
    $('hm-explain-btn').addEventListener('click', _onExplain);
    setInterval(function () { if (!document.hidden) _renderStats(); }, 1000);
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) { _lastAttn = 0; _lastSlow = 0; _tick(); }
    });
    if (_lastStatus) _onStatus(_lastStatus);
  });
})();
