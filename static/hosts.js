// hosts.js — extracted from core.js (Netwatch 4.0 page split). Code moved verbatim.

const LAT_SPARK_SAMPLES = 8;

let _latHistory = [];

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

function renderSummary(data){
  const up = data.hosts.filter(h => h.is_up).length;
  const total = data.hosts.length;
  const down = data.hosts.filter(h => !h.is_up && h.status === 'DOWN').length;
  const degraded = data.hosts.filter(h => h.status === 'DEGRADED').length;
  const maintenance = data.hosts.filter(h => h.status === 'MAINTENANCE').length;
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
  if(avgLat !== null){
    _latHistory.push(avgLat);
    if(_latHistory.length > LAT_SPARK_SAMPLES) _latHistory.shift();
  }
  const latSpark = document.getElementById('s-lat-spark');
  if(latSpark) latSpark.setAttribute('points', nwSparkPoints(_latHistory, 100, 22));
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

nwStatus.subscribe(function(data){
  renderSummary(data);
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
