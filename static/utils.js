function deviceIcon(type, size){
  return '<svg class="dev-icon" width="' + size + '" height="' + size + '" viewBox="0 0 32 32"'
    + ' style="vertical-align:middle;flex-shrink:0" aria-hidden="true">'
    + '<use href="#topo-icon-' + (type || 'host') + '"/></svg>';
}

function escapeHtml(s){
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function fmtLatency(ms){
  if(ms === null) return '<span style="color:var(--hint)">- ms</span>';
  const c = ms < 50 ? 'var(--green-text)' : ms < 150 ? 'var(--amber-text)' : 'var(--red-text)';
  return '<span style="color:' + c + '">' + ms.toFixed(1) + ' ms</span>';
}

function uptimeColor(pct){
  if(pct === null) return 'var(--hint)';
  if(pct >= 95) return 'var(--green)';
  if(pct >= 80) return 'var(--amber)';
  return 'var(--red)';
}

function durationStr(seconds){
  if(seconds < 60) return seconds + 's';
  if(seconds < 3600) return Math.floor(seconds/60) + 'm ' + (seconds % 60) + 's';
  const h = Math.floor(seconds/3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h + 'h ' + m + 'm';
}

function lastSeenStr(seconds){
  if(seconds === null || seconds === undefined) return 'never';
  if(seconds < 60) return seconds + 's ago';
  if(seconds < 3600) return Math.floor(seconds/60) + 'm ago';
  if(seconds < 86400) return Math.floor(seconds/3600) + 'h ago';
  return Math.floor(seconds/86400) + 'd ago';
}

function sortHosts(hosts){
  return hosts.slice().sort((a,b) => {
    const aDown = !a.is_up && a.status === 'DOWN';
    const bDown = !b.is_up && b.status === 'DOWN';
    if(aDown !== bDown) return aDown ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
}

function _bytesHuman(b){
  if(b === undefined || b === null) return '-';
  if(b < 1024) return b + ' B';
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = b / 1024;
  let i = 0;
  while(v >= 1024 && i < units.length - 1){ v /= 1024; i++; }
  return v.toFixed(v >= 100 ? 0 : 1) + ' ' + units[i];
}

function _uptimeHuman(s){
  if(s === undefined || s === null) return '-';
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if(d > 0) return d + 'd ' + h + 'h';
  if(h > 0) return h + 'h ' + m + 'm';
  return m + 'm';
}

function _tempColor(t){
  if(t === undefined || t === null) return 'var(--hint)';
  if(t < 60) return 'var(--green)';
  if(t < 75) return 'var(--amber)';
  return 'var(--red)';
}

function _pctColor(p){
  if(p === undefined || p === null) return 'var(--hint)';
  if(p < 70) return 'var(--green)';
  if(p < 90) return 'var(--amber)';
  return 'var(--red)';
}

function _pctClass(p){
  if(p === undefined || p === null) return '';
  if(p < 70) return 'green';
  if(p < 90) return 'amber';
  return 'red';
}

function _loadColor(load, cores){
  if(load === undefined || load === null) return 'var(--hint)';
  const ratio = cores ? (load / cores) : load;
  if(ratio < 0.7) return 'var(--green)';
  if(ratio < 1.2) return 'var(--amber)';
  return 'var(--red)';
}

function _loadClass(load, cores){
  if(load === undefined || load === null) return '';
  const ratio = cores ? (load / cores) : load;
  if(ratio < 0.7) return 'green';
  if(ratio < 1.2) return 'amber';
  return 'red';
}

function ipValid(ip){
  return /^(\d{1,3}\.){3}\d{1,3}$/.test(ip) && ip.split('.').every(n => parseInt(n) >= 0 && parseInt(n) <= 255);
}

function macValid(m){
  if(!m) return true;
  return /^([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}$/.test(m.trim()) || /^[0-9a-fA-F]{12}$/.test(m.trim());
}

function setStatus(msg, kind){
  const el = document.getElementById('save-status');
  el.textContent = msg;
  el.className = 'save-status ' + (kind || '');
}

function toast(msg, kind){
  let wrap = document.getElementById('nw-toasts');
  if(!wrap){
    wrap = document.createElement('div');
    wrap.id = 'nw-toasts';
    wrap.setAttribute('aria-live', 'polite');
    document.body.appendChild(wrap);
  }
  const t = document.createElement('div');
  t.className = 'nw-toast ' + (kind || 'info');
  t.textContent = msg;
  const tid = setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 250); }, 4000);
  t.onclick = () => { clearTimeout(tid); t.remove(); };
  wrap.appendChild(t);
}
