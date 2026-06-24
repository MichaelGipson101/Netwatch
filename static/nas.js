function fetchNas() {
  fetch('/api/nas')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      window.nwLastNas = data;
      renderNas(data);
    })
    .catch(function() {
      var el = document.getElementById('nas-content');
      if (el) el.innerHTML = '<div class="nas-unavailable">Could not reach Netwatch server.</div>';
    });
}

function renderNas(data) {
  var el = document.getElementById('nas-content');
  if (!el) return;

  if (!data.reachable && !(data.pools && data.pools.length)) {
    el.innerHTML = renderNasUnavailable(data);
    return;
  }

  var html = renderNasActionBar(data);
  html += renderNasAlerts(data.alerts);
  if (data.reachable && (!data.pools || !data.pools.length)) {
    html += '<div class="nas-unavailable">No pools found on TrueNAS.</div>';
  }
  (data.pools || []).forEach(function(pool) { html += renderPoolSection(pool); });
  if (data.replication_tasks && data.replication_tasks.length) {
    html += renderReplicationSection(data.replication_tasks);
  }
  el.innerHTML = html;
}

function renderNasUnavailable(data) {
  var msg = data.error === 'NAS not configured'
    ? 'TrueNAS is not configured. Add <code>truenas_url</code> and <code>truenas_api_key</code> to <code>auth.json</code>.'
    : 'TrueNAS is unreachable. Check connection and API key.';
  return '<div class="nas-unavailable">' + msg + '</div>';
}

function renderNasActionBar(data) {
  var ago = data.last_updated ? nasTimeAgo(new Date(data.last_updated)) : 'never';
  var info = data.reachable
    ? '<span class="nas-meta">Last updated ' + ago + ' \xB7 polls every 15 min</span>'
    : '<span class="nas-warn">TrueNAS unreachable \xB7 last data ' + ago + '</span>';
  return '<div class="nas-action-bar"><button class="btn nas-refresh-btn" onclick="fetchNas()">&#8635; Refresh now</button>' + info + '</div>';
}

function renderNasAlerts(alerts) {
  if (!alerts || !alerts.length) return '';
  var rows = alerts.map(function(a) {
    var badgeCls = (a.level === 'WARNING') ? 'nas-badge-warn' : 'nas-badge-err';
    var dismissBtn = (typeof _authState !== 'undefined' && _authState.admin)
      ? '<button class="btn" style="margin-left:auto" data-klass="' + escapeHtml(a.klass) +
        '" onclick="dismissNasAlert(this)">Dismiss</button>'
      : '';
    return '<div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border)">' +
      '<span class="nas-badge ' + badgeCls + '">' + escapeHtml(a.level) + '</span>' +
      '<span style="flex:1;font-size:13px">' + escapeHtml(a.message) + '</span>' +
      dismissBtn +
      '</div>';
  }).join('');
  return '<div class="nas-section-label">TrueNAS Alerts</div>' +
    '<div class="nas-card">' + rows + '</div>';
}

async function dismissNasAlert(btn) {
  var klass = btn.dataset.klass;
  btn.disabled = true;
  try {
    const res = await apiFetch('/api/nas/ignore-alert', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({klass: klass}),
    });
    if (!res.ok) { toast('Could not dismiss alert.', 'error'); btn.disabled = false; return; }
    var row = btn.closest('div[style*="border-bottom"]');
    if (row) row.remove();
    toast('Alert category dismissed.', 'success');
  } catch (e) { toast('Network error', 'error'); btn.disabled = false; }
}

function renderPoolSection(pool) {
  var used = pool.capacity_used_bytes || 0;
  var total = pool.capacity_total_bytes || 0;
  var pct = total ? Math.round(used / total * 100) : 0;
  var statusCls = pool.status === 'ONLINE' ? 'nas-status-ok' : 'nas-status-err';
  var scrub = pool.last_scrub || {};
  var scrubLabel = scrub.status === 'FINISHED' ? (scrub.errors ? 'Errors' : 'Clean') : (scrub.status || '—');
  var scrubCls = scrub.errors ? 'nas-status-err' : 'nas-status-ok';
  var scrubDate = scrub.end_time ? nasFmtDate(scrub.end_time) : '—';
  var nextScrub = pool.next_scrub ? nasFmtDate(pool.next_scrub) : '—';
  var nextDays = pool.next_scrub ? nasDaysAway(pool.next_scrub) : null;
  var nextSub = nextDays !== null ? nextDays + ' day' + (nextDays === 1 ? '' : 's') + ' away' : '';
  var badgeCls = pool.status === 'ONLINE' ? 'nas-badge-ok' : 'nas-badge-err';

  return '<div class="nas-section-label">Pool health</div>' +
    '<div class="nas-metrics">' +
      '<div class="nas-metric"><div class="nas-metric-label">Pool status</div>' +
        '<div class="nas-metric-value ' + statusCls + '">' + escapeHtml(pool.status) + '</div>' +
        '<div class="nas-metric-sub">' + escapeHtml(pool.name) + '</div></div>' +
      '<div class="nas-metric"><div class="nas-metric-label">Capacity used</div>' +
        '<div class="nas-metric-value">' + nasFmtBytes(used) + '</div>' +
        '<div class="nas-metric-sub">of ' + nasFmtBytes(total) + ' (' + pct + '%)</div></div>' +
      '<div class="nas-metric"><div class="nas-metric-label">Last scrub</div>' +
        '<div class="nas-metric-value ' + scrubCls + '">' + escapeHtml(scrubLabel) + '</div>' +
        '<div class="nas-metric-sub">' + scrubDate + ' \xB7 ' + (scrub.errors || 0) + ' error(s)</div></div>' +
      '<div class="nas-metric"><div class="nas-metric-label">Next scrub</div>' +
        '<div class="nas-metric-value">' + nextScrub + '</div>' +
        '<div class="nas-metric-sub">' + nextSub + '</div></div>' +
    '</div>' +
    '<div class="nas-card">' +
      '<div class="nas-card-hdr"><span class="nas-card-title">VDEV layout</span>' +
        '<span class="nas-badge ' + badgeCls + '">' + escapeHtml(pool.status) + '</span></div>' +
      renderVdevs(pool.vdevs || []) +
    '</div>';
}

function renderVdevs(vdevs) {
  if (!vdevs.length) return '<div class="nas-vdev-row"><span class="nas-muted">No VDEV data</span></div>';
  return vdevs.map(function(v) {
    var dotCls = v.status === 'ONLINE' ? 'nas-dot-ok' : 'nas-dot-err';
    var disks = (v.disks || []).map(function(d) {
      var dDot = d.status === 'ONLINE' ? 'nas-dot-ok' : 'nas-dot-err';
      return '<div class="nas-vdev-row nas-vdev-indent">' +
        '<span class="nas-dot ' + dDot + '"></span>' +
        '<span class="nas-vdev-name">' + escapeHtml(d.name) + '</span>' +
        '<span class="nas-muted">' + escapeHtml(d.status) + '</span></div>';
    }).join('');
    return '<div class="nas-vdev-row">' +
      '<span class="nas-dot ' + dotCls + '"></span>' +
      '<span class="nas-vdev-name">' + escapeHtml(v.type.toLowerCase()) + '-' + escapeHtml(v.name) + '</span>' +
      '<span class="nas-muted">' + escapeHtml(v.status) + '</span></div>' + disks;
  }).join('');
}

function renderReplicationSection(tasks) {
  var rows = tasks.map(function(t) {
    var badge = nasRepBadge(t);
    return '<div class="nas-rep-row">' +
      '<span class="nas-rep-name">' + escapeHtml(t.name) + '</span>' +
      '<span class="nas-rep-meta">Last run: ' + (t.last_run ? nasFmtDate(t.last_run) : '—') + '</span>' +
      '<span class="nas-badge ' + badge.cls + '">' + escapeHtml(badge.label) + '</span></div>';
  }).join('');
  return '<div class="nas-section-label" style="margin-top:1.5rem">Replication tasks</div>' +
    '<div class="nas-card">' + rows + '</div>';
}

function nasRepBadge(task) {
  if (!task.last_state && !task.last_run) {
    return { label: 'Never run', cls: 'nas-badge-warn' };
  }
  var okStates = ['SUCCESS', 'FINISHED', 'PENDING', 'RUNNING'];
  if (task.last_state && okStates.indexOf(task.last_state) === -1) {
    return { label: 'Failed', cls: 'nas-badge-err' };
  }
  if (task.last_run) {
    var diffH = (Date.now() - new Date(task.last_run).getTime()) / 3600000;
    if (diffH > 25) return { label: 'Stale (' + Math.floor(diffH) + 'h)', cls: 'nas-badge-warn' };
  }
  return { label: 'Success', cls: 'nas-badge-ok' };
}

function nasFmtBytes(b) {
  if (!b) return '0 B';
  var units = ['B', 'KB', 'MB', 'GB', 'TB'];
  var i = 0;
  while (b >= 1024 && i < units.length - 1) { b /= 1024; i++; }
  return (i > 0 ? b.toFixed(1) : Math.round(b)) + ' ' + units[i];
}

function nasFmtDate(iso) {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  } catch(e) { return iso; }
}

function nasTimeAgo(date) {
  var diffMin = Math.round((Date.now() - date.getTime()) / 60000);
  if (diffMin < 1) return 'just now';
  if (diffMin < 60) return diffMin + ' min ago';
  return Math.round(diffMin / 60) + 'h ago';
}

function nasDaysAway(iso) {
  try {
    var diff = new Date(iso).getTime() - Date.now();
    return Math.max(0, Math.ceil(diff / 86400000));
  } catch(e) { return null; }
}
