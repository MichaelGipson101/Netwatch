// Theme: the inline <head> script resolves auto -> light|dark before first
// paint and exposes window.nwApplyTheme. setTheme() handles button wiring
// and delegates actual theme application to that head script.
function setTheme(mode){
  localStorage.setItem('nw-theme', mode);
  if(window.nwApplyTheme) window.nwApplyTheme();
  document.querySelectorAll('#theme-toggle button').forEach(b => {
    const active = b.dataset.themeBtn === mode;
    b.classList.toggle('active', active);
    b.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}
document.addEventListener('DOMContentLoaded', () => {
  const current = localStorage.getItem('nw-theme') || 'auto';
  document.querySelectorAll('#theme-toggle button').forEach(b => {
    b.classList.toggle('active', b.dataset.themeBtn === current);
    b.setAttribute('aria-pressed', b.dataset.themeBtn === current ? 'true' : 'false');
    b.addEventListener('click', () => setTheme(b.dataset.themeBtn));
  });

  let initialTab = localStorage.getItem('nw-tab') || 'topology';
  if (initialTab === 'storage') initialTab = 'servers';  // renamed in v3.41
  setTab(initialTab);
  // Restore Cards/Web view preference for the topology tab
  if(typeof setTopoView === 'function') setTopoView(_topoView);
  document.querySelectorAll('.tab').forEach(t => {
    t.addEventListener('click', () => setTab(t.dataset.tab));
  });

  const compactSaved = localStorage.getItem('nw-compact') === 'true';
  document.getElementById('compact-mode').checked = compactSaved;
  document.body.classList.toggle('compact', compactSaved);
  document.getElementById('compact-mode').addEventListener('change', e => {
    document.body.classList.toggle('compact', e.target.checked);
    localStorage.setItem('nw-compact', e.target.checked);
  });

  // App boot: auth gate, polling loops. Lives here (not inventory.js) so a
  // failure in any later-loaded file can't kill the heartbeat.
  fetchAuthState();
  setInterval(fetchAuthState, 60000);
  setInterval(refresh, REFRESH);
  setInterval(clockTick, 1000);
  clockTick();

  const footerRefresh = document.getElementById('footer-refresh');
  if(footerRefresh) footerRefresh.textContent = 'refreshes every ' + (REFRESH/1000) + ' s';
});

function setTab(tab){
  document.querySelectorAll('.tab').forEach(t => {
    t.classList.toggle('active', t.dataset.tab === tab);
    t.setAttribute('aria-selected', t.dataset.tab === tab ? 'true' : 'false');
  });
  // Web-overlay metrics only apply when topology tab is active in web mode
  document.body.classList.toggle('nw-topo-web',
    tab === 'topology' && _topoView === 'web');
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + tab));
  localStorage.setItem('nw-tab', tab);
  if(tab === 'inventory' && typeof fetchInventory === 'function') fetchInventory();
  if(tab === 'servers'   && typeof initServersTab === 'function') initServersTab();
  if(tab === 'briefs') fetchBriefs();
  // Re-fetch topology when switching to the tab (but only after D3 has loaded
  // at least once — initial load is handled by setTopoView on page boot).
  if(tab === 'topology' && _topoD3Loaded && typeof fetchAndRenderTopologyWeb === 'function') fetchAndRenderTopologyWeb();
}

const REFRESH = 5000;
let _firstRender = true;
let lastOk = true;
let lastData = null;
let openDrawerIp = null;
let drawerHistRange = 24;  // hours; persists across drawer opens this session
let _drawerOpener = null;  // element that triggered openDrawer — focus returned on close
const HIST_RANGES = [['1h', 1], ['6h', 6], ['24h', 24], ['7d', 168]];










function renderHost(h){
  const isIdle = h.status === 'IDLE';
  const isDegraded = h.status === 'DEGRADED';
  const dotCls = h.status === 'WAIT' ? 'dot-wt' : isDegraded ? 'dot-degraded' : h.is_up ? 'dot-up' : (isIdle ? 'dot-idle' : 'dot-dn');
  const badgeCls = h.status === 'WAIT' ? 'badge-wt' : isDegraded ? 'badge-degraded' : h.is_up ? 'badge-up' : (isIdle ? 'badge-idle' : 'badge-dn');
  const nameStyle = 'style="display:flex;align-items:center;gap:5px'
    + (h.is_up || h.status === 'WAIT' || isIdle || isDegraded ? '' : ';color:var(--red)')
    + '"';
  const uPct = h.uptime_pct;
  const uColor = isIdle ? 'var(--hint)' : uptimeColor(uPct);
  const uBarColor = isIdle ? 'var(--border)' : uColor;
  const uBarW = uPct !== null ? uPct.toFixed(1) : 0;
  const uLabel = uPct !== null ? uPct.toFixed(1) + '%' : '-%';
  const rowCls = isDegraded ? ' degraded-row' : (h.is_up || h.status === 'WAIT' || isIdle ? '' : ' down-row');
  const ipAttr = ' data-ip="' + escapeHtml(h.ip) + '"';
  return '<div class="row' + rowCls + '"' + ipAttr + ' tabindex="0" role="button" onclick="openDrawer(this.dataset.ip)">'
    + '<div><span class="dot ' + dotCls + '"></span></div>'
    + '<div><div class="host-name" ' + nameStyle + '>'
    + deviceIcon(h.device_type, 22)
    + '<span>' + escapeHtml(h.name) + '</span></div><div class="host-ip-sub">' + escapeHtml(h.ip) + '</div></div>'
    + '<div class="col-ip">' + escapeHtml(h.ip) + '</div>'
    + '<div><span class="badge ' + badgeCls + '">' + h.status + '</span></div>'
    + '<div class="lat">' + fmtLatency(h.latency_ms) + '</div>'
    + '<div class="uptime-cell"><div class="uptime-track"><div class="uptime-fill" style="width:' + uBarW + '%;background:' + uBarColor + '"></div></div><span class="uptime-pct" style="color:' + uColor + '">' + uLabel + '</span></div>'
    + '<div class="col-ping">' + h.last_checked + '</div>'
    + '</div>';
}

let _hostStatusChip = 'all';

function setHostChip(btn){
  _hostStatusChip = btn.dataset.status;
  document.querySelectorAll('.hosts-status-chip').forEach(b => b.classList.toggle('active', b === btn));
  applyHostFilter();
}

function applyHostFilter(){
  if(!lastData) return;
  const q = (document.getElementById('hosts-filter').value || '').toLowerCase().trim();
  const chip = _hostStatusChip;
  const filtered = lastData.hosts.filter(h => {
    if(chip === 'down'     && h.status !== 'DOWN')     return false;
    if(chip === 'degraded' && h.status !== 'DEGRADED') return false;
    if(q && !h.name.toLowerCase().includes(q) && !h.ip.includes(q)) return false;
    return true;
  });
  renderGroups({...lastData, hosts: filtered});
}

function renderGroups(data){
  if(!data.hosts.length){
    document.getElementById('groups').innerHTML =
      '<div class="events-empty"><div class="events-empty-icon" style="background:var(--subtle);color:var(--hint)">⊘</div>'
      + '<div class="events-empty-title">No hosts match</div>'
      + '<div class="events-empty-sub">Try clearing the filter or status chips.</div></div>';
    return;
  }
  const groups = {};
  data.hosts.forEach(h => {
    if(!groups[h.group]) groups[h.group] = [];
    groups[h.group].push(h);
  });
  document.getElementById('groups').innerHTML = Object.entries(groups).map(([name, hosts]) => {
    const sorted = sortHosts(hosts);
    const downCount = hosts.filter(h => h.status === 'DOWN').length;
    const labelExtras = downCount > 0 ? '<span class="down-pill">' + downCount + ' DOWN</span>' : '';
    return '<div class="group">'
      + '<div class="group-label">' + escapeHtml(name) + labelExtras + '</div>'
      + '<div class="table' + (downCount > 0 ? ' has-down' : '') + '">'
      + '<div class="row hdr"><div></div><div>Host</div><div class="col-ip">IP address</div><div>Status</div><div>Latency</div><div>Uptime</div><div class="col-ping">Last ping</div></div>'
      + sorted.map(renderHost).join('')
      + '</div></div>';
  }).join('');
}

function renderTopologyNode(h){
  const isIdle = h.status === 'IDLE';
  const isDegraded = h.status === 'DEGRADED';
  const cls = h.status === 'WAIT' ? 'wait' : isDegraded ? 'degraded' : h.is_up ? 'up' : (isIdle ? 'idle' : 'down');
  let lat;
  if(isIdle) lat = 'idle';
  else if(h.status === 'WAIT') lat = '...';
  else if(isDegraded) lat = 'degraded';
  else if(h.is_up && h.latency_ms !== null) lat = h.latency_ms.toFixed(1) + 'ms';
  else lat = 'offline';
  return '<div class="node ' + cls + '" data-ip="' + escapeHtml(h.ip) + '" tabindex="0" role="button" onclick="openDrawer(this.dataset.ip)">'
    + '<span class="node-dot"></span>'
    + '<span class="node-name">' + escapeHtml(h.name) + '</span>'
    + '<span class="node-lat">' + lat + '</span>'
    + '</div>';
}

function renderTopology(data){
  const groups = {};
  data.hosts.forEach(h => {
    if(!groups[h.group]) groups[h.group] = [];
    groups[h.group].push(h);
  });
  document.getElementById('topo-grid').innerHTML = Object.entries(groups).map(([name, hosts]) => {
    const sorted = sortHosts(hosts);
    const upCount = hosts.filter(h => h.is_up).length;
    const downCount = hosts.filter(h => h.status === 'DOWN').length;
    const totalNonIdle = hosts.filter(h => h.status !== 'IDLE').length;
    return '<div class="topo-group' + (downCount > 0 ? ' has-down' : '') + '">'
      + '<div class="topo-hdr">'
      + '<div class="topo-name">' + escapeHtml(name) + '</div>'
      + '<div class="topo-count' + (downCount > 0 ? ' has-down' : '') + '">' + upCount + ' of ' + totalNonIdle + ' up</div>'
      + '</div>'
      + '<div class="nodes">' + sorted.map(renderTopologyNode).join('') + '</div>'
      + '</div>';
  }).join('');

  const problemHosts = data.hosts.filter(h => h.status === 'DOWN');
  const banner = document.getElementById('problem-banner');
  const list = document.getElementById('problem-banner-list');
  const titleEl = document.getElementById('problem-banner-title');
  if(problemHosts.length > 0){
    banner.classList.add('show');
    titleEl.textContent = problemHosts.length + ' host' + (problemHosts.length > 1 ? 's' : '') + ' offline';
    list.innerHTML = problemHosts.map(h => {
      let dur = '';
      if(h.last_seen_up_seconds !== undefined && h.last_seen_up_seconds !== null){
        dur = '<span class="dur">down ' + durationStr(h.last_seen_up_seconds) + '</span>';
      } else {
        dur = '<span class="dur">down</span>';
      }
      return '<div class="problem-pill" data-ip="' + escapeHtml(h.ip) + '" tabindex="0" role="button" onclick="openDrawer(this.dataset.ip)"><span class="name">' + escapeHtml(h.name) + '</span><span class="ip">' + escapeHtml(h.ip) + '</span>' + dur + '</div>';
    }).join('');
  } else {
    banner.classList.remove('show');
  }
}

function renderEvents(data){
  const events = data.events || [];
  const ongoing = events.filter(e => e.ongoing).length;
  const countBadge = document.getElementById('events-count');
  if(ongoing > 0){
    countBadge.textContent = ongoing;
    countBadge.style.display = '';
  } else {
    countBadge.style.display = 'none';
  }
  const empty = document.getElementById('events-empty');
  const list = document.getElementById('events-list');
  if(!events.length){
    empty.style.display = '';
    list.style.display = 'none';
    return;
  }
  empty.style.display = 'none';
  list.style.display = '';
  const dayLabel = ts => {
    const d = new Date(ts * 1000), now = new Date();
    const sameDay = (a,b) => a.toDateString() === b.toDateString();
    if(sameDay(d, now)) return 'Today';
    const y = new Date(now); y.setDate(now.getDate() - 1);
    if(sameDay(d, y)) return 'Yesterday';
    return d.toLocaleDateString(undefined, {month:'short', day:'numeric'});
  };
  let lastDay = null, html = '';
  events.forEach(e => {
    const day = e.started_ts ? dayLabel(e.started_ts) : '';
    if(day && day !== lastDay){
      html += '<div class="events-day-hdr">' + day + '</div>';
      lastDay = day;
    }
    const cls = e.ongoing ? 'ongoing' : 'resolved';
    const badgeCls = e.ongoing ? 'badge-dn' : 'badge-up';
    const badgeTxt = e.ongoing ? 'ONGOING' : 'RESOLVED';
    const dur = durationStr(e.duration_seconds || 0);
    const timeLabel = e.started_ts
      ? new Date(e.started_ts * 1000).toLocaleTimeString(undefined, {hourCycle:'h23'})
      : (e.started_str || '');
    html += '<div class="event ' + cls + '" data-ip="' + escapeHtml(e.host_ip) + '" tabindex="0" role="button" onclick="openDrawer(this.dataset.ip)">'
      + '<div class="event-bar"></div>'
      + '<div class="event-host">' + escapeHtml(e.host_name) + ' <span class="ip">' + escapeHtml(e.host_ip) + '</span></div>'
      + '<div class="event-time">' + escapeHtml(timeLabel) + '</div>'
      + '<div class="event-dur">' + dur + '</div>'
      + '<div class="event-status"><span class="badge ' + badgeCls + '">' + badgeTxt + '</span></div>'
      + '</div>';
  });
  list.innerHTML = html;
}

let _briefsFetched = false;

function fetchBriefs() {
  if (_briefsFetched) return;
  fetch('/api/brief', {credentials: 'same-origin'})
    .then(r => r.ok ? r.json() : Promise.reject(r.status))
    .then(d => { _briefsFetched = true; renderBriefs(d.briefs || []); })
    .catch(() => {});
}

function renderBriefs(briefs) {
  const empty  = document.getElementById('briefs-empty');
  const list   = document.getElementById('briefs-list');
  const badge  = document.getElementById('briefs-count');

  const dayAgo = Date.now() / 1000 - 86400;
  const recent = briefs.filter(b => b.created_ts > dayAgo).length;
  badge.textContent = recent;
  badge.style.display = recent > 0 ? '' : 'none';

  if (!briefs.length) {
    empty.style.display = '';
    list.style.display  = 'none';
    return;
  }
  empty.style.display = 'none';
  list.style.display  = '';

  const dateKey    = ts => new Date(ts * 1000).toDateString();
  const dateLabel  = ts => {
    const d = new Date(ts * 1000), now = new Date();
    if (d.toDateString() === now.toDateString()) return 'Today';
    const y = new Date(now); y.setDate(now.getDate() - 1);
    if (d.toDateString() === y.toDateString()) return 'Yesterday';
    return d.toLocaleDateString(undefined, {weekday:'long', month:'short', day:'numeric'});
  };
  const timeLabel  = ts => new Date(ts * 1000).toLocaleTimeString(undefined, {hour:'numeric', minute:'2-digit'});
  const briefLabel = ts => { const h = new Date(ts * 1000).getHours(); return h < 12 ? 'Morning Brief' : h < 17 ? 'Afternoon Brief' : 'Evening Brief'; };

  // Group by calendar date (briefs already sorted newest-first)
  const groups = [];
  let curKey = null;
  for (const b of briefs) {
    const k = dateKey(b.created_ts);
    if (k !== curKey) { groups.push({label: dateLabel(b.created_ts), items: []}); curKey = k; }
    groups[groups.length - 1].items.push(b);
  }

  let firstCard = true;
  const html = groups.map(g => {
    const cards = g.items.map(b => {
      const s      = b.stats || {};
      const dn     = s.down  ?? 0;
      const idle   = s.idle  ?? 0;
      const up     = s.up    ?? 0;
      const accent = dn > 0 ? 'var(--red)' : idle > 0 ? 'var(--amber)' : 'var(--green)';
      const label  = briefLabel(b.created_ts);
      const paragraphs = (b.narrative || '').split(/\n\n+/)
        .filter(p => p.trim())
        .map(p => `<p class="brief-para">${escapeHtml(p.trim())}</p>`)
        .join('');
      const dnStyle   = dn   > 0 ? ' style="color:var(--red-text)"'   : '';
      const idleStyle = idle > 0 ? ' style="color:var(--amber-text)"' : '';
      const open = firstCard;
      firstCard  = false;
      return `<div class="brief-card${open ? ' open' : ''}" style="--brief-accent:${accent}">
        <div class="brief-header">
          <div class="brief-header-left">
            <span class="brief-label">${escapeHtml(label)}</span>
            <span class="brief-time">${escapeHtml(timeLabel(b.created_ts))}</span>
          </div>
          <span class="brief-chevron">▾</span>
        </div>
        <div class="brief-body">
          ${paragraphs}
          <div class="brief-footer">
            <span style="color:var(--green-text)">${up} up</span>
            <span${dnStyle}>${dn} dn</span>
            <span${idleStyle}>${idle} idle</span>
          </div>
        </div>
      </div>`;
    }).join('');
    return `<div class="briefs-date-label">${escapeHtml(g.label)}</div>${cards}`;
  }).join('');

  list.innerHTML = html;
  list.querySelectorAll('.brief-header').forEach(hdr => {
    hdr.addEventListener('click', () => hdr.closest('.brief-card').classList.toggle('open'));
  });
}

function renderSummary(data){
  const up = data.hosts.filter(h => h.is_up).length;
  const total = data.hosts.length;
  const down = data.hosts.filter(h => !h.is_up && h.status === 'DOWN').length;
  const degraded = data.hosts.filter(h => h.status === 'DEGRADED').length;
  const lats = data.hosts.filter(h => h.latency_ms !== null).map(h => h.latency_ms);
  const avgLat = lats.length ? (lats.reduce((a,b)=>a+b,0)/lats.length) : null;
  const alwaysOnUpts = data.hosts.filter(h => h.always_on !== false && h.uptime_pct !== null).map(h => h.uptime_pct);
  const avgUpt = alwaysOnUpts.length ? (alwaysOnUpts.reduce((a,b)=>a+b,0)/alwaysOnUpts.length) : null;
  const upEl = document.getElementById('s-up');
  upEl.innerHTML = up + ' <sup>/ ' + total + '</sup>';
  upEl.style.color = down > 0 ? 'var(--red)' : (degraded > 0 ? 'var(--amber)' : 'var(--green)');
  const upCard = document.getElementById('scard-up');
  upCard.classList.toggle('scard-health-ok',  down === 0 && degraded === 0 && total > 0);
  upCard.classList.toggle('scard-health-warn', down > 0 || degraded > 0);
  // Mirror to overlay
  const ovUp = document.getElementById('ov-up');
  const ovTot = document.getElementById('ov-tot');
  if(ovUp){
    ovUp.textContent = up;
    ovUp.style.color = down > 0 ? 'var(--red)' : (degraded > 0 ? 'var(--amber)' : 'var(--green)');
  }
  if(ovTot) ovTot.textContent = total;
  let subTxt;
  if(down > 0 && degraded > 0) subTxt = down + ' offline, ' + degraded + ' degraded';
  else if(down > 0) subTxt = down + ' host' + (down>1?'s':'') + ' offline';
  else if(degraded > 0) subTxt = degraded + ' service issue' + (degraded>1?'s':'');
  else subTxt = 'all hosts online';
  document.getElementById('s-up-sub').textContent = subTxt;
  const latEl = document.getElementById('s-lat');
  latEl.innerHTML = avgLat !== null ? avgLat.toFixed(1) + ' <sup>ms</sup>' : '-';
  latEl.style.color = 'var(--blue)';
  const ovLat = document.getElementById('ov-lat');
  if(ovLat) ovLat.innerHTML = (avgLat !== null ? avgLat.toFixed(1) : '-') + '<span class="topo-overlay-unit">ms</span>';
  const uptEl = document.getElementById('s-upt');
  uptEl.innerHTML = avgUpt !== null ? avgUpt.toFixed(1) + ' <sup>%</sup>' : '-';
  uptEl.style.color = avgUpt !== null && avgUpt >= 95 ? 'var(--green)' : 'var(--amber)';
  const ovUpt = document.getElementById('ov-upt');
  if(ovUpt){
    ovUpt.innerHTML = (avgUpt !== null ? avgUpt.toFixed(1) : '-') + '<span class="topo-overlay-unit">%</span>';
    ovUpt.style.color = avgUpt !== null && avgUpt >= 95 ? 'var(--green)' : 'var(--amber)';
  }
  const totEl = document.getElementById('s-tot');
  totEl.innerHTML = total + ' <sup>hosts</sup>';
  totEl.style.color = 'var(--text)';
  document.getElementById('s-interval').textContent = data.settings.default_interval + 's poll interval';
}

async function refresh(){
  try {
    const res = await fetch('/api/status');
    if(res.status === 401){
      if(_authState.logged_in){ _authState.logged_in = false; updateAuthUI(); openLogin(refresh); }
      else { showLanding(_authState.setup_required ? 'setup' : 'login'); }
      return;
    }
    if(!res.ok) throw new Error('bad');
    const data = await res.json();
    lastData = data;
    if(window.updateMiraStatus) window.updateMiraStatus(data);
    window.nwLastData = data;
    const down = data.hosts.filter(h => !h.is_up && h.status === 'DOWN').length;
    const fav = document.getElementById('favicon-link');
    if(fav){
      const want = down > 0 ? '/static/favicon-alert.svg' : '/static/favicon.svg';
      if(!fav.href.endsWith(want)) fav.href = want;
    }
    if(_firstRender){
      _firstRender = false;
      if(!window.matchMedia('(prefers-reduced-motion: reduce)').matches){
        document.body.classList.add('nw-anim');
        // Safe only while 900ms < REFRESH: no re-render lands mid-animation.
        setTimeout(() => document.body.classList.remove('nw-anim'), 900);
      }
    }
    renderSummary(data);
    renderTopology(data);
    updateTopologyWebStatus(data);
    renderGroups(data);
    if(_hostStatusChip !== 'all' || (document.getElementById('hosts-filter') && document.getElementById('hosts-filter').value)){
      applyHostFilter();
    }
    renderEvents(data);
    if(openDrawerIp){
      const h = data.hosts.find(x => x.ip === openDrawerIp);
      if(h) renderDrawer(h, data);
    }
    // Note: pi-health auto-refresh happens inside renderDrawer when h.is_pi
    if(!lastOk){
      document.getElementById('err-banner').style.display = 'none';
      const pipEl = document.getElementById('pip');
      pipEl.classList.remove('stale');
      pipEl.querySelector('span:last-child').textContent = 'live';
      lastOk = true;
    }
  } catch(e) {
    document.getElementById('err-banner').style.display = 'block';
    const pipEl = document.getElementById('pip');
    pipEl.classList.add('stale');
    pipEl.querySelector('span:last-child').textContent = 'stale';
    lastOk = false;
    if(window.updateMiraStatus) window.updateMiraStatus(null);
  }
}

function clockTick(){
  const d = new Date();
  const p = n => String(n).padStart(2,'0');
  document.getElementById('clock').textContent =
    d.getFullYear() + '-' + p(d.getMonth()+1) + '-' + p(d.getDate()) + '  ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

function openDrawer(ip){
  if(!lastData) return;
  const h = lastData.hosts.find(x => x.ip === ip);
  if(!h) return;
  _drawerOpener = document.activeElement;
  openDrawerIp = ip;
  renderDrawer(h, lastData);
  document.getElementById('drawer').classList.add('open');
  document.getElementById('drawer-backdrop').classList.add('open');
  const closeBtn = document.querySelector('.drawer-close');
  if(closeBtn) closeBtn.focus();
}
function closeDrawer(){
  openDrawerIp = null;
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('drawer-backdrop').classList.remove('open');
  // Clear the cached host so next open does a fresh render
  const body = document.getElementById('drawer-body');
  if(body) body.dataset.hostIp = '';
  if(_drawerOpener && _drawerOpener.focus){ _drawerOpener.focus(); }
  _drawerOpener = null;
}

function renderDrawer(h, data){
  const dotEl = document.getElementById('d-dot');
  const iconColor = h.status === 'WAIT' ? 'var(--amber)' : h.status === 'DEGRADED' ? 'var(--amber)' : h.is_up ? 'var(--green)' : (h.status === 'IDLE' ? 'var(--hint)' : 'var(--red)');
  const iconType = h.device_type || 'host';
  dotEl.className = 'drawer-icon-wrap';
  dotEl.innerHTML = '<svg width="32" height="32" viewBox="0 0 32 32" style="color:' + iconColor + '" aria-hidden="true"><use href="#topo-icon-' + iconType + '"/></svg>';
  document.getElementById('d-name').textContent = h.name;
  const badgeCls = h.status === 'WAIT' ? 'badge-wt' : h.status === 'DEGRADED' ? 'badge-degraded' : h.is_up ? 'badge-up' : (h.status === 'IDLE' ? 'badge-idle' : 'badge-dn');
  document.getElementById('d-meta').innerHTML =
    '<span>' + escapeHtml(h.ip) + '</span><span>·</span><span>' + escapeHtml(h.group) + '</span>'
    + '<span class="badge ' + badgeCls + '">' + h.status + '</span>';

  // Stats
  const isIdle = h.status === 'IDLE';
  const lats = (h.history || []).filter(x => x === true).length;
  const totalPings = (h.history || []).length;
  let avgLat = null;
  if(h.latency_ms !== null) avgLat = h.latency_ms;
  const availLabel = h.uptime_pct !== null ? h.uptime_pct.toFixed(1) + ' <sup>%</sup>' : '-';
  const uColor = isIdle ? 'var(--hint)' : uptimeColor(h.uptime_pct);
  const statusColor = h.status === 'WAIT' || h.status === 'DEGRADED' ? 'var(--amber-text)'
    : h.is_up ? 'var(--green-text)' : (isIdle ? 'var(--hint)' : 'var(--red-text)');

  let statsHtml = '<div class="d-statgrid">'
    + '<div class="d-stat"><div class="d-stat-label">STATUS</div><div class="d-stat-val" style="color:' + statusColor + '">' + h.status + '</div></div>'
    + '<div class="d-stat"><div class="d-stat-label">LATENCY</div><div class="d-stat-val blue">' + (h.latency_ms !== null ? h.latency_ms.toFixed(1) + ' <sup>ms</sup>' : '-') + '</div></div>'
    + '<div class="d-stat"><div class="d-stat-label">' + (isIdle ? 'AVAILABILITY' : 'UPTIME') + '</div><div class="d-stat-val" style="color:' + uColor + '">' + availLabel + '</div></div>'
    + '<div class="d-stat"><div class="d-stat-label">LAST SEEN</div><div class="d-stat-val" style="font-size:14px">' + lastSeenStr(h.last_seen_up_seconds) + '</div></div>'
    + '</div>';

  // Links section (always visible - primary defaults to http://<ip>)
  let linksHtml = '';
  const primaryUrl = (h.links && h.links.primary) || ('http://' + h.ip);
  const extras = (h.links && h.links.extras) || [];
  linksHtml = '<div class="d-section"><div class="d-section-hdr"><span>Quick links</span></div>'
    + '<a class="d-link-primary" href="' + escapeHtml(primaryUrl) + '" target="_blank" rel="noopener">'
    + '<span><span class="d-link-name">Open</span> <span class="d-link-url">' + escapeHtml(primaryUrl) + '</span></span>'
    + '<span class="d-link-arrow">→</span></a>';
  if(extras.length){
    linksHtml += '<div class="d-link-extras">' + extras.map(e =>
      '<a class="d-link-extra" href="' + escapeHtml(e.url) + '" target="_blank" rel="noopener">'
      + '<span class="d-link-name">' + escapeHtml(e.name) + '</span>'
      + '<span class="d-link-url">' + escapeHtml(e.url) + '</span></a>'
    ).join('') + '</div>';
  }
  linksHtml += '</div>';

  // Sparkline
  const hist = h.history || [];
  let sparkHtml = '';
  if(hist.length > 0){
    sparkHtml = '<div class="d-section"><div class="d-section-hdr"><span>Recent ping history</span><span style="color:var(--muted)">last ' + hist.length + ' pings</span></div>'
      + '<div class="d-spark-wrap"><div class="d-spark">'
      + hist.map(v => v ? '<div class="d-spark-bar" style="height:36px"></div>' : '<div class="d-spark-bar dn"></div>').join('')
      + '</div><div class="d-spark-axis"><span>oldest</span><span>now</span></div></div></div>';
  }

  // Latency history (filled async by loadDrawerHistory after the body builds)
  const histHtml = '<div class="d-section" id="d-hist-section"></div>';

  // Specs
  const specs = h.specs || {};
  const specEntries = [
    ['CPU', specs.cpu],
    ['RAM', specs.ram],
    ['STORAGE', specs.storage],
    ['OS', specs.os],
    ['MAC', specs.mac, true],
  ].filter(([_,v]) => v && String(v).trim());
  let specsHtml = '';
  if(specEntries.length){
    specsHtml = '<div class="d-section"><div class="d-section-hdr"><span>Specs</span></div><div class="d-specs">'
      + specEntries.map(([k,v,mono]) => '<div class="d-spec-row"><div class="d-spec-key">' + k + '</div><div class="d-spec-val' + (mono ? ' mono' : '') + '">' + escapeHtml(v) + '</div></div>').join('')
      + '</div></div>';
  }

  // Notes
  let notesHtml = '';
  if(h.notes && String(h.notes).trim()){
    notesHtml = '<div class="d-section"><div class="d-section-hdr"><span>Notes</span></div><div class="d-notes">' + escapeHtml(h.notes) + '</div></div>';
  }

  // Recent incidents (filter to this host)
  const events = (data.events || []).filter(e => e.host_ip === h.ip);
  let incHtml = '<div class="d-section"><div class="d-section-hdr"><span>Recent incidents</span><span style="color:var(--muted)">' + events.length + ' total</span></div>';
  if(events.length === 0){
    incHtml += '<div class="d-empty">No incidents recorded for this host.</div></div>';
  } else {
    incHtml += '<div class="d-incidents">' + events.slice(0, 5).map(e => {
      const cls = e.ongoing ? 'ongoing' : '';
      const bdgCls = e.ongoing ? 'ongoing' : 'resolved';
      const bdgTxt = e.ongoing ? 'ONGOING' : 'RESOLVED';
      return '<div class="d-incident ' + cls + '">'
        + '<div class="d-incident-bar"></div>'
        + '<div class="d-incident-time">' + escapeHtml(e.started_str) + ' <span class="dur">' + durationStr(e.duration_seconds) + '</span></div>'
        + '<div><span class="d-incident-bdg ' + bdgCls + '">' + bdgTxt + '</span></div>'
        + '</div>';
    }).join('') + '</div></div>';
  }

  // Wake-on-LAN action (only for always_on=false hosts with a MAC set)
  let actionsHtml = '';
  if(h.always_on === false && specs.mac && String(specs.mac).trim()){
    actionsHtml = '<div class="d-section"><div class="d-section-hdr"><span>Actions</span></div>'
      + '<div class="d-actions">'
      + '<button class="d-action-btn" id="d-wake-btn" data-ip="' + escapeHtml(h.ip) + '"><span>Wake this device</span><span class="arrow">→</span></button>'
      + '</div>'
      + '<div class="d-action-hint">Sends a Wake-on-LAN magic packet to ' + escapeHtml(specs.mac) + ' on your local network. Requires WoL to be enabled in the host\'s BIOS/UEFI and OS.</div>'
      + '<div class="d-action-status" id="d-wake-status"></div>'
      + '</div>';
  }

  // Services section (only if host has any configured)
  let svcHtml = '';
  if(h.services && h.services.length){
    const strictNote = h.strict ? '<span class="d-svc-strict-note">strict</span>' : '';
    svcHtml = '<div class="d-section"><div class="d-section-hdr"><span>Services ' + strictNote + '</span></div>'
      + '<div class="d-services">'
      + h.services.map(svc => {
        const stateClass = svc.ok === true ? 'ok' : svc.ok === false ? 'fail' : 'unknown';
        const stateTxt = svc.ok === true ? 'OK' : svc.ok === false ? (svc.error || 'fail') : '...';
        return '<div class="d-svc" data-svc-port="' + svc.port + '">'
          + '<span class="d-svc-dot ' + stateClass + '"></span>'
          + '<div><span class="d-svc-name">' + escapeHtml(svc.name) + '</span><span class="d-svc-port">:' + svc.port + '</span></div>'
          + '<span class="d-svc-state ' + stateClass + '">' + escapeHtml(stateTxt) + '</span>'
          + '<span class="d-svc-checked">' + escapeHtml(svc.checked || '') + '</span>'
          + '</div>';
      }).join('')
      + '</div></div>';
  }

  let piHtml = '';
  if(h.is_pi){
    piHtml = '<div class="d-section"><div class="d-section-hdr"><span>System health</span><span style="color:var(--muted)" id="d-pi-meta"></span></div>'
      + '<div class="d-pihealth" id="d-pihealth">'
      + '<div class="d-pi-row" data-metric="temp" style="display:none"><div class="d-pi-key">CPU TEMP</div><div class="d-pi-bar"><div class="d-pi-bar-fill"></div></div><span class="d-pi-val mono"></span></div>'
      + '<div class="d-pi-row" data-metric="load" style="display:none"><div class="d-pi-key">LOAD AVG</div><div class="d-pi-bar"><div class="d-pi-bar-fill"></div></div><span class="d-pi-val mono"></span></div>'
      + '<div class="d-pi-row" data-metric="mem" style="display:none"><div class="d-pi-key">MEMORY</div><div class="d-pi-bar"><div class="d-pi-bar-fill"></div></div><div class="d-pi-val mono"></div></div>'
      + '<div class="d-pi-row" data-metric="disk" style="display:none"><div class="d-pi-key">DISK</div><div class="d-pi-bar"><div class="d-pi-bar-fill"></div></div><div class="d-pi-val mono"></div></div>'
      + '<div class="d-pi-row" data-metric="uptime" style="display:none"><div class="d-pi-key">UPTIME</div><div></div><span class="d-pi-val mono"></span></div>'
      + '<div class="d-pi-empty" id="d-pi-empty">Reading metrics...</div>'
      + '</div></div>';
  }

  // Only rebuild the drawer body if it's a different host than what's currently shown.
  // Otherwise just update the stat values in place to avoid flashing.
  const drawerBody = document.getElementById('drawer-body');
  if(drawerBody.dataset.hostIp !== h.ip){
    drawerBody.dataset.hostIp = h.ip;
    drawerBody.innerHTML = statsHtml + linksHtml + '<div id="d-inv-section"></div>' + svcHtml + piHtml + sparkHtml + histHtml + specsHtml + notesHtml + incHtml + actionsHtml;
    fetchHostInventoryLink(h);
    loadDrawerHistory(h.ip);
    const wakeBtn = document.getElementById('d-wake-btn');
    if(wakeBtn) wakeBtn.addEventListener('click', () => sendWake(wakeBtn.dataset.ip));
  } else {
    // Same host - just update the stat values without rebuilding everything
    updateDrawerStats(h, data);
  }

  if(h.is_pi){
    updatePiHealth();
  }
}

// ── Latency history chart + daily uptime strip ──────────────────────────────

async function loadDrawerHistory(ip){
  const el = document.getElementById('d-hist-section');
  if(!el) return;
  el.innerHTML = '<div class="d-section-hdr"><span>Latency history</span><span style="color:var(--muted)">loading…</span></div>';
  try{
    const res = await fetch('/api/history?ip=' + encodeURIComponent(ip) + '&hours=' + drawerHistRange + '&days=60');
    if(!res.ok) throw new Error('HTTP ' + res.status);
    renderDrawerHistory(el, ip, await res.json());
  }catch(e){
    el.innerHTML = '<div class="d-section-hdr"><span>Latency history</span></div>'
      + '<div class="d-hist-empty">history unavailable</div>';
  }
}

function setHistRange(btn){
  drawerHistRange = parseInt(btn.dataset.hours, 10) || 24;
  loadDrawerHistory(btn.dataset.ip);
}

function renderDrawerHistory(el, ip, data){
  const btns = HIST_RANGES.map(([label, hrs]) =>
    '<button class="d-range-btn' + (hrs === drawerHistRange ? ' active' : '') + '" data-hours="' + hrs
    + '" data-ip="' + escapeHtml(ip) + '" onclick="setHistRange(this)">' + label + '</button>'
  ).join('');
  const hdr = '<div class="d-section-hdr"><span>Latency history</span><span class="d-range-group">' + btns + '</span></div>';
  const chart = '<div class="d-spark-wrap">' + latencyChartSvg(data.points || [], data.bucket_seconds || 60) + '</div>';
  let daysHtml = '';
  if(data.daily && data.daily.length){
    daysHtml = '<div class="d-section-hdr" style="margin-top:10px"><span>Daily uptime</span>'
      + '<span style="color:var(--muted)">last ' + data.daily.length + ' day' + (data.daily.length > 1 ? 's' : '') + '</span></div>'
      + '<div class="d-spark-wrap">' + dayStripHtml(data.daily) + '</div>';
  }
  el.innerHTML = hdr + chart + daysHtml;
}

function fmtChartTime(ts, rangeHours){
  const d = new Date(ts * 1000);
  if(rangeHours <= 48) return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  return (d.getMonth() + 1) + '/' + d.getDate();
}

function latencyChartSvg(points, bucketSeconds){
  const pts = points.filter(p => p.avg !== null && p.avg !== undefined);
  if(pts.length < 2) return '<div class="d-hist-empty">not enough data for this range yet</div>';
  const W = 560, H = 130, L = 38, R = 6, T = 8, B = 18;
  const t0 = points[0].t, t1 = points[points.length - 1].t + bucketSeconds;
  const maxLat = Math.max(...pts.map(p => (p.max !== null && p.max !== undefined) ? p.max : p.avg));
  const yMax = Math.max(1, maxLat * 1.12);
  const x = t => L + (t - t0) / Math.max(1, t1 - t0) * (W - L - R);
  const y = v => T + (1 - v / yMax) * (H - T - B);
  const band = pts.map((p, i) => (i ? 'L' : 'M') + x(p.t).toFixed(1) + ',' + y(p.min ?? p.avg).toFixed(1)).join('')
    + pts.slice().reverse().map(p => 'L' + x(p.t).toFixed(1) + ',' + y(p.max ?? p.avg).toFixed(1)).join('') + 'Z';
  const line = pts.map((p, i) => (i ? 'L' : 'M') + x(p.t).toFixed(1) + ',' + y(p.avg).toFixed(1)).join('');
  const tickW = Math.max(2, (W - L - R) / Math.max(1, points.length));
  const downs = points.filter(p => p.up_pct < 100).map(p =>
    '<rect class="d-lat-down" x="' + x(p.t).toFixed(1) + '" y="' + (H - B + 4) + '" width="' + tickW.toFixed(1) + '" height="4" rx="1"/>'
  ).join('');
  const grid = [0.5, 1].map(f =>
    '<line class="d-lat-grid" x1="' + L + '" y1="' + y(yMax * f).toFixed(1) + '" x2="' + (W - R) + '" y2="' + y(yMax * f).toFixed(1) + '"/>'
  ).join('');
  const rangeHours = (t1 - t0) / 3600;
  // fmtLatency returns an HTML span — SVG <text> needs plain strings
  const fmtMs = v => (v >= 10 ? v.toFixed(0) : v.toFixed(1)) + ' ms';
  return '<svg class="d-lat-chart" viewBox="0 0 ' + W + ' ' + H + '">'
    + grid
    + '<path class="d-lat-band" d="' + band + '"/>'
    + '<path class="d-lat-line" d="' + line + '"/>'
    + downs
    + '<text class="d-lat-label" x="' + (L - 5) + '" y="' + (y(yMax) + 3) + '" text-anchor="end">' + fmtMs(yMax) + '</text>'
    + '<text class="d-lat-label" x="' + (L - 5) + '" y="' + (y(yMax * 0.5) + 3) + '" text-anchor="end">' + fmtMs(yMax * 0.5) + '</text>'
    + '<text class="d-lat-label" x="' + L + '" y="' + (H - 4) + '">' + fmtChartTime(t0, rangeHours) + '</text>'
    + '<text class="d-lat-label" x="' + (W - R) + '" y="' + (H - 4) + '" text-anchor="end">' + fmtChartTime(t1, rangeHours) + '</text>'
    + '</svg>';
}

function dayStripHtml(daily){
  const cells = daily.map(d => {
    const pct = d.uptime_pct;
    let cls = 'nodata';
    if(pct !== null && pct !== undefined){
      cls = pct >= 99 ? 'ok' : (pct >= 80 ? 'warn' : 'bad');
    }
    const tip = d.day + ' — ' + (pct === null ? 'no data' : pct + '% up')
      + (d.latency_avg !== null && d.latency_avg !== undefined ? ' · ' + d.latency_avg + ' ms avg' : '');
    return '<div class="d-day ' + cls + '" title="' + escapeHtml(tip) + '"></div>';
  }).join('');
  return '<div class="d-days">' + cells + '</div>'
    + '<div class="d-spark-axis"><span>' + escapeHtml(daily[0].day) + '</span><span>' + escapeHtml(daily[daily.length - 1].day) + '</span></div>';
}

function updateDrawerStats(h, data){
  // Update the stats grid in place. We just replace the four stat values
  // with new innerHTML. The structure stays put so there's no flicker.
  const isIdle = h.status === 'IDLE';
  const availLabel = h.uptime_pct !== null ? h.uptime_pct.toFixed(1) + ' <sup>%</sup>' : '-';
  const uColor = isIdle ? 'var(--hint)' : uptimeColor(h.uptime_pct);
  const statusColor = h.status === 'WAIT' || h.status === 'DEGRADED' ? 'var(--amber-text)'
    : h.is_up ? 'var(--green-text)' : (isIdle ? 'var(--hint)' : 'var(--red-text)');

  const stats = document.querySelectorAll('#drawer-body .d-statgrid .d-stat-val');
  if(stats.length >= 4){
    stats[0].className = 'd-stat-val';
    stats[0].style.color = statusColor;
    stats[0].textContent = h.status;
    stats[1].innerHTML = (h.latency_ms !== null ? h.latency_ms.toFixed(1) + ' <sup>ms</sup>' : '-');
    stats[2].style.color = uColor;
    stats[2].innerHTML = availLabel;
    stats[3].textContent = lastSeenStr(h.last_seen_up_seconds);
  }

  // Refresh the header status badge since the host might have changed state
  const meta = document.getElementById('d-meta');
  if(meta){
    const badgeCls = h.status === 'WAIT' ? 'badge-wt' : h.status === 'DEGRADED' ? 'badge-degraded' : h.is_up ? 'badge-up' : (h.status === 'IDLE' ? 'badge-idle' : 'badge-dn');
    meta.innerHTML =
      '<span>' + escapeHtml(h.ip) + '</span><span>·</span><span>' + escapeHtml(h.group) + '</span>'
      + '<span class="badge ' + badgeCls + '">' + h.status + '</span>';
  }
  const dotEl = document.getElementById('d-dot');
  if(dotEl){
    const iconColor = h.status === 'WAIT' ? 'var(--amber)' : h.status === 'DEGRADED' ? 'var(--amber)' : h.is_up ? 'var(--green)' : (h.status === 'IDLE' ? 'var(--hint)' : 'var(--red)');
    const iconType = h.device_type || 'host';
    dotEl.className = 'drawer-icon-wrap';
    dotEl.innerHTML = '<svg width="32" height="32" viewBox="0 0 32 32" style="color:' + iconColor + '" aria-hidden="true"><use href="#topo-icon-' + iconType + '"/></svg>';
  }

  // If the services section is present, update each row in place so users
  // can watch service state change live without the section flickering.
  const svcContainer = document.querySelector('#drawer-body .d-services');
  if(svcContainer && h.services){
    h.services.forEach(svc => {
      const row = svcContainer.querySelector('[data-svc-port="' + svc.port + '"]');
      if(!row) return;
      const dot = row.querySelector('.d-svc-dot');
      const state = row.querySelector('.d-svc-state');
      const checked = row.querySelector('.d-svc-checked');
      const stateClass = svc.ok === true ? 'ok' : svc.ok === false ? 'fail' : 'unknown';
      if(dot) dot.className = 'd-svc-dot ' + stateClass;
      if(state){
        state.className = 'd-svc-state ' + stateClass;
        state.textContent = svc.ok === true ? 'OK' : svc.ok === false ? (svc.error || 'fail') : '...';
      }
      if(checked) checked.textContent = svc.checked || '';
    });
  }
}









async function updatePiHealth(){
  // In-place update: doesn't re-render the section, just changes values.
  // Each metric row already exists in the DOM; we just set its content
  // and visibility based on what came back.
  const target = document.getElementById('d-pihealth');
  if(!target) return;
  try {
    const res = await fetch('/api/pi-health');
    if(!res.ok) throw new Error('bad response');
    const h = await res.json();

    const meta = document.getElementById('d-pi-meta');
    if(meta) meta.textContent = h.cpu_count ? h.cpu_count + ' CPU cores' : '';

    const empty = target.querySelector('#d-pi-empty');
    if(empty) empty.style.display = 'none';

    const setRow = (metric, show, barPct, barColor, valHtml, valColor, valClass) => {
      const row = target.querySelector('[data-metric="' + metric + '"]');
      if(!row) return;
      if(!show){ row.style.display = 'none'; return; }
      row.style.display = '';
      const bar = row.querySelector('.d-pi-bar-fill');
      if(bar){
        if(barPct === null || barPct === undefined){
          bar.parentElement.style.visibility = 'hidden';
        } else {
          bar.parentElement.style.visibility = '';
          bar.style.width = Math.max(2, Math.min(100, barPct)) + '%';
          bar.style.background = barColor;
        }
      }
      const val = row.querySelector('.d-pi-val');
      if(val){
        val.innerHTML = valHtml;
        val.style.color = valColor || '';
        val.className = 'd-pi-val mono ' + (valClass || '');
      }
    };

    setRow('temp',
      h.cpu_temp_c !== undefined,
      h.cpu_temp_c !== undefined ? (h.cpu_temp_c / 90) * 100 : null,
      _tempColor(h.cpu_temp_c),
      h.cpu_temp_c !== undefined ? h.cpu_temp_c.toFixed(1) + ' &deg;C' : '-',
      _tempColor(h.cpu_temp_c)
    );

    setRow('load',
      h.load_1m !== undefined,
      h.load_1m !== undefined ? (h.load_1m / (h.cpu_count || 1)) * 100 : null,
      _loadColor(h.load_1m, h.cpu_count),
      h.load_1m !== undefined ? h.load_1m.toFixed(2) + ' &middot; ' + h.load_5m.toFixed(2) + ' &middot; ' + h.load_15m.toFixed(2) : '-',
      null,
      _loadClass(h.load_1m, h.cpu_count)
    );

    setRow('mem',
      h.mem_pct !== undefined,
      h.mem_pct,
      _pctColor(h.mem_pct),
      h.mem_pct !== undefined
        ? '<div>' + h.mem_pct.toFixed(1) + '%</div><div style="font-size:10px;color:var(--hint);text-align:right">' + _bytesHuman(h.mem_used_bytes) + ' / ' + _bytesHuman(h.mem_total_bytes) + '</div>'
        : '-',
      null,
      _pctClass(h.mem_pct)
    );

    setRow('disk',
      h.disk_pct !== undefined,
      h.disk_pct,
      _pctColor(h.disk_pct),
      h.disk_pct !== undefined
        ? '<div>' + h.disk_pct.toFixed(1) + '%</div><div style="font-size:10px;color:var(--hint);text-align:right">' + _bytesHuman(h.disk_used_bytes) + ' / ' + _bytesHuman(h.disk_total_bytes) + '</div>'
        : '-',
      null,
      _pctClass(h.disk_pct)
    );

    setRow('uptime',
      h.uptime_seconds !== undefined,
      null, null,
      _uptimeHuman(h.uptime_seconds)
    );

    // If literally nothing came back, show the empty state
    const anyVisible = target.querySelectorAll('.d-pi-row[style=""], .d-pi-row:not([style])').length;
    if(anyVisible === 0 && empty){
      empty.textContent = 'No system metrics available.';
      empty.style.display = '';
    }
  } catch(e){
    const empty = target.querySelector('#d-pi-empty');
    if(empty){
      empty.textContent = 'Could not read metrics.';
      empty.style.display = '';
    }
  }
}

async function sendWake(ip){
  const btn = document.getElementById('d-wake-btn');
  const status = document.getElementById('d-wake-status');
  btn.disabled = true;
  status.className = 'd-action-status';
  status.textContent = 'Sending magic packet...';
  try {
    const res = await apiFetch('/api/wake', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ip })
    });
    const data = await res.json();
    if(!res.ok){
      status.className = 'd-action-status error';
      status.textContent = data.error || 'Wake failed';
    } else {
      status.className = 'd-action-status success';
      status.textContent = 'Magic packet sent at ' + new Date().toLocaleTimeString();
    }
  } catch(e){
    status.className = 'd-action-status error';
    status.textContent = 'Network error';
  } finally {
    btn.disabled = false;
  }
}

document.addEventListener('keydown', e => {
  if(e.key !== 'Escape') return;
  const open = id => { const el = document.getElementById(id); return el && el.classList.contains('open'); };
  const aiUsage = document.getElementById('ai-usage-modal');
  const aiPanel = document.getElementById('ai-panel');
  // Order follows the z-index ladder: modals (50) > drawer (41) > AI panel (37)
  if(open('discover-overlay')) closeDiscover();
  else if(open('import-overlay')) closeImportModal();
  else if(open('inv-edit-overlay')) closeInventoryEditor();
  else if(open('add-host-overlay')) closeAddHostModal();
  else if(open('modal-overlay')) closeEditor();
  else if(openDrawerIp) closeDrawer();
  else if(aiUsage && !aiUsage.classList.contains('hidden')) aiUsage.classList.add('hidden');
  else if(aiPanel && !aiPanel.classList.contains('hidden')) aiPanel.classList.add('hidden');
});

// ── Editor ──

function closeEditor(){ document.getElementById('modal-overlay').classList.remove('open'); }

function openAddHostModal(){
  if(_authState.setup_required){ openSetup(); return; }
  if(!_authState.logged_in){ openLogin(() => openAddHostModal()); return; }
  // Clear all fields
  ['ah-name','ah-ip','ah-group','ah-interval','ah-cpu','ah-ram','ah-storage','ah-os','ah-mac','ah-notes'].forEach(cls => {
    const el = document.querySelector('.' + cls);
    if(el) el.tagName === 'TEXTAREA' ? (el.value = '') : (el.value = cls === 'ah-group' ? 'General' : '');
  });
  ['ah-alwayson','ah-alert'].forEach(cls => {
    const el = document.querySelector('.' + cls);
    if(el) el.checked = true;
  });
  document.getElementById('add-host-error').textContent = '';
  document.getElementById('add-host-status').textContent = '';
  document.getElementById('add-host-overlay').classList.add('open');
  setTimeout(() => { const el = document.querySelector('.ah-name'); if(el) el.focus(); }, 50);
}

function closeAddHostModal(){
  document.getElementById('add-host-overlay').classList.remove('open');
}

async function saveAddHost(){
  const nameEl  = document.querySelector('.ah-name');
  const ipEl    = document.querySelector('.ah-ip');
  const macEl   = document.querySelector('.ah-mac');
  const errEl   = document.getElementById('add-host-error');
  const statEl  = document.getElementById('add-host-status');
  errEl.textContent = '';
  [nameEl, ipEl, macEl].forEach(el => el && el.classList.remove('invalid'));

  const name  = nameEl.value.trim();
  const ip    = ipEl.value.trim();
  const group = (document.querySelector('.ah-group').value.trim()) || 'General';
  const intervalRaw = document.querySelector('.ah-interval').value.trim();
  const mac   = macEl.value.trim();

  let hasError = false;
  if(!name){ nameEl.classList.add('invalid'); hasError = true; }
  if(!ipValid(ip)){ ipEl.classList.add('invalid'); hasError = true; }
  if(mac && !macValid(mac)){ macEl.classList.add('invalid'); hasError = true; }
  if(hasError){ errEl.textContent = 'Fix the highlighted fields.'; return; }

  const entry = { name, ip, group, always_on: document.querySelector('.ah-alwayson').checked };
  if(!document.querySelector('.ah-alert').checked) entry.alert = false;
  if(intervalRaw){ const iv = parseInt(intervalRaw); if(!isNaN(iv) && iv >= 5) entry.interval = iv; }

  const specs = {};
  [['cpu','ah-cpu'],['ram','ah-ram'],['storage','ah-storage'],['os','ah-os'],['mac','ah-mac']].forEach(([k, cls]) => {
    const el = document.querySelector('.' + cls);
    if(el && el.value.trim()) specs[k] = el.value.trim();
  });
  if(Object.keys(specs).length) entry.specs = specs;
  const notes = document.querySelector('.ah-notes').value.trim();
  if(notes) entry.notes = notes;

  statEl.textContent = 'Saving…';
  try {
    const existing = await fetch('/api/hosts');
    if(existing.status === 401){ closeAddHostModal(); openLogin(() => openAddHostModal()); return; }
    const existingData = await existing.json();
    const hosts = [...(existingData.hosts || []), entry];

    if(hosts.some((h, i) => i !== hosts.length - 1 && h.ip === ip)){
      ipEl.classList.add('invalid');
      errEl.textContent = 'A host with this IP already exists.';
      statEl.textContent = '';
      return;
    }

    const res = await apiFetch('/api/hosts', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ hosts })
    });
    const data = await res.json();
    if(!res.ok){ statEl.textContent = ''; errEl.textContent = data.error || 'Save failed.'; return; }
    statEl.textContent = 'Added!';
    setTimeout(() => { closeAddHostModal(); refresh(); }, 600);
  } catch(e){ statEl.textContent = ''; errEl.textContent = 'Network error.'; }
}

function addRow(h){
  const row = document.createElement('div');
  row.className = 'edit-row';
  const alwaysOn = !h || h.always_on !== false;
  const alertOn  = !h || h.alert !== false;
  const specs = (h && h.specs) || {};
  const notes = (h && h.notes) || '';
  const hasLinks = !!(h && h.links && (h.links.primary || (h.links.extras && h.links.extras.length)));
  const hasServices = !!(h && Array.isArray(h.services) && h.services.length);
  const hasData = !!(specs.cpu || specs.ram || specs.storage || specs.os || specs.mac || notes || hasLinks || hasServices);

  row.innerHTML =
    '<div class="row-main">'
    + '<input type="text" placeholder="My device" class="f-name" value="' + (h ? escapeHtml(h.name) : '') + '">'
    + '<input type="text" placeholder="192.168.1.1" class="f-ip" value="' + (h ? escapeHtml(h.ip) : '') + '">'
    + '<input type="text" placeholder="Network" class="f-group" value="' + (h ? escapeHtml(h.group || "General") : "General") + '">'
    + '<input type="number" min="5" placeholder="30" class="f-interval" value="' + (h && h.interval ? h.interval : '') + '">'
    + '<div class="ao-cell"><input type="checkbox" class="f-alwayson" title="Always on? Uncheck for laptops/phones/etc." ' + (alwaysOn ? 'checked' : '') + '></div>'
    + '<div class="ao-cell"><input type="checkbox" class="f-alert" title="Alert on down? Uncheck to silence ntfy notifications for this host." ' + (alertOn ? 'checked' : '') + '></div>'
    + '<button class="more-btn' + (hasData ? ' has-data' : '') + '" type="button" title="' + (hasData ? 'More fields (this host has saved extras)' : 'More fields (specs, notes, links)') + '">...</button>'
    + '<button class="del-btn" title="Remove" type="button">X</button>'
    + '</div>'
    + '<div class="row-extra">'
    + '<label>CPU<input type="text" class="f-cpu" placeholder="e.g. Intel i9-12900K" value="' + escapeHtml(specs.cpu || '') + '"></label>'
    + '<label>RAM<input type="text" class="f-ram" placeholder="e.g. 64 GB DDR5" value="' + escapeHtml(specs.ram || '') + '"></label>'
    + '<label>Storage<input type="text" class="f-storage" placeholder="e.g. 2TB NVMe" value="' + escapeHtml(specs.storage || '') + '"></label>'
    + '<label>OS<input type="text" class="f-os" placeholder="e.g. Windows 11" value="' + escapeHtml(specs.os || '') + '"></label>'
    + '<label class="full">MAC address<div class="mac-row"><input type="text" class="f-mac" placeholder="aa:bb:cc:dd:ee:ff (required for Wake-on-LAN)" value="' + escapeHtml(specs.mac || '') + '" data-auto="' + (specs.mac_auto ? '1' : '') + '"><button type="button" class="mac-detect-btn" onclick="detectMac(this)">Detect</button>' + (specs.mac_auto ? '<span class="mac-auto-tag" title="This MAC was auto-detected from the network">auto</span>' : '') + '</div></label>'
    + '<label class="full">Services (TCP port checks)<div class="svc-wrap"></div><button type="button" class="add-svc-btn">+ Add service</button><label class="svc-strict-toggle"><input type="checkbox" class="f-strict" ' + ((h && h.strict) ? 'checked' : '') + '>Strict mode (mark host DEGRADED if any service fails)</label></label>'
    + '<label class="full">Primary URL<input type="text" class="f-primary-url" placeholder="http://' + escapeHtml(h ? h.ip : 'host') + ' (defaults to http://<ip> if blank)" value="' + escapeHtml((h && h.links && h.links.primary && !h.links.primary.endsWith("/" + h.ip) ? h.links.primary : "")) + '"></label>'
    + '<label class="full">Extra links<div class="extras-wrap" data-ip="' + escapeHtml(h ? h.ip : "") + '"></div><button type="button" class="add-extra-btn">+ Add link</button></label>'
    + '<label class="full">Notes<textarea class="f-notes" placeholder="Anything else worth remembering about this device.">' + escapeHtml(notes) + '</textarea></label>'
    + '</div>';

  document.getElementById('edit-rows').appendChild(row);

  // Wire up the more/delete buttons
  row.querySelector('.more-btn').addEventListener('click', () => {
    const extra = row.querySelector('.row-extra');
    const btn = row.querySelector('.more-btn');
    extra.classList.toggle('open');
    btn.classList.toggle('open', extra.classList.contains('open'));
  });
  row.querySelector('.del-btn').addEventListener('click', () => row.remove());

  // Populate extras + wire up "+ Add link"
  const extrasWrap = row.querySelector('.extras-wrap');
  const initialExtras = (h && h.links && Array.isArray(h.links.extras)) ? h.links.extras : [];
  initialExtras.forEach(e => addExtraLinkRow(extrasWrap, e.name, e.url));
  row.querySelector('.add-extra-btn').addEventListener('click', () => addExtraLinkRow(extrasWrap, '', ''));

  // Populate services + wire up "+ Add service"
  const svcWrap = row.querySelector('.svc-wrap');
  const initialServices = (h && Array.isArray(h.services)) ? h.services : [];
  initialServices.forEach(s => addServiceRow(svcWrap, s.port, s.name));
  row.querySelector('.add-svc-btn').addEventListener('click', () => addServiceRow(svcWrap, '', ''));
}

function addServiceRow(container, port, name){
  const div = document.createElement('div');
  div.className = 'svc-row';
  div.innerHTML =
    '<input type="number" class="f-svc-port" placeholder="80" min="1" max="65535" value="' + escapeHtml(port !== undefined && port !== null ? String(port) : '') + '">'
    + '<input type="text" class="f-svc-name" placeholder="e.g. Web UI, SSH" value="' + escapeHtml(name || '') + '">'
    + '<button type="button" class="del-btn" title="Remove">X</button>';
  container.appendChild(div);
  div.querySelector('.del-btn').addEventListener('click', () => div.remove());
}

async function detectMac(btn){
  const row = btn.closest('.edit-row');
  if(!row) return;
  const ipEl = row.querySelector('.f-ip');
  const macEl = row.querySelector('.f-mac');
  if(!ipEl || !macEl) return;
  const ip = ipEl.value.trim();
  if(!ip){ toast('Set the IP first, then try Detect.', 'info'); return; }
  const origText = btn.textContent;
  btn.disabled = true; btn.textContent = '...';
  try {
    const res = await apiFetch('/api/detect-mac', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ip })
    });
    const data = await res.json();
    if(!res.ok){
      toast(data.error || 'Could not detect MAC', 'error');
      return;
    }
    macEl.value = data.mac;
    // Mark as auto-detected (will be saved as mac_auto: true)
    macEl.dataset.auto = '1';
    // Add the visual tag if not already there
    const parent = macEl.parentElement;
    let tag = parent.querySelector('.mac-auto-tag');
    if(!tag){
      tag = document.createElement('span');
      tag.className = 'mac-auto-tag';
      tag.title = 'This MAC was auto-detected from the network';
      tag.textContent = 'auto';
      parent.appendChild(tag);
    }
  } catch(e){
    toast('Network error during MAC detection', 'error');
  } finally {
    btn.disabled = false; btn.textContent = origText;
  }
}

function addExtraLinkRow(container, name, url){
  const div = document.createElement('div');
  div.className = 'extra-link-row';
  div.innerHTML =
    '<input type="text" class="f-extra-name" placeholder="Label (e.g. Admin)" value="' + escapeHtml(name || '') + '">'
    + '<input type="text" class="f-extra-url" placeholder="https://..." value="' + escapeHtml(url || '') + '">'
    + '<button type="button" class="del-btn" title="Remove">X</button>';
  container.appendChild(div);
  div.querySelector('.del-btn').addEventListener('click', () => div.remove());
}




let _discoverPollTimer = null;

function openDiscover(){
  document.getElementById('discover-overlay').classList.add('open');
  document.getElementById('discover-status').textContent = 'Click "Scan now" to discover devices on your network.';
  document.getElementById('discover-results').style.display = 'none';
  document.getElementById('discover-list').innerHTML = '';
  document.getElementById('discover-add-btn').disabled = true;
  document.getElementById('discover-scan-btn').disabled = false;
  // Pre-check current state so we don't restart a finished scan
  refreshDiscoverState(false);
}
function closeDiscover(){
  document.getElementById('discover-overlay').classList.remove('open');
  if(_discoverPollTimer){ clearTimeout(_discoverPollTimer); _discoverPollTimer = null; }
}

async function startDiscover(){
  const btn = document.getElementById('discover-scan-btn');
  const statusEl = document.getElementById('discover-status');
  btn.disabled = true;
  statusEl.textContent = 'Starting scan...';
  document.getElementById('discover-list').innerHTML = '';
  document.getElementById('discover-results').style.display = 'none';
  try {
    const res = await apiFetch('/api/discover', { method: 'POST' });
    const data = await res.json();
    if(!res.ok){
      statusEl.textContent = 'Error: ' + (data.error || 'could not start scan');
      btn.disabled = false;
      return;
    }
    statusEl.textContent = data.message || 'Scan started. This may take a few seconds...';
    pollDiscover();
  } catch(e){
    statusEl.textContent = 'Network error starting scan.';
    btn.disabled = false;
  }
}

function pollDiscover(){
  refreshDiscoverState(true);
}

async function refreshDiscoverState(continuePolling){
  try {
    const res = await fetch('/api/discover');
    if(!res.ok) throw new Error('bad');
    const state = await res.json();
    const statusEl = document.getElementById('discover-status');
    const btn = document.getElementById('discover-scan-btn');

    if(state.running){
      statusEl.textContent = 'Scanning ' + (state.subnet || 'network') + '...';
      btn.disabled = true;
      if(continuePolling){
        _discoverPollTimer = setTimeout(pollDiscover, 1500);
      }
      return;
    }
    if(state.error){
      statusEl.textContent = 'Scan failed: ' + state.error;
      btn.disabled = false;
      return;
    }
    if(state.finished && state.results){
      const total = state.results.length;
      const newOnes = state.results.filter(r => !r.already_monitored).length;
      statusEl.textContent = 'Scan of ' + state.subnet + ' complete - ' + total + ' devices found, ' + newOnes + ' new.';
      renderDiscoverResults(state.results);
      btn.disabled = false;
      btn.textContent = 'Scan again';
      return;
    }
    // No previous scan
    statusEl.textContent = 'Click "Scan now" to discover devices on ' + (state.subnet || 'your network') + '.';
    btn.disabled = false;
  } catch(e){
    document.getElementById('discover-status').textContent = 'Could not reach netwatch.';
  }
}

function renderDiscoverResults(results){
  const wrap = document.getElementById('discover-results');
  const list = document.getElementById('discover-list');
  if(!results.length){
    wrap.style.display = 'none';
    return;
  }
  wrap.style.display = '';
  list.innerHTML = results.map(r => {
    const knownTag = r.already_monitored ? '<span class="disc-known-tag">already monitored</span>' : '';
    const hostnameDisplay = r.hostname || (r.vendor ? '(' + r.vendor + ')' : '(unknown)');
    const vendorLine = (r.hostname && r.vendor) ? '<span class="disc-vendor">' + escapeHtml(r.vendor) + '</span>' : '';
    const checkbox = r.already_monitored ? '' : '<input type="checkbox" class="disc-check" data-ip="' + escapeHtml(r.ip) + '" data-name="' + escapeHtml(r.hostname || r.vendor || ('Host ' + r.ip)) + '" data-mac="' + escapeHtml(r.mac || '') + '">';
    return '<div class="disc-row' + (r.already_monitored ? ' known' : '') + '">'
      + '<div>' + checkbox + '</div>'
      + '<div class="disc-ip">' + escapeHtml(r.ip) + '</div>'
      + '<div class="disc-name"><span class="disc-hostname">' + escapeHtml(hostnameDisplay) + knownTag + '</span>' + vendorLine + '</div>'
      + '<div class="disc-mac">' + escapeHtml(r.mac || '') + '</div>'
      + '</div>';
  }).join('');
  // Wire up checkboxes to enable/disable the Add button
  const update = () => {
    const any = list.querySelectorAll('.disc-check:checked').length > 0;
    document.getElementById('discover-add-btn').disabled = !any;
  };
  list.querySelectorAll('.disc-check').forEach(cb => cb.addEventListener('change', update));
  update();
}

async function addDiscovered(){
  const checked = document.querySelectorAll('.disc-check:checked');
  if(!checked.length) return;
  // Fetch the existing host list so we can append to it (rather than replace)
  let existing = [];
  try {
    const res = await fetch('/api/hosts');
    const data = await res.json();
    existing = data.hosts || [];
  } catch(e){
    document.getElementById('discover-status').textContent = 'Could not load existing hosts.';
    return;
  }
  // Build new entries
  const additions = [];
  checked.forEach(cb => {
    const ip = cb.dataset.ip;
    const name = cb.dataset.name || ('Host ' + ip);
    const mac = cb.dataset.mac;
    const entry = { name, ip, group: 'Discovered' };
    if(mac){ entry.specs = { mac }; }
    additions.push(entry);
  });
  const merged = existing.concat(additions);
  const res = await apiFetch('/api/hosts', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ hosts: merged })
  });
  const data = await res.json();
  if(!res.ok){
    document.getElementById('discover-status').textContent = 'Save failed: ' + (data.error || 'unknown');
    return;
  }
  document.getElementById('discover-status').textContent = 'Added ' + additions.length + ' host' + (additions.length===1?'':'s') + '. Closing...';
  setTimeout(() => {
    closeDiscover();
    closeEditor();
    refresh();
  }, 800);
}

async function saveHosts(){
  const rows = document.querySelectorAll('#edit-rows .edit-row');
  const hosts = [];
  let hasError = false;
  const seenIps = new Set();
  rows.forEach(row => {
    const nameEl = row.querySelector('.f-name');
    const ipEl = row.querySelector('.f-ip');
    const groupEl = row.querySelector('.f-group');
    const intervalEl = row.querySelector('.f-interval');
    const macEl = row.querySelector('.f-mac');
    [nameEl, ipEl, macEl].forEach(el => el && el.classList.remove('invalid'));
    const name = nameEl.value.trim();
    const ip = ipEl.value.trim();
    const group = groupEl.value.trim() || 'General';
    const intervalRaw = intervalEl.value.trim();
    if(!name && !ip) return;
    if(!name){ nameEl.classList.add('invalid'); hasError = true; }
    if(!ipValid(ip)){ ipEl.classList.add('invalid'); hasError = true; }
    if(seenIps.has(ip)){ ipEl.classList.add('invalid'); hasError = true; }
    seenIps.add(ip);
    const mac = macEl.value.trim();
    if(!macValid(mac)){ macEl.classList.add('invalid'); hasError = true; }

    const entry = { name, ip, group };
    if(intervalRaw){
      const iv = parseInt(intervalRaw);
      if(!isNaN(iv) && iv >= 5) entry.interval = iv;
    }
    const alwaysOnEl = row.querySelector('.f-alwayson');
    entry.always_on = alwaysOnEl ? alwaysOnEl.checked : true;
    const alertEl = row.querySelector('.f-alert');
    if(alertEl && !alertEl.checked) entry.alert = false;

    const specs = {};
    ['cpu','ram','storage','os','mac'].forEach(k => {
      const el = row.querySelector('.f-' + k);
      if(el && el.value.trim()) specs[k] = el.value.trim();
    });
    // Preserve mac_auto flag if the MAC field still has its auto marker
    // (macEl is the one already grabbed earlier in this function for validation)
    if(macEl && macEl.dataset.auto === '1' && macEl.value.trim()){
      specs.mac_auto = true;
    }
    if(Object.keys(specs).length) entry.specs = specs;
    const notesEl = row.querySelector('.f-notes');
    if(notesEl && notesEl.value.trim()) entry.notes = notesEl.value.trim();

    // Links
    const links = {};
    const primaryEl = row.querySelector('.f-primary-url');
    const primaryVal = primaryEl ? primaryEl.value.trim() : '';
    if(primaryVal){
      if(!/^https?:\/\//.test(primaryVal)){ primaryEl.classList.add('invalid'); hasError = true; }
      else links.primary = primaryVal;
    }
    const extras = [];
    row.querySelectorAll('.extra-link-row').forEach(extraRow => {
      const en = extraRow.querySelector('.f-extra-name').value.trim();
      const eu = extraRow.querySelector('.f-extra-url').value.trim();
      if(!en && !eu) return;
      if(!en || !eu || !/^https?:\/\//.test(eu)){
        extraRow.querySelector('.f-extra-url').classList.add('invalid');
        if(!en) extraRow.querySelector('.f-extra-name').classList.add('invalid');
        hasError = true;
        return;
      }
      extras.push({ name: en, url: eu });
    });
    if(extras.length) links.extras = extras;
    if(Object.keys(links).length) entry.links = links;

    // Services
    const services = [];
    row.querySelectorAll('.svc-row').forEach(svcRow => {
      const portEl = svcRow.querySelector('.f-svc-port');
      const nameEl = svcRow.querySelector('.f-svc-name');
      portEl.classList.remove('invalid');
      const portRaw = portEl.value.trim();
      const svcName = nameEl.value.trim();
      if(!portRaw && !svcName) return;
      const portNum = parseInt(portRaw);
      if(isNaN(portNum) || portNum < 1 || portNum > 65535){
        portEl.classList.add('invalid'); hasError = true; return;
      }
      services.push({ port: portNum, name: svcName || ('port ' + portNum) });
    });
    if(services.length) entry.services = services;
    const strictEl = row.querySelector('.f-strict');
    if(strictEl && strictEl.checked) entry.strict = true;

    hosts.push(entry);
  });
  if(hasError){ setStatus('Fix the highlighted fields and try again', 'error'); return; }
  setStatus('Saving...', '');
  try {
    const res = await apiFetch('/api/hosts', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ hosts })
    });
    const data = await res.json();
    if(!res.ok){ setStatus(data.error || 'Save failed', 'error'); return; }
    setStatus('Saved ' + hosts.length + ' host' + (hosts.length===1?'':'s') + '.', 'success');
    setTimeout(() => { closeEditor(); refresh(); }, 700);
  } catch(e) { setStatus('Network error while saving', 'error'); }
}
