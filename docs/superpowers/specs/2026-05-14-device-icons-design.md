# Device Icons — Design Spec
**Date:** 2026-05-14
**Scope:** Add two new device types (`tablet`, `phone`), expose `device_type` on `/api/status`, and render clean inline SVG icons in the host card list, topology graph, and inventory table.

---

## 1. New Device Types

Add `tablet` and `phone` as first-class types alongside the existing six. Every place that enumerates device types must be updated:

### dashboard.html
- **Inventory form dropdown** (`<select class="inv-f-device_type">`): add two new `<option>` elements:
  ```html
  <option value="tablet">Tablet</option>
  <option value="phone">Phone / mobile</option>
  ```
- **`INV_TYPE_COLUMNS`**: add entries for `tablet` and `phone`, both with the same column layout as `peripheral` (system, category, p:subtype, p:model, linked).
- **`INV_TYPE_LABELS`**: add `tablet: 'Tablets'` and `phone: 'Phones'`.
- **`INV_TYPE_ORDER`**: append `'tablet'` and `'phone'` at the end.

### monitor.py — topology node renderer
The topology `if/elif` chain in the D3 node-drawing code currently has no cases for `tablet` or `phone`. They fall through to the default (host circle, r=22). Add explicit cases that render them like `peripheral` (small circle, r=14, label below), distinguishable from `peripheral` only by icon.

---

## 2. Server — expose `device_type` on `/api/status`

### `InventoryDB.get_device_type_map()`
New method returning `{ip: device_type}` for all inventory records that have a non-empty IP:

```python
def get_device_type_map(self):
    with self.lock:
        cur = self.conn.execute(
            "SELECT ip, device_type FROM inventory WHERE ip IS NOT NULL AND ip != ''"
        )
        return {row[0]: (row[1] or "host") for row in cur.fetchall()}
```

### `build_api_payload()`
Gains an optional `inventory_db=None` parameter. When provided, calls `get_device_type_map()` once and annotates each host dict:

```python
def build_api_payload(host_manager, settings, incident_log=None, inventory_db=None):
    hosts = host_manager.list_hosts()
    device_types = inventory_db.get_device_type_map() if inventory_db else {}
    return {
        ...
        "hosts": [
            {**h.to_dict(), "device_type": device_types.get(h.ip, "host")}
            for h in hosts
        ],
        ...
    }
```

### `/api/status` call site (in `do_GET`)
Update the call to pass `inventory_db`:
```python
self._send_json(200, build_api_payload(host_manager, settings, incident_log, inventory_db))
```

### Invariants
- Hosts with no inventory record silently default to `"host"`.
- `get_device_type_map()` runs under the shared DB lock — same pattern as all other InventoryDB reads.
- `build_api_payload` remains fully functional with `inventory_db=None` (existing tests pass unchanged).

---

## 3. SVG Icon Sprite

A single hidden `<svg>` block is added immediately after `<body>` in `dashboard.html`:

```html
<svg xmlns="http://www.w3.org/2000/svg" style="display:none">
  <symbol id="icon-host"       viewBox="0 0 24 24"> ... </symbol>
  <symbol id="icon-vm"         viewBox="0 0 24 24"> ... </symbol>
  <symbol id="icon-network"    viewBox="0 0 24 24"> ... </symbol>
  <symbol id="icon-ups"        viewBox="0 0 24 24"> ... </symbol>
  <symbol id="icon-disk"       viewBox="0 0 24 24"> ... </symbol>
  <symbol id="icon-tablet"     viewBox="0 0 24 24"> ... </symbol>
  <symbol id="icon-phone"      viewBox="0 0 24 24"> ... </symbol>
  <symbol id="icon-peripheral" viewBox="0 0 24 24"> ... </symbol>
</svg>
```

All symbols: `fill="none"`, `stroke="currentColor"`, `stroke-width="1.75"`, `stroke-linecap="round"`, `stroke-linejoin="round"`.

### Icon shapes (24×24 viewBox)

| ID | Description | Key paths |
|---|---|---|
| `icon-host` | Monitor + stand | Rect (2,3,20,15,rx2) + line(10,18,14,18) + line(8,21,16,21) |
| `icon-vm` | CPU chip | Rect(7,7,10,10,rx1) + 3 tick pairs top/bottom + 2 tick pairs left/right |
| `icon-network` | Switch box + ports | Rect(2,7,20,10,rx2) + 3 circles at y=12 |
| `icon-ups` | Battery + bolt | Rect(6,4,12,16,rx2) + cap(10,2,4,2) + lightning path |
| `icon-disk` | Hard drive cylinder | Ellipse(12,6,8,3) + lines(4,6,4,18)+(20,6,20,18) + ellipse(12,18,8,3) |
| `icon-tablet` | Portrait tablet | Rect(5,2,14,20,rx2) + circle(12,19.5,0.75) |
| `icon-phone` | Portrait phone | Rect(7,2,10,20,rx2) + line(10,4,14,4,stroke-width=2) |
| `icon-peripheral` | Power plug | Rect(8,12,8,8,rx1) + line(12,12,12,5) + line(9,7,9,10) + line(15,7,15,10) |

### Helper function

Add a JS helper used at all three render sites:

```javascript
function deviceIcon(type, size) {
  const id = 'icon-' + (type || 'host');
  return '<svg width="' + size + '" height="' + size + '" viewBox="0 0 24 24" fill="none" '
    + 'stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" '
    + 'style="vertical-align:middle;flex-shrink:0">'
    + '<use href="#' + id + '"/></svg>';
}
```

---

## 4. Host Cards

In `renderHost(h)`, prepend `deviceIcon(h.device_type, 13)` inside the name cell:

Current:
```javascript
+ '<div><div class="host-name" ' + nameStyle + '>' + escapeHtml(h.name) + '</div>...
```

Replace with:
```javascript
+ '<div><div class="host-name" ' + nameStyle + ' style="display:flex;align-items:center;gap:5px">'
+ deviceIcon(h.device_type, 13)
+ '<span>' + escapeHtml(h.name) + '</span></div>...
```

The icon uses `stroke="currentColor"` and inherits `nameStyle`'s color (red when host is down). No additional CSS class needed.

---

## 5. Topology Nodes

After each node shape is appended in the D3 node-drawing block, add a centered icon `<use>`. This is done per branch of the `if/elif` chain:

For host (r=22): append a `<svg:use>` element at (-7, -7), width/height 14, href `#icon-{device_type}`.
For vm (r=16): 10×10 at (-5, -5).
For network/ups rect (110×38): 14×14 at (-47, -7) — left-aligned inside the box.
For disk (32×32 rect): 12×12 at (-6, -6).
For peripheral/tablet/phone (r=14): 10×10 at (-5, -5).

All topology icons: `stroke="white"`, `stroke-opacity="0.7"`, `fill="none"`.

D3 `<use>` append pattern (same for each branch, with size/offset varying per node type):
```javascript
sel.append('use')
  .attr('href', '#icon-' + (d.device_type || 'host'))
  .attr('x', -7).attr('y', -7)
  .attr('width', 14).attr('height', 14)
  .attr('stroke', 'white')
  .attr('stroke-opacity', 0.7)
  .attr('fill', 'none')
  .attr('stroke-width', 1.75)
  .attr('stroke-linecap', 'round')
  .attr('stroke-linejoin', 'round');
```

---

## 6. Inventory Table

In `renderTypeTable()` (dashboard.html), the first `<td>` in each row is the system name column. Wrap its content in a flex container and prepend `deviceIcon(rec.device_type, 14)`:

Current:
```javascript
cols.forEach(c => {
  out += '<td>' + formatInvCell(rec, c.key) + '</td>';
});
```

Replace with:
```javascript
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
```

The icon is 14×14px, inline with the system name text, separated by 5px — matching the host card treatment.

---

## 7. Out of Scope

- No changes to `hosts.yaml` format or the monitoring pipeline.
- No server-side icon rendering.
- No icon customisation per host (type-level only).
- `monitor.py.bak_*` files — already excluded from git, not touched.
