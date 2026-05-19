# Inventory Polish & Add Host FAB — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Polish the inventory tab (icons, filters, empty states, status pills) and add a floating "Add host" button with a dedicated single-record modal.

**Architecture:** All changes are in `dashboard.html` — the single-file dashboard served by `monitor.py`. The file has a `<style>` block (~line 1–1000), HTML body (~1000–1680), and a `<script>` block (~1677–5300). The Python test suite covers the server, not the JS; verify JS changes manually in the browser. Run `python -m pytest tests/ -q` after each task to confirm nothing regressed on the server side.

**Tech Stack:** Vanilla HTML/CSS/JS. No build step, no framework. Existing SVG sprite sheets: `#icon-*` (thin-stroke, viewBox 0 0 24 24) and `#topo-icon-*` (dimensional, viewBox 0 0 32 32).

---

## File Structure

**Modified:** `dashboard.html` only.

Key landmarks (line numbers approximate — search by string if off by a few lines):
- CSS block: lines 1–1000
- `deviceIcon()` function: ~line 1727
- Host list `deviceIcon` call: ~line 1790
- HTML inventory section: ~lines 1454–1467
- FAB + Add Host modal insertion point: after `#discover-overlay` (~line 1674)
- `_inventoryFilter` declaration: ~line 4002
- `renderInventoryChips()`: ~line 4062
- `formatInvCell()` linked block: ~line 4231
- `renderInventoryTablesByType()`: ~line 4283
- `renderTypeTable()` heading block: ~line 4348
- Inventory table `deviceIcon` call: ~line 4371

---

## Task 1: Replace `deviceIcon()` with dimensional topo icons

**Files:**
- Modify: `dashboard.html` — `deviceIcon()` function (~line 1727) + two call sites

- [ ] **Step 1: Update `deviceIcon()` to use `#topo-icon-*` sprites**

Find the function at ~line 1727:
```js
function deviceIcon(type, size){
  const id = 'icon-' + (type || 'host');
  return '<svg width="' + size + '" height="' + size + '" viewBox="0 0 24 24" fill="none"'
    + ' stroke="currentColor" stroke-width="1.75" stroke-linecap="round"'
    + ' stroke-linejoin="round" style="vertical-align:middle;flex-shrink:0"'
    + ' aria-hidden="true"><use href="#' + id + '"/></svg>';
}
```

Replace the entire function with:
```js
function deviceIcon(type, size){
  return '<svg width="' + size + '" height="' + size + '" viewBox="0 0 32 32"'
    + ' style="vertical-align:middle;flex-shrink:0" aria-hidden="true">'
    + '<use href="#topo-icon-' + (type || 'host') + '"/></svg>';
}
```

- [ ] **Step 2: Update the host list call site from 13px to 18px**

Find at ~line 1790:
```js
    + deviceIcon(h.device_type, 13)
```

Change to:
```js
    + deviceIcon(h.device_type, 18)
```

- [ ] **Step 3: Update the inventory table call site from 14px to 18px**

Find at ~line 4371:
```js
          + deviceIcon(rec.device_type, 14)
```

Change to:
```js
          + deviceIcon(rec.device_type, 18)
```

- [ ] **Step 4: Verify**

Run `python -m pytest tests/ -q` — all 11 tests should pass.

Open the dashboard in a browser. Check the Hosts tab — each host row should show the dimensional chassis icon instead of the thin-stroke one. Check the Inventory tab (if you have records) — same icon style in the first column. Both should be 18px.

- [ ] **Step 5: Commit**

```bash
git add dashboard.html
git commit -m "feat: replace thin-stroke device icons with dimensional topo icons"
```

---

## Task 2: Status pill polish in inventory table

**Files:**
- Modify: `dashboard.html` — `formatInvCell()` linked block (~line 4231) + `.inv-link-pill` CSS (~line 739)

- [ ] **Step 1: Update `formatInvCell` linked block**

Find at ~line 4231:
```js
  if(key === 'linked'){
    if(!rec.linked_host) return '<span class="inv-link-pill none">no link</span>';
    const lh = rec.linked_host;
    const cls = lh.status === 'DEGRADED' ? 'degraded'
              : lh.is_up ? 'up'
              : (lh.status === 'IDLE' ? 'idle' : 'down');
    return '<span class="inv-link-pill ' + cls + '">' + escapeHtml(lh.status) + '</span>';
  }
```

Replace with:
```js
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
```

- [ ] **Step 2: Update `.inv-link-pill` idle style to match the green/red/amber pattern**

Find the `.inv-link-pill.idle` rule at ~line 742:
```css
.inv-link-pill.idle{background:var(--subtle);color:var(--text);border:1px solid var(--border-light);font-weight:500}
```

Replace with:
```css
.inv-link-pill.idle{background:var(--subtle);color:var(--muted);border:1px solid var(--border-light)}
```

- [ ] **Step 3: Verify**

Run `python -m pytest tests/ -q`.

Open the Inventory tab. The Status column should now show "Up" (green), "Down" (red), "Degraded" (amber), "Idle" (muted), or italic grey "no link" — all title-case, no more all-caps.

- [ ] **Step 4: Commit**

```bash
git add dashboard.html
git commit -m "feat: inventory status pills are title-case with polished no-link style"
```

---

## Task 3: Type heading icons

**Files:**
- Modify: `dashboard.html` — `renderTypeTable()` heading block (~line 4348)

- [ ] **Step 1: Add the icon to the type heading HTML**

Find at ~line 4348:
```js
    out += '<div class="inv-type-heading">'
      + '<span class="inv-type-heading-label">' + escapeHtml(INV_TYPE_LABELS[deviceType]) + '</span>'
      + '<span class="inv-type-heading-count">' + rows.length + '</span>'
      + '</div>';
```

Replace with:
```js
    out += '<div class="inv-type-heading">'
      + '<svg width="16" height="16" viewBox="0 0 32 32" style="flex-shrink:0" aria-hidden="true"><use href="#topo-icon-' + deviceType + '"/></svg>'
      + '<span class="inv-type-heading-label">' + escapeHtml(INV_TYPE_LABELS[deviceType]) + '</span>'
      + '<span class="inv-type-heading-count">' + rows.length + '</span>'
      + '</div>';
```

- [ ] **Step 2: Verify**

Run `python -m pytest tests/ -q`.

Open the Inventory tab with "All" device type selected and multiple device types in your inventory. The section headings (e.g. "HOSTS", "VMS") should each show a small dimensional icon to their left.

- [ ] **Step 3: Commit**

```bash
git add dashboard.html
git commit -m "feat: inventory type headings show device icon"
```

---

## Task 4: Empty state — smarter messaging

**Files:**
- Modify: `dashboard.html` — `renderInventoryTablesByType()` empty-state block (~line 4311) + `_inventoryFilter` check

- [ ] **Step 1: Update the empty-state block in `renderInventoryTablesByType`**

Find at ~line 4283, the full `renderInventoryTablesByType` function. Locate the empty-state block at ~line 4311:
```js
  // Empty-state
  const container = document.getElementById('inv-tables');
  const emptyEl = document.getElementById('inv-empty');
  if(rows.length === 0){
    container.innerHTML = '';
    if(emptyEl) emptyEl.style.display = '';
    return;
  }
  if(emptyEl) emptyEl.style.display = 'none';
```

Replace with:
```js
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
```

- [ ] **Step 2: Update `.inv-empty` CSS to match the events empty state style**

Find at ~line 745:
```css
.inv-empty{padding:32px 20px;text-align:center;color:var(--muted);font-size:13px;background:var(--subtle);border:1px dashed var(--border);border-radius:8px}
```

Replace with:
```css
.inv-empty{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:48px 20px;text-align:center}
```

- [ ] **Step 3: Remove the static content from `#inv-empty` in the HTML**

Find at ~line 1464:
```html
    <div id="inv-empty" class="inv-empty" style="display:none">
      No inventory yet. Click "Import XLSX" to bootstrap from a spreadsheet, or "+ Add" to create records one at a time.
    </div>
```

Replace with:
```html
    <div id="inv-empty" class="inv-empty" style="display:none"></div>
```

- [ ] **Step 4: Verify**

Run `python -m pytest tests/ -q`.

Open the Inventory tab. If you have no records: you should see the "No inventory yet" card with a `+` icon. If you have records, type something in the search box that matches nothing — you should see "No results" with the `⊘` icon and a different subtitle.

- [ ] **Step 5: Commit**

```bash
git add dashboard.html
git commit -m "feat: inventory empty state distinguishes no-records from filter-no-match"
```

---

## Task 5: Status filter chips

**Files:**
- Modify: `dashboard.html` — `_inventoryFilter` declaration (~line 4002), `renderInventoryChips()` (~line 4062), `renderInventoryTablesByType()` (~line 4283), `setInvTypeFilter()` (~line 4103), `setInvCategoryFilter()` (~line 4111)

- [ ] **Step 1: Add `status` to `_inventoryFilter`**

Find at ~line 4002:
```js
let _inventoryFilter = { category: null, deviceType: null, search: '' };
```

Replace with:
```js
let _inventoryFilter = { category: null, deviceType: null, search: '', status: null };
```

- [ ] **Step 2: Add `setInvStatusFilter` function**

After `setInvCategoryFilter` (~line 4111), add:
```js
function setInvStatusFilter(s){
  _inventoryFilter.status = s;
  renderInventoryChips();
  renderInventoryRows();
}
```

- [ ] **Step 3: Add status chip row to `renderInventoryChips`**

Find the top of `renderInventoryChips` at ~line 4062. The function currently builds `typeChips` and `catChips` and sets `inv-chips` innerHTML. Add a `statusChips` row above typeChips. Replace the entire `document.getElementById('inv-chips').innerHTML = ...` block (the last lines of `renderInventoryChips`) with:

```js
  const STATUS_OPTS = [
    {val: null,        label: 'All'},
    {val: 'up',        label: 'Up'},
    {val: 'down',      label: 'Down'},
    {val: 'unlinked',  label: 'Unlinked'},
  ];
  const statusChips = STATUS_OPTS.map(o =>
    '<span class="inv-chip ' + (_inventoryFilter.status === o.val ? 'active' : '') + '"'
    + ' onclick="setInvStatusFilter(' + (o.val === null ? 'null' : JSON.stringify(o.val)) + ')">'
    + escapeHtml(o.label) + '</span>'
  );
  document.getElementById('inv-chips').innerHTML =
    '<div class="inv-chip-row">' + statusChips.join('') + '</div>'
    + '<div class="inv-chip-row inv-chip-row-types">' + typeChips.join('') + '</div>'
    + (catChips.length > 1 ? '<div class="inv-chip-row inv-chip-row-cats">' + catChips.join('') + '</div>' : '');
```

- [ ] **Step 4: Apply status filter in `renderInventoryTablesByType`**

In `renderInventoryTablesByType`, add status filtering right after the existing `_inventoryFilter.search` block (~line 4302):

```js
  if(_inventoryFilter.status === 'up'){
    rows = rows.filter(i => i.linked_host && i.linked_host.is_up && i.linked_host.status !== 'DEGRADED' && i.linked_host.status !== 'WAIT');
  } else if(_inventoryFilter.status === 'down'){
    rows = rows.filter(i => i.linked_host && (!i.linked_host.is_up || i.linked_host.status === 'DEGRADED' || i.linked_host.status === 'WAIT'));
  } else if(_inventoryFilter.status === 'unlinked'){
    rows = rows.filter(i => !i.linked_host);
  }
```

- [ ] **Step 5: Reset status filter when type changes**

In `setInvTypeFilter` (~line 4103):
```js
function setInvTypeFilter(t){
  _inventoryFilter.deviceType = t;
  // Reset category filter when type changes - keeps things simple
  _inventoryFilter.category = null;
  renderInventoryChips();
  renderInventoryRows();
}
```

No change needed here — status filter is intentionally preserved across type changes.

- [ ] **Step 6: Verify**

Run `python -m pytest tests/ -q`.

Open the Inventory tab. You should see a new row of chips above the type chips: "All · Up · Down · Unlinked". Click "Unlinked" — only inventory records with no linked host should appear. Click "Up" — only records whose linked host is currently up. Click "All" to reset.

- [ ] **Step 7: Commit**

```bash
git add dashboard.html
git commit -m "feat: inventory status filter chips (All / Up / Down / Unlinked)"
```

---

## Task 6: Chip counts respect search filter

**Files:**
- Modify: `dashboard.html` — `renderInventoryChips()` (~line 4062)

- [ ] **Step 1: Apply search filter before calculating chip counts**

Find `renderInventoryChips` at ~line 4062. Currently it starts:
```js
function renderInventoryChips(){
  const typeCounts = {};
  _inventoryData.forEach(i => {
```

The counts need to be calculated from the search-filtered dataset. Find the top of `renderInventoryChips` and replace it so search is applied first:

```js
function renderInventoryChips(){
  // Calculate counts from the search-filtered set so chips reflect what's visible
  let searchFiltered = _inventoryData;
  if(_inventoryFilter.search){
    const q = _inventoryFilter.search.toLowerCase();
    searchFiltered = _inventoryData.filter(i =>
      (i.system || '').toLowerCase().includes(q)
      || (i.role || '').toLowerCase().includes(q)
      || (i.os || '').toLowerCase().includes(q)
      || (i.cpu || '').toLowerCase().includes(q)
      || (i.gpu || '').toLowerCase().includes(q)
      || JSON.stringify(i.properties || {}).toLowerCase().includes(q)
    );
  }
  const typeCounts = {};
  searchFiltered.forEach(i => {
```

Also update the `All` chip count and the `scope` variable (used for category chip counts) to use `searchFiltered` instead of `_inventoryData`:

Find:
```js
  const typeChips = ['<span class="inv-chip inv-chip-type ' + (_inventoryFilter.deviceType === null ? 'active' : '') + '" onclick="setInvTypeFilter(null)">All<span class="inv-chip-count">' + _inventoryData.length + '</span></span>'];
```
Replace `_inventoryData.length` with `searchFiltered.length`.

Find:
```js
  let scope = _inventoryData;
  if(_inventoryFilter.deviceType !== null){
    scope = scope.filter(i => (i.device_type || 'host') === _inventoryFilter.deviceType);
```
Replace `_inventoryData` with `searchFiltered`.

- [ ] **Step 2: Verify**

Run `python -m pytest tests/ -q`.

Open the Inventory tab with multiple device types. Type a search term that only matches some records. The type chip counts should update to reflect only the matching records (e.g. "Host (2)" not "Host (5)"). Clearing the search should restore original counts.

- [ ] **Step 3: Commit**

```bash
git add dashboard.html
git commit -m "feat: inventory chip counts reflect active search filter"
```

---

## Task 7: FAB — floating "Add host" button

**Files:**
- Modify: `dashboard.html` — CSS block (add FAB rules) + HTML (add FAB element after `#discover-overlay`)

- [ ] **Step 1: Add FAB CSS**

In the CSS block, after the `.modal-foot` rules (search for `.modal-foot` to locate), add:

```css
.fab{position:fixed;bottom:28px;right:28px;z-index:35;display:flex;align-items:center;gap:8px;padding:0 20px;height:52px;border-radius:26px;background:var(--green);color:#fff;font-family:'DM Sans',sans-serif;font-size:14px;font-weight:600;border:none;cursor:pointer;box-shadow:0 4px 18px rgba(93,187,141,.45);transition:transform .15s,box-shadow .15s;white-space:nowrap}
.fab:hover{transform:translateY(-2px);box-shadow:0 6px 24px rgba(93,187,141,.55)}
.fab:active{transform:translateY(0);box-shadow:0 3px 12px rgba(93,187,141,.4)}
.fab-icon{font-size:22px;font-weight:300;line-height:1;margin-top:-1px}
.fab-label{line-height:1}
@media(max-width:600px){.fab-label{display:none}.fab{padding:0;width:52px;justify-content:center}}
```

- [ ] **Step 2: Add FAB element to HTML**

Find the end of `#discover-overlay` (~line 1673):
```html
</div>
```
(The closing `</div>` of `#discover-overlay`.)

After it, add:
```html
<button class="fab" onclick="openAddHostModal()" aria-label="Add host">
  <span class="fab-icon">+</span>
  <span class="fab-label">Add host</span>
</button>
```

- [ ] **Step 3: Verify**

Run `python -m pytest tests/ -q`.

Open the dashboard. A green pill button reading "+ Add host" should float at the bottom-right of the viewport on all tabs. On a narrow window (<600px), it should collapse to a circle with just the `+`.

- [ ] **Step 4: Commit**

```bash
git add dashboard.html
git commit -m "feat: floating Add host button (FAB) fixed bottom-right"
```

---

## Task 8: Add Host modal HTML

**Files:**
- Modify: `dashboard.html` — HTML (add `#add-host-overlay` modal after the FAB element)

- [ ] **Step 1: Add the Add Host modal markup**

Immediately after the FAB `<button>` added in Task 7, add:

```html
<div class="modal-overlay" id="add-host-overlay">
  <div class="modal">
    <div class="modal-hdr">
      <div class="modal-title">Add host</div>
      <button class="modal-close-btn" onclick="closeAddHostModal()" aria-label="Close">×</button>
    </div>
    <div class="modal-body">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px">
        <label style="display:flex;flex-direction:column;gap:4px;font-size:11px;font-family:'DM Mono',monospace;letter-spacing:.05em;color:var(--hint);text-transform:uppercase">
          Name
          <input type="text" placeholder="My device" class="ah-name" style="padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:13px;background:var(--surface);color:var(--text)">
        </label>
        <label style="display:flex;flex-direction:column;gap:4px;font-size:11px;font-family:'DM Mono',monospace;letter-spacing:.05em;color:var(--hint);text-transform:uppercase">
          IP address
          <input type="text" placeholder="192.168.1.1" class="ah-ip" style="padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:13px;background:var(--surface);color:var(--text)">
        </label>
        <label style="display:flex;flex-direction:column;gap:4px;font-size:11px;font-family:'DM Mono',monospace;letter-spacing:.05em;color:var(--hint);text-transform:uppercase">
          Group
          <input type="text" placeholder="General" class="ah-group" value="General" style="padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:13px;background:var(--surface);color:var(--text)">
        </label>
        <label style="display:flex;flex-direction:column;gap:4px;font-size:11px;font-family:'DM Mono',monospace;letter-spacing:.05em;color:var(--hint);text-transform:uppercase">
          Interval (s)
          <input type="number" min="5" placeholder="30" class="ah-interval" style="padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:13px;background:var(--surface);color:var(--text)">
        </label>
      </div>
      <div style="display:flex;gap:20px;margin-bottom:10px;font-size:13px">
        <label style="display:inline-flex;align-items:center;gap:7px;cursor:pointer">
          <input type="checkbox" class="ah-alwayson" checked> Always on
        </label>
        <label style="display:inline-flex;align-items:center;gap:7px;cursor:pointer">
          <input type="checkbox" class="ah-alert" checked> Alert on down
        </label>
      </div>
      <details style="margin-bottom:4px">
        <summary style="font-size:12px;color:var(--muted);cursor:pointer;user-select:none;padding:4px 0">More fields (specs, MAC, notes, links)</summary>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px">
          <label style="display:flex;flex-direction:column;gap:4px;font-size:11px;font-family:'DM Mono',monospace;letter-spacing:.05em;color:var(--hint);text-transform:uppercase">CPU<input type="text" placeholder="e.g. Intel i9-12900K" class="ah-cpu" style="padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:13px;background:var(--surface);color:var(--text)"></label>
          <label style="display:flex;flex-direction:column;gap:4px;font-size:11px;font-family:'DM Mono',monospace;letter-spacing:.05em;color:var(--hint);text-transform:uppercase">RAM<input type="text" placeholder="e.g. 64 GB DDR5" class="ah-ram" style="padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:13px;background:var(--surface);color:var(--text)"></label>
          <label style="display:flex;flex-direction:column;gap:4px;font-size:11px;font-family:'DM Mono',monospace;letter-spacing:.05em;color:var(--hint);text-transform:uppercase">Storage<input type="text" placeholder="e.g. 2TB NVMe" class="ah-storage" style="padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:13px;background:var(--surface);color:var(--text)"></label>
          <label style="display:flex;flex-direction:column;gap:4px;font-size:11px;font-family:'DM Mono',monospace;letter-spacing:.05em;color:var(--hint);text-transform:uppercase">OS<input type="text" placeholder="e.g. Windows 11" class="ah-os" style="padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:13px;background:var(--surface);color:var(--text)"></label>
          <label style="display:flex;flex-direction:column;gap:4px;font-size:11px;font-family:'DM Mono',monospace;letter-spacing:.05em;color:var(--hint);text-transform:uppercase;grid-column:1/-1">MAC address<input type="text" placeholder="aa:bb:cc:dd:ee:ff (required for Wake-on-LAN)" class="ah-mac" style="padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:13px;background:var(--surface);color:var(--text)"></label>
          <label style="display:flex;flex-direction:column;gap:4px;font-size:11px;font-family:'DM Mono',monospace;letter-spacing:.05em;color:var(--hint);text-transform:uppercase;grid-column:1/-1">Notes<textarea class="ah-notes" placeholder="Anything else worth remembering." style="padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:13px;background:var(--surface);color:var(--text);resize:vertical;min-height:56px;width:100%;box-sizing:border-box"></textarea></label>
        </div>
      </details>
      <div id="add-host-error" style="font-size:12px;color:var(--red-text);margin-top:6px;min-height:18px"></div>
    </div>
    <div class="modal-foot">
      <div class="save-status" id="add-host-status"></div>
      <div style="display:flex;gap:8px">
        <button class="btn" onclick="closeAddHostModal()">Cancel</button>
        <button class="btn btn-primary" onclick="saveAddHost()">Add host</button>
      </div>
    </div>
  </div>
</div>
```

- [ ] **Step 2: Verify HTML renders**

Run `python -m pytest tests/ -q`.

Open the dashboard. Click the FAB — the "Add host" modal should appear. It won't save yet (JS not wired up). Close it with the × or Cancel button (those call `closeAddHostModal()` which doesn't exist yet — the modal won't close, that's expected). Confirm the layout looks right.

- [ ] **Step 3: Commit**

```bash
git add dashboard.html
git commit -m "feat: Add host modal HTML structure"
```

---

## Task 9: Add Host modal JS

**Files:**
- Modify: `dashboard.html` — `<script>` block (add `openAddHostModal`, `closeAddHostModal`, `saveAddHost` near `openEditor`)

- [ ] **Step 1: Add the three JS functions**

Find `function closeEditor` at ~line 3348:
```js
function closeEditor(){ document.getElementById('modal-overlay').classList.remove('open'); }
```

Immediately after it, add:

```js
function openAddHostModal(){
  if(_authState.setup_required){ openSetup(); return; }
  if(!_authState.logged_in){ openLogin(() => openAddHostModal()); return; }
  // Clear all fields
  ['ah-name','ah-ip','ah-group','ah-interval','ah-cpu','ah-ram','ah-storage','ah-os','ah-mac','ah-notes'].forEach(cls => {
    const el = document.querySelector('.' + cls);
    if(el) el.tagName === 'TEXTAREA' ? (el.value = '') : (el.value = cls === 'ah-group' ? 'General' : '');
  });
  ['ah-alwayson','ah-alert'].forEach(cls => {
    const el = document.querySelector('.' + cls);
    if(el) el.checked = true;
  });
  document.getElementById('add-host-error').textContent = '';
  document.getElementById('add-host-status').textContent = '';
  document.getElementById('add-host-overlay').classList.add('open');
  setTimeout(() => { const el = document.querySelector('.ah-name'); if(el) el.focus(); }, 50);
}

function closeAddHostModal(){
  document.getElementById('add-host-overlay').classList.remove('open');
}

async function saveAddHost(){
  const nameEl  = document.querySelector('.ah-name');
  const ipEl    = document.querySelector('.ah-ip');
  const macEl   = document.querySelector('.ah-mac');
  const errEl   = document.getElementById('add-host-error');
  const statEl  = document.getElementById('add-host-status');
  errEl.textContent = '';
  [nameEl, ipEl, macEl].forEach(el => el && el.classList.remove('invalid'));

  const name  = nameEl.value.trim();
  const ip    = ipEl.value.trim();
  const group = (document.querySelector('.ah-group').value.trim()) || 'General';
  const intervalRaw = document.querySelector('.ah-interval').value.trim();
  const mac   = macEl.value.trim();

  let hasError = false;
  if(!name){ nameEl.classList.add('invalid'); hasError = true; }
  if(!ipValid(ip)){ ipEl.classList.add('invalid'); hasError = true; }
  if(!macValid(mac)){ macEl.classList.add('invalid'); hasError = true; }
  if(hasError){ errEl.textContent = 'Fix the highlighted fields.'; return; }

  const entry = { name, ip, group, always_on: document.querySelector('.ah-alwayson').checked };
  if(!document.querySelector('.ah-alert').checked) entry.alert = false;
  if(intervalRaw){ const iv = parseInt(intervalRaw); if(!isNaN(iv) && iv >= 5) entry.interval = iv; }

  const specs = {};
  [['cpu','ah-cpu'],['ram','ah-ram'],['storage','ah-storage'],['os','ah-os'],['mac','ah-mac']].forEach(([k, cls]) => {
    const el = document.querySelector('.' + cls);
    if(el && el.value.trim()) specs[k] = el.value.trim();
  });
  if(Object.keys(specs).length) entry.specs = specs;
  const notes = document.querySelector('.ah-notes').value.trim();
  if(notes) entry.notes = notes;

  statEl.textContent = 'Saving…';
  try {
    const existing = await fetch('/api/hosts');
    if(existing.status === 401){ closeAddHostModal(); openLogin(() => openAddHostModal()); return; }
    const existingData = await existing.json();
    const hosts = [...(existingData.hosts || []), entry];

    if(hosts.some((h, i) => i !== hosts.length - 1 && h.ip === ip)){
      ipEl.classList.add('invalid');
      errEl.textContent = 'A host with this IP already exists.';
      statEl.textContent = '';
      return;
    }

    const res = await fetch('/api/hosts', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ hosts })
    });
    const data = await res.json();
    if(!res.ok){ statEl.textContent = ''; errEl.textContent = data.error || 'Save failed.'; return; }
    statEl.textContent = 'Added!';
    setTimeout(() => { closeAddHostModal(); refresh(); }, 600);
  } catch(e){ statEl.textContent = ''; errEl.textContent = 'Network error.'; }
}
```

- [ ] **Step 2: Verify**

Run `python -m pytest tests/ -q`.

Click the FAB. Fill in a name and IP, click "Add host". The modal should close after ~600ms and the new host should appear in the Hosts tab. Try clicking "Add host" with an empty name — the name field should turn red and show "Fix the highlighted fields." Try a duplicate IP — it should show an error. Try the Cancel button — modal closes without saving.

- [ ] **Step 3: Commit**

```bash
git add dashboard.html
git commit -m "feat: Add host modal JS (openAddHostModal, closeAddHostModal, saveAddHost)"
```

---

## Self-Review

**Spec coverage:**
1. ✅ FAB extended pill, fixed bottom-right, all tabs → Tasks 7–9
2. ✅ Dedicated Add Host modal (no host list), auth-gated, GET+append+POST save path → Tasks 8–9
3. ✅ `deviceIcon()` → topo icons at 18px, both call sites → Task 1
4. ✅ Status pill title-case, WAIT handled, italic "no link" → Task 2
5. ✅ Type heading icons → Task 3
6. ✅ Smarter empty state (filtered vs truly empty) → Task 4
7. ✅ Status filter chips (All/Up/Down/Unlinked) → Task 5
8. ✅ Chip counts respect search → Task 6

**Placeholder scan:** No TBDs, no "handle edge cases" without code, no "similar to Task N".

**Type consistency:**
- `openAddHostModal` / `closeAddHostModal` / `saveAddHost` defined in Task 9, called from HTML in Task 8 ✓
- `.ah-name`, `.ah-ip`, `.ah-group`, `.ah-interval`, `.ah-alwayson`, `.ah-alert`, `.ah-cpu`, `.ah-ram`, `.ah-storage`, `.ah-os`, `.ah-mac`, `.ah-notes` — class names consistent between Task 8 HTML and Task 9 JS ✓
- `_inventoryFilter.status` added in Task 5 Step 1, used in `renderInventoryTablesByType` (Task 5 Step 4) and `renderInventoryChips` (Task 5 Step 3) ✓
- `setInvStatusFilter` defined in Task 5 Step 2, called from chip onclick in Task 5 Step 3 ✓
- `ipValid()` and `macValid()` already defined at ~line 3471 — reused in `saveAddHost` ✓
