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
  editingConn: null, editDraft: null, editPorts: undefined, editOrig: null, pendingEdit: null,
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
  renderCxTable();
}

function mountConnectionsTab(){
  _cxState.mounted = true;
  // Seed from the current auth state so the first updateAuthUI() call after
  // mount isn't misread as a login change (mountConnectionsTab already does
  // its own initial cxRefreshAll() below).
  if(typeof _authState !== 'undefined') _cxState.lastLoggedIn = _authState.logged_in;
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
  _cxState.editDraft = {parent_port: c.parent_port || '', connection_type: c.connection_type || 'ethernet',
                        notes: c.notes || ''};
  _cxState.editOrig = {parent_port: _cxState.editDraft.parent_port, connection_type: _cxState.editDraft.connection_type,
                       notes: _cxState.editDraft.notes};
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
  if(d.connection_type !== 'wifi' && (d.parent_port || '') !== (orig.parent_port || '')) body.parent_port = d.parent_port;
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
  const out = await cxPost('/api/connections/' + id, {swap: true});
  if(!out.ok){ toast('Could not swap: ' + out.error, 'error'); return; }
  toast('Direction swapped', 'success');
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
  if(opts && opts.edit) cxStartEdit(id);
  else cxRenderTableRows({force: true});
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
  if(s.kind === 'device') extra = cxDeviceEditorHtml(s);
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
    _cxState.drafts[id] = Object.assign({}, _cxState.drafts[id], {[f.dataset.draft]: f.value});
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
    el.innerHTML = _cxState.error ? '' : '<div class="cx-muted">Loading…</div>';
    if(countEl) countEl.textContent = '';
    return;
  }
  const items = data.items || [];
  if(countEl) countEl.textContent = items.length ? items.length + ' pending' : '';
  // Keep focus if the user is typing in a suggestion's editor.
  const active = document.activeElement;
  if(active && el.contains(active) && active.matches('input, select')) return;
  if(!items.length){
    el.innerHTML = '<div class="cx-empty">Nothing to review. New suggestions appear here after each discovery scan.</div>';
    return;
  }
  el.innerHTML = cxGroupSuggestions(items).map(g =>
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
  toast(cxDoneMessage(s, action), 'success');
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
  if(!confirm('Accept ' + plan.send.length + ' ' + what + '?' + skippedNote)) return;
  plan.send.forEach(it => { _cxState.busy[it.id] = true; });
  renderCxSuggestions();
  const out = await cxPost('/api/suggestions/accept-all', {items: plan.send});
  plan.send.forEach(it => { delete _cxState.busy[it.id]; });
  if(!out.ok){ toast('Could not accept: ' + out.error, 'error'); renderCxSuggestions(); return; }
  const results = out.body.results || [];
  const okN = results.filter(r => r.ok).length;
  toast(okN === results.length
    ? 'Accepted ' + okN
    : 'Accepted ' + okN + ' of ' + results.length + '. The rest changed since you looked; they\'re refreshed below.',
    okN === results.length ? 'success' : 'info');
  qaInvalidateInventory();
  if(typeof fetchInventory === 'function') fetchInventory();
  connectionsChanged();
}
