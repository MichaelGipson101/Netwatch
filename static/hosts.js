// hosts.js — extracted from core.js (Netwatch 4.0 page split). Code moved verbatim.

function renderHost(h){
  const isIdle = h.status === 'IDLE';
  const isDegraded = h.status === 'DEGRADED';
  const isMaintenance = h.status === 'MAINTENANCE';
  const dotCls = isMaintenance ? 'dot-maintenance' : h.status === 'WAIT' ? 'dot-wt' : isDegraded ? 'dot-degraded' : h.is_up ? 'dot-up' : (isIdle ? 'dot-idle' : 'dot-dn');
  const badgeCls = isMaintenance ? 'badge-maintenance' : h.status === 'WAIT' ? 'badge-wt' : isDegraded ? 'badge-degraded' : h.is_up ? 'badge-up' : (isIdle ? 'badge-idle' : 'badge-dn');
  const nameStyle = 'style="display:flex;align-items:center;gap:5px'
    + (h.is_up || h.status === 'WAIT' || isIdle || isDegraded || isMaintenance ? '' : ';color:var(--red)')
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

// Monitor toolbar one-liner, e.g. "21/29 up · 4.2 ms · 99.6%"
function renderMonitorSummary(data){
  const el = document.getElementById('mon-summary');
  if(!el) return;
  const s = nwComputeSummary(data);
  el.className = 'mon-summary mon-summary-' + (s.down > 0 ? 'down' : (s.degraded > 0 ? 'warn' : 'ok'));
  const text = s.up + '/' + s.total + ' up'
    + (s.avgLat !== null ? ' · ' + s.avgLat.toFixed(1) + ' ms' : '')
    + (s.avgUpt !== null ? ' · ' + s.avgUpt.toFixed(1) + '%' : '');
  // Only touch the node when the text changed: it is aria-live, so a rewrite on every poll
  // would make screen readers re-announce an unchanged summary every few seconds.
  if(el.textContent !== text) el.textContent = text;
}

nwStatus.subscribe(function(data){
  renderMonitorSummary(data);
  renderGroups(data);
  if(_hostStatusChip !== 'all' || (document.getElementById('hosts-filter') && document.getElementById('hosts-filter').value)){
    applyHostFilter();
  }
});

nwOnReady(function(){
  const cm = document.getElementById('compact-mode');
  if(!cm) return;
  const compactSaved = localStorage.getItem('nw-compact') === 'true';
  cm.checked = compactSaved;
  document.body.classList.toggle('compact', compactSaved);
  cm.addEventListener('change', e => {
    document.body.classList.toggle('compact', e.target.checked);
    localStorage.setItem('nw-compact', e.target.checked);
  });
});
