// topology-cards.js — extracted from core.js (Netwatch 4.0 page split). Code moved verbatim.

function renderTopologyNode(h){
  const isIdle = h.status === 'IDLE';
  const isDegraded = h.status === 'DEGRADED';
  const isMaintenance = h.status === 'MAINTENANCE';
  const cls = isMaintenance ? 'maintenance' : h.status === 'WAIT' ? 'wait' : isDegraded ? 'degraded' : h.is_up ? 'up' : (isIdle ? 'idle' : 'down');
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

nwStatus.subscribe(renderTopology);

// Topology web-view canvas overlay metrics (#ov-*), formerly part of renderSummary
function renderTopoOverlay(data){
  const s = nwComputeSummary(data);
  const health = s.down > 0 ? 'var(--red)' : (s.degraded > 0 ? 'var(--amber)' : 'var(--green)');
  const ovUp = document.getElementById('ov-up');
  const ovTot = document.getElementById('ov-tot');
  if(ovUp){ ovUp.textContent = s.up; ovUp.style.color = health; }
  if(ovTot) ovTot.textContent = s.total;
  const ovLat = document.getElementById('ov-lat');
  if(ovLat) ovLat.innerHTML = (s.avgLat !== null ? s.avgLat.toFixed(1) : '-') + '<span class="topo-overlay-unit">ms</span>';
  const ovUpt = document.getElementById('ov-upt');
  if(ovUpt){
    ovUpt.innerHTML = (s.avgUpt !== null ? s.avgUpt.toFixed(1) : '-') + '<span class="topo-overlay-unit">%</span>';
    ovUpt.style.color = s.avgUpt !== null && s.avgUpt >= 95 ? 'var(--green)' : 'var(--amber)';
  }
}
nwStatus.subscribe(renderTopoOverlay);
