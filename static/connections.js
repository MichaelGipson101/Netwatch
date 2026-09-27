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
  error: null, openChip: null, scanPolling: false, lastPending: null, lastLoggedIn: null,
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
  // One in-flight refresh at a time - but a call that arrives mid-flight must
  // coalesce (run again right after), not be dropped: every mutation's
  // connectionsChanged() has to land, or the workspace shows stale data.
  if(_cxState.refreshing){ _cxState.refreshQueued = true; return; }
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
    if(_cxState.pendingEdit !== null) cxStartEdit(_cxState.pendingEdit);
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
  renderCxTable();
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

function cxRowHtml(c, now, isDrift){
  const src = c.source || 'manual';
  return '<tr data-conn="' + c.id + '"' + (isDrift ? ' class="cx-row-drift"' : '') + '>'
    + '<td><div class="cx-pair">'
      + '<button type="button" class="cx-dev" onclick="openInventoryDrawer(' + c.child_id + ')">'
        + deviceIcon(c.child_type || 'host', 16) + '<span>' + escapeHtml(c.child_name) + '</span></button>'
      + '<span class="cx-arrow" aria-hidden="true">→</span>'
      + '<button type="button" class="cx-dev" onclick="openInventoryDrawer(' + c.parent_id + ')">'
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
  return '<tr class="cx-edit-row" data-conn="' + c.id + '"><td colspan="6"><div class="cx-edit">'
    + '<div class="cx-edit-title">' + escapeHtml(c.child_name) + ' → ' + escapeHtml(c.parent_name) + '</div>'
    + '<label class="qa-field"><span>Port</span>' + portCtl + '</label>'
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
    + '<div class="cx-edit-actions">'
      + (ambiguous ? '<button type="button" class="btn" onclick="cxSwapConnection(' + c.id + ')">⇅ Swap direction</button>' : '')
      + '<button type="button" class="btn btn-ghost" onclick="cxCancelEdit()">Cancel</button>'
      + '<button type="button" class="btn btn-primary" onclick="cxSaveEdit(' + c.id + ')">Save</button>'
    + '</div></div></td></tr>';
}

function renderCxTable(){
  const el = document.getElementById('cx-table');
  if(!el) return;
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
      _cxState.query = search.value;
      _cxState.editingConn = null;
      cxRenderTableRows();
    });
    // Edit-row inputs write straight into the draft, so re-renders keep them.
    const sync = e => {
      const f = e.target.closest('.cx-edit-field');
      if(!f || !_cxState.editDraft) return;
      _cxState.editDraft[f.dataset.field] = f.value;
      if(f.dataset.field === 'connection_type' && e.type === 'change') cxRenderTableRows();
    };
    el.addEventListener('input', sync);
    el.addEventListener('change', sync);
  }
  cxRenderTableRows();
}

function cxRenderTableRows(){
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
  // keeps the values either way, this just keeps focus.
  const active = document.activeElement;
  if(active && active.closest && active.closest('.cx-edit-row') && el.contains(active)) return;
  const rows = cxFilterConnections(conns, _cxState.filter, _cxState.query, driftIds);
  const now = Math.floor(Date.now() / 1000);
  el.querySelector('tbody').innerHTML = rows.map(c => _cxState.editingConn === c.id
    ? cxEditRowHtml(c, issues[c.id] || [])
    : cxRowHtml(c, now, Object.prototype.hasOwnProperty.call(issues, c.id))).join('');
  const empty = el.querySelector('.cx-table-empty');
  empty.hidden = rows.length > 0;
  empty.textContent = conns.length
    ? 'No connections match this filter.'
    : 'No connections recorded yet. Add one above, or accept suggestions as discovery finds them.';
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

function cxSetFilter(f){
  _cxState.filter = f;
  _cxState.editingConn = null;
  cxRenderTableRows();
}

async function cxStartEdit(id){
  const c = cxFindConnection(id);
  if(!c){ _cxState.pendingEdit = id; return; }   // applied after the next refresh
  _cxState.pendingEdit = null;
  _cxState.editingConn = id;
  _cxState.editDraft = {parent_port: c.parent_port || '', connection_type: c.connection_type || 'ethernet',
                        notes: c.notes || ''};
  _cxState.editOrigPort = c.parent_port || '';
  _cxState.editPorts = undefined;
  cxRenderTableRows();
  const body = await cxGetJson('/api/ports/' + c.parent_id);
  if(_cxState.editingConn !== id) return;
  _cxState.editPorts = body && !body.migrationPending ? body.ports : null;
  const match = qaMatchPortOption(qaPortOptions(_cxState.editPorts), _cxState.editDraft.parent_port);
  if(match){   // "8" stored, "Port 8" live: same port, not a change
    _cxState.editDraft.parent_port = match;
    _cxState.editOrigPort = match;
  }
  cxRenderTableRows();
}

function cxCancelEdit(){
  _cxState.editingConn = null;
  _cxState.editDraft = null;
  cxRenderTableRows();
}

async function cxSaveEdit(id){
  const c = cxFindConnection(id);
  const d = _cxState.editDraft;
  if(!c || !d) return;
  const body = {};
  if(d.connection_type !== c.connection_type) body.connection_type = d.connection_type;
  if(d.connection_type !== 'wifi' && (d.parent_port || '') !== (_cxState.editOrigPort || '')) body.parent_port = d.parent_port;
  if((d.notes || '') !== (c.notes || '')) body.notes = d.notes;
  if(!Object.keys(body).length){ cxCancelEdit(); return; }
  const out = await cxPost('/api/connections/' + id, body);
  if(!out.ok){ toast('Could not save: ' + out.error, 'error'); return; }
  const w = out.body.warnings || [];
  if(w.indexOf('port_in_use') !== -1) toast('Saved. Heads up: another connection uses that port.', 'info');
  else if(w.indexOf('parent_port_cleared') !== -1) toast('Saved. The port was cleared: wifi links don\'t use one.', 'info');
  else toast('Connection saved', 'success');
  _cxState.editingConn = null;
  _cxState.editDraft = null;
  connectionsChanged();
}

async function cxSwapConnection(id){
  const out = await cxPost('/api/connections/' + id, {swap: true});
  if(!out.ok){ toast('Could not swap: ' + out.error, 'error'); return; }
  toast('Direction swapped', 'success');
  _cxState.editingConn = null;
  _cxState.editDraft = null;
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
  if(opts && opts.edit) cxStartEdit(id);
  else cxRenderTableRows();
}
