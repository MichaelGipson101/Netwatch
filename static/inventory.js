// ── Inventory state ──
let _inventoryData = [];
// Per-type sort state. Each type gets its own column/direction so sorting
// hosts by RAM doesn't try to also sort peripherals by a column they lack.
let _inventorySort = {
  host:       { col: 'system', dir: 'asc' },
  network:    { col: 'system', dir: 'asc' },
  ups:        { col: 'system', dir: 'asc' },
  disk:       { col: 'system', dir: 'asc' },
  peripheral: { col: 'system', dir: 'asc' },
};
let _inventoryFilter = { category: null, deviceType: null, search: '', status: null };
let _editingInvId = null;

async function fetchInventory(){
  try {
    const res = await fetch('/api/inventory');
    if(!res.ok) return;
    const data = await res.json();
    _inventoryData = data.items || [];
    renderInventoryTab();
  } catch(e){ /* ignore */ }
}

function renderInventoryTab(){
  // Update tab count
  const cnt = document.getElementById('inv-count');
  if(cnt){
    if(_inventoryData.length > 0){
      cnt.style.display = '';
      cnt.textContent = _inventoryData.length;
    } else {
      cnt.style.display = 'none';
    }
  }
  renderInventoryMetrics();
  renderInventoryChips();
  renderInventoryRows();
}

function renderInventoryMetrics(){
  const total = _inventoryData.length;
  const hostsOnly = _inventoryData.filter(i => (i.device_type || 'host') === 'host');
  const hostsCount = hostsOnly.length;
  const active = hostsOnly.filter(i => i.linked_host && i.linked_host.is_up).length;
  const activeItems = hostsOnly.filter(i => i.linked_host && i.linked_host.is_up);
  const totalPower = activeItems.reduce((s, i) => s + (i.tdp_watts || 0), 0);
  const totalScore = hostsOnly.reduce((s, i) => s + (i.cpu_score || 0), 0);
  // Per-type breakdown
  const typeCounts = {};
  _inventoryData.forEach(i => {
    const t = i.device_type || 'host';
    typeCounts[t] = (typeCounts[t] || 0) + 1;
  });
  // Use the global INV_TYPE_ORDER constant. For the breakdown line we
  // lowercase the labels and keep "other" for peripheral (was a special
  // case in the original code). Singular form is used when count === 1.
  const breakdownLabels = Object.assign({}, INV_TYPE_LABELS, {peripheral: 'Other'});
  const breakdownParts = INV_TYPE_ORDER
    .filter(t => typeCounts[t])
    .map(t => typeCounts[t] + ' ' + (typeCounts[t] === 1
      ? (INV_TYPE_SINGULAR[t] || t)
      : (breakdownLabels[t] || t).toLowerCase()));
  const html = [
    '<div class="inv-metric"><div class="inv-metric-label">Total devices</div><div class="inv-metric-val">' + total + '</div><div class="inv-metric-sub">' + (breakdownParts.join(' · ') || 'no records yet') + '</div></div>',
    '<div class="inv-metric"><div class="inv-metric-label">Hosts</div><div class="inv-metric-val">' + hostsCount + '</div><div class="inv-metric-sub">' + active + ' currently online</div></div>',
    '<div class="inv-metric"><div class="inv-metric-label">Est. live power</div><div class="inv-metric-val">' + totalPower + '<span class="unit">W</span></div><div class="inv-metric-sub">across ' + active + ' active hosts</div></div>',
    '<div class="inv-metric"><div class="inv-metric-label">Total CPU score</div><div class="inv-metric-val">' + Math.round(totalScore).toLocaleString() + '</div><div class="inv-metric-sub">across hosts</div></div>',
    '<div class="inv-metric"><div class="inv-metric-label">Architectures</div><div class="inv-metric-val" style="font-size:14px">' + (Array.from(new Set(hostsOnly.map(i => i.architecture).filter(Boolean))).join(', ') || '-') + '</div><div class="inv-metric-sub">across hosts</div></div>',
  ].join('');
  document.getElementById('inv-metrics').innerHTML = html;
}

let _invCatsExpanded = false;
const INV_CAT_LIMIT = 8;

function renderInventoryChips(){
  let searchFiltered = _inventoryData;
  if(_inventoryFilter.search){
    const q = _inventoryFilter.search.toLowerCase();
    searchFiltered = _inventoryData.filter(i =>
      (i.system || '').toLowerCase().includes(q)
      || (i.role || '').toLowerCase().includes(q)
      || (i.os || '').toLowerCase().includes(q)
      || (i.cpu || '').toLowerCase().includes(q)
      || (i.gpu || '').toLowerCase().includes(q)
      || JSON.stringify(i.properties || {}).toLowerCase().includes(q));
  }
  const chipBtn = (label, count, active, onclick) =>
    '<button type="button" class="inv-chip' + (active ? ' active' : '') + '" onclick="' + onclick + '">'
    + escapeHtml(label) + (count !== null ? '<span class="inv-chip-count">' + count + '</span>' : '') + '</button>';

  const typeCounts = {};
  searchFiltered.forEach(i => { const t = i.device_type || 'host'; typeCounts[t] = (typeCounts[t] || 0) + 1; });
  const typeChips = [chipBtn('All', searchFiltered.length, _inventoryFilter.deviceType === null, 'setInvTypeFilter(null)')];
  INV_TYPE_ORDER.forEach(t => {
    if(!typeCounts[t]) return;
    const safeArg = JSON.stringify(t).replace(/"/g, '&quot;');
    typeChips.push(chipBtn(INV_TYPE_LABELS[t] || t, typeCounts[t], _inventoryFilter.deviceType === t, 'setInvTypeFilter(' + safeArg + ')'));
  });

  let scope = searchFiltered;
  if(_inventoryFilter.deviceType !== null) scope = scope.filter(i => (i.device_type || 'host') === _inventoryFilter.deviceType);
  const cats = {};
  scope.forEach(i => { const c = i.category || '(uncategorized)'; cats[c] = (cats[c] || 0) + 1; });
  const sortedCats = Object.entries(cats).sort((a,b) => b[1] - a[1]);
  const visibleCats = _invCatsExpanded ? sortedCats : sortedCats.slice(0, INV_CAT_LIMIT);
  const catChips = [chipBtn('All', scope.length, _inventoryFilter.category === null, 'setInvCategoryFilter(null)')];
  visibleCats.forEach(([cat, count]) => {
    const safeArg = JSON.stringify(cat).replace(/"/g, '&quot;');
    catChips.push(chipBtn(cat, count, _inventoryFilter.category === cat, 'setInvCategoryFilter(' + safeArg + ')'));
  });
  // Keep an active-but-hidden category visible so its chip never vanishes
  if(!_invCatsExpanded && _inventoryFilter.category !== null
     && !visibleCats.some(([c]) => c === _inventoryFilter.category) && cats[_inventoryFilter.category]){
    const safeArg = JSON.stringify(_inventoryFilter.category).replace(/"/g, '&quot;');
    catChips.push(chipBtn(_inventoryFilter.category, cats[_inventoryFilter.category], true, 'setInvCategoryFilter(' + safeArg + ')'));
  }
  if(sortedCats.length > INV_CAT_LIMIT){
    catChips.push('<button type="button" class="inv-chip inv-chip-more" onclick="toggleInvCats()">'
      + (_invCatsExpanded ? 'Show less' : '+' + (sortedCats.length - INV_CAT_LIMIT) + ' more') + '</button>');
  }

  const STATUS_OPTS = [
    {val: null, label: 'All'}, {val: 'up', label: 'Up'},
    {val: 'down', label: 'Down'}, {val: 'unlinked', label: 'Unlinked'},
  ];
  const statusChips = STATUS_OPTS.map(o =>
    chipBtn(o.label, null, _inventoryFilter.status === o.val,
      'setInvStatusFilter(' + (o.val === null ? 'null' : JSON.stringify(o.val).replace(/"/g, '&quot;')) + ')'));

  const rowFn = (label, chips) =>
    '<div class="inv-chip-row"><span class="inv-chip-row-label">' + label + '</span>' + chips.join('') + '</div>';
  document.getElementById('inv-chips').innerHTML =
    rowFn('Status', statusChips)
    + rowFn('Type', typeChips)
    + (catChips.length > 1 ? rowFn('Category', catChips) : '');
}

function toggleInvCats(){
  _invCatsExpanded = !_invCatsExpanded;
  renderInventoryChips();
}

function setInvTypeFilter(t){
  _inventoryFilter.deviceType = t;
  // Reset category filter when type changes - keeps things simple
  _inventoryFilter.category = null;
  _invCatsExpanded = false;
  renderInventoryChips();
  renderInventoryRows();
}

function setInvCategoryFilter(cat){
  _inventoryFilter.category = cat;
  renderInventoryChips();
  renderInventoryRows();
}

function setInvStatusFilter(s){
  _inventoryFilter.status = s;
  renderInventoryChips();
  renderInventoryRows();
}

function renderInventoryRows(){
  return renderInventoryTablesByType();
}

// Column definitions per type. Keys map to the inventory record's top-level
// fields OR (when prefixed with "p:") into the properties JSON blob.
// Each column also declares a sortKey for sortValue() to use.
const INV_TYPE_COLUMNS = {
  host: [
    {key:'system',     label:'System',   sort:'system'},
    {key:'category',   label:'Category', sort:'category'},
    {key:'cpu',        label:'CPU',      sort:'cpu'},
    {key:'ram_gb',     label:'RAM',      sort:'ram_gb'},
    {key:'os',         label:'OS',       sort:'os'},
    {key:'linked',     label:'Status',   sort:'linked'},
  ],
  vm: [
    {key:'system',           label:'System',     sort:'system'},
    {key:'p:hypervisor',     label:'Hypervisor', sort:'p:hypervisor'},
    {key:'p:vcpu_count',     label:'vCPU',       sort:'p:vcpu_count'},
    {key:'p:ram_alloc_gb',   label:'RAM (GB)',   sort:'p:ram_alloc_gb'},
    {key:'p:disk_alloc_gb',  label:'Disk (GB)',  sort:'p:disk_alloc_gb'},
    {key:'os',               label:'OS',         sort:'os'},
    {key:'linked',           label:'Status',     sort:'linked'},
  ],
  network: [
    {key:'system',           label:'System',   sort:'system'},
    {key:'category',         label:'Category', sort:'category'},
    {key:'p:port_count',     label:'Ports',    sort:'p:port_count'},
    {key:'p:poe_watts',      label:'PoE',      sort:'p:poe_watts'},
    {key:'p:uplink_speed',   label:'Uplink',   sort:'p:uplink_speed'},
    {key:'p:managed',        label:'Managed',  sort:'p:managed'},
    {key:'linked',           label:'Status',   sort:'linked'},
  ],
  ups: [
    {key:'system',              label:'System',      sort:'system'},
    {key:'category',            label:'Category',    sort:'category'},
    {key:'p:capacity_va',       label:'Capacity',    sort:'p:capacity_va'},
    {key:'p:runtime_min',       label:'Runtime',     sort:'p:runtime_min'},
    {key:'p:battery_age_years', label:'Battery age', sort:'p:battery_age_years'},
    {key:'linked',              label:'Status',      sort:'linked'},
  ],
  disk: [
    {key:'system',          label:'System',    sort:'system'},
    {key:'category',        label:'Category',  sort:'category'},
    {key:'p:capacity_gb',   label:'Capacity',  sort:'p:capacity_gb'},
    {key:'p:interface',     label:'Interface', sort:'p:interface'},
    {key:'p:rpm',           label:'RPM',       sort:'p:rpm'},
    {key:'p:used_in',       label:'Used in',   sort:'p:used_in'},
    {key:'linked',          label:'Status',    sort:'linked'},
  ],
  peripheral: [
    {key:'system',     label:'System',   sort:'system'},
    {key:'category',   label:'Category', sort:'category'},
    {key:'p:subtype',  label:'Subtype',  sort:'p:subtype'},
    {key:'p:model',    label:'Model',    sort:'p:model'},
    {key:'linked',     label:'Status',   sort:'linked'},
  ],
  tablet: [
    {key:'system',     label:'System',   sort:'system'},
    {key:'category',   label:'Category', sort:'category'},
    {key:'p:subtype',  label:'Subtype',  sort:'p:subtype'},
    {key:'p:model',    label:'Model',    sort:'p:model'},
    {key:'linked',     label:'Status',   sort:'linked'},
  ],
  phone: [
    {key:'system',     label:'System',   sort:'system'},
    {key:'category',   label:'Category', sort:'category'},
    {key:'p:subtype',  label:'Subtype',  sort:'p:subtype'},
    {key:'p:model',    label:'Model',    sort:'p:model'},
    {key:'linked',     label:'Status',   sort:'linked'},
  ],
  printer: [
    {key:'system',     label:'System',   sort:'system'},
    {key:'category',   label:'Category', sort:'category'},
    {key:'p:subtype',  label:'Subtype',  sort:'p:subtype'},
    {key:'p:model',    label:'Model',    sort:'p:model'},
    {key:'linked',     label:'Status',   sort:'linked'},
  ],
};

const INV_TYPE_LABELS = {
  host: 'Hosts', vm: 'VMs', network: 'Network', ups: 'UPS',
  disk: 'Disks', peripheral: 'Peripherals',
  tablet: 'Tablets', phone: 'Phones', printer: 'Printers'
};
const INV_TYPE_SINGULAR = {
  host: 'host', vm: 'vm', network: 'network', ups: 'ups',
  disk: 'disk', peripheral: 'other', tablet: 'tablet', phone: 'phone', printer: 'printer'
};
const INV_TYPE_ORDER = ['host', 'vm', 'network', 'ups', 'disk', 'peripheral', 'tablet', 'phone', 'printer'];

// Get a value from a record using a column key. "p:foo" reads from
// the properties blob; bare keys read top-level fields.
function invColumnValue(rec, key){
  if(key.startsWith('p:')){
    const props = rec.properties || {};
    return props[key.slice(2)];
  }
  if(key === 'linked'){
    if(!rec.linked_host) return 0;
    return rec.linked_host.is_up ? 2 : 1;
  }
  return rec[key];
}

// Format a column value for display in the table.
function formatInvCell(rec, key){
  const v = invColumnValue(rec, key);

  if(key === 'system'){
    let html = '<div class="inv-system">' + escapeHtml(v || '') + '</div>';
    if(rec.role) html += '<div class="inv-role">' + escapeHtml(rec.role) + '</div>';
    return html;
  }
  if(key === 'category'){
    return '<span class="inv-cat-tag">' + escapeHtml(v || '-') + '</span>';
  }
  if(key === 'linked'){
    if(!rec.linked_host) return '<span style="color:var(--hint);font-size:11px;font-style:italic">no link</span>';
    const lh = rec.linked_host;
    let cls, label;
    if(lh.status === 'DEGRADED'){ cls = 'degraded'; label = 'Degraded'; }
    else if(lh.status === 'WAIT'){ cls = 'degraded'; label = 'Wait'; }
    else if(lh.is_up){ cls = 'up'; label = 'Up'; }
    else if(lh.status === 'IDLE'){ cls = 'idle'; label = 'Idle'; }
    else { cls = 'down'; label = 'Down'; }
    return '<span class="inv-link-pill ' + cls + '">' + label + '</span>';
  }
  // Host columns
  if(key === 'cpu'){
    let html = escapeHtml(v || '-');
    if(rec.architecture) html += '<div class="inv-mono">' + escapeHtml(rec.architecture) + '</div>';
    return html;
  }
  if(key === 'ram_gb'){
    if(v === null || v === undefined) return '-';
    return v < 1 ? (v * 1024).toFixed(0) + ' MB' : v + ' GB';
  }
  if(key === 'os') return escapeHtml(v || '-');

  // Type-specific properties
  if(key === 'p:port_count')   return v ? v + ' ports' : '-';
  if(key === 'p:poe_watts')    return v ? v + ' W' : '-';
  if(key === 'p:uplink_speed') return escapeHtml(v || '-');
  if(key === 'p:managed')      return v === true ? 'Managed' : v === false ? 'Unmanaged' : '-';
  if(key === 'p:capacity_va')  return v ? v.toLocaleString() + ' VA' : '-';
  if(key === 'p:runtime_min')  return v ? v + ' min' : '-';
  if(key === 'p:capacity_gb'){
    if(!v) return '-';
    return v >= 1000 ? (v/1000).toFixed(1) + ' TB' : v + ' GB';
  }
  if(key === 'p:rpm')          return v ? v.toLocaleString() + ' RPM' : (v === 0 ? 'SSD' : '-');
  if(key === 'p:interface')    return escapeHtml(v || '-');
  if(key === 'p:used_in')      return escapeHtml(v || '-');
  if(key === 'p:subtype')      return escapeHtml(v || '-');
  if(key === 'p:model')        return escapeHtml(v || '-');
  if(key === 'p:battery_age_years') return escapeHtml(v || '-');
  if(key === 'p:health')       return escapeHtml(v || '-');

  return escapeHtml(v == null ? '-' : String(v));
}

// Sortable value for a record + column. Numbers stay numeric, strings
// lowercase for case-insensitive compare.
function invSortValue(rec, key){
  const v = invColumnValue(rec, key);
  if(v === null || v === undefined) return '';
  if(typeof v === 'number') return v;
  if(typeof v === 'boolean') return v ? 1 : 0;
  return String(v).toLowerCase();
}

function renderInventoryTablesByType(){
  // Apply global filters (deviceType, category, search) to get the working set
  let rows = _inventoryData;
  if(_inventoryFilter.deviceType !== null){
    rows = rows.filter(i => (i.device_type || 'host') === _inventoryFilter.deviceType);
  }
  if(_inventoryFilter.category !== null){
    rows = rows.filter(i => (i.category || '(uncategorized)') === _inventoryFilter.category);
  }
  if(_inventoryFilter.search){
    const q = _inventoryFilter.search.toLowerCase();
    rows = rows.filter(i => {
      return (i.system || '').toLowerCase().includes(q)
        || (i.role || '').toLowerCase().includes(q)
        || (i.os || '').toLowerCase().includes(q)
        || (i.cpu || '').toLowerCase().includes(q)
        || (i.gpu || '').toLowerCase().includes(q)
        || JSON.stringify(i.properties || {}).toLowerCase().includes(q);
    });
  }
  if(_inventoryFilter.status === 'up'){
    rows = rows.filter(i => i.linked_host && i.linked_host.is_up && i.linked_host.status !== 'DEGRADED' && i.linked_host.status !== 'WAIT');
  } else if(_inventoryFilter.status === 'down'){
    rows = rows.filter(i => i.linked_host && (!i.linked_host.is_up || i.linked_host.status === 'DEGRADED' || i.linked_host.status === 'WAIT'));
  } else if(_inventoryFilter.status === 'unlinked'){
    rows = rows.filter(i => !i.linked_host);
  }

  // Group rows by device_type
  const byType = {};
  rows.forEach(r => {
    const t = r.device_type || 'host';
    (byType[t] = byType[t] || []).push(r);
  });

  // Empty-state
  const container = document.getElementById('inv-tables');
  const emptyEl = document.getElementById('inv-empty');
  if(rows.length === 0){
    container.innerHTML = '';
    if(emptyEl){
      const isFiltered = _inventoryData.length > 0;
      emptyEl.style.display = '';
      if(isFiltered){
        emptyEl.innerHTML = '<div class="events-empty-icon" style="background:var(--subtle);color:var(--hint)">⊘</div>'
          + '<div class="events-empty-title">No results</div>'
          + '<div class="events-empty-sub">No records match this filter. Try clearing the search or chips above.</div>';
      } else {
        emptyEl.innerHTML = '<div class="events-empty-icon">+</div>'
          + '<div class="events-empty-title">No inventory yet</div>'
          + '<div class="events-empty-sub">Import an XLSX spreadsheet or click "+ Add" to create records one at a time.</div>';
      }
    }
    return;
  }
  if(emptyEl) emptyEl.style.display = 'none';

  // Build a table per type that has records, in INV_TYPE_ORDER. We show
  // section headings only when "All" is selected (i.e. no type filter)
  // since otherwise the active chip already conveys the type.
  const showHeadings = _inventoryFilter.deviceType === null
    && Object.keys(byType).length > 1;
  const html = INV_TYPE_ORDER
    .filter(t => byType[t] && byType[t].length)
    .map(t => renderTypeTable(t, byType[t], showHeadings))
    .join('');
  container.innerHTML = html;
}

function renderTypeTable(deviceType, rows, showHeading){
  const cols = INV_TYPE_COLUMNS[deviceType] || INV_TYPE_COLUMNS.host;
  const sortState = _inventorySort[deviceType] || {col:'system', dir:'asc'};
  // Sort the rows according to this type's current sort state
  const sortedRows = [...rows].sort((a, b) => {
    const av = invSortValue(a, sortState.col);
    const bv = invSortValue(b, sortState.col);
    let cmp;
    if(typeof av === 'number' && typeof bv === 'number') cmp = av - bv;
    else cmp = String(av).localeCompare(String(bv));
    return sortState.dir === 'asc' ? cmp : -cmp;
  });

  let out = '';
  if(showHeading){
    out += '<div class="inv-type-heading">'
      + '<svg width="16" height="16" viewBox="0 0 32 32" style="flex-shrink:0" aria-hidden="true"><use href="#topo-icon-' + deviceType + '"/></svg>'
      + '<span class="inv-type-heading-label">' + escapeHtml(INV_TYPE_LABELS[deviceType]) + '</span>'
      + '<span class="inv-type-heading-count">' + rows.length + '</span>'
      + '</div>';
  }
  out += '<div class="inv-table-wrap inv-table-' + deviceType + '">';
  out += '<table class="inv-table">';
  out += '<thead><tr>';
  cols.forEach(c => {
    const arrow = sortState.col === c.sort
      ? (sortState.dir === 'asc' ? ' sort-asc' : ' sort-desc')
      : '';
    out += '<th class="inv-th' + arrow + '" data-sort="' + escapeHtml(c.sort)
        + '" data-type="' + escapeHtml(deviceType) + '">'
        + escapeHtml(c.label) + '</th>';
  });
  out += '</tr></thead>';
  out += '<tbody>';
  sortedRows.forEach(rec => {
    out += '<tr class="inv-row" tabindex="0" onclick="openInventoryDrawer(' + rec.id + ')">';
    cols.forEach((c, i) => {
      if(i === 0){
        out += '<td><span style="display:flex;align-items:center;gap:5px">'
          + deviceIcon(rec.device_type, 22)
          + formatInvCell(rec, c.key)
          + '</span></td>';
      } else {
        out += '<td>' + formatInvCell(rec, c.key) + '</td>';
      }
    });
    out += '</tr>';
  });
  out += '</tbody></table></div>';
  return out;
}

// Wire up sort headers + search. Each table has its own sort state per type;
// the th carries data-type so we know which one to update.
document.addEventListener('click', e => {
  const th = e.target.closest('.inv-th');
  if(!th) return;
  const col = th.dataset.sort;
  const type = th.dataset.type || 'host';
  const state = _inventorySort[type] || (_inventorySort[type] = {col:'system', dir:'asc'});
  if(state.col === col){
    state.dir = state.dir === 'asc' ? 'desc' : 'asc';
  } else {
    state.col = col;
    state.dir = 'asc';
  }
  renderInventoryRows();
});
document.addEventListener('input', e => {
  if(e.target.id === 'inv-search'){
    _inventoryFilter.search = e.target.value;
    renderInventoryRows();
  }
  // If user manually edits a MAC field, clear the "auto" flag visually
  if(e.target.classList.contains('f-mac')){
    e.target.dataset.auto = '';
    const tag = e.target.parentElement.querySelector('.mac-auto-tag');
    if(tag) tag.remove();
  }
  // Refresh the link picker's auto-suggest when the system name changes
  // (only for new records - existing records have their MAC already set)
  if(e.target.classList.contains('inv-f-system') && !_editingInvId){
    const sel = document.querySelector('.inv-f-link');
    if(sel && !sel.value){
      const macField = document.querySelector('.inv-f-mac');
      renderInvLinkPicker({
        system: e.target.value,
        mac: macField ? macField.value : '',
      });
    }
  }
});

// Editor
const INVENTORY_TYPE_PROPERTIES = {
  host: [],
  vm: [
    {key:"hypervisor",    type:"string", label:"Hypervisor (Proxmox/KVM/ESXi/etc.)"},
    {key:"vcpu_count",    type:"int",    label:"vCPU count"},
    {key:"ram_alloc_gb",  type:"int",    label:"Allocated RAM (GB)"},
    {key:"disk_alloc_gb", type:"int",    label:"Allocated disk (GB)"},
    {key:"autostart",     type:"bool",   label:"Auto-starts with host"},
    {key:"proxmox_vmid",  type:"int",    label:"Proxmox VMID"},
  ],
  network: [
    {key:"port_count",     type:"int",    label:"Port count"},
    {key:"poe_watts",      type:"int",    label:"PoE budget (W)"},
    {key:"managed",        type:"bool",   label:"Managed"},
    {key:"uplink_speed",   type:"string", label:"Uplink speed (e.g. 10G SFP+)"},
  ],
  ups: [
    {key:"capacity_va",       type:"int",    label:"Capacity (VA)"},
    {key:"capacity_wh",       type:"int",    label:"Capacity (Wh)"},
    {key:"runtime_min",       type:"int",    label:"Runtime at full load (min)"},
    {key:"battery_age_years", type:"string", label:"Battery age (e.g. '2 years')"},
  ],
  disk: [
    {key:"capacity_gb",  type:"int",    label:"Capacity (GB)"},
    {key:"interface",    type:"string", label:"Interface (SATA / NVMe / USB)"},
    {key:"rpm",          type:"int",    label:"RPM (blank for SSD)"},
    {key:"used_in",      type:"string", label:"Currently installed in"},
    {key:"health",       type:"string", label:"Health status"},
  ],
  peripheral: [
    {key:"subtype",  type:"string", label:"Subtype (KVM, monitor, keyboard, etc.)"},
    {key:"model",    type:"string", label:"Model"},
  ],
  tablet: [
    {key:"subtype",  type:"string", label:"Subtype (iPad, Android, Surface, etc.)"},
    {key:"model",    type:"string", label:"Model"},
  ],
  phone: [
    {key:"subtype",  type:"string", label:"Subtype (iPhone, Android, etc.)"},
    {key:"model",    type:"string", label:"Model"},
  ],
  printer: [
    {key:"subtype",  type:"string", label:"Subtype (laser, inkjet, label, etc.)"},
    {key:"model",    type:"string", label:"Model"},
  ],
};

function onInvTypeChange(newType){
  const hostFields = document.querySelector(".inv-host-fields");
  const typeSlot   = document.getElementById("inv-type-fields");
  if(!hostFields || !typeSlot) return;
  // Show host-specific fields for host AND vm types - VMs are hosts
  // that happen to run on a hypervisor, so they use all host fields PLUS
  // the VM-specific properties.
  const hostLikeTypes = (newType === "host" || newType === "vm");
  hostFields.style.display = hostLikeTypes ? "contents" : "none";
  // Render type-specific fields (if any)
  const props = INVENTORY_TYPE_PROPERTIES[newType] || [];
  if(props.length === 0){
    typeSlot.innerHTML = "";
    typeSlot.style.display = "none";
    return;
  }
  typeSlot.style.display = "contents";
  typeSlot.innerHTML = props.map(p => {
    if(p.type === "bool"){
      return '<label class="full"><span style="display:inline-flex;align-items:center;gap:8px;text-transform:none;letter-spacing:0">' +
        '<input type="checkbox" class="inv-p-' + p.key + '" style="width:auto"> ' + escapeHtml(p.label) + '</span></label>';
    } else if(p.type === "int"){
      return '<label>' + escapeHtml(p.label) + '<input type="number" step="1" class="inv-p-' + p.key + '"></label>';
    } else {
      return '<label>' + escapeHtml(p.label) + '<input type="text" class="inv-p-' + p.key + '"></label>';
    }
  }).join("");
}

async function openInventoryEditor(existingId){
  if(!_authState.logged_in){
    if(_authState.setup_required) openSetup();
    else openLogin(() => openInventoryEditor(existingId));
    return;
  }
  _editingInvId = existingId || null;
  document.getElementById('inv-edit-title').textContent = existingId ? 'Edit inventory record' : 'New inventory record';
  document.getElementById('inv-delete-btn').style.display = existingId ? '' : 'none';
  document.getElementById('inv-edit-error').textContent = '';
  // Clear all fields
  ['system','category','role','cpu','ram_gb','gpu','architecture','os','cpu_score','tdp_watts','tpm','mac','ip','serial','notes'].forEach(f => {
    const el = document.querySelector('.inv-f-' + f);
    if(el) el.value = '';
  });
  let loadedRec = null;
  if(existingId){
    try {
      const res = await fetch('/api/inventory/' + existingId);
      if(res.ok){
        loadedRec = await res.json();
        ['system','category','role','cpu','ram_gb','gpu','architecture','os','cpu_score','tdp_watts','tpm','mac','ip','serial','notes'].forEach(f => {
          const el = document.querySelector('.inv-f-' + f);
          if(el && loadedRec[f] !== null && loadedRec[f] !== undefined) el.value = loadedRec[f];
        });
        // Set the type dropdown and render type-specific fields
        const dtype = loadedRec.device_type || 'host';
        const typeEl = document.querySelector('.inv-f-device_type');
        if(typeEl) typeEl.value = dtype;
        onInvTypeChange(dtype);
        // Populate type-specific property values
        const props = loadedRec.properties || {};
        Object.keys(props).forEach(k => {
          const el = document.querySelector('.inv-p-' + k);
          if(!el) return;
          if(el.type === 'checkbox') el.checked = !!props[k];
          else el.value = props[k];
        });
      }
    } catch(e){}
  } else {
    // New record: default to host, render host fields
    const typeEl = document.querySelector('.inv-f-device_type');
    if(typeEl) typeEl.value = 'host';
    onInvTypeChange('host');
  }
  // Populate the link picker. For new records loadedRec is null; we still
  // pass current form values so the auto-suggest works on the system name.
  if(!loadedRec){
    const sysEl = document.querySelector('.inv-f-system');
    loadedRec = { system: sysEl ? sysEl.value : '', mac: '' };
  }
  renderInvLinkPicker(loadedRec);
  document.getElementById('inv-edit-overlay').classList.add('open');
}
function closeInventoryEditor(){
  document.getElementById('inv-edit-overlay').classList.remove('open');
}

// Normalise a MAC string for comparison. Returns lowercase 12-hex-char form
// or '' if the input doesn't parse.
function normMac(s){
  if(!s) return '';
  const clean = String(s).toLowerCase().replace(/[^0-9a-f]/g, '');
  return clean.length === 12 ? clean : '';
}

// Cheap fuzzy similarity between two strings (0-1 range).
// Used to auto-suggest a host match based on system name.
function fuzzyMatch(a, b){
  if(!a || !b) return 0;
  const x = String(a).toLowerCase();
  const y = String(b).toLowerCase();
  if(x === y) return 1;
  if(x.includes(y) || y.includes(x)) return 0.8;
  // Token overlap
  const tokA = new Set(x.split(/[^a-z0-9]+/).filter(t => t.length > 2));
  const tokB = new Set(y.split(/[^a-z0-9]+/).filter(t => t.length > 2));
  if(!tokA.size || !tokB.size) return 0;
  let shared = 0;
  tokA.forEach(t => { if(tokB.has(t)) shared++; });
  return shared / Math.max(tokA.size, tokB.size);
}

// Populate the "Link to monitored host" dropdown based on current data.
// Called after the editor opens (and after _editingInvId / fields are set).
async function renderInvLinkPicker(currentRec){
  const sel = document.querySelector('.inv-f-link');
  const hint = document.getElementById('inv-link-hint');
  if(!sel) return;
  // Reset to placeholder
  sel.innerHTML = '<option value="">(none — not linked to a monitored host)</option>';

  // Gather monitored hosts from cached lastData (set by refresh()).
  const hosts = (typeof lastData !== 'undefined' && lastData && lastData.hosts) ? lastData.hosts : [];
  if(!hosts.length){
    if(hint) hint.textContent = 'No monitored hosts found yet. Add a host first to enable linking.';
    return;
  }

  // Find which hosts are already linked to other inventory records,
  // so we can warn when picking would steal a link.
  const otherLinks = {};  // mac_normalised -> system_name
  _inventoryData.forEach(i => {
    const m = normMac(i.mac);
    if(!m) return;
    if(_editingInvId && i.id === _editingInvId) return;  // skip self
    otherLinks[m] = i.system;
  });

  const currentMacNorm = normMac(currentRec && currentRec.mac);

  // Auto-suggest: if no MAC is set yet, find best fuzzy match by system name
  let suggestedIp = null;
  if(currentRec && currentRec.system && !currentMacNorm){
    let best = { score: 0, ip: null };
    hosts.forEach(h => {
      const score = fuzzyMatch(currentRec.system, h.name);
      if(score > best.score){ best = { score, ip: h.ip }; }
    });
    if(best.score >= 0.5) suggestedIp = best.ip;
  }

  // Sort hosts by group then name for a stable, browseable dropdown
  const sorted = [...hosts].sort((a, b) => {
    const ga = (a.group || '').toLowerCase();
    const gb = (b.group || '').toLowerCase();
    if(ga !== gb) return ga.localeCompare(gb);
    return (a.name || '').toLowerCase().localeCompare((b.name || '').toLowerCase());
  });

  let selectedFound = false;
  sorted.forEach(h => {
    const hostMac = (h.specs && h.specs.mac) ? h.specs.mac : '';
    const macNorm = normMac(hostMac);
    const opt = document.createElement('option');
    opt.value = h.ip;
    let label = h.name + ' (' + h.ip + ')';
    if(!macNorm){
      label += ' — no MAC set on host';
      opt.disabled = true;
    } else if(otherLinks[macNorm]){
      label += ' — already linked to ' + otherLinks[macNorm];
    } else if(h.ip === suggestedIp){
      label += ' — likely match';
    }
    opt.textContent = label;
    opt.dataset.mac = macNorm;
    // Pre-select if MAC matches current record
    if(macNorm && macNorm === currentMacNorm){
      opt.selected = true;
      selectedFound = true;
    }
    sel.appendChild(opt);
  });

  // If we couldn't find a host matching the current MAC but there's a fuzzy
  // suggestion, surface it in the hint instead of preselecting (less invasive)
  if(!selectedFound && suggestedIp){
    const suggested = sorted.find(h => h.ip === suggestedIp);
    if(suggested && hint){
      hint.textContent = 'Suggested match: ' + suggested.name + ' (' + suggested.ip + ') — pick from dropdown to confirm';
    }
  } else if(selectedFound && hint){
    hint.textContent = 'Linked. Picking another host will replace the MAC below.';
  } else if(currentMacNorm && !selectedFound && hint){
    hint.textContent = 'MAC is set but does not match any monitored host.';
  } else if(hint){
    hint.textContent = 'Pick a host to auto-fill the MAC below.';
  }
}

// Called when the user picks a host from the dropdown.
function handleInvLinkChange(sel){
  const macField = document.querySelector('.inv-f-mac');
  if(!macField) return;
  const opt = sel.options[sel.selectedIndex];
  if(!opt || !opt.value){
    // "(none)" picked - clear the MAC
    macField.value = '';
    const hint = document.getElementById('inv-link-hint');
    if(hint) hint.textContent = 'Link cleared. Pick a host to re-link.';
    return;
  }
  const mac = opt.dataset.mac;
  if(mac){
    // Format for display: aa:bb:cc:dd:ee:ff
    macField.value = mac.match(/.{2}/g).join(':');
    const hint = document.getElementById('inv-link-hint');
    if(hint) hint.textContent = 'Linked to ' + opt.textContent.split(' — ')[0] + '. MAC has been auto-filled.';
  }
}
async function submitInventory(ev){
  if(ev) ev.preventDefault();
  const data = {};
  ['system','category','role','cpu','ram_gb','gpu','architecture','os','cpu_score','tdp_watts','tpm','mac','ip','serial','notes'].forEach(f => {
    const el = document.querySelector('.inv-f-' + f);
    if(el) data[f] = el.value.trim();
  });
  // Type + type-specific properties
  const typeEl = document.querySelector('.inv-f-device_type');
  const dtype = typeEl ? typeEl.value : 'host';
  data.device_type = dtype;
  const propDefs = INVENTORY_TYPE_PROPERTIES[dtype] || [];
  const props = {};
  propDefs.forEach(p => {
    const el = document.querySelector('.inv-p-' + p.key);
    if(!el) return;
    if(p.type === 'bool'){
      props[p.key] = !!el.checked;
    } else if(p.type === 'int'){
      const v = el.value.trim();
      if(v !== '') props[p.key] = parseInt(v, 10);
    } else {
      const v = el.value.trim();
      if(v !== '') props[p.key] = v;
    }
  });
  data.properties = props;
  const err = document.getElementById('inv-edit-error');
  err.textContent = '';
  if(!data.system){ err.textContent = 'System name is required'; return; }
  try {
    const url = _editingInvId ? '/api/inventory/' + _editingInvId : '/api/inventory';
    const res = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)
    });
    const result = await res.json();
    if(!res.ok){ err.textContent = result.error || 'Save failed'; return; }
    closeInventoryEditor();
    closeDrawer();
    await fetchInventory();
  } catch(e){ err.textContent = 'Network error'; }
}
async function deleteInventory(){
  if(!_editingInvId) return;
  if(!confirm('Delete this inventory record? This cannot be undone.')) return;
  try {
    const res = await fetch('/api/inventory/' + _editingInvId + '/delete', { method: 'POST' });
    if(res.ok){
      closeInventoryEditor();
      closeDrawer();
      await fetchInventory();
    }
  } catch(e){}
}

// Inventory drawer (reuses the host drawer chrome)
async function openInventoryDrawer(invId){
  try {
    const res = await fetch('/api/inventory/' + invId);
    if(!res.ok) return;
    const rec = await res.json();
    renderInventoryDrawer(rec);
  } catch(e){}
}
function renderInventoryDrawer(rec){
  openDrawerIp = 'inv:' + rec.id;
  document.getElementById('drawer').classList.add('open');
  document.getElementById('drawer-backdrop').classList.add('open');
  const dotEl = document.getElementById('d-dot');
  if(dotEl){ dotEl.className = 'drawer-icon-wrap'; dotEl.innerHTML = '<svg width="32" height="32" viewBox="0 0 32 32" style="color:var(--hint)" aria-hidden="true"><use href="#topo-icon-' + (rec.device_type || 'host') + '"/></svg>'; }
  document.getElementById('d-name').textContent = rec.system || 'Inventory record';
  const meta = document.getElementById('d-meta');
  if(meta){
    const dtypeLabel = {host:'Host', network:'Network', ups:'UPS', disk:'Disk',
      peripheral:'Peripheral', tablet:'Tablet', phone:'Phone', printer:'Printer'}[rec.device_type || 'host'] || 'Host';
    meta.innerHTML = '<span class="inv-mono" style="background:var(--blue-bg);color:var(--blue);padding:2px 7px;border-radius:3px;font-size:10px">' + escapeHtml(dtypeLabel) + '</span>'
      + '<span>·</span><span>' + escapeHtml(rec.category || '-') + '</span>';
    if(rec.architecture && (rec.device_type || 'host') === 'host'){
      meta.innerHTML += '<span>·</span><span class="inv-mono">' + escapeHtml(rec.architecture) + '</span>';
    }
  }
  const dtype = rec.device_type || 'host';
  let fields = [];
  if(dtype === 'host'){
    fields = [
      ['Role / status', rec.role],
      ['CPU',           rec.cpu],
      ['GPU',           rec.gpu],
      ['RAM',           rec.ram_gb !== null && rec.ram_gb !== undefined ? (rec.ram_gb < 1 ? (rec.ram_gb * 1024).toFixed(0) + ' MB' : rec.ram_gb + ' GB') : null],
      ['OS',            rec.os],
      ['CPU score',     rec.cpu_score ? rec.cpu_score.toLocaleString() : null],
      ['Max TDP',       rec.tdp_watts ? rec.tdp_watts + ' W' : null],
      ['TPM version',   rec.tpm],
      ['MAC',           rec.mac],
      ['IP',            rec.ip],
      ['Serial',        rec.serial],
    ];
  } else {
    // Common fields for non-host types
    fields = [['Role / status', rec.role]];
    // Type-specific properties
    const props = rec.properties || {};
    const propDefs = INVENTORY_TYPE_PROPERTIES[dtype] || [];
    propDefs.forEach(p => {
      let val = props[p.key];
      if(val === undefined || val === null || val === '') return;
      if(p.type === 'bool') val = val ? 'Yes' : 'No';
      else if(p.type === 'int') val = Number(val).toLocaleString();
      fields.push([p.label, val]);
    });
    fields.push(['MAC', rec.mac]);
    fields.push(['IP', rec.ip]);
    fields.push(['Serial', rec.serial]);
  }
  let specsHtml = '<div class="d-section"><div class="d-section-hdr"><span>Specifications</span></div><div class="inv-drawer-section">';
  fields.forEach(([k, v]) => {
    if(v === null || v === undefined || v === '') return;
    specsHtml += '<div class="inv-drawer-row"><div class="inv-drawer-key">' + escapeHtml(k.toUpperCase()) + '</div><div class="inv-drawer-val">' + escapeHtml(String(v)) + '</div></div>';
  });
  specsHtml += '</div></div>';

  let linkHtml = '';
  if(rec.linked_host){
    const lh = rec.linked_host;
    const cls = (lh.status === 'DEGRADED' || lh.status === 'WAIT') ? 'degraded' : lh.is_up ? 'up' : (lh.status === 'IDLE' ? 'idle' : 'down');
    // The card itself navigates to the host drawer; the inline buttons
    // below offer common actions (Wake) without requiring tab switch.
    // We use stopPropagation on the buttons so clicking them doesn't
    // also trigger the card's navigation onclick.
    const macForWake = (rec.mac || '').trim();
    const wakeBtnHtml = macForWake
      ? '<button class="inv-link-action-btn" '
        + 'onclick="event.stopPropagation();sendWakeFromInventory(' + JSON.stringify(lh.ip).replace(/"/g, '&quot;') + ', this)">'
        + 'Wake</button>'
      : '';
    linkHtml = '<div class="d-section"><div class="d-section-hdr"><span>Linked monitored host</span></div>'
      + '<div class="inv-link-host-card" onclick="navigateToHostDrawerSafe(' + JSON.stringify(lh.ip).replace(/"/g, '&quot;') + ')">'
      + '<div class="inv-link-host-info">'
        + '<div style="font-weight:500">' + escapeHtml(lh.name) + '</div>'
        + '<div class="inv-mono" style="font-size:11px;color:var(--muted);margin-top:2px">' + escapeHtml(lh.ip) + '</div>'
      + '</div>'
      + '<div class="inv-link-host-right">'
        + '<span class="inv-link-pill ' + cls + '">' + escapeHtml(lh.status) + '</span>'
        + wakeBtnHtml
        + '<span class="inv-link-arrow">→</span>'
      + '</div>'
      + '</div>'
      + '<div class="inv-link-action-status" id="inv-link-action-status"></div>'
      + '</div>';
  }

  let notesHtml = '';
  if(rec.notes){
    notesHtml = '<div class="d-section"><div class="d-section-hdr"><span>Notes</span></div>'
      + '<div style="background:var(--subtle);border:1px solid var(--border-light);border-radius:8px;padding:11px 13px;font-size:13px;line-height:1.5;white-space:pre-wrap">' + escapeHtml(rec.notes) + '</div></div>';
  }

  const editBtn = '<button class="d-action-btn" onclick="openInventoryEditor(' + rec.id + ')">'
    + '<span>Edit record</span><span class="arrow">→</span></button>';

  // Placeholder for the connections section - filled async after main render
  const connsHtml = '<div class="d-section" id="d-connections-section"><div class="d-section-hdr"><span>Connections</span></div><div id="d-connections-body" class="conn-loading">Loading...</div></div>';
  document.getElementById('drawer-body').innerHTML = linkHtml + specsHtml + connsHtml + notesHtml + '<div class="d-section">' + editBtn + '</div>';
  document.getElementById('drawer-body').dataset.hostIp = 'inv:' + rec.id;
  // Fetch and render connections without blocking the drawer open
  loadInventoryConnections(rec.id);
}

// State for the inline add-connection form (per-drawer)
let _connFormState = { open: false, deviceId: null };

async function loadInventoryConnections(deviceId){
  const body = document.getElementById('d-connections-body');
  if(!body) return;
  try {
    const [connRes, invRes] = await Promise.all([
      fetch('/api/inventory/' + deviceId + '/connections'),
      fetch('/api/inventory'),
    ]);
    if(!connRes.ok || !invRes.ok){
      body.innerHTML = '<div class="conn-empty">Could not load connections.</div>';
      return;
    }
    const conns = (await connRes.json()).items || [];
    const allInv = (await invRes.json()).items || [];
    body.innerHTML = renderConnectionsBody(deviceId, conns, allInv);
  } catch(e){
    body.innerHTML = '<div class="conn-empty">Error: ' + escapeHtml(e.message) + '</div>';
  }
}

function renderConnectionsBody(deviceId, conns, allInv){
  const out = conns.filter(c => c.direction === 'out');
  const inb = conns.filter(c => c.direction === 'in');

  const typeIcon = {
    ethernet: '──',  // box drawing horizontal
    fiber:    '≈',        // wave (suggestive of light/optical)
    wifi:     '⦰',        // empty set / signal indicator
    virtual:  '◈',        // diamond (suggests "container/inside")
    power:    '⚡',        // lightning bolt
    usb:      '⇌',        // double arrow
    console:  '→',        // arrow
    other:    '·',
  };

  let html = '';

  if(out.length){
    html += '<div class="conn-group"><div class="conn-group-label">Plugged into</div>';
    out.forEach(c => {
      const icon = typeIcon[c.connection_type] || typeIcon.other;
      const portInfo = [];
      if(c.from_port) portInfo.push('via ' + escapeHtml(c.from_port));
      if(c.to_port)   portInfo.push('port ' + escapeHtml(c.to_port));
      const portStr = portInfo.length ? ' <span class="conn-port">(' + portInfo.join(', ') + ')</span>' : '';
      html += '<div class="conn-row" onclick="event.stopPropagation()">'
        + '<span class="conn-icon" title="' + escapeHtml(c.connection_type) + '">' + icon + '</span>'
        + '<span class="conn-target" onclick="openInventoryDrawer(' + c.to_device_id + ')">'
        + escapeHtml(c.to_name) + portStr + '</span>'
        + '<button class="conn-del" title="Remove connection" onclick="deleteConnection(' + c.id + ', ' + deviceId + ')">×</button>'
        + '</div>';
    });
    html += '</div>';
  }

  if(inb.length){
    html += '<div class="conn-group"><div class="conn-group-label">Things plugged into me</div>';
    inb.forEach(c => {
      const icon = typeIcon[c.connection_type] || typeIcon.other;
      const portInfo = [];
      if(c.to_port)   portInfo.push('port ' + escapeHtml(c.to_port));
      if(c.from_port) portInfo.push('via ' + escapeHtml(c.from_port));
      const portStr = portInfo.length ? ' <span class="conn-port">(' + portInfo.join(', ') + ')</span>' : '';
      html += '<div class="conn-row" onclick="event.stopPropagation()">'
        + '<span class="conn-icon" title="' + escapeHtml(c.connection_type) + '">' + icon + '</span>'
        + '<span class="conn-target" onclick="openInventoryDrawer(' + c.from_device_id + ')">'
        + escapeHtml(c.from_name) + portStr + '</span>'
        + '<button class="conn-del" title="Remove connection" onclick="deleteConnection(' + c.id + ', ' + deviceId + ')">×</button>'
        + '</div>';
    });
    html += '</div>';
  }

  if(!out.length && !inb.length){
    html += '<div class="conn-empty">No connections recorded yet.</div>';
  }

  // Inline add form
  html += '<div class="conn-add">';
  if(_connFormState.open && _connFormState.deviceId === deviceId){
    // Build the device dropdown excluding self
    const others = allInv.filter(i => i.id !== deviceId);
    others.sort((a, b) => (a.system || '').localeCompare(b.system || ''));
    const deviceOpts = '<option value="">-- pick a device --</option>'
      + others.map(i => {
          const dt = i.device_type || 'host';
          const label = i.system + ' (' + dt + ')';
          return '<option value="' + i.id + '">' + escapeHtml(label) + '</option>';
        }).join('');

    html += '<div class="conn-form">'
      + '<div class="conn-form-row">'
        + '<label>Connect to<select class="conn-f-target" onchange="onConnTargetChange(this.value)">' + deviceOpts + '</select></label>'
        + '<label>Type<select class="conn-f-type"><option value="ethernet">Ethernet</option><option value="fiber">Fiber</option><option value="wifi">WiFi</option><option value="virtual">Virtual (VM → host)</option><option value="power">Power</option><option value="usb">USB</option><option value="console">Console</option><option value="other">Other</option></select></label>'
      + '</div>'
      + '<div class="conn-form-row">'
        + '<label>From port (this device, optional)<input type="text" class="conn-f-from-port" placeholder="e.g. eth0, WAN"></label>'
        + '<label>To port (target device)<span class="conn-f-to-port-wrap"><input type="text" class="conn-f-to-port" placeholder="e.g. 4"></span></label>'
      + '</div>'
      + '<div class="conn-form-actions">'
        + '<button class="btn" onclick="submitConnection(' + deviceId + ')">Add connection</button>'
        + '<button class="btn btn-ghost" onclick="cancelConnection()">Cancel</button>'
      + '</div>'
    + '</div>';
  } else {
    html += '<button class="btn btn-ghost conn-add-btn" onclick="startAddConnection(' + deviceId + ')">+ Add connection</button>';
  }
  html += '</div>';

  return html;
}

function startAddConnection(deviceId){
  _connFormState = { open: true, deviceId: deviceId };
  loadInventoryConnections(deviceId);
}

function cancelConnection(){
  _connFormState = { open: false, deviceId: null };
  if(openDrawerIp && openDrawerIp.startsWith('inv:')){
    const id = parseInt(openDrawerIp.split(':')[1], 10);
    loadInventoryConnections(id);
  }
}

async function onConnTargetChange(targetId){
  // If the target is a network device with a port_count, swap the to_port
  // input for a numbered dropdown showing which ports are taken.
  const wrap = document.querySelector('.conn-f-to-port-wrap');
  if(!wrap) return;
  if(!targetId){
    wrap.innerHTML = '<input type="text" class="conn-f-to-port" placeholder="e.g. 4">';
    return;
  }
  try {
    const [recRes, connRes] = await Promise.all([
      fetch('/api/inventory/' + targetId),
      fetch('/api/inventory/' + targetId + '/connections'),
    ]);
    if(!recRes.ok){ return; }
    const rec = await recRes.json();
    const conns = connRes.ok ? (await connRes.json()).items : [];
    const props = rec.properties || {};
    const portCount = parseInt(props.port_count, 10);
    if(!portCount || portCount < 1 || portCount > 96){
      // Not a port-aware device: keep the free-text input
      wrap.innerHTML = '<input type="text" class="conn-f-to-port" placeholder="port (optional)">';
      return;
    }
    // Build map of port -> device using it
    const used = {};
    conns.filter(c => c.direction === 'in' && c.to_port).forEach(c => {
      used[c.to_port] = c.from_name;
    });
    let opts = '<option value="">-- select port --</option>';
    for(let i = 1; i <= portCount; i++){
      const u = used[String(i)];
      opts += '<option value="' + i + '"' + (u ? ' disabled' : '') + '>Port ' + i
            + (u ? ' (in use by ' + escapeHtml(u) + ')' : '')
            + '</option>';
    }
    wrap.innerHTML = '<select class="conn-f-to-port">' + opts + '</select>';
  } catch(e){
    /* fall back to text input - already there */
  }
}

async function submitConnection(deviceId){
  const targetEl = document.querySelector('.conn-f-target');
  const typeEl   = document.querySelector('.conn-f-type');
  const fpEl     = document.querySelector('.conn-f-from-port');
  const tpEl     = document.querySelector('.conn-f-to-port');
  if(!targetEl || !targetEl.value){
    toast('Please pick a device to connect to.', 'info');
    return;
  }
  const data = {
    to_device_id:    parseInt(targetEl.value, 10),
    connection_type: typeEl ? typeEl.value : 'ethernet',
    from_port:       fpEl ? fpEl.value.trim() : '',
    to_port:         tpEl ? (tpEl.value || '').trim() : '',
  };
  try {
    const res = await fetch('/api/inventory/' + deviceId + '/connections', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data),
    });
    if(!res.ok){
      let msg = 'Failed (HTTP ' + res.status + ')';
      try { const j = await res.json(); if(j.error) msg = j.error; } catch(e){}
      toast('Could not add connection: ' + msg, 'error');
      return;
    }
    _connFormState = { open: false, deviceId: null };
    loadInventoryConnections(deviceId);
  } catch(e){
    toast('Network error: ' + e.message, 'error');
  }
}

async function deleteConnection(connId, deviceId){
  if(!confirm('Remove this connection?')) return;
  try {
    const res = await fetch('/api/connections/' + connId + '/delete', {method: 'POST'});
    if(!res.ok){
      let msg = 'Failed (HTTP ' + res.status + ')';
      try { const j = await res.json(); if(j.error) msg = j.error; } catch(e){}
      toast('Could not delete: ' + msg, 'error');
      return;
    }
    loadInventoryConnections(deviceId);
  } catch(e){
    toast('Network error: ' + e.message, 'error');
  }
}

async function fetchHostInventoryLink(h){
  const slot = document.getElementById('d-inv-section');
  if(!slot) return;
  const mac = h && h.specs && h.specs.mac;
  if(!mac){ slot.innerHTML = ''; return; }
  try {
    const res = await fetch('/api/inventory');
    if(!res.ok){ slot.innerHTML = ''; return; }
    const data = await res.json();
    // Match by MAC normalised
    const target = (mac || '').replace(/[^0-9a-f]/gi, '').toLowerCase();
    const rec = (data.items || []).find(i => {
      const m = (i.mac || '').replace(/[^0-9a-f]/gi, '').toLowerCase();
      return m && m === target;
    });
    if(!rec){ slot.innerHTML = ''; return; }
    const sysSafe = escapeHtml(rec.system || '');
    const catSafe = rec.category ? '<span class="inv-cat-tag">' + escapeHtml(rec.category) + '</span> ' : '';
    const roleSafe = rec.role ? '<div style="font-size:12px;color:var(--muted);margin-top:3px">' + escapeHtml(rec.role) + '</div>' : '';
    slot.innerHTML = '<div class="d-section"><div class="d-section-hdr"><span>Inventory record</span></div>'
      + '<div class="inv-link-host-card" onclick="openInventoryDrawer(' + rec.id + ')">'
      + '<div><div style="font-weight:500">' + sysSafe + '</div>'
      + '<div style="margin-top:3px">' + catSafe + '</div>'
      + roleSafe + '</div>'
      + '<span class="d-link-arrow" style="color:var(--hint);font-family:DM Mono,monospace">→</span>'
      + '</div></div>';
  } catch(e){ slot.innerHTML = ''; }
}

function openHostDrawerByIp(ip){
  // openDrawer takes an IP and looks up the host in lastData itself
  closeDrawer();
  openDrawer(ip);
}

function navigateToHostDrawer(ip){
  // Switch to the Hosts tab (if not already there) and open the host
  // drawer for the given IP. This gives the user a coherent landing
  // experience: tab matches drawer content.
  closeDrawer();  // closes the inventory drawer (same #drawer element)
  if(typeof setTab === 'function'){
    const currentTab = localStorage.getItem('nw-tab');
    if(currentTab !== 'hosts'){
      setTab('hosts');
    }
  }
  // Small delay lets the tab switch settle before opening the drawer.
  // Without it, the drawer can render before the tab transition completes
  // and look like it's appearing in the wrong place.
  setTimeout(() => { openDrawer(ip); }, 80);
}

// Wrapper that adds basic diagnostic logging if the drawer fails to open.
// Used by inline onclick handlers (the inventory drawer's linked-host card
// in particular). The original navigateToHostDrawer is preserved for any
// other call sites and works the same way.
function navigateToHostDrawerSafe(ip){
  if(!lastData || !lastData.hosts){
    console.warn('[netwatch] navigate to host drawer: no host data loaded yet');
    return;
  }
  const exists = lastData.hosts.some(h => h.ip === ip);
  if(!exists){
    console.warn('[netwatch] navigate to host drawer: ip not in monitored hosts list:', ip);
    // Soft failure UX: flash a status into the inventory drawer if it's open
    const status = document.getElementById('inv-link-action-status');
    if(status){
      status.className = 'inv-link-action-status error';
      status.textContent = ip + ' is not currently in the monitored hosts list.';
      setTimeout(() => {
        if(status) { status.className = 'inv-link-action-status'; status.textContent = ''; }
      }, 4000);
    }
    return;
  }
  navigateToHostDrawer(ip);
}

async function sendWakeFromInventory(ip, btn){
  // Wake-on-LAN from the inventory drawer's linked-host card. Same
  // backend endpoint as the host-drawer's Wake button, just with
  // inline status feedback in the inventory drawer instead.
  if(!_authState.logged_in){
    if(_authState.setup_required) openSetup();
    else openLogin(() => sendWakeFromInventory(ip, btn));
    return;
  }
  const status = document.getElementById('inv-link-action-status');
  if(btn) btn.disabled = true;
  if(status){
    status.className = 'inv-link-action-status';
    status.textContent = 'Sending magic packet...';
  }
  try {
    const res = await fetch('/api/wake', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ip })
    });
    const data = await res.json();
    if(!res.ok){
      if(status){
        status.className = 'inv-link-action-status error';
        status.textContent = data.error || 'Wake failed';
      }
    } else {
      if(status){
        status.className = 'inv-link-action-status success';
        status.textContent = 'Magic packet sent at ' + new Date().toLocaleTimeString();
      }
    }
  } catch(e){
    if(status){
      status.className = 'inv-link-action-status error';
      status.textContent = 'Network error';
    }
  } finally {
    if(btn) btn.disabled = false;
  }
}

// Import modal
function openImportModal(){
  if(!_authState.logged_in){
    if(_authState.setup_required) openSetup();
    else openLogin(() => openImportModal());
    return;
  }
  document.getElementById('import-overlay').classList.add('open');
  document.getElementById('import-result').style.display = 'none';
  document.getElementById('import-file').value = '';
  document.getElementById('import-submit-btn').disabled = false;
  document.getElementById('import-submit-btn').textContent = 'Upload';
}
function closeImportModal(){
  document.getElementById('import-overlay').classList.remove('open');
}
function toggleExportMenu(e){
  e.stopPropagation();
  document.getElementById('inv-export-menu').classList.toggle('open');
}
function closeExportMenu(){
  const m = document.getElementById('inv-export-menu');
  if(m) m.classList.remove('open');
}
document.addEventListener('click', function(e){
  if(!e.target.closest('.inv-export-split')) closeExportMenu();
});
async function downloadInventoryExport(btn, scope){
  scope = scope || 'hosts';
  // Admin-only, so check auth first to give a friendly error
  if(!_authState.logged_in){
    if(_authState.setup_required) openSetup();
    else openLogin(() => downloadInventoryExport(btn));
    return;
  }
  if(!_authState.admin){
    toast("Inventory export requires admin access.", 'info');
    return;
  }
  const origText = btn ? btn.textContent : null;
  if(btn){ btn.disabled = true; btn.textContent = "Building..."; }
  try {
    const res = await fetch("/api/inventory-export?scope=" + scope);
    if(!res.ok){
      let msg = "Export failed (HTTP " + res.status + ")";
      try { const j = await res.json(); if(j.error) msg = j.error; } catch(e){}
      toast(msg, 'error');
      return;
    }
    const blob = await res.blob();
    const dispo = res.headers.get("Content-Disposition") || "";
    const m = dispo.match(/filename="?([^";]+)"?/);
    const filename = m ? m[1] : "netwatch-inventory.xlsx";
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch(e){
    toast("Export failed: " + e.message, 'error');
  } finally {
    if(btn){ btn.disabled = false; btn.textContent = origText || "Export XLSX"; }
  }
}

async function submitImport(){
  const fileInput = document.getElementById('import-file');
  if(!fileInput.files.length){
    showImportResult('err', 'Please select an XLSX file first.');
    return;
  }
  const file = fileInput.files[0];
  const mode = document.querySelector('input[name="import-mode"]:checked').value;
  const btn = document.getElementById('import-submit-btn');
  btn.disabled = true; btn.textContent = 'Uploading...';
  const fd = new FormData();
  fd.append('file', file);
  fd.append('mode', mode);
  try {
    const res = await fetch('/api/inventory-import', { method: 'POST', body: fd });
    const data = await res.json();
    if(!res.ok){
      showImportResult('err', data.error || 'Import failed');
      btn.disabled = false; btn.textContent = 'Upload';
      return;
    }
    let msg = 'Added ' + data.added + ' record' + (data.added === 1 ? '' : 's');
    if(data.skipped > 0) msg += ', skipped ' + data.skipped + ' duplicate' + (data.skipped === 1 ? '' : 's');
    if(data.errors && data.errors.length){
      msg += '. ' + data.errors.length + ' error(s): ' + data.errors.slice(0,3).map(e => e.system + ': ' + e.error).join('; ');
    }
    showImportResult('ok', msg);
    btn.textContent = 'Done';
    await fetchInventory();
    setTimeout(closeImportModal, 2500);
  } catch(e){
    showImportResult('err', 'Network error during import');
    btn.disabled = false; btn.textContent = 'Upload';
  }
}
function showImportResult(cls, msg){
  const el = document.getElementById('import-result');
  el.className = 'import-result ' + cls;
  el.textContent = msg;
  el.style.display = '';
}

