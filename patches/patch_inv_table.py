#!/usr/bin/env python3
"""
netwatch patch: per-type inventory tables + chip filter fix.

Two related changes:

1. Bug fix: type filter chips (Hosts / Network / UPS / Disks / Peripherals)
   weren't firing when clicked. The onclick attribute was being built with
   JSON.stringify(t) which produced literal double quotes that clashed with
   the attribute's outer quotes, so the click handler never ran. Fix: HTML-
   escape the same way category chips already do.

2. Layout improvement: instead of one table that forces every type into
   shared CPU / RAM / OS columns (so a TV's model number ended up where
   RAM should be), each type now gets its own table with appropriate
   columns:

     Hosts:       System | Category | CPU | RAM | OS | Status
     Network:     System | Category | Ports | PoE | Uplink | Managed | Status
     UPS:         System | Category | Capacity | Runtime | Battery age | Status
     Disk:        System | Category | Capacity | Interface | RPM | Used in | Status
     Peripheral:  System | Category | Subtype | Model | Status

   When viewing "All" types, all tables stack vertically with section
   headings showing each type's count. When a type filter chip is active,
   only that one table shows. Empty types are hidden.

   Sort state is now per-type so sorting hosts by RAM doesn't try to apply
   the same sort to peripherals.

Must be applied AFTER patch_inv_types.py.

Run once from ~/netwatch/:
    python3 patch_inv_per_type_tables.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_pertype.
Idempotent - safe to re-run.
"""

import os, shutil, sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_pertype"
SENTINEL = "renderInventoryTablesByType"


PATCHES = [
    # ──────────────────────────────────────────────────────────────────────
    # 1. Fix the chip filter bug. The HTML-escape pattern matches the
    # category-chip code that already works.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  typeOrder.forEach(t => {
    if(!typeCounts[t]) return;  // hide empty types
    typeChips.push('<span class="inv-chip inv-chip-type ' + (_inventoryFilter.deviceType === t ? 'active' : '') + '" onclick="setInvTypeFilter(' + JSON.stringify(t) + ')">' + typeLabels[t] + '<span class="inv-chip-count">' + typeCounts[t] + '</span></span>');
  });''',
        '''  typeOrder.forEach(t => {
    if(!typeCounts[t]) return;  // hide empty types
    // HTML-escape the JSON string so the inline onclick attribute parses
    // correctly. Without this, JSON.stringify produces "host" with literal
    // quotes that clash with the surrounding attribute quotes.
    const safeArg = JSON.stringify(t).replace(/"/g, '&quot;');
    typeChips.push('<span class="inv-chip inv-chip-type ' + (_inventoryFilter.deviceType === t ? 'active' : '') + '" onclick="setInvTypeFilter(' + safeArg + ')">' + typeLabels[t] + '<span class="inv-chip-count">' + typeCounts[t] + '</span></span>');
  });'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 2. Replace the fixed table HTML in the view pane with a single
    # container. Multiple per-type tables get rendered into it by JS.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''    <div class="inv-table-wrap">
      <table class="inv-table">
        <thead>
          <tr>
            <th class="inv-th" data-sort="system">System</th>
            <th class="inv-th" data-sort="category">Category</th>
            <th class="inv-th" data-sort="cpu">CPU</th>
            <th class="inv-th" data-sort="ram_gb">RAM</th>
            <th class="inv-th" data-sort="os">OS</th>
            <th class="inv-th" data-sort="linked">Status</th>
          </tr>
        </thead>
        <tbody id="inv-rows"></tbody>
      </table>
    </div>''',
        '''    <div id="inv-tables"></div>'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 3. Replace the global single-table sort state with a per-type map.
    # Old _inventorySort was a single object; new is keyed by device_type.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''let _inventorySort = { col: 'system', dir: 'asc' };''',
        '''// Per-type sort state. Each type gets its own column/direction so sorting
// hosts by RAM doesn't try to also sort peripherals by a column they lack.
let _inventorySort = {
  host:       { col: 'system', dir: 'asc' },
  network:    { col: 'system', dir: 'asc' },
  ups:        { col: 'system', dir: 'asc' },
  disk:       { col: 'system', dir: 'asc' },
  peripheral: { col: 'system', dir: 'asc' },
};'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 4. Replace renderInventoryRows entirely with the per-type renderer.
    # The old function had ~100 lines (filter + sort + tbody.innerHTML build);
    # we need to replace ALL of it, not just the filter section, otherwise
    # the orphan tail leaves the JS broken with stray }).join('');\n}.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''function renderInventoryRows(){
  // Apply filter + search
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
        || (i.gpu || '').toLowerCase().includes(q);
    });
  }

  // Sort
  const { col, dir } = _inventorySort;
  rows.sort((a, b) => {
    let av, bv;
    if(col === 'linked'){
      av = a.linked_host ? (a.linked_host.is_up ? 2 : 1) : 0;
      bv = b.linked_host ? (b.linked_host.is_up ? 2 : 1) : 0;
    } else {
      av = a[col]; bv = b[col];
      if(av === null || av === undefined) av = '';
      if(bv === null || bv === undefined) bv = '';
    }
    let cmp;
    if(typeof av === 'number' && typeof bv === 'number') cmp = av - bv;
    else cmp = String(av).toLowerCase().localeCompare(String(bv).toLowerCase());
    return dir === 'asc' ? cmp : -cmp;
  });

  document.querySelectorAll('.inv-th').forEach(th => {
    th.classList.remove('sort-asc', 'sort-desc');
    if(th.dataset.sort === col) th.classList.add(dir === 'asc' ? 'sort-asc' : 'sort-desc');
  });

  const tbody = document.getElementById('inv-rows');
  document.getElementById('inv-empty').style.display = rows.length === 0 ? '' : 'none';
  tbody.innerHTML = rows.map(i => {
    const dtype = i.device_type || 'host';
    const ramStr = i.ram_gb !== null && i.ram_gb !== undefined
      ? (i.ram_gb < 1 ? (i.ram_gb * 1024).toFixed(0) + ' MB' : i.ram_gb + ' GB')
      : '-';
    let linkPill = '<span class="inv-link-pill none">no link</span>';
    if(i.linked_host){
      const lh = i.linked_host;
      const cls = lh.status === 'DEGRADED' ? 'degraded' : lh.is_up ? 'up' : (lh.status === 'IDLE' ? 'idle' : 'down');
      linkPill = '<span class="inv-link-pill ' + cls + '">' + escapeHtml(lh.status) + '</span>';
    }
    // Type-aware spec cells
    let specCell, ramCell, osCell;
    const props = i.properties || {};
    if(dtype === 'host'){
      specCell = '<td><div>' + escapeHtml(i.cpu || '-') + '</div>'
        + (i.architecture ? '<div class="inv-mono">' + escapeHtml(i.architecture) + '</div>' : '') + '</td>';
      ramCell  = '<td class="inv-mono">' + ramStr + '</td>';
      osCell   = '<td>' + escapeHtml(i.os || '-') + '</td>';
    } else if(dtype === 'network'){
      const ports = props.port_count ? (props.port_count + ' ports') : '-';
      const poe   = props.poe_watts ? (props.poe_watts + 'W PoE') : '';
      specCell = '<td><div>' + escapeHtml(ports) + '</div>'
        + (poe ? '<div class="inv-mono">' + escapeHtml(poe) + '</div>' : '') + '</td>';
      ramCell  = '<td class="inv-mono">' + escapeHtml(props.uplink_speed || '') + '</td>';
      osCell   = '<td>' + (props.managed ? 'Managed' : 'Unmanaged') + '</td>';
    } else if(dtype === 'ups'){
      const va = props.capacity_va ? (props.capacity_va + ' VA') : '-';
      const wh = props.capacity_wh ? (props.capacity_wh + ' Wh') : '';
      specCell = '<td><div>' + escapeHtml(va) + '</div>'
        + (wh ? '<div class="inv-mono">' + escapeHtml(wh) + '</div>' : '') + '</td>';
      ramCell  = '<td class="inv-mono">' + (props.runtime_min ? props.runtime_min + ' min' : '') + '</td>';
      osCell   = '<td>' + escapeHtml(props.battery_age_years || '') + '</td>';
    } else if(dtype === 'disk'){
      const cap = props.capacity_gb
        ? (props.capacity_gb >= 1000 ? (props.capacity_gb/1000).toFixed(1) + ' TB' : props.capacity_gb + ' GB')
        : '-';
      specCell = '<td><div>' + escapeHtml(cap) + '</div>'
        + (props.interface ? '<div class="inv-mono">' + escapeHtml(props.interface) + '</div>' : '') + '</td>';
      ramCell  = '<td class="inv-mono">' + (props.rpm ? props.rpm.toLocaleString() + ' RPM' : 'SSD') + '</td>';
      osCell   = '<td>' + escapeHtml(props.used_in || '') + '</td>';
    } else {
      // peripheral / fallback
      specCell = '<td>' + escapeHtml(props.subtype || '-') + '</td>';
      ramCell  = '<td class="inv-mono">' + escapeHtml(props.model || '') + '</td>';
      osCell   = '<td></td>';
    }
    return '<tr class="inv-row" onclick="openInventoryDrawer(' + i.id + ')">'
      + '<td><div class="inv-system">' + escapeHtml(i.system || '') + '</div>'
      + (i.role ? '<div class="inv-role">' + escapeHtml(i.role) + '</div>' : '') + '</td>'
      + '<td><span class="inv-cat-tag">' + escapeHtml(i.category || '-') + '</span></td>'
      + specCell + ramCell + osCell
      + '<td>' + linkPill + '</td>'
      + '</tr>';
  }).join('');
}''',
        '''function renderInventoryRows(){
  return renderInventoryTablesByType();
}

// Column definitions per type. Keys map to the inventory record\'s top-level
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
};

const INV_TYPE_LABELS = {
  host: 'Hosts', network: 'Network', ups: 'UPS',
  disk: 'Disks', peripheral: 'Peripherals'
};
const INV_TYPE_ORDER = ['host', 'network', 'ups', 'disk', 'peripheral'];

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
    if(!rec.linked_host) return '<span class="inv-link-pill none">no link</span>';
    const lh = rec.linked_host;
    const cls = lh.status === 'DEGRADED' ? 'degraded'
              : lh.is_up ? 'up'
              : (lh.status === 'IDLE' ? 'idle' : 'down');
    return '<span class="inv-link-pill ' + cls + '">' + escapeHtml(lh.status) + '</span>';
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
    if(emptyEl) emptyEl.style.display = '';
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
    out += '<tr class="inv-row" onclick="openInventoryDrawer(' + rec.id + ')">';
    cols.forEach(c => {
      out += '<td>' + formatInvCell(rec, c.key) + '</td>';
    });
    out += '</tr>';
  });
  out += '</tbody></table></div>';
  return out;
}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 5. Update the sort click handler to read data-type and update the
    # right per-type sort state.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''// Wire up sort headers + search
document.addEventListener('click', e => {
  const th = e.target.closest('.inv-th');
  if(!th) return;
  const col = th.dataset.sort;
  if(_inventorySort.col === col){
    _inventorySort.dir = _inventorySort.dir === 'asc' ? 'desc' : 'asc';
  } else {
    _inventorySort.col = col;
    _inventorySort.dir = 'asc';
  }
  renderInventoryRows();
});''',
        '''// Wire up sort headers + search. Each table has its own sort state per type;
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
});'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 6. CSS for type section headings + spacing between stacked tables.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''.inv-empty{padding:32px 20px;text-align:center;color:var(--muted);font-size:13px;background:var(--subtle);border:1px dashed var(--border);border-radius:8px}''',
        '''.inv-empty{padding:32px 20px;text-align:center;color:var(--muted);font-size:13px;background:var(--subtle);border:1px dashed var(--border);border-radius:8px}
#inv-tables > .inv-table-wrap + .inv-type-heading{margin-top:18px}
.inv-type-heading{display:flex;align-items:center;gap:9px;padding:0 4px 8px 4px;margin-top:4px}
.inv-type-heading-label{font-family:'DM Mono',monospace;font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:var(--text);font-weight:600}
.inv-type-heading-count{font-family:'DM Mono',monospace;font-size:10px;color:var(--hint);background:var(--subtle);padding:2px 8px;border-radius:10px;border:1px solid var(--border-light)}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 7. Bump version
    # ──────────────────────────────────────────────────────────────────────
    (
        'netwatch v3.16 - raspberry pi',
        'netwatch v3.17 - raspberry pi'
    ),
]


def main():
    if not os.path.isfile(TARGET):
        print(f"ERROR: {TARGET} not found.")
        sys.exit(1)

    content = open(TARGET).read()

    if SENTINEL in content:
        print(f"NOTE: '{SENTINEL}' found - patch already applied.")
        sys.exit(0)

    if "INVENTORY_TYPE_PROPERTIES" not in content:
        print("ERROR: This patch requires patch_inv_types first.")
        sys.exit(1)

    shutil.copy2(TARGET, BACKUP)
    print(f"[OK] Backed up {TARGET} -> {BACKUP}")

    applied = 0
    for i, (old, new) in enumerate(PATCHES, 1):
        count = content.count(old)
        if count == 0:
            print(f"[FAIL] Patch #{i}: target not found")
            shutil.copy2(BACKUP, TARGET); sys.exit(1)
        if count > 1:
            print(f"[FAIL] Patch #{i}: matches {count}x")
            shutil.copy2(BACKUP, TARGET); sys.exit(1)
        content = content.replace(old, new, 1)
        applied += 1

    if 'VERSION = "3.16"' in content:
        content = content.replace('VERSION = "3.16"', 'VERSION = "3.17"', 1)

    open(TARGET, "w").write(content)

    import ast
    try:
        ast.parse(open(TARGET).read())
        print("[OK] Resulting Python is valid")
    except SyntaxError as e:
        print(f"[FAIL] Syntax error: {e}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)

    print(f"[OK] Applied {applied} patches")
    print()
    print("Next steps:")
    print("  1. sudo systemctl restart netwatch")
    print("  2. Open Inventory tab. With 'All' selected, you'll see separate")
    print("     stacked tables for Hosts, Network, UPS, Disks, Peripherals -")
    print("     each with type-appropriate columns and headers.")
    print("  3. Click a type chip - that table shows alone (no heading needed).")
    print("  4. Click a column header - sorts within that table only;")
    print("     other types keep their own sort.")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
