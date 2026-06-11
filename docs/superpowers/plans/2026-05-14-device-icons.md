# Device Icons Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `tablet`, `phone`, and `printer` device types, expose `device_type` on `/api/status`, and render inline SVG icons in host cards, the inventory table, and topology nodes.

**Architecture:** All Python changes go into `monitor.py` (new `InventoryDB.get_device_type_map()` method + `build_api_payload()` update). All frontend changes go into `dashboard.html` (SVG sprite, `deviceIcon()` helper, three render-site edits, new type registrations). No new files created.

**Tech Stack:** Python 3 stdlib (sqlite3), vanilla JS, inline SVG, D3.js (already present for topology).

---

## File Map

| File | What changes |
|---|---|
| `monitor.py` | New `InventoryDB.get_device_type_map()` method; `build_api_payload()` gains optional `inventory_db` param; `/api/status` call site updated |
| `dashboard.html` | SVG sprite block; `deviceIcon()` helper; dropdown options; `INV_TYPE_COLUMNS/LABELS/ORDER`; topology `if/elif` chain + 4 topology helper functions; `renderHost()`; `renderTypeTable()` |
| `tests/test_netwatch.py` | 3 new tests for `get_device_type_map()` and `build_api_payload()` |

---

## Task 1: Server — expose `device_type` on `/api/status`

**Files:**
- Modify: `monitor.py` (InventoryDB class ~line 1298; `build_api_payload` ~line 2641; `/api/status` call ~line 2744)
- Test: `tests/test_netwatch.py` (append)

- [ ] **Step 1: Append failing tests to `tests/test_netwatch.py`**

```python
# ── device_type in /api/status ──────────────────────────────────────────────

from monitor import HistoryDB, InventoryDB, build_api_payload


class _FakeHost:
    def __init__(self, ip):
        self.ip = ip
        self.is_up = True
        self.last_checked = None
        self.always_on = True
    def to_dict(self):
        return {"ip": self.ip, "is_up": self.is_up}


class _FakeHostManager:
    def __init__(self, hosts): self._hosts = hosts
    def list_hosts(self): return self._hosts


def _make_idb(tmpdir):
    db_path = os.path.join(tmpdir, "icons_test.db")
    hdb = HistoryDB(db_path)
    idb = InventoryDB(hdb)
    return hdb, idb


def test_get_device_type_map_returns_ip_to_type():
    with tempfile.TemporaryDirectory() as tmpdir:
        hdb, idb = _make_idb(tmpdir)
        with hdb.lock:
            hdb.conn.execute(
                "INSERT INTO inventory (system, ip, device_type, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ("TabletA", "10.0.0.1", "tablet", 0, 0)
            )
            hdb.conn.execute(
                "INSERT INTO inventory (system, ip, device_type, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ("Phone1", "10.0.0.2", "phone", 0, 0)
            )
        result = idb.get_device_type_map()
        assert result == {"10.0.0.1": "tablet", "10.0.0.2": "phone"}
        hdb.close()


def test_get_device_type_map_excludes_null_ip():
    with tempfile.TemporaryDirectory() as tmpdir:
        hdb, idb = _make_idb(tmpdir)
        with hdb.lock:
            hdb.conn.execute(
                "INSERT INTO inventory (system, ip, device_type, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ("NoIP", None, "host", 0, 0)
            )
        result = idb.get_device_type_map()
        assert result == {}
        hdb.close()


def test_build_api_payload_annotates_device_type():
    with tempfile.TemporaryDirectory() as tmpdir:
        hdb, idb = _make_idb(tmpdir)
        with hdb.lock:
            hdb.conn.execute(
                "INSERT INTO inventory (system, ip, device_type, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ("MyTablet", "10.0.0.1", "tablet", 0, 0)
            )
        hm = _FakeHostManager([_FakeHost("10.0.0.1"), _FakeHost("10.0.0.99")])
        payload = build_api_payload(hm, {}, inventory_db=idb)
        hosts = {h["ip"]: h for h in payload["hosts"]}
        assert hosts["10.0.0.1"]["device_type"] == "tablet"
        assert hosts["10.0.0.99"]["device_type"] == "host"  # no record → default
        hdb.close()
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py::test_get_device_type_map_returns_ip_to_type tests/test_netwatch.py::test_get_device_type_map_excludes_null_ip tests/test_netwatch.py::test_build_api_payload_annotates_device_type -v
```

Expected: `AttributeError: 'InventoryDB' object has no attribute 'get_device_type_map'`

- [ ] **Step 3: Add `get_device_type_map()` to `InventoryDB`**

Find `InventoryDB.list_all` (search for `def list_all`). Insert the new method immediately before it:

```python
    def get_device_type_map(self):
        """Return {ip: device_type} for all inventory records with a non-empty IP."""
        with self.lock:
            cur = self.conn.execute(
                "SELECT ip, device_type FROM inventory"
                " WHERE ip IS NOT NULL AND ip != ''"
            )
            return {row[0]: (row[1] or "host") for row in cur.fetchall()}
```

- [ ] **Step 4: Update `build_api_payload()`**

Find (around line 2641):
```python
def build_api_payload(host_manager, settings, incident_log=None):
    hosts = host_manager.list_hosts()
    events = incident_log.list_incidents() if incident_log else []
    return {
        "generated": datetime.now().isoformat(),
        "settings":  settings,
        "summary": {
            "total":   len(hosts),
            "up":      sum(1 for h in hosts if h.is_up),
            "down":    sum(1 for h in hosts if not h.is_up and h.last_checked and h.always_on),
            "idle":    sum(1 for h in hosts if not h.is_up and h.last_checked and not h.always_on),
            "pending": sum(1 for h in hosts if not h.last_checked),
        },
        "hosts":  [h.to_dict() for h in hosts],
        "events": events,
    }
```

Replace with:
```python
def build_api_payload(host_manager, settings, incident_log=None, inventory_db=None):
    hosts = host_manager.list_hosts()
    events = incident_log.list_incidents() if incident_log else []
    device_types = inventory_db.get_device_type_map() if inventory_db else {}
    return {
        "generated": datetime.now().isoformat(),
        "settings":  settings,
        "summary": {
            "total":   len(hosts),
            "up":      sum(1 for h in hosts if h.is_up),
            "down":    sum(1 for h in hosts if not h.is_up and h.last_checked and h.always_on),
            "idle":    sum(1 for h in hosts if not h.is_up and h.last_checked and not h.always_on),
            "pending": sum(1 for h in hosts if not h.last_checked),
        },
        "hosts": [
            {**h.to_dict(), "device_type": device_types.get(h.ip, "host")}
            for h in hosts
        ],
        "events": events,
    }
```

- [ ] **Step 5: Update the `/api/status` call site**

Find (around line 2744):
```python
                self._send_json(200, build_api_payload(host_manager, settings, incident_log))
```

Replace with:
```python
                self._send_json(200, build_api_payload(host_manager, settings, incident_log, inventory_db))
```

- [ ] **Step 6: Run all tests**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py -v
```

Expected: All 10 tests PASSED.

- [ ] **Step 7: Smoke test**

```bash
cd /home/mgipson/netwatch && timeout 3 python monitor.py --no-tui --no-web || true
```

Expected: No tracebacks.

- [ ] **Step 8: Commit**

```bash
cd /home/mgipson/netwatch
git add monitor.py tests/test_netwatch.py
git commit -m "Expose device_type per host in /api/status via inventory lookup"
```

---

## Task 2: New device types, SVG sprite, and `deviceIcon()` helper

**Files:**
- Modify: `dashboard.html` (6 locations — all listed below with exact current text to find)

This task registers the three new types everywhere they need to appear in the frontend and lays the groundwork (sprite + helper) that Tasks 3 and 4 depend on. Do all steps before committing.

- [ ] **Step 1: Add three options to the inventory form dropdown**

Find:
```html
            <option value="peripheral">Peripheral / other</option>
          </select>
```

Replace with:
```html
            <option value="peripheral">Peripheral / other</option>
            <option value="tablet">Tablet</option>
            <option value="phone">Phone / mobile</option>
            <option value="printer">Printer</option>
          </select>
```

- [ ] **Step 2: Add entries to `INV_TYPE_COLUMNS`**

Find:
```javascript
  peripheral: [
    {key:'system',     label:'System',   sort:'system'},
    {key:'category',   label:'Category', sort:'category'},
    {key:'p:subtype',  label:'Subtype',  sort:'p:subtype'},
    {key:'p:model',    label:'Model',    sort:'p:model'},
    {key:'linked',     label:'Status',   sort:'linked'},
  ],
};
```

Replace with:
```javascript
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
```

- [ ] **Step 3: Update `INV_TYPE_LABELS` and `INV_TYPE_ORDER`**

Find:
```javascript
const INV_TYPE_LABELS = {
  host: 'Hosts', vm: 'VMs', network: 'Network', ups: 'UPS',
  disk: 'Disks', peripheral: 'Peripherals'
};
const INV_TYPE_ORDER = ['host', 'vm', 'network', 'ups', 'disk', 'peripheral'];
```

Replace with:
```javascript
const INV_TYPE_LABELS = {
  host: 'Hosts', vm: 'VMs', network: 'Network', ups: 'UPS',
  disk: 'Disks', peripheral: 'Peripherals',
  tablet: 'Tablets', phone: 'Phones', printer: 'Printers'
};
const INV_TYPE_ORDER = ['host', 'vm', 'network', 'ups', 'disk', 'peripheral', 'tablet', 'phone', 'printer'];
```

- [ ] **Step 4: Add topology support for the three new types**

Four topology helper functions need updating. Each change is a one-line addition.

**4a. `nodeRadiusFor()`** — find:
```javascript
  if(d.device_type === 'peripheral') return 16;
```
Replace with:
```javascript
  if(d.device_type === 'peripheral' || d.device_type === 'tablet'
     || d.device_type === 'phone' || d.device_type === 'printer') return 16;
```

**4b. `seedPosition()`** — find:
```javascript
  if(t === 'peripheral') return { x: cx + 200 + (Math.random() - 0.5) * 80, y: cy - 100 + (Math.random() - 0.5) * 50 };
```
Replace with:
```javascript
  if(t === 'peripheral' || t === 'tablet' || t === 'phone' || t === 'printer')
    return { x: cx + 200 + (Math.random() - 0.5) * 80, y: cy - 100 + (Math.random() - 0.5) * 50 };
```

**4c. `buildNodeTip()` typeLabel map** — find:
```javascript
  const typeLabel = {host:'Host', network:'Network', ups:'UPS', disk:'Disk', peripheral:'Peripheral'}[d.device_type] || d.device_type;
```
Replace with:
```javascript
  const typeLabel = {host:'Host', network:'Network', ups:'UPS', disk:'Disk',
    peripheral:'Peripheral', tablet:'Tablet', phone:'Phone', printer:'Printer'}[d.device_type] || d.device_type;
```

**4d. Bounding-box radius calculation** — find:
```javascript
    else if(n.device_type === 'peripheral') r = 28;
```
Replace with:
```javascript
    else if(n.device_type === 'peripheral' || n.device_type === 'tablet'
            || n.device_type === 'phone' || n.device_type === 'printer') r = 28;
```

**4e. Node shape renderer — add new branch** — find:
```javascript
    } else if(d.device_type === 'peripheral'){
      sel.append('circle').attr('class', 'topo-node-shape').attr('r', 14);
      sel.append('text').attr('class', 'topo-node-label-below')
        .attr('y', 14 + 14).text(truncateLabel(d.name, 20));
    } else if(d.device_type === 'vm'){
```
Replace with:
```javascript
    } else if(d.device_type === 'peripheral' || d.device_type === 'tablet'
              || d.device_type === 'phone' || d.device_type === 'printer'){
      sel.append('circle').attr('class', 'topo-node-shape').attr('r', 14);
      sel.append('text').attr('class', 'topo-node-label-below')
        .attr('y', 14 + 14).text(truncateLabel(d.name, 20));
    } else if(d.device_type === 'vm'){
```

- [ ] **Step 5: Add the SVG sprite block**

Find the opening `<body>` tag (search for `<body>`). Insert the sprite block immediately after it:

```html
<svg xmlns="http://www.w3.org/2000/svg" style="display:none" aria-hidden="true">
  <symbol id="icon-host" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
    <rect x="2" y="3" width="20" height="14" rx="2"/>
    <path d="M8 21h8M12 17v4"/>
  </symbol>
  <symbol id="icon-vm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
    <rect x="7" y="7" width="10" height="10" rx="1"/>
    <path d="M7 10H5M7 14H5M17 10h2M17 14h2M10 7V5M14 7V5M10 17v2M14 17v2"/>
  </symbol>
  <symbol id="icon-network" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
    <rect x="2" y="9" width="20" height="6" rx="2"/>
    <path d="M6 9V6M10 9V6M14 9V6M18 9V6"/>
    <path d="M6 15v3M18 15v3"/>
  </symbol>
  <symbol id="icon-ups" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
    <rect x="6" y="5" width="12" height="16" rx="2"/>
    <path d="M10 3h4"/>
    <path d="M13 10l-2 4h4l-2 4"/>
  </symbol>
  <symbol id="icon-disk" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
    <ellipse cx="12" cy="6" rx="8" ry="3"/>
    <path d="M4 6v12c0 1.657 3.582 3 8 3s8-1.343 8-3V6"/>
  </symbol>
  <symbol id="icon-tablet" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
    <rect x="5" y="2" width="14" height="20" rx="2"/>
    <circle cx="12" cy="19" r="1"/>
  </symbol>
  <symbol id="icon-phone" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
    <rect x="7" y="2" width="10" height="20" rx="2"/>
    <path d="M10 6h4"/>
  </symbol>
  <symbol id="icon-printer" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
    <polyline points="6,9 6,2 18,2 18,9"/>
    <path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/>
    <rect x="6" y="14" width="12" height="8"/>
  </symbol>
  <symbol id="icon-peripheral" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
    <path d="M8 4v4M16 4v4"/>
    <rect x="5" y="8" width="14" height="6" rx="2"/>
    <path d="M12 14v4M9 20h6"/>
  </symbol>
</svg>
```

- [ ] **Step 6: Add `deviceIcon()` JS helper**

Find the `escapeHtml` function (search for `function escapeHtml`). Insert `deviceIcon` immediately before it:

```javascript
function deviceIcon(type, size){
  const id = 'icon-' + (type || 'host');
  return '<svg width="' + size + '" height="' + size + '" viewBox="0 0 24 24" fill="none"'
    + ' stroke="currentColor" stroke-width="1.75" stroke-linecap="round"'
    + ' stroke-linejoin="round" style="vertical-align:middle;flex-shrink:0"'
    + ' aria-hidden="true"><use href="#' + id + '"/></svg>';
}
```

- [ ] **Step 7: Visual smoke test — start the server**

```bash
cd /home/mgipson/netwatch && python monitor.py --no-tui &
```

Open `http://192.168.6.90:8080`. Verify:
- Dashboard loads without JS errors (check browser console)
- Inventory form dropdown now shows Tablet, Phone / mobile, Printer options
- The topology graph renders existing nodes without errors

Kill the server:
```bash
kill %1
```

- [ ] **Step 8: Commit**

```bash
cd /home/mgipson/netwatch
git add dashboard.html
git commit -m "Add tablet/phone/printer types, SVG sprite, and deviceIcon helper"
```

---

## Task 3: Icons in host cards and inventory table

**Files:**
- Modify: `dashboard.html` (two JS functions)

- [ ] **Step 1: Update `renderHost()` to prepend the icon**

Find (around line 1572):
```javascript
  return '<div class="row' + rowCls + '"' + ipAttr + ' onclick="openDrawer(this.dataset.ip)">'
    + '<div><span class="dot ' + dotCls + '"></span></div>'
    + '<div><div class="host-name" ' + nameStyle + '>' + escapeHtml(h.name) + '</div><div class="host-ip-sub">' + escapeHtml(h.ip) + '</div></div>'
```

Replace with:
```javascript
  return '<div class="row' + rowCls + '"' + ipAttr + ' onclick="openDrawer(this.dataset.ip)">'
    + '<div><span class="dot ' + dotCls + '"></span></div>'
    + '<div><div class="host-name" ' + nameStyle + ' style="display:flex;align-items:center;gap:5px">'
    + deviceIcon(h.device_type, 13)
    + '<span>' + escapeHtml(h.name) + '</span></div><div class="host-ip-sub">' + escapeHtml(h.ip) + '</div></div>'
```

- [ ] **Step 2: Update `renderTypeTable()` to prepend the icon in the first column**

Find (around line 4121):
```javascript
  sortedRows.forEach(rec => {
    out += '<tr class="inv-row" onclick="openInventoryDrawer(' + rec.id + ')">';
    cols.forEach(c => {
      out += '<td>' + formatInvCell(rec, c.key) + '</td>';
    });
    out += '</tr>';
  });
```

Replace with:
```javascript
  sortedRows.forEach(rec => {
    out += '<tr class="inv-row" onclick="openInventoryDrawer(' + rec.id + ')">';
    cols.forEach((c, i) => {
      if(i === 0){
        out += '<td><span style="display:flex;align-items:center;gap:5px">'
          + deviceIcon(rec.device_type, 14)
          + formatInvCell(rec, c.key)
          + '</span></td>';
      } else {
        out += '<td>' + formatInvCell(rec, c.key) + '</td>';
      }
    });
    out += '</tr>';
  });
```

- [ ] **Step 3: Visual test**

```bash
cd /home/mgipson/netwatch && python monitor.py --no-tui &
```

Open `http://192.168.6.90:8080`. Verify:
- Each host row shows a small monitor icon to the left of the host name
- The icon color turns red alongside the host name when a host is down
- The inventory table shows a small icon in the first column of each row
- Icons are the correct type for each device (monitor for hosts, chip for VMs, etc.)

Kill the server:
```bash
kill %1
```

- [ ] **Step 4: Commit**

```bash
cd /home/mgipson/netwatch
git add dashboard.html
git commit -m "Add device type icons to host card list and inventory table"
```

---

## Task 4: Icons in topology nodes

**Files:**
- Modify: `dashboard.html` (the D3 node-drawing block, around lines 1980–2028)

This task adds a `<use>` element inside each topology node shape after the shape is appended. The icon is white at 70% opacity so it reads cleanly over any node fill colour.

- [ ] **Step 1: Add icon to the network/ups branch**

The network/ups branch renders a wide rounded rect (110×38) with a centered label inside. The icon goes left-aligned inside the box; shift the label right slightly.

Find:
```javascript
    if(d.device_type === 'network' || d.device_type === 'ups'){
      const w = 110, h = 38;
      sel.append('rect')
        .attr('class', 'topo-node-shape')
        .attr('x', -w/2).attr('y', -h/2)
        .attr('width', w).attr('height', h)
        .attr('rx', 8).attr('ry', 8);
      sel.append('text').attr('class', 'topo-node-label-inside')
        .attr('y', 5).text(truncateLabel(d.name, 16));
```

Replace with:
```javascript
    if(d.device_type === 'network' || d.device_type === 'ups'){
      const w = 110, h = 38;
      sel.append('rect')
        .attr('class', 'topo-node-shape')
        .attr('x', -w/2).attr('y', -h/2)
        .attr('width', w).attr('height', h)
        .attr('rx', 8).attr('ry', 8);
      sel.append('use')
        .attr('href', '#icon-' + (d.device_type || 'host'))
        .attr('x', -48).attr('y', -8)
        .attr('width', 16).attr('height', 16)
        .attr('stroke', 'white').attr('stroke-opacity', 0.7)
        .attr('fill', 'none').attr('stroke-width', 1.75)
        .attr('stroke-linecap', 'round').attr('stroke-linejoin', 'round');
      sel.append('text').attr('class', 'topo-node-label-inside')
        .attr('x', 6).attr('y', 5).text(truncateLabel(d.name, 13));
```

- [ ] **Step 2: Add icon to the disk branch**

Find:
```javascript
    } else if(d.device_type === 'disk'){
      const s = 32;
      sel.append('rect')
        .attr('class', 'topo-node-shape')
        .attr('x', -s/2).attr('y', -s/2)
        .attr('width', s).attr('height', s)
        .attr('rx', 5).attr('ry', 5);
      sel.append('text').attr('class', 'topo-node-label-below')
        .attr('y', s/2 + 14).text(truncateLabel(d.name, 20));
```

Replace with:
```javascript
    } else if(d.device_type === 'disk'){
      const s = 32;
      sel.append('rect')
        .attr('class', 'topo-node-shape')
        .attr('x', -s/2).attr('y', -s/2)
        .attr('width', s).attr('height', s)
        .attr('rx', 5).attr('ry', 5);
      sel.append('use')
        .attr('href', '#icon-disk')
        .attr('x', -6).attr('y', -6)
        .attr('width', 12).attr('height', 12)
        .attr('stroke', 'white').attr('stroke-opacity', 0.7)
        .attr('fill', 'none').attr('stroke-width', 1.75)
        .attr('stroke-linecap', 'round').attr('stroke-linejoin', 'round');
      sel.append('text').attr('class', 'topo-node-label-below')
        .attr('y', s/2 + 14).text(truncateLabel(d.name, 20));
```

- [ ] **Step 3: Add icon to the peripheral/tablet/phone/printer branch**

Find (the branch updated in Task 2 Step 4e):
```javascript
    } else if(d.device_type === 'peripheral' || d.device_type === 'tablet'
              || d.device_type === 'phone' || d.device_type === 'printer'){
      sel.append('circle').attr('class', 'topo-node-shape').attr('r', 14);
      sel.append('text').attr('class', 'topo-node-label-below')
        .attr('y', 14 + 14).text(truncateLabel(d.name, 20));
```

Replace with:
```javascript
    } else if(d.device_type === 'peripheral' || d.device_type === 'tablet'
              || d.device_type === 'phone' || d.device_type === 'printer'){
      sel.append('circle').attr('class', 'topo-node-shape').attr('r', 14);
      sel.append('use')
        .attr('href', '#icon-' + (d.device_type || 'peripheral'))
        .attr('x', -5).attr('y', -5)
        .attr('width', 10).attr('height', 10)
        .attr('stroke', 'white').attr('stroke-opacity', 0.7)
        .attr('fill', 'none').attr('stroke-width', 1.75)
        .attr('stroke-linecap', 'round').attr('stroke-linejoin', 'round');
      sel.append('text').attr('class', 'topo-node-label-below')
        .attr('y', 14 + 14).text(truncateLabel(d.name, 20));
```

- [ ] **Step 4: Add icon to the vm branch**

Find:
```javascript
    } else if(d.device_type === 'vm'){
      // VM - smaller circle than a host, slight purple tint, conveys
      // "runs inside something else" via reduced size
      sel.append('circle').attr('class', 'topo-node-shape').attr('r', 16);
      sel.append('text').attr('class', 'topo-node-label-below')
        .attr('y', 16 + 14).text(truncateLabel(d.name, 20));
```

Replace with:
```javascript
    } else if(d.device_type === 'vm'){
      // VM - smaller circle than a host, slight purple tint, conveys
      // "runs inside something else" via reduced size
      sel.append('circle').attr('class', 'topo-node-shape').attr('r', 16);
      sel.append('use')
        .attr('href', '#icon-vm')
        .attr('x', -5).attr('y', -5)
        .attr('width', 10).attr('height', 10)
        .attr('stroke', 'white').attr('stroke-opacity', 0.7)
        .attr('fill', 'none').attr('stroke-width', 1.75)
        .attr('stroke-linecap', 'round').attr('stroke-linejoin', 'round');
      sel.append('text').attr('class', 'topo-node-label-below')
        .attr('y', 16 + 14).text(truncateLabel(d.name, 20));
```

- [ ] **Step 5: Add icon to the host (default) branch**

Find:
```javascript
    } else {
      // host (default)
      sel.append('circle').attr('class', 'topo-node-shape').attr('r', 22);
      sel.append('text').attr('class', 'topo-node-label-below')
        .attr('y', 22 + 14).text(truncateLabel(d.name, 20));
    }
```

Replace with:
```javascript
    } else {
      // host (default)
      sel.append('circle').attr('class', 'topo-node-shape').attr('r', 22);
      sel.append('use')
        .attr('href', '#icon-host')
        .attr('x', -7).attr('y', -7)
        .attr('width', 14).attr('height', 14)
        .attr('stroke', 'white').attr('stroke-opacity', 0.7)
        .attr('fill', 'none').attr('stroke-width', 1.75)
        .attr('stroke-linecap', 'round').attr('stroke-linejoin', 'round');
      sel.append('text').attr('class', 'topo-node-label-below')
        .attr('y', 22 + 14).text(truncateLabel(d.name, 20));
    }
```

- [ ] **Step 6: Visual test**

```bash
cd /home/mgipson/netwatch && python monitor.py --no-tui &
```

Open `http://192.168.6.90:8080` and navigate to the Topology tab. Verify:
- Each topology node shows a small white icon inside its shape
- Host circles show a monitor icon; network/UPS rectangles show their respective icons with the label shifted right
- The icon doesn't overlap the node label
- Nodes that were already rendering correctly (status glow, animations) are unaffected

Kill the server:
```bash
kill %1
```

- [ ] **Step 7: Commit**

```bash
cd /home/mgipson/netwatch
git add dashboard.html
git commit -m "Add device type icons inside topology graph nodes"
```
