# Inventory Export Scope Selection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `scope` parameter to the inventory export so users can download hosts-only (current behaviour) or a multi-sheet workbook with every device type on its own tab, and expose the choice via a split dropdown button in the dashboard toolbar.

**Architecture:** Three layers touched — (1) `monitor.py`'s `export_inventory_to_xlsx` function gains a `scope` keyword arg and multi-sheet logic; (2) the `/api/inventory-export` handler reads a `?scope=` query param and forwards it; (3) `dashboard.html` replaces the single Export XLSX button with a split dropdown. Tasks 3–4 (NAS setup) are manual steps run on the Pi that are never committed to the repo.

**Tech Stack:** Python 3 / openpyxl (already used), vanilla HTML/CSS/JS (no framework), SMB/CIFS via cifs-utils.

---

## File Structure

**Modified:**
- `monitor.py` — `export_inventory_to_xlsx` (add scope + multi-sheet), `/api/inventory-export` handler (read ?scope)
- `dashboard.html` — `downloadInventoryExport` JS (add scope param + URL), CSS (split button styles), HTML (replace button)

**Out-of-repo (manual, never committed):**
- `/etc/netwatch-nas.creds` — SMB credentials (chmod 600)
- `/etc/fstab` — SMB mount entry
- `/mnt/netwatch-nas/` — mount point
- `/usr/local/bin/netwatch-nas-backup.py` — daily backup script

---

## Task 1: `export_inventory_to_xlsx` — scope parameter + multi-sheet

**Files:**
- Modify: `monitor.py` — `export_inventory_to_xlsx` function (~line 1659)
- Test: `tests/test_netwatch.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_netwatch.py`. The file already imports `HistoryDB, InventoryDB` and has `_make_idb(tmpdir)`. Add this import at the top of the file if not present:

```python
from monitor import export_inventory_to_xlsx
import io
```

Then add these three tests at the end of the file:

```python
def test_export_scope_hosts_filters_to_hosts_only():
    try:
        import openpyxl
    except ImportError:
        return  # skip if openpyxl not installed
    with tempfile.TemporaryDirectory() as tmpdir:
        hdb, idb = _make_idb(tmpdir)
        with hdb.lock:
            hdb.conn.execute(
                "INSERT INTO inventory (system, ip, device_type, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ("Server1", "10.0.0.1", "host", 0, 0)
            )
            hdb.conn.execute(
                "INSERT INTO inventory (system, ip, device_type, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ("VM1", "10.0.0.2", "vm", 0, 0)
            )
        data, filename = export_inventory_to_xlsx(idb, scope='hosts')
        assert data is not None, filename
        assert "hosts" in filename
        wb = openpyxl.load_workbook(io.BytesIO(data))
        assert wb.sheetnames == ["Inventory"]
        ws = wb.active
        # Only 1 data row (host); VM excluded
        assert ws.max_row == 2  # header + 1 host
        assert ws.cell(row=2, column=2).value == "Server1"
        hdb.close()


def test_export_scope_all_creates_one_sheet_per_type():
    try:
        import openpyxl
    except ImportError:
        return
    with tempfile.TemporaryDirectory() as tmpdir:
        hdb, idb = _make_idb(tmpdir)
        with hdb.lock:
            hdb.conn.execute(
                "INSERT INTO inventory (system, ip, device_type, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ("Server1", "10.0.0.1", "host", 0, 0)
            )
            hdb.conn.execute(
                "INSERT INTO inventory (system, ip, device_type, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ("VM1", "10.0.0.2", "vm", 0, 0)
            )
        data, filename = export_inventory_to_xlsx(idb, scope='all')
        assert data is not None, filename
        assert "all" in filename
        wb = openpyxl.load_workbook(io.BytesIO(data))
        assert "Hosts" in wb.sheetnames
        assert "VMs" in wb.sheetnames
        assert "Network" not in wb.sheetnames  # no network records → no sheet
        assert wb["Hosts"].cell(row=2, column=2).value == "Server1"
        assert wb["VMs"].cell(row=2, column=2).value == "VM1"
        hdb.close()


def test_export_scope_defaults_to_hosts():
    try:
        import openpyxl
    except ImportError:
        return
    with tempfile.TemporaryDirectory() as tmpdir:
        hdb, idb = _make_idb(tmpdir)
        with hdb.lock:
            hdb.conn.execute(
                "INSERT INTO inventory (system, ip, device_type, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ("VM1", "10.0.0.2", "vm", 0, 0)
            )
        # No scope arg → defaults to hosts → VM excluded → 0 data rows
        data, filename = export_inventory_to_xlsx(idb)
        assert data is not None
        wb = openpyxl.load_workbook(io.BytesIO(data))
        assert wb.active.max_row == 1  # header only, no host records
        hdb.close()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py::test_export_scope_hosts_filters_to_hosts_only tests/test_netwatch.py::test_export_scope_all_creates_one_sheet_per_type tests/test_netwatch.py::test_export_scope_defaults_to_hosts -v
```

Expected: 3 FAILs (function signature mismatch or wrong behaviour).

- [ ] **Step 3: Update `export_inventory_to_xlsx` in `monitor.py`**

Find the function at ~line 1659. Replace it entirely with:

```python
def export_inventory_to_xlsx(inventory_db, scope='hosts'):
    """Build an XLSX file in memory containing inventory records.

    scope='hosts' (default): exports only host-type records on a single sheet
      named "Inventory". Filename: netwatch-inventory-hosts-{hostname}-{date}.xlsx
    scope='all': exports all device types, one sheet per type that has records.
      Sheet order follows INV_TYPE_ORDER. Filename: netwatch-inventory-all-…xlsx

    Column layout matches the import format for round-tripping host records.
    Returns (bytes, filename) on success, or (None, error_msg) on failure.
    """
    try:
        import openpyxl
        from openpyxl.styles import Font, Alignment, PatternFill
    except ImportError:
        return None, "openpyxl not available"

    import io
    import socket
    from datetime import datetime as _dt

    COLUMNS = [
        ("Category",             "category"),
        ("System",               "system"),
        ("Role / Status",        "role"),
        ("CPU",                  "cpu"),
        ("RAM_GB",               "ram_gb"),
        ("GPU",                  "gpu"),
        ("Architecture",         "architecture"),
        ("OS",                   "os"),
        ("Estimated_CPU_Score",  "cpu_score"),
        ("Max_TDP_Watts",        "tdp_watts"),
        ("TPM_Version",          "tpm"),
        ("MAC_Primary",          "mac"),
        ("IP_Address",           "ip"),
        ("Service_Tag_Serial",   "serial"),
        ("Notes",                "notes"),
    ]

    SHEET_NAMES = {
        'host': 'Hosts', 'vm': 'VMs', 'network': 'Network',
        'ups': 'UPS', 'disk': 'Disks', 'peripheral': 'Peripherals',
        'tablet': 'Tablets', 'phone': 'Phones', 'printer': 'Printers',
    }
    TYPE_ORDER = ['host', 'vm', 'network', 'ups', 'disk', 'peripheral', 'tablet', 'phone', 'printer']

    def _write_sheet(ws, records):
        for col_idx, (header, _) in enumerate(COLUMNS, start=1):
            cell = ws.cell(row=1, column=col_idx, value=header)
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="left", vertical="center")
            cell.fill = PatternFill(start_color="EEEEEE", end_color="EEEEEE", fill_type="solid")
        for row_idx, rec in enumerate(records, start=2):
            for col_idx, (_, field) in enumerate(COLUMNS, start=1):
                ws.cell(row=row_idx, column=col_idx, value=rec.get(field))
        for col_idx, (header, field) in enumerate(COLUMNS, start=1):
            max_len = len(header)
            for rec in records:
                val = rec.get(field)
                if val is not None and len(str(val)) > max_len:
                    max_len = len(str(val))
            col_letter = openpyxl.utils.get_column_letter(col_idx)
            ws.column_dimensions[col_letter].width = min(max_len + 2, 50)
        ws.freeze_panes = "A2"

    try:
        hostname = socket.gethostname() or "unknown"
        date_str = _dt.now().strftime("%Y-%m-%d")

        all_records = inventory_db.list_all()

        if scope == 'all':
            wb = openpyxl.Workbook()
            wb.remove(wb.active)  # remove default blank sheet

            # Group records by device_type
            by_type = {}
            for r in all_records:
                dt = r.get('device_type') or 'host'
                by_type.setdefault(dt, []).append(r)

            # Write sheets in TYPE_ORDER, then any unrecognised types
            ordered = [t for t in TYPE_ORDER if t in by_type]
            extras  = [t for t in by_type if t not in TYPE_ORDER]
            for dt in ordered + extras:
                sheet_name = SHEET_NAMES.get(dt, dt.title())
                ws = wb.create_sheet(title=sheet_name)
                _write_sheet(ws, by_type[dt])

            filename = f"netwatch-inventory-all-{hostname}-{date_str}.xlsx"
        else:
            # scope == 'hosts' (default)
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Inventory"
            records = [r for r in all_records
                       if (r.get("device_type") or "host") == "host"]
            _write_sheet(ws, records)
            filename = f"netwatch-inventory-hosts-{hostname}-{date_str}.xlsx"

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue(), filename

    except Exception as e:
        return None, f"export failed: {e}"
```

- [ ] **Step 4: Run the new tests**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py::test_export_scope_hosts_filters_to_hosts_only tests/test_netwatch.py::test_export_scope_all_creates_one_sheet_per_type tests/test_netwatch.py::test_export_scope_defaults_to_hosts -v
```

Expected: 3 PASSes.

- [ ] **Step 5: Run the full suite**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/ -q
```

Expected: all 14 tests pass (11 old + 3 new).

- [ ] **Step 6: Commit**

```bash
git add monitor.py tests/test_netwatch.py
git commit -m "feat: export_inventory_to_xlsx supports scope=hosts|all (multi-sheet)"
```

---

## Task 2: `/api/inventory-export` handler reads `?scope`

**Files:**
- Modify: `monitor.py` — handler at ~line 2797

- [ ] **Step 1: Update the handler**

Find at ~line 2797:
```python
            if self.path == "/api/inventory-export":
```

The path with a query string (`/api/inventory-export?scope=all`) won't match an exact `==` check. Replace the condition and the `export_inventory_to_xlsx` call:

Find this block:
```python
            if self.path == "/api/inventory-export":
                # Build XLSX in memory and stream it back as a download.
                # Admin-only because inventory contains hardware identifiers.
                if not self._require_auth(admin_only=True):
                    return
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"})
                    return
                try:
                    data, result = export_inventory_to_xlsx(inventory_db)
```

Replace with:
```python
            if self.path == "/api/inventory-export" or self.path.startswith("/api/inventory-export?"):
                # Build XLSX in memory and stream it back as a download.
                # Admin-only because inventory contains hardware identifiers.
                if not self._require_auth(admin_only=True):
                    return
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"})
                    return
                try:
                    from urllib.parse import urlparse as _up, parse_qs as _pqs
                    _scope = _pqs(_up(self.path).query).get('scope', ['hosts'])[0]
                    if _scope not in ('hosts', 'all'):
                        _scope = 'hosts'
                    data, result = export_inventory_to_xlsx(inventory_db, scope=_scope)
```

Leave the rest of the handler block (the `if data is None`, `send_response`, etc.) exactly as-is.

- [ ] **Step 2: Verify**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/ -q
```

Expected: all tests still pass.

- [ ] **Step 3: Commit**

```bash
git add monitor.py
git commit -m "feat: /api/inventory-export reads ?scope=hosts|all query param"
```

---

## Task 3: Dashboard split dropdown button

**Files:**
- Modify: `dashboard.html` — CSS (add split button rules), HTML (replace export button), JS (update `downloadInventoryExport` + add toggle helpers)

- [ ] **Step 1: Add CSS for the split dropdown**

Find the `.inv-toolbar-actions` CSS rule (~line 717):
```css
.inv-toolbar-actions{display:flex;gap:8px}
```

Immediately after it, add:
```css
.inv-export-split{position:relative;display:flex}
.inv-export-split .inv-export-main{border-top-right-radius:0;border-bottom-right-radius:0;border-right:none}
.inv-export-split .inv-export-chevron{border-top-left-radius:0;border-bottom-left-radius:0;padding:0 9px;font-size:11px}
.inv-export-menu{position:absolute;top:calc(100% + 4px);right:0;background:var(--surface);border:1px solid var(--border);border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.12);min-width:160px;z-index:20;display:none;flex-direction:column;overflow:hidden}
.inv-export-menu.open{display:flex}
.inv-export-menu button{padding:9px 14px;text-align:left;font-size:13px;font-family:'DM Sans',sans-serif;background:none;border:none;cursor:pointer;color:var(--text);border-bottom:1px solid var(--border-light)}
.inv-export-menu button:last-child{border-bottom:none}
.inv-export-menu button:hover{background:var(--subtle)}
```

- [ ] **Step 2: Replace the export button in HTML**

Find at ~line 1464:
```html
        <button class="btn" onclick="downloadInventoryExport(this)">Export XLSX</button>
```

Replace with:
```html
        <div class="inv-export-split">
          <button class="btn inv-export-main" onclick="downloadInventoryExport(this,'hosts')">Export XLSX</button>
          <button class="btn inv-export-chevron" onclick="toggleExportMenu(event)" aria-label="More export options">˅</button>
          <div class="inv-export-menu" id="inv-export-menu">
            <button onclick="downloadInventoryExport(null,'hosts');closeExportMenu()">Export Hosts</button>
            <button onclick="downloadInventoryExport(null,'all');closeExportMenu()">Export All Types</button>
          </div>
        </div>
```

- [ ] **Step 3: Update `downloadInventoryExport` JS**

Find at ~line 5420:
```js
async function downloadInventoryExport(btn){
```

Replace the first line and the fetch line:

Change the function signature from:
```js
async function downloadInventoryExport(btn){
```
to:
```js
async function downloadInventoryExport(btn, scope){
  scope = scope || 'hosts';
```

Then find:
```js
    const res = await fetch("/api/inventory-export");
```

Replace with:
```js
    const res = await fetch("/api/inventory-export?scope=" + scope);
```

Leave everything else in the function untouched.

- [ ] **Step 4: Add toggle/close helper functions and outside-click listener**

Find `async function downloadInventoryExport` and add these two functions IMMEDIATELY BEFORE it:

```js
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
```

- [ ] **Step 5: Verify**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/ -q
```

Expected: all tests pass.

Restart netwatch and open the dashboard (you'll need the Pi's service restarted to see the change):
```bash
sudo systemctl restart netwatch
```

Open the Inventory tab. You should see "Export XLSX" with a small chevron (`˅`) to its right. Clicking "Export XLSX" directly should download hosts-only XLSX. Clicking the chevron should reveal a two-item menu: "Export Hosts" and "Export All Types". Clicking "Export All Types" should download a multi-sheet workbook.

- [ ] **Step 6: Commit**

```bash
git add dashboard.html
git commit -m "feat: inventory export split dropdown — hosts-only or all types"
```

---

## Task 4: NAS setup (manual — do NOT commit to repo)

These steps are run directly on the Pi. Nothing here goes into git.

- [ ] **Step 1: Install cifs-utils**

```bash
sudo apt-get install -y cifs-utils
```

- [ ] **Step 2: Create the credentials file**

```bash
sudo tee /etc/netwatch-nas.creds > /dev/null <<'EOF'
username=mgipson
password=2976
EOF
sudo chmod 600 /etc/netwatch-nas.creds
sudo chown root:root /etc/netwatch-nas.creds
```

- [ ] **Step 3: Create the mount point**

```bash
sudo mkdir -p /mnt/netwatch-nas
```

- [ ] **Step 4: Find your user ID**

```bash
id mgipson
```

Note the `uid=` number (likely 1000). Use it in the fstab entry below.

- [ ] **Step 5: Add fstab entry**

```bash
sudo tee -a /etc/fstab > /dev/null <<'EOF'
//192.168.6.125/SharedFolderNAS /mnt/netwatch-nas cifs credentials=/etc/netwatch-nas.creds,uid=1000,gid=1000,iocharset=utf8,_netdev,nofail,vers=3.0 0 0
EOF
```

(Replace `1000` with actual uid from Step 4 if different.)

- [ ] **Step 6: Mount and create the backup folder**

```bash
sudo mount /mnt/netwatch-nas
mkdir -p /mnt/netwatch-nas/NetwatchBackups
ls /mnt/netwatch-nas/
```

Expected: `NetwatchBackups` folder visible.

- [ ] **Step 7: Write the backup script**

```bash
sudo tee /usr/local/bin/netwatch-nas-backup.py > /dev/null <<'EOF'
#!/usr/bin/env python3
"""Daily netwatch inventory backup to NAS.
Exports all device types (scope=all) and writes to /mnt/netwatch-nas/NetwatchBackups/.
"""
import sys, os

sys.path.insert(0, '/home/mgipson/netwatch')
from monitor import export_inventory_to_xlsx, HistoryDB, InventoryDB

DB_PATH  = '/home/mgipson/netwatch/netwatch.db'
NAS_DIR  = '/mnt/netwatch-nas/NetwatchBackups'
LOG_LINE = '[netwatch-backup]'

if not os.path.ismount('/mnt/netwatch-nas'):
    print(f'{LOG_LINE} NAS not mounted at /mnt/netwatch-nas — skipping', flush=True)
    sys.exit(0)

hdb = HistoryDB(DB_PATH)
try:
    idb = InventoryDB(hdb)
    data, result = export_inventory_to_xlsx(idb, scope='all')
    if data is None:
        print(f'{LOG_LINE} Export failed: {result}', file=sys.stderr, flush=True)
        sys.exit(1)
    os.makedirs(NAS_DIR, exist_ok=True)
    dest = os.path.join(NAS_DIR, result)
    with open(dest, 'wb') as f:
        f.write(data)
    print(f'{LOG_LINE} Backed up {len(data)} bytes → {dest}', flush=True)
finally:
    hdb.close()
EOF
sudo chmod +x /usr/local/bin/netwatch-nas-backup.py
```

- [ ] **Step 8: Test the backup script manually**

```bash
python3 /usr/local/bin/netwatch-nas-backup.py
```

Expected output: `[netwatch-backup] Backed up XXXXX bytes → /mnt/netwatch-nas/NetwatchBackups/netwatch-inventory-all-….xlsx`

Then verify:
```bash
ls /mnt/netwatch-nas/NetwatchBackups/
```

- [ ] **Step 9: Add the cron job**

```bash
(crontab -l 2>/dev/null; echo "0 2 * * * /usr/bin/python3 /usr/local/bin/netwatch-nas-backup.py >> /var/log/netwatch-nas-backup.log 2>&1") | crontab -
```

Verify it was added:
```bash
crontab -l
```

Expected: the new line `0 2 * * * /usr/bin/python3 …` appears.

---

## Self-Review

**Spec coverage:**
1. ✅ `export_inventory_to_xlsx` gains `scope` param — Task 1
2. ✅ `scope='hosts'` behaviour unchanged; single "Inventory" sheet, filename includes "hosts" — Task 1
3. ✅ `scope='all'` creates one sheet per type with records, ordered by TYPE_ORDER — Task 1
4. ✅ Sheet names match INV_TYPE_LABELS: Hosts, VMs, Network, UPS, Disks, Peripherals, Tablets, Phones, Printers — Task 1
5. ✅ Unknown device_type falls back to `.title()` — Task 1 (SHEET_NAMES.get fallback)
6. ✅ API handler reads `?scope=`, validates to `hosts|all`, defaults to `hosts` — Task 2
7. ✅ `downloadInventoryExport` gains `scope` param, passes to fetch URL — Task 3
8. ✅ Left segment of split button directly triggers hosts export; chevron opens menu — Task 3
9. ✅ Menu items: "Export Hosts" and "Export All Types" — Task 3
10. ✅ Dropdown closes on outside click — Task 3
11. ✅ Import not changed — no task (YAGNI)
12. ✅ NAS: SMB mount, credentials file protected 600, backup script, daily 2am cron — Task 4

**Placeholder scan:** No TBDs.

**Type consistency:**
- `scope` param: `'hosts'` or `'all'` — consistent across Task 1 (function), Task 2 (handler), Task 3 (JS URL)
- `_write_sheet(ws, records)` nested function defined in Task 1 and only used within `export_inventory_to_xlsx` — no cross-task leakage
- `toggleExportMenu`, `closeExportMenu` defined in Task 3 Step 4, called from HTML in Task 3 Step 2 ✓
