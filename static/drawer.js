// drawer.js — extracted from core.js (Netwatch 4.0 page split). Code moved verbatim.

let openDrawerIp = null;

let drawerHistRange = 24;  // hours; persists across drawer opens this session

let _drawerOpener = null;  // element that triggered openDrawer — focus returned on close

const HIST_RANGES = [['1h', 1], ['6h', 6], ['24h', 24], ['7d', 168]];

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

function _maintenanceSectionHtml(h){
  if(!_authState.admin) return '';
  const startHidden = h.status === 'MAINTENANCE' ? ' style="display:none"' : '';
  const activeHidden = h.status === 'MAINTENANCE' ? '' : ' style="display:none"';
  return '<div class="d-section" id="d-maintenance-section"><div class="d-section-hdr"><span>Maintenance</span></div>'
    + '<div class="d-actions" id="d-maintenance-start-row"' + startHidden + '>'
    + '<select id="d-maintenance-duration">'
    + '<option value="1800">30 minutes</option>'
    + '<option value="3600" selected>1 hour</option>'
    + '<option value="14400">4 hours</option>'
    + '</select>'
    + '<input type="text" id="d-maintenance-reason" placeholder="Reason (optional)" maxlength="200">'
    + '<button class="d-action-btn" id="d-maintenance-start-btn" data-ip="' + escapeHtml(h.ip) + '"><span>Start Maintenance</span><span class="arrow">→</span></button>'
    + '</div>'
    + '<div class="d-actions" id="d-maintenance-active-row"' + activeHidden + '>'
    + '<span id="d-maintenance-active-label"></span>'
    + '<button class="d-action-btn" id="d-maintenance-clear-btn" data-ip="' + escapeHtml(h.ip) + '"><span>Clear now</span><span class="arrow">→</span></button>'
    + '</div>'
    + '<div class="d-action-status" id="d-maintenance-status"></div>'
    + '</div>';
}

function _maintenanceLabel(h){
  const until = new Date(h.maintenance_until);
  return h.maintenance_reason
    ? 'In maintenance (' + escapeHtml(h.maintenance_reason) + ') — ends ' + until.toLocaleTimeString()
    : 'In maintenance — ends ' + until.toLocaleTimeString();
}

function renderDrawer(h, data){
  const dotEl = document.getElementById('d-dot');
  const iconColor = h.status === 'WAIT' ? 'var(--amber)' : h.status === 'DEGRADED' ? 'var(--amber)' : h.status === 'MAINTENANCE' ? 'var(--amber)' : h.is_up ? 'var(--green)' : (h.status === 'IDLE' ? 'var(--hint)' : 'var(--red)');
  const iconType = h.device_type || 'host';
  dotEl.className = 'drawer-icon-wrap';
  dotEl.innerHTML = '<svg width="32" height="32" viewBox="0 0 32 32" style="color:' + iconColor + '" aria-hidden="true"><use href="#topo-icon-' + iconType + '"/></svg>';
  document.getElementById('d-name').textContent = h.name;
  const badgeCls = h.status === 'WAIT' ? 'badge-wt' : h.status === 'DEGRADED' ? 'badge-degraded' : h.status === 'MAINTENANCE' ? 'badge-maintenance' : h.is_up ? 'badge-up' : (h.status === 'IDLE' ? 'badge-idle' : 'badge-dn');
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
  const statusColor = h.status === 'WAIT' || h.status === 'DEGRADED' || h.status === 'MAINTENANCE' ? 'var(--amber-text)'
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
    drawerBody.innerHTML = statsHtml + linksHtml + '<div id="d-inv-section"></div>' + svcHtml + piHtml + sparkHtml + histHtml + specsHtml + notesHtml + incHtml + actionsHtml + _maintenanceSectionHtml(h);
    fetchHostInventoryLink(h);
    loadDrawerHistory(h.ip);
    const wakeBtn = document.getElementById('d-wake-btn');
    if(wakeBtn) wakeBtn.addEventListener('click', () => sendWake(wakeBtn.dataset.ip));
    const maintStartBtn = document.getElementById('d-maintenance-start-btn');
    if(maintStartBtn) maintStartBtn.addEventListener('click', () => startMaintenanceFromDrawer(maintStartBtn.dataset.ip));
    const maintClearBtn = document.getElementById('d-maintenance-clear-btn');
    if(maintClearBtn) maintClearBtn.addEventListener('click', () => clearMaintenanceFromDrawer(maintClearBtn.dataset.ip));
    if(h.status === 'MAINTENANCE'){
      const lbl = document.getElementById('d-maintenance-active-label');
      if(lbl) lbl.textContent = _maintenanceLabel(h);
    }
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
  const statusColor = h.status === 'WAIT' || h.status === 'DEGRADED' || h.status === 'MAINTENANCE' ? 'var(--amber-text)'
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
    const badgeCls = h.status === 'MAINTENANCE' ? 'badge-maintenance' : h.status === 'WAIT' ? 'badge-wt' : h.status === 'DEGRADED' ? 'badge-degraded' : h.is_up ? 'badge-up' : (h.status === 'IDLE' ? 'badge-idle' : 'badge-dn');
    meta.innerHTML =
      '<span>' + escapeHtml(h.ip) + '</span><span>·</span><span>' + escapeHtml(h.group) + '</span>'
      + '<span class="badge ' + badgeCls + '">' + h.status + '</span>';
  }
  const dotEl = document.getElementById('d-dot');
  if(dotEl){
    const iconColor = h.status === 'WAIT' ? 'var(--amber)' : h.status === 'DEGRADED' ? 'var(--amber)' : h.status === 'MAINTENANCE' ? 'var(--amber)' : h.is_up ? 'var(--green)' : (h.status === 'IDLE' ? 'var(--hint)' : 'var(--red)');
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

  // The maintenance section is only ever built into the drawer HTML for admins
  // (see _maintenanceSectionHtml), but this in-place path runs on every poll of
  // an already-open drawer regardless of auth changes since it was rendered -
  // e.g. logging out without closing the drawer first. Re-check admin state
  // here too, or a stale admin-only section (and its still-bound buttons)
  // would keep showing to a viewer who is no longer an admin.
  const maintSection = document.getElementById('d-maintenance-section');
  if(maintSection){
    if(!_authState.admin){
      maintSection.style.display = 'none';
    } else {
      maintSection.style.display = '';
      const startRow = document.getElementById('d-maintenance-start-row');
      const activeRow = document.getElementById('d-maintenance-active-row');
      if(startRow && activeRow){
        if(h.status === 'MAINTENANCE'){
          startRow.style.display = 'none';
          activeRow.style.display = '';
          const lbl = document.getElementById('d-maintenance-active-label');
          if(lbl) lbl.textContent = _maintenanceLabel(h);
        } else {
          startRow.style.display = '';
          activeRow.style.display = 'none';
        }
      }
    }
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

async function startMaintenanceFromDrawer(ip){
  const duration = parseInt(document.getElementById('d-maintenance-duration').value, 10);
  const reason = document.getElementById('d-maintenance-reason').value.trim();
  const btn = document.getElementById('d-maintenance-start-btn');
  const status = document.getElementById('d-maintenance-status');
  btn.disabled = true;
  status.className = 'd-action-status';
  status.textContent = 'Starting maintenance...';
  try {
    const res = await apiFetch('/api/maintenance/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ip, duration_seconds: duration, reason })
    });
    const data = await res.json();
    if(!res.ok){
      status.className = 'd-action-status error';
      status.textContent = data.error || 'Failed to start maintenance';
    } else {
      status.className = 'd-action-status success';
      status.textContent = 'Maintenance started.';
    }
  } catch (e) {
    status.className = 'd-action-status error';
    status.textContent = 'Network error';
  } finally {
    btn.disabled = false;
  }
}

async function clearMaintenanceFromDrawer(ip){
  const btn = document.getElementById('d-maintenance-clear-btn');
  const status = document.getElementById('d-maintenance-status');
  btn.disabled = true;
  status.className = 'd-action-status';
  status.textContent = 'Clearing maintenance...';
  try {
    const res = await apiFetch('/api/maintenance/clear', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ip })
    });
    const data = await res.json();
    if(!res.ok){
      status.className = 'd-action-status error';
      status.textContent = data.error || 'Failed to clear maintenance';
    } else {
      status.className = 'd-action-status success';
      status.textContent = 'Maintenance cleared.';
    }
  } catch (e) {
    status.className = 'd-action-status error';
    status.textContent = 'Network error';
  } finally {
    btn.disabled = false;
  }
}

nwStatus.subscribe(function(data){
  if(openDrawerIp){
    const h = data.hosts.find(x => x.ip === openDrawerIp);
    if(h) renderDrawer(h, data);
  }
});

// ?host=<ip> deep link (from Home's host icons and cross-page "open this host" hand-offs).
// The value is only ever compared against monitored IPs, never rendered as HTML.
function nwHostParam(search){
  const v = new URLSearchParams(search || '').get('host');
  return v ? v : null;
}
nwOnReady(function(){
  const ip = nwHostParam(location.search);
  if(!ip) return;
  nwStatus.subscribeOnce(function(data){
    if(data.hosts.some(h => h.ip === ip)) openDrawer(ip);   // unknown IPs: silently ignored
  });
});
