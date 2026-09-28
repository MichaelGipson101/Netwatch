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
  mounted: false, quickMounted: false, seq: 0, refreshing: false, refreshQueued: false,
  status: null, suggestions: null, connections: null, inventory: [], categories: [], portMaps: [],
  error: null, migrationPending: false, openChip: null, scanPolling: false, lastPending: null, lastLoggedIn: null,
  filter: 'all', query: '', highlightConn: null, highlightSugg: null, highlightAfterSeq: 0, suggSeq: 0,
  editingConn: null, editDraft: null, editPorts: undefined, editOrig: null, pendingEdit: null, editFocus: null,
  swappedIds: {}, drafts: {}, busy: {}, unmonitored: null, monitorBusy: false,
  tableOpen: null,   // null = not read from localStorage yet
};

// ── "All connections" is collapsed until opened; the choice is remembered.
// Actions that need a row (Fix…, highlights, an uplink tile) open it for
// this session without changing the remembered choice.
const CX_TABLE_OPEN_KEY = 'nw-cx-table-open';

function cxTableIsOpen(){
  if(_cxState.tableOpen === null){
    let v = null;
    try { v = localStorage.getItem(CX_TABLE_OPEN_KEY); } catch(e){}
    _cxState.tableOpen = v === '1';
  }
  return _cxState.tableOpen;
}

function cxApplyTableOpen(){
  const open = cxTableIsOpen();
  const el = document.getElementById('cx-table');
  const btn = document.getElementById('cx-table-toggle');
  if(el) el.hidden = !open;
  if(btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function cxSetTableOpen(open, remember){
  _cxState.tableOpen = !!open;
  if(remember){
    try { localStorage.setItem(CX_TABLE_OPEN_KEY, open ? '1' : '0'); } catch(e){}
  }
  cxApplyTableOpen();
  if(open) renderCxTable();
}

function cxToggleTable(){
  cxSetTableOpen(!cxTableIsOpen(), true);
}

// The quick add control mounted in this workspace (not the drawer/port-map
// ones) - locked while migration-pending so its Add button can't be used on
// a read-only backend. Set once in mountConnectionsTab.
let _cxQuickAdd = null;

// Excludes migration-pending on purpose: that state already has its own
// honest "read-only for now" message and shouldn't trigger a refresh loop
// every 5s poll just because connections/suggestions are (expectedly) null.
function cxNeedsRetry(){
  return !_cxState.migrationPending
    && (_cxState.status === null || _cxState.connections === null || _cxState.suggestions === null);
}

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
  // One in-flight refresh at a time - but a call that arrives mid-flight must
  // coalesce (run again right after), not be dropped: every mutation's
  // connectionsChanged() has to land, or the workspace shows stale data.
  if(_cxState.refreshing){ _cxState.refreshQueued = true; return; }
  _cxState.refreshing = true;
  try {
    const seq = ++_cxState.seq;
    const [status, suggestions, connections, inventory, unmonitored] = await Promise.all([
      cxGetJson('/api/discovery/status'), cxGetJson('/api/suggestions'),
      cxGetJson('/api/connections'), qaLoadInventory(),
      cxIsAdmin() ? cxGetJson('/api/discovery/unmonitored-guests') : Promise.resolve(null)]);
    if(seq !== _cxState.seq) return;
    const pending = [suggestions, connections].some(x => x && x.migrationPending);
    _cxState.migrationPending = pending;
    if(pending){
      // Migration message takes precedence over a plain load failure.
      _cxState.error = 'The connections upgrade hasn\'t finished on the server yet, so this page is read-only for now.';
    } else if(status === null || connections === null || suggestions === null){
      // Distinct from "still loading": one of the fetches actually failed
      // (401/500/network) - status alone succeeding isn't enough to call
      // the load a success.
      _cxState.error = 'Couldn\'t load connection data. It will retry on the next refresh.';
    } else {
      _cxState.error = null;
    }
    if(typeof _cxQuickAdd !== 'undefined' && _cxQuickAdd) _cxQuickAdd.setLocked(pending);
    _cxState.status = status && !status.migrationPending ? status : null;
    _cxState.suggestions = suggestions && !suggestions.migrationPending ? suggestions : null;
    _cxState.connections = connections && !connections.migrationPending ? connections : null;
    _cxState.inventory = inventory || [];
    _cxState.unmonitored = unmonitored && Array.isArray(unmonitored.guests) ? unmonitored : null;
    _cxState.categories = Array.from(new Set(_cxState.inventory.map(i => i.category).filter(Boolean))).sort();
    const maps = (_cxState.status && _cxState.status.port_maps) || [];
    const ports = await Promise.all(maps.map(m => cxGetJson('/api/ports/' + m.device_id)));
    if(seq !== _cxState.seq) return;
    _cxState.portMaps = maps.map((m, i) => ({device_id: m.device_id, name: m.name, data: ports[i]}));
    // Marks that suggestions have actually been re-fetched AND rendered as
    // of this seq - cxFlashSuggestion waits for this to pass the click's
    // seq before resolving, so it never judges a stale cached render.
    _cxState.suggSeq = seq;
    cxRender();
    if(_cxState.pendingEdit !== null){
      // Consume it before replaying: if the connection still isn't there
      // (e.g. it was deleted elsewhere), the replay must not re-queue itself
      // forever - cxStartEdit(id) only re-sets pendingEdit for a fresh,
      // non-replay call.
      const pending = _cxState.pendingEdit;
      _cxState.pendingEdit = null;
      cxStartEdit(pending, {fromReplay: true});
    }
  } finally {
    _cxState.refreshing = false;
    if(_cxState.refreshQueued){
      _cxState.refreshQueued = false;
      cxRefreshAll();
    }
  }
}

function cxRender(){
  renderCxStatus();
  renderCxSuggestions();
  renderCxPortMaps();
  renderCxTable();
}

function mountConnectionsTab(){
  _cxState.mounted = true;
  // Seed from the current auth state so the first updateAuthUI() call after
  // mount isn't misread as a login change (mountConnectionsTab already does
  // its own initial cxRefreshAll() below).
  if(typeof _authState !== 'undefined') _cxState.lastLoggedIn = _authState.logged_in;
  const quickBox = document.getElementById('cx-quick');
  if(!_cxState.quickMounted && quickBox){
    _cxQuickAdd = renderQuickAdd(quickBox, {onAdded: () => connectionsChanged()});
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

// Called from refresh() in shell.js with /api/status's suggestions_pending.
function updateConnectionsBadge(n){
  n = n || 0;
  const el = document.getElementById('conn-count');
  if(el){ el.style.display = n > 0 ? '' : 'none'; el.textContent = n; }
  const changed = _cxState.lastPending !== null && n !== _cxState.lastPending;
  _cxState.lastPending = n;
  // Refresh when a scan landed, or (retry path) the last load never fully
  // succeeded - this 5s poll is what recovers the workspace from a failed
  // first fetch without the user having to leave and re-enter the tab.
  // cxRefreshAll's own refreshing guard keeps this from stacking overlapping
  // fetches. cxNeedsRetry() excludes migration-pending on purpose.
  if(_cxState.mounted && (changed || cxNeedsRetry())) cxRefreshAll();
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

// ── All connections table (spec §5.5) ───────────────────────────────────────

const CX_FILTERS = [['all', 'All'], ['drift', 'Drift ⚠'], ['manual', 'Manual'], ['discovered', 'Discovered']];

function cxDriftIssues(suggestions){
  const out = {};
  ((suggestions && suggestions.items) || []).forEach(s => {
    const p = s.payload || {};
    if(s.kind !== 'drift' || p.connection_id === null || p.connection_id === undefined) return;
    out[p.connection_id] = (out[p.connection_id] || []).concat(p.issues || []);
  });
  return out;
}

function cxPortSortKey(port){
  const s = String(port || '');
  const m = s.match(/(\d+)$/);
  return [s.replace(/\d+$/, '').toLowerCase(), m ? parseInt(m[1], 10) : -1];
}

function cxCompareConnections(a, b){
  const pa = String(a.parent_name || '').toLowerCase();
  const pb = String(b.parent_name || '').toLowerCase();
  if(pa !== pb) return pa < pb ? -1 : 1;
  const ka = cxPortSortKey(a.parent_port);
  const kb = cxPortSortKey(b.parent_port);
  if(ka[0] !== kb[0]) return ka[0] < kb[0] ? -1 : 1;
  if(ka[1] !== kb[1]) return ka[1] - kb[1];
  const ca = String(a.child_name || '').toLowerCase();
  const cb = String(b.child_name || '').toLowerCase();
  return ca < cb ? -1 : ca > cb ? 1 : 0;
}

function cxFilterConnections(conns, filter, query, driftIds){
  const drift = new Set(driftIds || []);
  const q = String(query || '').trim().toLowerCase();
  return (conns || []).filter(c => {
    if(filter === 'drift' && !drift.has(c.id)) return false;
    if(filter === 'manual' && c.source !== 'manual') return false;
    if(filter === 'discovered' && c.source === 'manual') return false;
    if(!q) return true;
    return [c.child_name, c.parent_name, c.parent_port, c.child_port, c.connection_type, c.source, c.notes]
      .some(v => v && String(v).toLowerCase().indexOf(q) !== -1);
  }).sort(cxCompareConnections);
}

function cxFilterCounts(conns, driftIds){
  const drift = new Set(driftIds || []);
  const out = {all: 0, drift: 0, manual: 0, discovered: 0};
  (conns || []).forEach(c => {
    out.all++;
    if(drift.has(c.id)) out.drift++;
    if(c.source === 'manual') out.manual++;
    else out.discovered++;
  });
  return out;
}

function cxLastSeenText(ts, nowSec){
  return ts ? lastSeenStr(Math.max(0, nowSec - ts)) : '—';
}

function cxFindConnection(id){
  return ((_cxState.connections && _cxState.connections.items) || []).find(c => c.id === id) || null;
}

// Device buttons in the connections table. The inventory drawer lives on the Lab page (inventory.js);
// a page that loads connections.js without it (Home) sends the user to the Lab inventory instead.
function cxOpenDevice(id){
  if(typeof openInventoryDrawer === 'function' && document.getElementById('drawer')) openInventoryDrawer(id);
  else location.href = '/lab/inventory';
}

function cxRowHtml(c, now, isDrift){
  const src = c.source || 'manual';
  return '<tr data-conn="' + c.id + '"' + (isDrift ? ' class="cx-row-drift"' : '') + '>'
    + '<td><div class="cx-pair">'
      + '<button type="button" class="cx-dev" onclick="cxOpenDevice(' + c.child_id + ')">'
        + deviceIcon(c.child_type || 'host', 16) + '<span>' + escapeHtml(c.child_name) + '</span></button>'
      + '<span class="cx-arrow" aria-hidden="true">→</span>'
      + '<button type="button" class="cx-dev" onclick="cxOpenDevice(' + c.parent_id + ')">'
        + deviceIcon(c.parent_type || 'host', 16) + '<span>' + escapeHtml(c.parent_name) + '</span></button>'
      + (isDrift ? '<span class="cx-drift-flag" title="A suggestion disagrees with this connection">⚠</span>' : '')
    + '</div></td>'
    + '<td class="pve-td-mono">' + escapeHtml(c.parent_port || '—')
      + (c.child_port ? '<span class="cx-child-port"> via ' + escapeHtml(c.child_port) + '</span>' : '') + '</td>'
    + '<td>' + escapeHtml(c.connection_type || '') + '</td>'
    + '<td><span class="cx-src cx-src-' + escapeHtml(src) + '">' + escapeHtml(CX_SOURCE_LABELS[src] || src) + '</span></td>'
    + '<td class="cx-muted">' + escapeHtml(cxLastSeenText(c.last_seen, now)) + '</td>'
    + '<td class="pve-td-actions">'
      + '<button type="button" class="btn btn-ghost cx-row-btn" onclick="cxStartEdit(' + c.id + ')">Edit</button>'
      + '<button type="button" class="conn-del" aria-label="Remove connection" title="Remove connection" onclick="cxDeleteConnection(' + c.id + ')">×</button>'
    + '</td></tr>';
}

function cxEditRowHtml(c, issues){
  const d = _cxState.editDraft || {};
  const wifi = d.connection_type === 'wifi';
  let portCtl;
  if(_cxState.editPorts === undefined){
    portCtl = '<span class="cx-muted">Loading ports…</span>';
  } else {
    const opts = qaPortOptions(_cxState.editPorts);
    if(opts){
      const known = opts.some(p => p.value === d.parent_port);
      portCtl = '<select class="cx-edit-field" data-field="parent_port"' + (wifi ? ' disabled' : '') + '>'
        + '<option value="">— none —</option>'
        + (d.parent_port && !known
          ? '<option value="' + escapeHtml(d.parent_port) + '" selected>' + escapeHtml(d.parent_port) + ' (not a port on this device)</option>'
          : '')
        + opts.map(p => '<option value="' + escapeHtml(p.value) + '"' + (p.value === d.parent_port ? ' selected' : '') + '>'
          + escapeHtml(p.label) + '</option>').join('')
        + '</select>';
    } else {
      portCtl = '<input type="text" class="cx-edit-field" data-field="parent_port" value="' + escapeHtml(d.parent_port || '') + '"'
        + (wifi ? ' disabled' : '') + ' autocomplete="off" spellcheck="false">';
    }
  }
  const ambiguous = (issues || []).indexOf('ambiguous_direction') !== -1;
  // Once the user has explicitly swapped a connection this session, keep
  // offering the control even after the swap settles the drift (the
  // suggestion that flagged 'ambiguous_direction' disappears on refresh) -
  // otherwise a wrong swap can't be undone without reloading the page.
  const showSwap = ambiguous || !!(_cxState.swappedIds && _cxState.swappedIds[c.id]);
  // A wifi draft with a stale stored port (migration drift, or an edge
  // that's simply been wifi for a while with old data) can't clear it via
  // the disabled port control - cxSaveEdit sends parent_port: null instead.
  // Tell the user that's what Save will do.
  const staleWifiPort = wifi && _cxState.editOrig && _cxState.editOrig.parent_port;
  return '<tr class="cx-edit-row" data-conn="' + c.id + '"><td colspan="6"><div class="cx-edit">'
    + '<div class="cx-edit-title">' + escapeHtml(c.child_name) + ' → ' + escapeHtml(c.parent_name) + '</div>'
    + '<label class="qa-field"><span>Port on ' + escapeHtml(c.parent_name) + '</span>' + portCtl + '</label>'
    + '<label class="qa-field"><span>Via (' + escapeHtml(c.child_name) + '\'s port)</span>'
      + '<input type="text" class="cx-edit-field" data-field="child_port" value="' + escapeHtml(d.child_port || '') + '"'
      + ' autocomplete="off" spellcheck="false" placeholder="optional"></label>'
    + '<label class="qa-field"><span>Type</span><select class="cx-edit-field" data-field="connection_type">'
      + QA_CONNECTION_TYPES.map(t => '<option value="' + t + '"' + (t === d.connection_type ? ' selected' : '') + '>' + t + '</option>').join('')
    + '</select></label>'
    + '<label class="qa-field"><span>Notes</span><input type="text" class="cx-edit-field" data-field="notes" value="'
      + escapeHtml(d.notes || '') + '"></label>'
    + (c.source && c.source !== 'manual'
      ? '<p class="cx-muted cx-edit-hint">Saving makes this connection manual: discovery will report differences instead of updating it.</p>'
      : '')
    + (ambiguous
      ? '<p class="cx-muted cx-edit-hint">Netwatch couldn\'t tell which end is upstream. If it\'s the wrong way round, swap it.</p>'
      : '')
    + (staleWifiPort
      ? '<p class="cx-muted cx-edit-hint">Wifi links don\'t use a port — saving clears \'' + escapeHtml(_cxState.editOrig.parent_port) + '\'.</p>'
      : '')
    + '<div class="cx-edit-actions">'
      + (showSwap ? '<button type="button" class="btn" onclick="cxSwapConnection(' + c.id + ')">⇅ Swap direction</button>' : '')
      + '<button type="button" class="btn btn-ghost" onclick="cxCancelEdit()">Cancel</button>'
      + '<button type="button" class="btn btn-primary" onclick="cxSaveEdit(' + c.id + ')">Save</button>'
    + '</div></div></td></tr>';
}

function renderCxTable(){
  const el = document.getElementById('cx-table');
  if(!el) return;
  cxApplyTableOpen();
  const countEl = document.getElementById('cx-table-count');
  const all = (_cxState.connections && _cxState.connections.items) || [];
  if(countEl) countEl.textContent = _cxState.connections ? String(all.length) : '';
  if(!el.querySelector('.cx-table-tools')){
    el.innerHTML = '<div class="cx-table-tools">'
      + '<div class="cx-filter-chips" role="group" aria-label="Filter connections"></div>'
      + '<input type="search" class="inv-search cx-search" placeholder="Search connections…" aria-label="Search connections">'
      + '</div>'
      + '<div class="pve-table-scroll"><table class="cx-table"><thead><tr>'
      + '<th>Child → Parent</th><th>Port</th><th>Type</th><th>Source</th><th>Last seen</th>'
      + '<th><span class="cx-sr-only">Actions</span></th>'
      + '</tr></thead><tbody></tbody></table></div>'
      + '<div class="cx-table-empty cx-muted" hidden></div>';
    const search = el.querySelector('.cx-search');
    search.addEventListener('input', () => {
      if(cxEditDraftDirty() && !confirm('Discard your unsaved changes to the open connection edit?')){
        search.value = _cxState.query;   // revert; keep the edit open
        return;
      }
      _cxState.query = search.value;
      _cxState.editingConn = null;
      cxRenderTableRows({force: true});
    });
    // Edit-row inputs write straight into the draft, so re-renders keep them.
    const sync = e => {
      const f = e.target.closest('.cx-edit-field');
      if(!f || !_cxState.editDraft) return;
      _cxState.editDraft[f.dataset.field] = f.value;
      if(f.dataset.field === 'connection_type' && e.type === 'change') cxRenderTableRows({force: true});
    };
    el.addEventListener('input', sync);
    el.addEventListener('change', sync);
  }
  cxRenderTableRows();
}

function cxRenderTableRows(opts){
  const el = document.getElementById('cx-table');
  if(!el || !el.querySelector('tbody')) return;
  const conns = (_cxState.connections && _cxState.connections.items) || [];
  const issues = cxDriftIssues(_cxState.suggestions);
  const driftIds = Object.keys(issues).map(Number);
  const counts = cxFilterCounts(conns, driftIds);
  el.querySelector('.cx-filter-chips').innerHTML = CX_FILTERS.map(f =>
    '<button type="button" class="inv-chip' + (_cxState.filter === f[0] ? ' active' : '') + '" data-filter="' + f[0] + '"'
    + ' aria-pressed="' + (_cxState.filter === f[0] ? 'true' : 'false') + '" onclick="cxSetFilter(this.dataset.filter)">'
    + escapeHtml(f[1]) + '<span class="inv-chip-count">' + counts[f[0]] + '</span></button>').join('');
  const search = el.querySelector('.cx-search');
  if(search.value !== _cxState.query) search.value = _cxState.query;
  // Don't rebuild the rows under the user's cursor mid-typing; the draft
  // keeps the values either way, this just keeps focus. Skipped entirely for
  // a forced (user-initiated) render: clicking Cancel/Save/Swap focuses the
  // button that triggered it, which lives inside .cx-edit-row, so an
  // unconditional guard here would swallow the very render that action needs.
  const force = !!(opts && opts.force);
  const active = document.activeElement;
  if(!force && active && active.closest && active.closest('.cx-edit-row') && el.contains(active)) return;
  const rows = cxFilterConnections(conns, _cxState.filter, _cxState.query, driftIds);
  const now = Math.floor(Date.now() / 1000);
  el.querySelector('tbody').innerHTML = rows.map(c => _cxState.editingConn === c.id
    ? cxEditRowHtml(c, issues[c.id] || [])
    : cxRowHtml(c, now, Object.prototype.hasOwnProperty.call(issues, c.id))).join('');
  const empty = el.querySelector('.cx-table-empty');
  empty.hidden = rows.length > 0;
  empty.textContent = _cxState.error
    ? _cxState.error
    : (conns.length
      ? 'No connections match this filter.'
      : 'No connections recorded yet. Add one above, or accept suggestions as discovery finds them.');
  if(_cxState.highlightConn !== null){
    const row = el.querySelector('tr[data-conn="' + _cxState.highlightConn + '"]');
    if(row){
      row.classList.add('cx-row-flash');
      row.scrollIntoView({block: 'center', behavior: 'smooth'});
      setTimeout(() => row.classList.remove('cx-row-flash'), 2000);
    }
    _cxState.highlightConn = null;
  }
}

// True when an edit row is open and its draft has diverged from the
// snapshot cxStartEdit took (_cxState.editOrig) - i.e. discarding it now
// would lose real, unsaved changes.
function cxEditDraftDirty(){
  const d = _cxState.editDraft, o = _cxState.editOrig;
  if(_cxState.editingConn === null || !d || !o) return false;
  return d.parent_port !== o.parent_port || (d.child_port || '') !== (o.child_port || '')
    || d.connection_type !== o.connection_type || d.notes !== o.notes;
}

function cxSetFilter(f){
  if(cxEditDraftDirty() && !confirm('Discard your unsaved changes to the open connection edit?')) return;
  _cxState.filter = f;
  _cxState.editingConn = null;
  cxRenderTableRows({force: true});
}

async function cxStartEdit(id, opts){
  const c = cxFindConnection(id);
  if(!c){
    // Not loaded yet (e.g. right after cxHighlightConnection triggers a
    // refresh): try again once fresh data lands. A replay call that still
    // can't find it must not re-queue itself forever.
    if(!(opts && opts.fromReplay)) _cxState.pendingEdit = id;
    return;
  }
  _cxState.pendingEdit = null;
  _cxState.editingConn = id;
  _cxState.editDraft = {parent_port: c.parent_port || '', child_port: c.child_port || '',
                        connection_type: c.connection_type || 'ethernet', notes: c.notes || ''};
  _cxState.editOrig = Object.assign({}, _cxState.editDraft);
  _cxState.editPorts = undefined;
  cxRenderTableRows({force: true});
  const body = await cxGetJson('/api/ports/' + c.parent_id);
  if(_cxState.editingConn !== id) return;
  _cxState.editPorts = body && !body.migrationPending ? body.ports : null;
  const match = qaMatchPortOption(qaPortOptions(_cxState.editPorts), _cxState.editDraft.parent_port);
  if(match){   // "8" stored, "Port 8" live: same port, not a change
    _cxState.editDraft.parent_port = match;
    _cxState.editOrig.parent_port = match;
  }
  cxRenderTableRows({force: true});
  if(_cxState.editFocus){
    const f = document.querySelector('.cx-edit-row [data-field="' + _cxState.editFocus + '"]');
    _cxState.editFocus = null;
    if(f){ f.focus(); if(f.select) f.select(); }
  }
}

function cxCancelEdit(){
  _cxState.editingConn = null;
  _cxState.editDraft = null;
  _cxState.editOrig = null;
  _cxState.pendingEdit = null;
  cxRenderTableRows({force: true});
}

async function cxSaveEdit(id){
  const c = cxFindConnection(id);
  const d = _cxState.editDraft;
  const orig = _cxState.editOrig;
  if(!c || !d || !orig) return;
  const body = {};
  if(d.connection_type !== orig.connection_type) body.connection_type = d.connection_type;
  if(d.connection_type !== 'wifi' && (d.parent_port || '') !== (orig.parent_port || '')){
    body.parent_port = d.parent_port;
  } else if(d.connection_type === 'wifi' && orig.connection_type === 'wifi' && orig.parent_port){
    // Already wifi on both ends of this edit, with a stale stored port (the
    // port control stays disabled so the draft can't differ here) - the
    // server only clears parent_port on a connection_type change, so send
    // it explicitly. A fresh switch to wifi doesn't need this: the server's
    // update_connection already clears the port when connection_type lands
    // in the same request.
    body.parent_port = null;
  }
  if((d.child_port || '') !== (orig.child_port || '')) body.child_port = d.child_port;
  if((d.notes || '') !== (orig.notes || '')) body.notes = d.notes;
  if(!Object.keys(body).length){ cxCancelEdit(); return; }
  const out = await cxPost('/api/connections/' + id, body);
  if(!out.ok){ toast('Could not save: ' + out.error, 'error'); return; }
  const w = out.body.warnings || [];
  if(w.indexOf('port_in_use') !== -1) toast('Saved. Heads up: another connection uses that port.', 'info');
  else if(w.indexOf('parent_port_cleared') !== -1) toast('Saved. The port was cleared: wifi links don\'t use one.', 'info');
  else toast('Connection saved', 'success');
  _cxState.editingConn = null;
  _cxState.editDraft = null;
  _cxState.editOrig = null;
  cxRenderTableRows({force: true});   // close the row now; connectionsChanged()'s refresh is unforced
  connectionsChanged();
}

async function cxSwapConnection(id){
  if(cxEditDraftDirty() && !confirm('Discard your unsaved changes to this connection and swap direction?')) return;
  const out = await cxPost('/api/connections/' + id, {swap: true});
  if(!out.ok){ toast('Could not swap: ' + out.error, 'error'); return; }
  toast('Direction swapped', 'success');
  // Remember it locally: the drift that made the ⇅ control appear settles
  // once the swap lands, but a wrong swap still needs to be undoable.
  _cxState.swappedIds = _cxState.swappedIds || {};
  _cxState.swappedIds[id] = true;
  _cxState.editingConn = null;
  _cxState.editDraft = null;
  _cxState.editOrig = null;
  cxRenderTableRows({force: true});   // close the row now; connectionsChanged()'s refresh is unforced
  connectionsChanged();
}

async function cxDeleteConnection(id){
  const c = cxFindConnection(id);
  if(!c || !confirm('Remove ' + c.child_name + ' → ' + c.parent_name + '?')) return;
  const out = await cxPost('/api/connections/' + id + '/delete', {});
  if(!out.ok){ toast('Could not remove: ' + out.error, 'error'); return; }
  toast('Connection removed', 'success');
  connectionsChanged();
}

function cxHighlightConnection(id, opts){
  const view = document.getElementById('view-connections');
  if(view && !view.classList.contains('active')) setTab('connections');
  _cxState.filter = 'all';
  _cxState.query = '';
  _cxState.highlightConn = id;
  _cxState.editFocus = (opts && opts.focus) || null;
  cxSetTableOpen(true, false);
  if(opts && opts.edit) cxStartEdit(id);
  else cxRenderTableRows({force: true});
}

function cxHighlightSuggestion(id){
  const view = document.getElementById('view-connections');
  const wasActive = !!(view && view.classList.contains('active'));
  // Capture BEFORE triggering any refresh below: cxRefreshAll bumps
  // _cxState.seq synchronously, so this must be the value from strictly
  // before that call for the "fresh enough" check in cxFlashSuggestion to
  // ever pass.
  _cxState.highlightAfterSeq = _cxState.seq;
  _cxState.highlightSugg = Number(id);
  if(!wasActive) setTab('connections');   // mountConnectionsTab() already refreshes
  else cxRefreshAll();                    // already on the tab: setTab wouldn't be called, so refresh explicitly
  cxFlashSuggestion();
}

// Called now and after every inbox render, so it also works when the tab
// is still loading. Says so when the suggestion is already gone - but only
// once suggestions have actually been re-fetched AND rendered since the
// click (_cxState.suggSeq passing the seq captured at click time): setTab
// paints the CACHED inbox immediately and a refresh lands ~100ms later, and
// judging the stale cached DOM can raise a false "already handled".
function cxFlashSuggestion(){
  const id = _cxState.highlightSugg;
  if(id === null || id === undefined) return;
  if(_cxState.suggSeq <= _cxState.highlightAfterSeq) return;   // no fresh render yet - stay pending
  const el = document.querySelector('.cx-sugg[data-sid="' + id + '"]');
  if(!el){
    // _cxState.suggestions is {items: [...]} once loaded (see
    // renderCxSuggestions), not an array; null/undefined means "still
    // loading" - don't declare it gone until we've actually seen the list.
    if(_cxState.suggestions && Array.isArray(_cxState.suggestions.items)){
      _cxState.highlightSugg = null;
      toast('That suggestion was already handled');
    }
    return;
  }
  _cxState.highlightSugg = null;
  const group = el.closest('details');
  if(group) group.open = true;
  el.scrollIntoView({block: 'center', behavior: 'smooth'});
  el.classList.add('cx-sugg-flash');
  setTimeout(() => el.classList.remove('cx-sugg-flash'), 2200);
}

// ── Suggestions inbox (spec §5.3) ───────────────────────────────────────────

const CX_KIND_ORDER = ['device', 'edge', 'drift', 'identity', 'shared_port'];
const CX_KIND_LABELS = {device: 'New devices', edge: 'New connections', drift: 'Drift',
                        identity: 'Identity', shared_port: 'Shared ports'};
// Drift and identity are always decided one at a time (spec §5.3).
const CX_BULK_KINDS = ['device', 'edge', 'shared_port'];

function cxGroupSuggestions(items){
  const groups = [];
  CX_KIND_ORDER.forEach(kind => {
    const mine = (items || []).filter(s => s.kind === kind);
    if(!mine.length) return;
    const bySource = {};
    mine.forEach(s => { (bySource[s.source] = bySource[s.source] || []).push(s); });
    groups.push({
      kind: kind, label: CX_KIND_LABELS[kind], count: mine.length,
      bulk: CX_BULK_KINDS.indexOf(kind) !== -1,
      sources: Object.keys(bySource).sort().map(src => ({
        source: src, label: CX_SOURCE_LABELS[src] || src, items: bySource[src]})),
    });
  });
  return groups;
}

function cxSuggestionActions(s){
  const p = s.payload || {};
  switch(s.kind){
    case 'device':
    case 'edge':
      return [['accept', 'Accept', true], ['dismiss', 'Dismiss', false]];
    case 'shared_port':
      return [['accept', 'Create placeholder', true], ['dismiss', 'Dismiss', false]];
    case 'identity':
      return [['accept', p.candidate_id !== null && p.candidate_id !== undefined ? 'Yes' : 'That\'s the one', true],
              ['dismiss', 'No', false]];
    case 'drift':
      if(p.action === 'replace') return [['accept', 'Replace', true], ['dismiss', 'Keep mine', false]];
      if(p.action === 'remove') return [['accept', 'Remove', true], ['dismiss', 'Keep it', false]];
      return [['fix', 'Fix…', true], ['dismiss', 'Mark reviewed', false]];
    default:
      return [['dismiss', 'Dismiss', false]];
  }
}

function cxSuggestionText(s){
  const p = s.payload || {};
  if(s.source === 'migration'){
    return (p.child_name || '?') + ' → ' + (p.parent_name || '?') + ': ' + (p.message || 'needs review');
  }
  return p.message || (s.kind + ' suggestion');
}

function cxDoneMessage(s, action){
  if(action === 'dismiss') return s.kind === 'drift' ? 'Kept your version' : 'Dismissed';
  if(s.kind === 'drift') return (s.payload || {}).action === 'remove' ? 'Connection removed' : 'Connection updated';
  const msgs = {device: 'Device added', edge: 'Connection added', identity: 'Device identified',
                shared_port: 'Placeholder switch created'};
  return msgs[s.kind] || 'Done';
}

// Accept-all can't carry per-item edits, so hand-edited device suggestions
// are left out (the user is told) rather than accepted without their edits.
function cxBulkItems(items, kind, source, drafts){
  const send = [];
  let skipped = 0;
  (items || []).forEach(s => {
    if(s.kind !== kind || s.source !== source) return;
    const d = (drafts || {})[s.id] || {};
    const edited = ['system', 'device_type', 'category', 'device_id'].some(k => d[k] !== undefined);
    if(edited){ skipped++; return; }
    send.push({id: s.id, fingerprint: s.fingerprint});
  });
  return {send: send, skipped: skipped};
}

function cxFindSuggestion(id){
  return ((_cxState.suggestions && _cxState.suggestions.items) || []).find(s => s.id === id) || null;
}

// ── Guest monitoring: accepted Proxmox guests also go into hosts.yaml ──────
// Only admins can write hosts.yaml, so only admins see these controls.

function cxIsAdmin(){
  return typeof _authState !== 'undefined' && !!(_authState && _authState.admin);
}

function cxGuestDevice(s){
  const dev = (s && s.kind === 'device' && (s.payload || {}).device) || null;
  const props = (dev && dev.properties) || {};
  return dev && props.proxmox_vmid !== undefined && props.proxmox_vmid !== null ? dev : null;
}

// Ticked by default; the user can untick it per card.
function cxWantsMonitor(s, drafts, isAdmin){
  const dev = cxGuestDevice(s);
  if(!isAdmin || !dev || !dev.ip) return false;
  return ((drafts || {})[s.id] || {}).monitor !== false;
}

function cxMonitorHtml(s){
  const dev = cxGuestDevice(s);
  if(!dev || !cxIsAdmin()) return '';
  if(!dev.ip){
    return '<p class="cx-muted cx-sugg-monitor-hint">No IP yet, so it can\'t be monitored: start the guest or install its guest agent.</p>';
  }
  const quiet = (dev.properties || {}).autostart === false;
  return '<label class="cx-sugg-monitor"><input type="checkbox" data-draft="monitor"'
    + (cxWantsMonitor(s, _cxState.drafts, true) ? ' checked' : '') + '>'
    + '<span>Monitor it at <b>' + escapeHtml(dev.ip) + '</b>'
    + (quiet ? ' <span class="cx-muted">(no alerts: autostart is off)</span>' : '') + '</span></label>';
}

function cxMonitorToast(r){
  if(!r || r.monitored === undefined) return '';
  if(r.monitored) return ' and started monitoring it';
  const why = {no_ip: 'it has no IP yet', already_monitored: 'it\'s already monitored',
               admin_required: 'only admins can add monitored hosts'}[r.monitor_skipped];
  return ' (not monitored: ' + (why || 'hosts.yaml couldn\'t be updated') + ')';
}

// Backfill: accepted guests from before this feature, once. "Not now" is
// remembered for this exact set of guests - a new one brings the banner back.
const CX_GUEST_SNOOZE_KEY = 'nw-guest-monitor-snooze';

function cxGuestSnoozeKey(guests){
  return (guests || []).map(g => g.id).sort((a, b) => a - b).join(',');
}

function cxGuestBannerHtml(){
  const u = _cxState.unmonitored;
  if(!cxIsAdmin() || !u || !u.guests.length) return '';
  let snoozed = null;
  try { snoozed = localStorage.getItem(CX_GUEST_SNOOZE_KEY); } catch(e){}
  if(snoozed === cxGuestSnoozeKey(u.guests)) return '';
  const n = u.guests.length;
  const quiet = u.guests.filter(g => !g.alert).length;
  const names = u.guests.slice(0, 4).map(g => g.name).join(', ') + (n > 4 ? ' and ' + (n - 4) + ' more' : '');
  return '<div class="cx-guest-banner" role="status">'
    + '<p><b>' + n + ' Proxmox guest' + (n === 1 ? '' : 's') + ' in inventory ' + (n === 1 ? 'isn\'t' : 'aren\'t') + ' monitored</b>: '
    + escapeHtml(names) + '.'
    + (quiet ? ' <span class="cx-muted">' + quiet + ' without autostart will be added without alerts.</span>' : '')
    + (u.no_ip ? ' <span class="cx-muted">' + u.no_ip + ' more have no IP yet.</span>' : '') + '</p>'
    + '<div class="cx-guest-banner-actions">'
    + '<button type="button" class="btn btn-ghost" onclick="cxSnoozeGuestBanner()">Not now</button>'
    + '<button type="button" class="btn btn-primary"' + (_cxState.monitorBusy ? ' disabled' : '')
    + ' onclick="cxMonitorAllGuests()">Monitor ' + (n === 1 ? 'it' : 'them') + '</button>'
    + '</div></div>';
}

function cxSnoozeGuestBanner(){
  try { localStorage.setItem(CX_GUEST_SNOOZE_KEY, cxGuestSnoozeKey((_cxState.unmonitored || {}).guests)); } catch(e){}
  renderCxSuggestions();
}

async function cxMonitorAllGuests(){
  const guests = ((_cxState.unmonitored || {}).guests) || [];
  if(!guests.length || _cxState.monitorBusy) return;
  _cxState.monitorBusy = true;
  renderCxSuggestions();
  const out = await cxPost('/api/discovery/monitor-guests', {ids: guests.map(g => g.id)});
  _cxState.monitorBusy = false;
  if(!out.ok){ toast('Could not add them: ' + out.error, 'error'); renderCxSuggestions(); return; }
  const added = (out.body.added || []).length;
  toast(added ? 'Now monitoring ' + added + ' guest' + (added === 1 ? '' : 's') : 'Nothing new to monitor',
        added ? 'success' : 'info');
  connectionsChanged();
}

function cxDeviceEditorHtml(s){
  const dev = (s.payload || {}).device || {};
  const d = _cxState.drafts[s.id] || {};
  const val = k => (d[k] !== undefined ? d[k] : (dev[k] || ''));
  const types = typeof INV_TYPE_ORDER !== 'undefined' ? INV_TYPE_ORDER : ['host', 'vm', 'network'];
  const curType = val('device_type') || 'host';
  return '<details class="cx-sugg-edit"' + (d.open ? ' open' : '') + ' data-sid="' + s.id + '">'
    + '<summary>Edit before adding</summary><div class="cx-sugg-edit-grid">'
    + '<label class="qa-field"><span>Name</span><input type="text" data-draft="system" value="' + escapeHtml(val('system')) + '"></label>'
    + '<label class="qa-field"><span>Type</span><select data-draft="device_type">'
      + types.map(t => '<option value="' + t + '"' + (t === curType ? ' selected' : '') + '>' + t + '</option>').join('')
    + '</select></label>'
    + '<label class="qa-field"><span>Category</span><input type="text" data-draft="category" list="cx-cat-list" value="'
      + escapeHtml(val('category')) + '"></label>'
    + '</div></details>';
}

function cxIdentityPickerHtml(s){
  const d = _cxState.drafts[s.id] || {};
  return '<label class="qa-field cx-sugg-pick"><span>Which device is it?</span><select data-draft="device_id">'
    + '<option value="">Choose a device…</option>'
    + (_cxState.inventory || []).map(i => '<option value="' + i.id + '"' + (String(i.id) === String(d.device_id || '') ? ' selected' : '') + '>'
      + escapeHtml(i.system) + ' (' + escapeHtml(i.device_type || 'host') + ')</option>').join('')
    + '</select></label>';
}

function cxSuggestionHtml(s){
  const busy = !!_cxState.busy[s.id];
  const p = s.payload || {};
  let extra = '';
  if(s.kind === 'device') extra = cxMonitorHtml(s) + cxDeviceEditorHtml(s);
  if(s.kind === 'identity' && (p.candidate_id === null || p.candidate_id === undefined)) extra = cxIdentityPickerHtml(s);
  return '<div class="cx-sugg" data-sid="' + s.id + '">'
    + '<p class="cx-sugg-text">' + escapeHtml(cxSuggestionText(s)) + '</p>'
    + extra
    + '<div class="cx-sugg-actions">'
    + cxSuggestionActions(s).map(a =>
        '<button type="button" class="btn' + (a[2] ? ' btn-primary' : ' btn-ghost') + '"' + (busy ? ' disabled' : '')
        + ' onclick="cxSuggestionAction(' + s.id + ', \'' + a[0] + '\')">' + escapeHtml(a[1]) + '</button>').join('')
    + '</div></div>';
}

function cxInitSuggestionEvents(el){
  if(el.dataset.wired) return;
  el.dataset.wired = '1';
  const sync = e => {
    const f = e.target.closest('[data-draft]');
    const item = e.target.closest('.cx-sugg');
    if(!f || !item) return;
    const id = Number(item.dataset.sid);
    _cxState.drafts[id] = Object.assign({}, _cxState.drafts[id],
      {[f.dataset.draft]: f.type === 'checkbox' ? f.checked : f.value});
  };
  el.addEventListener('input', sync);
  el.addEventListener('change', sync);
  // <details> toggle doesn't bubble; listen in the capture phase.
  el.addEventListener('toggle', e => {
    const det = e.target;
    if(!det.classList || !det.classList.contains('cx-sugg-edit')) return;
    const id = Number(det.dataset.sid);
    _cxState.drafts[id] = Object.assign({}, _cxState.drafts[id], {open: det.open});
  }, true);
}

function renderCxSuggestions(){
  const el = document.getElementById('cx-suggestions');
  const countEl = document.getElementById('cx-sugg-count');
  if(!el) return;
  cxInitSuggestionEvents(el);
  const data = _cxState.suggestions;
  if(!data){
    el.innerHTML = _cxState.error
      ? '<div class="cx-empty cx-warn-text">' + escapeHtml(_cxState.error) + '</div>'
      : '<div class="cx-muted">Loading…</div>';
    if(countEl) countEl.textContent = '';
    return;
  }
  const items = data.items || [];
  if(countEl) countEl.textContent = items.length ? items.length + ' pending' : '';
  // Keep focus if the user is typing in a suggestion's editor.
  const active = document.activeElement;
  if(active && el.contains(active) && active.matches('input:not([type=checkbox]), select')) return;
  if(!items.length){
    el.innerHTML = cxGuestBannerHtml()
      + '<div class="cx-empty">Nothing to review. New suggestions appear here after each discovery scan.</div>';
    cxFlashSuggestion();
    return;
  }
  el.innerHTML = cxGuestBannerHtml() + cxGroupSuggestions(items).map(g =>
    '<div class="cx-sgroup">'
    + '<div class="cx-sgroup-hdr"><h3>' + escapeHtml(g.label) + '</h3><span class="cx-hdr-count">' + g.count + '</span></div>'
    + g.sources.map(sg =>
        '<div class="cx-ssource">'
        + '<div class="cx-ssource-hdr"><span>' + escapeHtml(sg.label) + ' · ' + sg.items.length + '</span>'
        + (g.bulk && sg.items.length > 1
          ? '<button type="button" class="btn btn-ghost cx-accept-all" data-kind="' + g.kind + '" data-source="' + escapeHtml(sg.source) + '"'
            + ' onclick="cxAcceptAll(this.dataset.kind, this.dataset.source)">Accept all</button>'
          : '')
        + '</div>'
        + sg.items.map(cxSuggestionHtml).join('')
        + '</div>').join('')
    + '</div>').join('')
    + '<datalist id="cx-cat-list">'
    + (_cxState.categories || []).map(c => '<option value="' + escapeHtml(c) + '">').join('')
    + '</datalist>';
  cxFlashSuggestion();
}

async function cxSuggestionAction(id, action){
  const s = cxFindSuggestion(id);
  if(!s) return;
  const p = s.payload || {};
  if(action === 'fix'){ cxHighlightConnection(p.connection_id, {edit: true}); return; }
  const body = {fingerprint: s.fingerprint};
  if(action === 'accept'){
    const d = _cxState.drafts[id] || {};
    if(s.kind === 'device'){
      const o = {};
      ['system', 'device_type', 'category'].forEach(k => { if(d[k] !== undefined) o[k] = String(d[k]).trim(); });
      if(o.system === ''){ toast('Give the device a name first', 'error'); return; }
      body.overrides = o;
      if(cxWantsMonitor(s, _cxState.drafts, cxIsAdmin())) body.monitor = true;
    } else if(s.kind === 'identity' && (p.candidate_id === null || p.candidate_id === undefined)){
      if(!d.device_id){ toast('Choose which device this is first', 'error'); return; }
      body.overrides = {device_id: parseInt(d.device_id, 10)};
    } else if(s.kind === 'drift'){
      body.action = p.action;
    }
  }
  _cxState.busy[id] = true;
  renderCxSuggestions();
  const out = await cxPost('/api/suggestions/' + id + '/' + (action === 'dismiss' ? 'dismiss' : 'accept'), body);
  delete _cxState.busy[id];
  if(out.status === 409){ toast('This suggestion changed — refreshed', 'info'); connectionsChanged(); return; }
  if(!out.ok){
    toast((action === 'dismiss' ? 'Could not dismiss: ' : 'Could not apply: ') + out.error, 'error');
    renderCxSuggestions();
    return;
  }
  delete _cxState.drafts[id];
  const skipped = out.body && out.body.monitored === false;
  toast(cxDoneMessage(s, action) + (action === 'accept' ? cxMonitorToast(out.body) : ''),
        skipped ? 'info' : 'success');
  if(action === 'accept' && ['device', 'shared_port', 'identity'].indexOf(s.kind) !== -1){
    qaInvalidateInventory();
    if(typeof fetchInventory === 'function') fetchInventory();
  }
  connectionsChanged();
}

async function cxAcceptAll(kind, source){
  const items = (_cxState.suggestions && _cxState.suggestions.items) || [];
  const plan = cxBulkItems(items, kind, source, _cxState.drafts);
  if(!plan.send.length){
    toast('Nothing to accept in bulk: edited suggestions need accepting one at a time.', 'info');
    return;
  }
  const what = (CX_KIND_LABELS[kind] || kind).toLowerCase() + ' from ' + (CX_SOURCE_LABELS[source] || source);
  const skippedNote = plan.skipped ? ' (' + plan.skipped + ' you edited will be left for you to accept one by one)' : '';
  const admin = cxIsAdmin();
  plan.send.forEach(it => { if(cxWantsMonitor(cxFindSuggestion(it.id), _cxState.drafts, admin)) it.monitor = true; });
  const monN = plan.send.filter(it => it.monitor).length;
  const monNote = monN ? ', and start monitoring ' + monN + ' of them' : '';
  if(!confirm('Accept ' + plan.send.length + ' ' + what + monNote + '?' + skippedNote)) return;
  plan.send.forEach(it => { _cxState.busy[it.id] = true; });
  renderCxSuggestions();
  const out = await cxPost('/api/suggestions/accept-all', {items: plan.send});
  plan.send.forEach(it => { delete _cxState.busy[it.id]; });
  if(!out.ok){ toast('Could not accept: ' + out.error, 'error'); renderCxSuggestions(); return; }
  const results = out.body.results || [];
  const okN = results.filter(r => r.ok).length;
  const monitored = results.filter(r => r.monitored).length;
  const monText = monitored ? ' · monitoring ' + monitored : '';
  toast(okN === results.length
    ? 'Accepted ' + okN + monText
    : 'Accepted ' + okN + ' of ' + results.length + '. The rest changed since you looked; they\'re refreshed below.',
    okN === results.length ? 'success' : 'info');
  qaInvalidateInventory();
  if(typeof fetchInventory === 'function') fetchInventory();
  connectionsChanged();
}

// ── Switch port map (spec §5.4) ─────────────────────────────────────────────

function cxFmtSpeed(mbps){
  if(!mbps) return '';
  return mbps >= 1000 ? (mbps / 1000) + ' Gbps' : mbps + ' Mbps';
}

function cxShortPortName(name){
  const s = String(name || '');
  const m = s.match(/^Port (\d+)$/);
  if(m) return m[1];
  return s.replace(/^SFP\+ (\d+)$/, 'SFP+$1');
}

function cxPortTile(p){
  const occ = p.occupants || [];
  const link = p.up === true ? 'up' : p.up === false ? 'down' : 'unknown';
  const state = occ.length ? 'occupied' : (p.up === true ? 'up' : 'down');
  // A device's own uplink occupies its port too (netwatch/storage.py
  // ports_for_device); mark it visually so it doesn't look like a stray
  // downlink to the same name.
  const occLabel = o => (o.uplink ? '↑ ' : '') + o.name;
  let label = '';
  if(occ.length === 1) label = occLabel(occ[0]);
  else if(occ.length > 1) label = '+' + occ.length;
  else if(p.up === true) label = '?';
  const bits = [p.name];
  if(p.up === true) bits.push('link up' + (p.speed_mbps ? ' · ' + cxFmtSpeed(p.speed_mbps) : ''));
  else if(p.up === false) bits.push('link down');
  if(p.poe) bits.push('PoE');
  if(occ.length) bits.push(occ.map(occLabel).join(', '));
  return {name: p.name, state: state, link: link, label: label, title: bits.join(' · '),
          conn_id: occ.length ? occ[0].connection_id : null,
          uplink: occ.length === 1 && !!occ[0].uplink};
}

function cxTileHtml(deviceId, p){
  const t = cxPortTile(p);
  const inner = '<span class="cx-tile-port"><i class="cx-led ' + t.link + '"></i>' + escapeHtml(cxShortPortName(t.name)) + '</span>'
    + '<span class="cx-tile-dev">' + escapeHtml(t.label) + '</span>';
  if(t.state === 'occupied' && t.uplink){
    // This device's own uplink: the port on THIS device is the edge's child
    // port, so open the edit row on that field rather than the parent's port.
    const tip = t.title + ' · this device\'s uplink: tap to edit';
    return '<button type="button" class="cx-tile cx-tile-occupied" title="' + escapeHtml(tip) + '"'
      + ' aria-label="' + escapeHtml(tip) + '" onclick="cxHighlightConnection(' + t.conn_id + ', {edit: true, focus: \'child_port\'})">' + inner + '</button>';
  }
  if(t.state === 'occupied'){
    return '<button type="button" class="cx-tile cx-tile-occupied" title="' + escapeHtml(t.title) + '"'
      + ' aria-label="' + escapeHtml(t.title) + '" onclick="cxHighlightConnection(' + t.conn_id + ')">' + inner + '</button>';
  }
  if(t.state === 'up'){
    const tip = t.title + ' · nothing recorded here: tap to add it';
    return '<button type="button" class="cx-tile cx-tile-up" title="' + escapeHtml(tip) + '" aria-label="' + escapeHtml(tip) + '"'
      + ' data-port="' + escapeHtml(t.name) + '" onclick="cxQuickAddAt(' + deviceId + ', this.dataset.port)">' + inner + '</button>';
  }
  return '<div class="cx-tile cx-tile-down" title="' + escapeHtml(t.title) + '">' + inner + '</div>';
}

// Switches that have a live port list: [{device_id, name, data}] -> drawable.
function cxLivePortMaps(maps){
  return (maps || []).filter(m => m && m.data && m.data.live && m.data.ports);
}

// One renderer for the Connections panel and its Overview copy: tile clicks
// switch to Connections themselves, so the copy needs nothing extra.
function cxPortFacesHtml(maps){
  return cxLivePortMaps(maps).map(m =>
    '<div class="cx-face">'
    + '<div class="cx-face-hdr">' + deviceIcon('network', 20) + '<span class="cx-face-name">' + escapeHtml(m.name) + '</span>'
    + '<span class="cx-face-legend"><span><i class="cx-led up"></i>link up</span><span><i class="cx-led down"></i>down</span>'
    + '<span><b class="cx-legend-q">?</b> not recorded</span></span></div>'
    + '<div class="cx-face-grid">' + m.data.ports.map(p => cxTileHtml(m.device_id, p)).join('') + '</div>'
    + '</div>').join('');
}

function renderCxPortMaps(){
  const panel = document.getElementById('cx-ports-panel');
  const el = document.getElementById('cx-ports');
  if(!panel || !el) return;
  panel.hidden = cxLivePortMaps(_cxState.portMaps).length === 0;
  el.innerHTML = cxPortFacesHtml(_cxState.portMaps);
}

// Port maps for callers outside the Connections tab (the Overview card).
async function cxLoadPortMaps(){
  const status = await cxGetJson('/api/discovery/status');
  const maps = (status && status.port_maps) || [];
  const ports = await Promise.all(maps.map(m => cxGetJson('/api/ports/' + m.device_id)));
  return maps.map((m, i) => ({device_id: m.device_id, name: m.name, data: ports[i]}));
}

// Up-but-unrecorded tile: open quick add with the switch and port filled in.
function cxQuickAddAt(deviceId, port){
  const view = document.getElementById('view-connections');
  if(view && !view.classList.contains('active')) setTab('connections');   // from the Overview copy
  const box = document.getElementById('cx-quick');
  if(!box) return;
  const qa = renderQuickAdd(box, {b_id: deviceId, parent_port: port, onAdded: () => connectionsChanged()});
  box.closest('.cx-panel').scrollIntoView({block: 'start', behavior: 'smooth'});
  if(qa) setTimeout(() => qa.focus(), 350);
}

nwOnSubview('connections', function(){ mountConnectionsTab(); });
