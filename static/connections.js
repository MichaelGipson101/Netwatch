// Connections workspace (Netwatch 4.0 plan 3): discovery status, quick add,
// suggestions inbox, switch port map and the table of every connection.
//
// Two fan-outs keep the page coherent - keep them complete:
// - connectionsChanged(): call after EVERY connection/suggestion/inventory
//   mutation, wherever it happens (workspace, drawer, quick add). It reloads
//   this workspace (once opened) and the open inventory drawer.
// - cxRender(): repaints every workspace panel. A new panel's renderCx*()
//   must be added here (tests/test_workspace.py pins the list).
// Panels render from _cxState (including in-progress drafts), never from
// the DOM, so a background refresh can't wipe what the user is typing.

const CX_SOURCE_LABELS = {manual: 'Manual', unifi: 'UniFi', proxmox: 'Proxmox',
                          inferred: 'Wifi inference', migration: 'Migration'};

// Explicit singular forms for counts whose plural doesn't just take an 's'
// (or whose naive 's'-strip would mangle it, e.g. "nodes" -> "nod"); anything
// else falls back to stripping one trailing 's'.
const CX_SINGULAR = {switches: 'switch', clients: 'client', nodes: 'node',
                     guests: 'guest', devices: 'device'};

let _cxState = {
  mounted: false, quickMounted: false, seq: 0, refreshing: false,
  status: null, suggestions: null, connections: null, inventory: [], categories: [], portMaps: [],
  error: null, openChip: null, scanPolling: false, lastPending: null,
  filter: 'all', query: '', highlightConn: null,
  editingConn: null, editDraft: null, editPorts: undefined, editOrigPort: null, pendingEdit: null,
  drafts: {}, busy: {},
};

// ── Pure helpers (unit-tested in node; keep brackets balanced in literals) ──

function cxCountsText(counts){
  if(!counts) return '';
  return Object.keys(counts).sort().map(k => {
    const n = counts[k];
    if(n === 1) return n + ' ' + (CX_SINGULAR[k] || k.replace(/s$/, ''));
    return n + ' ' + k;
  }).join(' · ');
}

function cxSourceChips(status, nowSec){
  const out = [];
  const sources = (status && status.sources) || {};
  Object.keys(sources).sort().forEach(name => {
    const s = sources[name] || {};
    if(!s.configured) return;
    let state = 'pending';
    let detail = 'Waiting for the first scan';
    if(s.ok === true){ state = 'ok'; detail = cxCountsText(s.counts) || 'Scan OK'; }
    else if(s.ok === false){ state = 'warn'; detail = s.error || 'Scan failed'; }
    out.push({name: name, label: CX_SOURCE_LABELS[name] || name, state: state,
              when: s.at ? lastSeenStr(Math.max(0, nowSec - s.at)) : null, detail: detail});
  });
  if(status && status.apply_error){
    out.push({name: 'apply', label: 'Last scan', state: 'warn', when: null,
              detail: 'The last scan could not be applied (' + status.apply_error + ')'});
  }
  return out;
}

// ── Data + fan-outs ─────────────────────────────────────────────────────────

async function cxGetJson(url){
  try {
    const res = await fetch(url);
    const body = await res.json().catch(() => null);
    if(res.status === 503 && body && body.error === 'migration_pending') return {migrationPending: true};
    return res.ok ? body : null;
  } catch(e){ return null; }
}

async function cxPost(url, body){
  try {
    const res = await apiFetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                     body: JSON.stringify(body || {})});
    const out = await res.json().catch(() => ({}));
    return {ok: res.ok, status: res.status, body: out, error: out.error || ('HTTP ' + res.status)};
  } catch(e){
    return {ok: false, status: 0, body: {}, error: 'network error'};
  }
}

async function cxRefreshAll(){
  if(_cxState.refreshing) return;   // one in-flight refresh at a time
  _cxState.refreshing = true;
  try {
    const seq = ++_cxState.seq;
    const [status, suggestions, connections, inventory] = await Promise.all([
      cxGetJson('/api/discovery/status'), cxGetJson('/api/suggestions'),
      cxGetJson('/api/connections'), qaLoadInventory()]);
    if(seq !== _cxState.seq) return;
    const pending = [suggestions, connections].some(x => x && x.migrationPending);
    if(pending){
      // Migration message takes precedence over a plain load failure.
      _cxState.error = 'The connections upgrade hasn\'t finished on the server yet, so this page is read-only for now.';
    } else if(status === null){
      // Distinct from "still loading": the fetch actually failed (401/500/network).
      _cxState.error = 'Couldn\'t load connection data. It will retry on the next refresh.';
    } else {
      _cxState.error = null;
    }
    _cxState.status = status && !status.migrationPending ? status : null;
    _cxState.suggestions = suggestions && !suggestions.migrationPending ? suggestions : null;
    _cxState.connections = connections && !connections.migrationPending ? connections : null;
    _cxState.inventory = inventory || [];
    _cxState.categories = Array.from(new Set(_cxState.inventory.map(i => i.category).filter(Boolean))).sort();
    const maps = (_cxState.status && _cxState.status.port_maps) || [];
    const ports = await Promise.all(maps.map(m => cxGetJson('/api/ports/' + m.device_id)));
    if(seq !== _cxState.seq) return;
    _cxState.portMaps = maps.map((m, i) => ({device_id: m.device_id, name: m.name, data: ports[i]}));
    cxRender();
  } finally {
    _cxState.refreshing = false;
  }
}

function cxRender(){
  renderCxStatus();
}

function mountConnectionsTab(){
  _cxState.mounted = true;
  if(!_cxState.quickMounted){
    renderQuickAdd(document.getElementById('cx-quick'), {onAdded: () => connectionsChanged()});
    _cxState.quickMounted = true;
  }
  cxRender();       // paint what we already have...
  cxRefreshAll();   // ...then fetch fresh data
}

function connectionsChanged(){
  if(_cxState.mounted) cxRefreshAll();
  if(typeof openDrawerIp !== 'undefined' && openDrawerIp && String(openDrawerIp).indexOf('inv:') === 0
     && typeof loadInventoryConnections === 'function'){
    loadInventoryConnections(parseInt(String(openDrawerIp).split(':')[1], 10));
  }
}

// Called from refresh() in core.js with /api/status's suggestions_pending.
function updateConnectionsBadge(n){
  n = n || 0;
  const el = document.getElementById('conn-count');
  if(el){ el.style.display = n > 0 ? '' : 'none'; el.textContent = n; }
  const changed = _cxState.lastPending !== null && n !== _cxState.lastPending;
  _cxState.lastPending = n;
  // Refresh when a scan landed, or (retry path) the last load never succeeded -
  // this 5s poll is what recovers the workspace from a failed first fetch
  // without the user having to leave and re-enter the tab. cxRefreshAll's own
  // refreshing guard keeps this from stacking overlapping fetches.
  if(_cxState.mounted && (changed || _cxState.status === null)) cxRefreshAll();
}

// ── Status strip (spec §5.1) ────────────────────────────────────────────────

function renderCxStatus(){
  const el = document.getElementById('cx-status');
  if(!el) return;
  const admin = typeof _authState !== 'undefined' && !!_authState.admin;
  if(_cxState.error){
    el.innerHTML = '<div class="cx-status-line cx-warn-text">' + escapeHtml(_cxState.error) + '</div>';
    return;
  }
  const st = _cxState.status;
  if(!st){
    el.innerHTML = '<div class="cx-status-line cx-muted">Loading discovery status…</div>';
    return;
  }
  const chips = cxSourceChips(st, Math.floor(Date.now() / 1000));
  if(!chips.length){
    el.innerHTML = '<div class="cx-status-line cx-muted">No discovery source is set up yet. '
      + (admin
        ? '<button type="button" class="btn btn-ghost cx-link-btn" onclick="cxOpenIntegrations()">Connect UniFi in Settings → Integrations</button>'
        : 'An admin can connect UniFi in Settings → Integrations.')
      + '</div>';
    return;
  }
  const scanning = !!st.scanning || _cxState.scanPolling;
  const open = chips.find(c => c.name === _cxState.openChip);
  el.innerHTML = '<div class="cx-status-line">'
    + chips.map(c =>
        '<button type="button" class="cx-chip cx-chip-' + c.state + '" data-chip="' + escapeHtml(c.name) + '"'
        + ' aria-expanded="' + (open && open.name === c.name ? 'true' : 'false') + '" title="' + escapeHtml(c.detail) + '"'
        + ' onclick="cxToggleChip(this.dataset.chip)">'
        + '<span class="cx-chip-icon" aria-hidden="true">' + (c.state === 'ok' ? '✓' : c.state === 'warn' ? '⚠' : '…') + '</span>'
        + '<span>' + escapeHtml(c.label) + '</span>'
        + (c.when ? '<span class="cx-chip-when">' + escapeHtml(c.when) + '</span>' : '')
        + '</button>').join('')
    + (admin
      ? '<button type="button" class="btn cx-scan-btn" onclick="cxScanNow()"' + (scanning ? ' disabled' : '') + '>'
        + (scanning ? 'Scanning…' : 'Scan now') + '</button>'
      : '')
    + '</div>'
    + (open
      ? '<div class="cx-chip-detail ' + (open.state === 'warn' ? 'cx-warn-text' : 'cx-muted') + '">'
        + escapeHtml(open.label) + ': ' + escapeHtml(open.detail) + '</div>'
      : '');
}

function cxToggleChip(name){
  _cxState.openChip = _cxState.openChip === name ? null : name;
  renderCxStatus();
}

async function cxOpenIntegrations(){
  await openSettings();
  switchSettingsTab('integrations');
}

async function cxScanNow(){
  if(_cxState.scanPolling) return;
  // Freshly-fetched, not the possibly-stale cached _cxState.status: if a scan
  // already landed since our last render, "before" must reflect that or the
  // poll below can see last_scan > before immediately and stop too early.
  const freshStatus = await cxGetJson('/api/discovery/status');
  const before = (freshStatus && freshStatus.last_scan) || (_cxState.status && _cxState.status.last_scan) || 0;
  _cxState.scanPolling = true;
  renderCxStatus();
  try {
    const out = await cxPost('/api/discovery/scan', {});
    if(!out.ok){ toast('Could not start a scan: ' + out.error, 'error'); return; }
    for(let i = 0; i < 40; i++){   // up to about two minutes
      await new Promise(r => setTimeout(r, 3000));
      const st = await cxGetJson('/api/discovery/status');
      if(st && !st.migrationPending){ _cxState.status = st; renderCxStatus(); }
      if(st && !st.scanning && (st.last_scan || 0) > before) break;
    }
  } finally {
    _cxState.scanPolling = false;
    await cxRefreshAll();
  }
}
