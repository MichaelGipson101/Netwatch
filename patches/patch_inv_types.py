#!/usr/bin/env python3
"""
netwatch patch: peripheral and accessory tracking in inventory.

Until now, inventory implicitly assumed every record was a "host" (a
computer with CPU/RAM/OS). This patch adds first-class support for
non-computer hardware:

  - host       (existing default - computers and servers)
  - network    (switches, routers, access points)
  - ups        (uninterruptible power supplies)
  - disk       (drives - both installed and shelf spares)
  - peripheral (KVMs, monitors, keyboards, anything else)

Each type has a tailored editor form and drawer view that shows only
the fields relevant to that type. The Inventory tab gets a new Type
filter chip row above the existing Category chips, and the metrics row
expands to include both "Total devices" (everything) and "Hosts"
(host-type only) so you can see both numbers at once.

Schema changes (idempotent):
  - Adds device_type column to inventory (defaults all existing rows to 'host')
  - Adds properties column for type-specific JSON-blob fields

Backward compatibility:
  - Every existing inventory record becomes type=host automatically
  - XLSX import/export remains host-only (peripherals are UI-only)
  - Existing API consumers see device_type='host' on legacy records

Must be applied AFTER patch_inv_export.py.

Run once from ~/netwatch/:
    python3 patch_inv_types.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_invtypes.
Idempotent - safe to re-run.
"""

import os, shutil, sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_invtypes"
SENTINEL = "INVENTORY_TYPE_PROPERTIES"  # presence means already patched


# ─── Module-level constants for type taxonomy ────────────────────────────────

NEW_TYPE_CONSTANTS = '''# Inventory type taxonomy. Each type has a list of "type-specific" properties
# stored in the properties JSON blob; common fields (system name, mac, ip,
# serial, notes, etc.) live as top-level columns and are shared across types.
INVENTORY_TYPES = ("host", "network", "ups", "disk", "peripheral")

INVENTORY_TYPE_PROPERTIES = {
    "host": [],  # all fields are top-level (cpu, ram, os, etc.)
    "network": [
        ("port_count",     "int",    "Port count"),
        ("poe_watts",      "int",    "PoE budget (W)"),
        ("managed",        "bool",   "Managed"),
        ("uplink_speed",   "string", "Uplink speed"),
    ],
    "ups": [
        ("capacity_va",       "int",    "Capacity (VA)"),
        ("capacity_wh",       "int",    "Capacity (Wh)"),
        ("runtime_min",       "int",    "Runtime (min) at full load"),
        ("battery_age_years", "string", "Battery age"),
    ],
    "disk": [
        ("capacity_gb",  "int",    "Capacity (GB)"),
        ("interface",    "string", "Interface (SATA/NVMe/USB)"),
        ("rpm",          "int",    "Spindle speed (RPM, blank for SSD)"),
        ("used_in",      "string", "Currently installed in"),
        ("health",       "string", "Health status"),
    ],
    "peripheral": [
        ("subtype",      "string", "Type (KVM, monitor, keyboard, etc.)"),
        ("model",        "string", "Model"),
    ],
}


'''


PATCHES = [
    # 1. Insert the type-taxonomy constants right before the InventoryDB class.
    (
        '''class InventoryDB:
    """SQLite-backed inventory store. Lives in the same database as ping history''',
        NEW_TYPE_CONSTANTS + '''class InventoryDB:
    """SQLite-backed inventory store. Lives in the same database as ping history'''
    ),

    # 2. Schema migration: add device_type + properties columns to inventory.
    # Idempotent via try/except for re-runs. Goes inside __init__ after
    # the existing executescript(SCHEMA).
    (
        '''        self.conn = history_db.conn
        self.conn.executescript(self.SCHEMA)
        logging.info("InventoryDB: schema ready")''',
        '''        self.conn = history_db.conn
        self.conn.executescript(self.SCHEMA)
        # Schema migration: add device_type and properties columns.
        # ALTER TABLE on existing column raises sqlite3.OperationalError.
        import sqlite3 as _sqlite3
        try:
            self.conn.execute("ALTER TABLE inventory ADD COLUMN device_type TEXT DEFAULT 'host' NOT NULL")
            logging.info("InventoryDB: added device_type column")
        except _sqlite3.OperationalError:
            pass
        try:
            self.conn.execute("ALTER TABLE inventory ADD COLUMN properties TEXT")
        except _sqlite3.OperationalError:
            pass
        logging.info("InventoryDB: schema ready")'''
    ),

    # 3. Extend FIELDS to include the new columns
    (
        '''    FIELDS = [
        "category", "system", "role", "cpu", "ram_gb", "gpu", "architecture",
        "os", "cpu_score", "tdp_watts", "tpm", "mac", "ip", "serial", "notes",
    ]''',
        '''    FIELDS = [
        "category", "system", "role", "cpu", "ram_gb", "gpu", "architecture",
        "os", "cpu_score", "tdp_watts", "tpm", "mac", "ip", "serial", "notes",
        "device_type", "properties",
    ]'''
    ),

    # 4. Update list_all SELECT to include new columns + JSON-decode properties
    (
        '''    def list_all(self):
        with self.lock:
            cur = self.conn.execute(
                "SELECT id, category, system, role, cpu, ram_gb, gpu, architecture, "
                "os, cpu_score, tdp_watts, tpm, mac, ip, serial, notes, "
                "created_at, updated_at FROM inventory ORDER BY system COLLATE NOCASE"
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]''',
        '''    def list_all(self):
        with self.lock:
            cur = self.conn.execute(
                "SELECT id, category, system, role, cpu, ram_gb, gpu, architecture, "
                "os, cpu_score, tdp_watts, tpm, mac, ip, serial, notes, "
                "device_type, properties, "
                "created_at, updated_at FROM inventory ORDER BY system COLLATE NOCASE"
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            for r in rows:
                self._decode_properties(r)
            return rows'''
    ),

    # 5. Update get() SELECT
    (
        '''    def get(self, inv_id):
        with self.lock:
            cur = self.conn.execute(
                "SELECT id, category, system, role, cpu, ram_gb, gpu, architecture, "
                "os, cpu_score, tdp_watts, tpm, mac, ip, serial, notes, "
                "created_at, updated_at FROM inventory WHERE id = ?", (inv_id,)
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))''',
        '''    def get(self, inv_id):
        with self.lock:
            cur = self.conn.execute(
                "SELECT id, category, system, role, cpu, ram_gb, gpu, architecture, "
                "os, cpu_score, tdp_watts, tpm, mac, ip, serial, notes, "
                "device_type, properties, "
                "created_at, updated_at FROM inventory WHERE id = ?", (inv_id,)
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            rec = dict(zip(cols, row))
            self._decode_properties(rec)
            return rec'''
    ),

    # 6. Update find_by_mac SELECT
    (
        '''    def find_by_mac(self, mac):
        """Return inventory record matching a MAC (normalized), or None."""
        norm = self.normalize_mac(mac)
        if not norm:
            return None
        with self.lock:
            cur = self.conn.execute(
                "SELECT id, category, system, role, cpu, ram_gb, gpu, architecture, "
                "os, cpu_score, tdp_watts, tpm, mac, ip, serial, notes, "
                "created_at, updated_at FROM inventory WHERE mac = ?", (norm,)
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))''',
        '''    def find_by_mac(self, mac):
        """Return inventory record matching a MAC (normalized), or None."""
        norm = self.normalize_mac(mac)
        if not norm:
            return None
        with self.lock:
            cur = self.conn.execute(
                "SELECT id, category, system, role, cpu, ram_gb, gpu, architecture, "
                "os, cpu_score, tdp_watts, tpm, mac, ip, serial, notes, "
                "device_type, properties, "
                "created_at, updated_at FROM inventory WHERE mac = ?", (norm,)
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            rec = dict(zip(cols, row))
            self._decode_properties(rec)
            return rec

    def _decode_properties(self, rec):
        """Decode the properties JSON blob into a dict in-place. If the
        blob is missing/malformed, set properties to {}."""
        raw = rec.get("properties")
        if raw is None or raw == "":
            rec["properties"] = {}
            return
        if isinstance(raw, dict):
            return  # already decoded
        try:
            import json
            rec["properties"] = json.loads(raw)
            if not isinstance(rec["properties"], dict):
                rec["properties"] = {}
        except (ValueError, TypeError):
            rec["properties"] = {}'''
    ),

    # 7. Update INSERT to include new columns
    (
        '''        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO inventory (category, system, role, cpu, ram_gb, gpu, "
                "architecture, os, cpu_score, tdp_watts, tpm, mac, ip, serial, notes, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (clean.get("category"), clean["system"], clean.get("role"),
                 clean.get("cpu"), clean.get("ram_gb"), clean.get("gpu"),
                 clean.get("architecture"), clean.get("os"), clean.get("cpu_score"),
                 clean.get("tdp_watts"), clean.get("tpm"), clean.get("mac"),
                 clean.get("ip"), clean.get("serial"), clean.get("notes"),
                 ts, ts)
            )
            return cur.lastrowid, None''',
        '''        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO inventory (category, system, role, cpu, ram_gb, gpu, "
                "architecture, os, cpu_score, tdp_watts, tpm, mac, ip, serial, notes, "
                "device_type, properties, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (clean.get("category"), clean["system"], clean.get("role"),
                 clean.get("cpu"), clean.get("ram_gb"), clean.get("gpu"),
                 clean.get("architecture"), clean.get("os"), clean.get("cpu_score"),
                 clean.get("tdp_watts"), clean.get("tpm"), clean.get("mac"),
                 clean.get("ip"), clean.get("serial"), clean.get("notes"),
                 clean.get("device_type") or "host",
                 clean.get("properties"),
                 ts, ts)
            )
            return cur.lastrowid, None'''
    ),

    # 8. Update _clean_input to handle device_type and properties
    (
        '''    def _clean_input(self, data):
        """Coerce / validate field values."""
        out = {}
        for f in self.FIELDS:
            if f not in data:
                continue
            v = data[f]
            if v is None or (isinstance(v, str) and v.strip() == ""):
                out[f] = None
                continue
            if f in ("ram_gb",):
                try: out[f] = float(v)
                except (ValueError, TypeError): out[f] = None
            elif f in ("cpu_score", "tdp_watts"):
                try: out[f] = int(float(v))
                except (ValueError, TypeError): out[f] = None
            elif f == "mac":
                out[f] = self.normalize_mac(v)
                if not out[f]:
                    out[f] = None
            else:
                out[f] = str(v).strip() if v is not None else None
        return out''',
        '''    def _clean_input(self, data):
        """Coerce / validate field values."""
        import json as _json
        out = {}
        for f in self.FIELDS:
            if f not in data:
                continue
            v = data[f]
            if v is None or (isinstance(v, str) and v.strip() == ""):
                out[f] = None
                continue
            if f in ("ram_gb",):
                try: out[f] = float(v)
                except (ValueError, TypeError): out[f] = None
            elif f in ("cpu_score", "tdp_watts"):
                try: out[f] = int(float(v))
                except (ValueError, TypeError): out[f] = None
            elif f == "mac":
                out[f] = self.normalize_mac(v)
                if not out[f]:
                    out[f] = None
            elif f == "device_type":
                t = str(v).strip().lower()
                out[f] = t if t in INVENTORY_TYPES else "peripheral"
            elif f == "properties":
                # Accept a dict (preferred), serialize to JSON for storage.
                # Strings are passed through if they parse as JSON dicts.
                if isinstance(v, dict):
                    out[f] = _json.dumps(v)
                elif isinstance(v, str):
                    try:
                        parsed = _json.loads(v)
                        out[f] = _json.dumps(parsed) if isinstance(parsed, dict) else None
                    except (ValueError, TypeError):
                        out[f] = None
                else:
                    out[f] = None
            else:
                out[f] = str(v).strip() if v is not None else None
        return out'''
    ),

    # 9. Frontend: replace renderInventoryMetrics to count by type
    (
        '''function renderInventoryMetrics(){
  const total = _inventoryData.length;
  // "Active" = has a linked monitored host that is up
  const active = _inventoryData.filter(i => i.linked_host && i.linked_host.is_up).length;
  // Power: sum TDP of active items
  const activeItems = _inventoryData.filter(i => i.linked_host && i.linked_host.is_up);
  const totalPower = activeItems.reduce((s, i) => s + (i.tdp_watts || 0), 0);
  // Compute: sum cpu_score across all (proxy for fleet capacity)
  const totalScore = _inventoryData.reduce((s, i) => s + (i.cpu_score || 0), 0);
  const html = [
    '<div class="inv-metric"><div class="inv-metric-label">Total systems</div><div class="inv-metric-val">' + total + '</div><div class="inv-metric-sub">' + active + ' currently online</div></div>',
    '<div class="inv-metric"><div class="inv-metric-label">Est. live power</div><div class="inv-metric-val">' + totalPower + '<span class="unit">W</span></div><div class="inv-metric-sub">across ' + active + ' active devices</div></div>',
    '<div class="inv-metric"><div class="inv-metric-label">Total CPU score</div><div class="inv-metric-val">' + Math.round(totalScore).toLocaleString() + '</div><div class="inv-metric-sub">PassMark equivalent</div></div>',
    '<div class="inv-metric"><div class="inv-metric-label">Architectures</div><div class="inv-metric-val" style="font-size:14px">' + (Array.from(new Set(_inventoryData.map(i => i.architecture).filter(Boolean))).join(', ') || '-') + '</div><div class="inv-metric-sub">across the fleet</div></div>',
  ].join('');
  document.getElementById('inv-metrics').innerHTML = html;
}''',
        '''function renderInventoryMetrics(){
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
  const typeOrder = ['host', 'network', 'ups', 'disk', 'peripheral'];
  const typeLabels = {host:'Hosts', network:'Network', ups:'UPS', disk:'Disks', peripheral:'Other'};
  const breakdownParts = typeOrder
    .filter(t => typeCounts[t])
    .map(t => typeCounts[t] + ' ' + typeLabels[t].toLowerCase());
  const html = [
    '<div class="inv-metric"><div class="inv-metric-label">Total devices</div><div class="inv-metric-val">' + total + '</div><div class="inv-metric-sub">' + (breakdownParts.join(' · ') || 'no records yet') + '</div></div>',
    '<div class="inv-metric"><div class="inv-metric-label">Hosts</div><div class="inv-metric-val">' + hostsCount + '</div><div class="inv-metric-sub">' + active + ' currently online</div></div>',
    '<div class="inv-metric"><div class="inv-metric-label">Est. live power</div><div class="inv-metric-val">' + totalPower + '<span class="unit">W</span></div><div class="inv-metric-sub">across ' + active + ' active hosts</div></div>',
    '<div class="inv-metric"><div class="inv-metric-label">Total CPU score</div><div class="inv-metric-val">' + Math.round(totalScore).toLocaleString() + '</div><div class="inv-metric-sub">across hosts</div></div>',
    '<div class="inv-metric"><div class="inv-metric-label">Architectures</div><div class="inv-metric-val" style="font-size:14px">' + (Array.from(new Set(hostsOnly.map(i => i.architecture).filter(Boolean))).join(', ') || '-') + '</div><div class="inv-metric-sub">across hosts</div></div>',
  ].join('');
  document.getElementById('inv-metrics').innerHTML = html;
}''',
    ),

    # 10. Add type filter chips above the existing category chips. We
    # restructure renderInventoryChips to render BOTH rows.
    (
        '''function renderInventoryChips(){
  const cats = {};
  _inventoryData.forEach(i => {
    const c = i.category || '(uncategorized)';
    cats[c] = (cats[c] || 0) + 1;
  });
  const sortedCats = Object.entries(cats).sort((a,b) => b[1] - a[1]);
  const chips = ['<span class="inv-chip ' + (_inventoryFilter.category === null ? 'active' : '') + '" onclick="setInvCategoryFilter(null)">All<span class="inv-chip-count">' + _inventoryData.length + '</span></span>'];
  sortedCats.forEach(([cat, count]) => {
    chips.push('<span class="inv-chip ' + (_inventoryFilter.category === cat ? 'active' : '') + '" onclick="setInvCategoryFilter(' + JSON.stringify(cat).replace(/"/g, '&quot;') + ')">' + escapeHtml(cat) + '<span class="inv-chip-count">' + count + '</span></span>');
  });
  document.getElementById('inv-chips').innerHTML = chips.join('');
}''',
        '''function renderInventoryChips(){
  // Type chips (top row)
  const typeCounts = {};
  _inventoryData.forEach(i => {
    const t = i.device_type || 'host';
    typeCounts[t] = (typeCounts[t] || 0) + 1;
  });
  const typeOrder = ['host', 'network', 'ups', 'disk', 'peripheral'];
  const typeLabels = {host:'Hosts', network:'Network', ups:'UPS', disk:'Disks', peripheral:'Peripherals'};
  const typeChips = ['<span class="inv-chip inv-chip-type ' + (_inventoryFilter.deviceType === null ? 'active' : '') + '" onclick="setInvTypeFilter(null)">All<span class="inv-chip-count">' + _inventoryData.length + '</span></span>'];
  typeOrder.forEach(t => {
    if(!typeCounts[t]) return;  // hide empty types
    typeChips.push('<span class="inv-chip inv-chip-type ' + (_inventoryFilter.deviceType === t ? 'active' : '') + '" onclick="setInvTypeFilter(' + JSON.stringify(t) + ')">' + typeLabels[t] + '<span class="inv-chip-count">' + typeCounts[t] + '</span></span>');
  });

  // Category chips (existing - second row). Filter categories by current
  // type filter so we only show categories that exist within the active type.
  let scope = _inventoryData;
  if(_inventoryFilter.deviceType !== null){
    scope = scope.filter(i => (i.device_type || 'host') === _inventoryFilter.deviceType);
  }
  const cats = {};
  scope.forEach(i => {
    const c = i.category || '(uncategorized)';
    cats[c] = (cats[c] || 0) + 1;
  });
  const sortedCats = Object.entries(cats).sort((a,b) => b[1] - a[1]);
  const catChips = ['<span class="inv-chip ' + (_inventoryFilter.category === null ? 'active' : '') + '" onclick="setInvCategoryFilter(null)">All<span class="inv-chip-count">' + scope.length + '</span></span>'];
  sortedCats.forEach(([cat, count]) => {
    catChips.push('<span class="inv-chip ' + (_inventoryFilter.category === cat ? 'active' : '') + '" onclick="setInvCategoryFilter(' + JSON.stringify(cat).replace(/"/g, '&quot;') + ')">' + escapeHtml(cat) + '<span class="inv-chip-count">' + count + '</span></span>');
  });

  document.getElementById('inv-chips').innerHTML =
    '<div class="inv-chip-row inv-chip-row-types">' + typeChips.join('') + '</div>' +
    (catChips.length > 1 ? '<div class="inv-chip-row inv-chip-row-cats">' + catChips.join('') + '</div>' : '');
}

function setInvTypeFilter(t){
  _inventoryFilter.deviceType = t;
  // Reset category filter when type changes - keeps things simple
  _inventoryFilter.category = null;
  renderInventoryChips();
  renderInventoryRows();
}'''
    ),

    # 11. Initialize the deviceType filter in the global state object
    (
        '''let _inventoryFilter = { category: null, search: '' };''',
        '''let _inventoryFilter = { category: null, deviceType: null, search: '' };'''
    ),

    # 12. Update renderInventoryRows to apply the type filter and render
    # type-aware row content. We replace the row-rendering logic.
    (
        '''function renderInventoryRows(){
  // Apply filter + search
  let rows = _inventoryData;
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
  }''',
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
  }'''
    ),

    # 13. Update the row HTML rendering to show type-aware "specs" column.
    # We replace just the inner <td> generation for CPU/RAM since those
    # don't apply to non-host types.
    (
        '''  tbody.innerHTML = rows.map(i => {
    const ramStr = i.ram_gb !== null && i.ram_gb !== undefined
      ? (i.ram_gb < 1 ? (i.ram_gb * 1024).toFixed(0) + ' MB' : i.ram_gb + ' GB')
      : '-';
    let linkPill = '<span class="inv-link-pill none">no link</span>';
    if(i.linked_host){
      const lh = i.linked_host;
      const cls = lh.status === 'DEGRADED' ? 'degraded' : lh.is_up ? 'up' : (lh.status === 'IDLE' ? 'idle' : 'down');
      linkPill = '<span class="inv-link-pill ' + cls + '">' + escapeHtml(lh.status) + '</span>';
    }
    return '<tr class="inv-row" onclick="openInventoryDrawer(' + i.id + ')">'
      + '<td><div class="inv-system">' + escapeHtml(i.system || '') + '</div>'
      + (i.role ? '<div class="inv-role">' + escapeHtml(i.role) + '</div>' : '') + '</td>'
      + '<td><span class="inv-cat-tag">' + escapeHtml(i.category || '-') + '</span></td>'
      + '<td><div>' + escapeHtml(i.cpu || '-') + '</div>'
      + (i.architecture ? '<div class="inv-mono">' + escapeHtml(i.architecture) + '</div>' : '') + '</td>'
      + '<td class="inv-mono">' + ramStr + '</td>'
      + '<td>' + escapeHtml(i.os || '-') + '</td>'
      + '<td>' + linkPill + '</td>'
      + '</tr>';
  }).join('');
}''',
        '''  tbody.innerHTML = rows.map(i => {
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
}'''
    ),

    # 14. Add the device_type dropdown to the inventory editor modal at the
    # very top of the form. We anchor on the existing System name field.
    (
        '''        <label class="full">System name <span style="color:var(--red);text-transform:none;letter-spacing:0">*</span><input type="text" class="inv-f-system" required></label>''',
        '''        <label class="full">Type
          <select class="inv-f-device_type" onchange="onInvTypeChange(this.value)">
            <option value="host">Host (computer/server)</option>
            <option value="network">Network device (switch/router/AP)</option>
            <option value="ups">UPS (power)</option>
            <option value="disk">Disk / drive</option>
            <option value="peripheral">Peripheral / other</option>
          </select>
        </label>
        <label class="full">System name <span style="color:var(--red);text-transform:none;letter-spacing:0">*</span><input type="text" class="inv-f-system" required></label>'''
    ),

    # 15. Wrap host-specific fields in a div we can hide based on type.
    # The host-specific fields are everything from CPU through TPM. We
    # add a wrapper div with a class so JS can toggle it.
    # We achieve this by anchoring on the CPU label and adding a wrapper
    # before it; the closing wrapper goes after TPM version.
    (
        '''        <label>CPU<input type="text" class="inv-f-cpu" placeholder="e.g. Ryzen 5 5600X"></label>''',
        '''        <div class="inv-host-fields" style="display:contents">
        <label>CPU<input type="text" class="inv-f-cpu" placeholder="e.g. Ryzen 5 5600X"></label>'''
    ),

    # 16. Close the wrapper and add a slot for type-specific fields after
    # the TPM version label. The slot will be populated by JS based on type.
    (
        '''        <label>TPM version<input type="text" class="inv-f-tpm" placeholder="2 / None / etc."></label>''',
        '''        <label>TPM version<input type="text" class="inv-f-tpm" placeholder="2 / None / etc."></label>
        </div>
        <div class="inv-type-fields" id="inv-type-fields" style="display:contents"></div>'''
    ),

    # 17. JS: handle type changes in the editor + populate type-specific
    # field rows. We add helpers near the existing openInventoryEditor.
    (
        '''async function openInventoryEditor(existingId){''',
        '''const INVENTORY_TYPE_PROPERTIES = {
  host: [],
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
};

function onInvTypeChange(newType){
  const hostFields = document.querySelector(".inv-host-fields");
  const typeSlot   = document.getElementById("inv-type-fields");
  if(!hostFields || !typeSlot) return;
  // Show host-specific fields only for host type
  hostFields.style.display = (newType === "host") ? "contents" : "none";
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

async function openInventoryEditor(existingId){'''
    ),

    # 18. In openInventoryEditor: when loading an existing record, set the
    # type dropdown + populate type-specific properties + call onInvTypeChange.
    # We anchor on the place where existing fields are populated.
    (
        '''  if(existingId){
    try {
      const res = await fetch('/api/inventory/' + existingId);
      if(res.ok){
        loadedRec = await res.json();
        ['system','category','role','cpu','ram_gb','gpu','architecture','os','cpu_score','tdp_watts','tpm','mac','ip','serial','notes'].forEach(f => {
          const el = document.querySelector('.inv-f-' + f);
          if(el && loadedRec[f] !== null && loadedRec[f] !== undefined) el.value = loadedRec[f];
        });
      }
    } catch(e){}
  }''',
        '''  if(existingId){
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
  }'''
    ),

    # 19. submitInventory: include device_type and properties in saved data
    (
        '''async function submitInventory(ev){
  if(ev) ev.preventDefault();
  const data = {};
  ['system','category','role','cpu','ram_gb','gpu','architecture','os','cpu_score','tdp_watts','tpm','mac','ip','serial','notes'].forEach(f => {
    const el = document.querySelector('.inv-f-' + f);
    if(el) data[f] = el.value.trim();
  });''',
        '''async function submitInventory(ev){
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
  data.properties = props;'''
    ),

    # 20. CSS: add styles for the new chip rows and type chips
    (
        '''.inv-chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px}''',
        '''.inv-chips{display:flex;flex-direction:column;gap:8px;margin-bottom:14px}
.inv-chip-row{display:flex;flex-wrap:wrap;gap:6px}
.inv-chip-row-types .inv-chip{font-weight:500}
.inv-chip-row-cats .inv-chip{font-size:10px;opacity:.9}'''
    ),

    # 21. Render type-specific drawer view. Replace the spec rows with
    # a type-aware list. Anchor on the current renderInventoryDrawer's
    # fields array.
    (
        '''  const fields = [
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
  ];''',
        '''  const dtype = rec.device_type || 'host';
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
  }'''
    ),

    # 22. Update the inventory drawer header to also show the type as a tag
    (
        '''  if(meta){
    meta.innerHTML = '<span>' + escapeHtml(rec.category || '-') + '</span>';
    if(rec.architecture){
      meta.innerHTML += '<span>·</span><span class="inv-mono">' + escapeHtml(rec.architecture) + '</span>';
    }
  }''',
        '''  if(meta){
    const dtypeLabel = {host:'Host', network:'Network', ups:'UPS', disk:'Disk', peripheral:'Peripheral'}[rec.device_type || 'host'] || 'Host';
    meta.innerHTML = '<span class="inv-mono" style="background:var(--blue-bg);color:var(--blue);padding:2px 7px;border-radius:3px;font-size:10px">' + escapeHtml(dtypeLabel) + '</span>'
      + '<span>·</span><span>' + escapeHtml(rec.category || '-') + '</span>';
    if(rec.architecture && (rec.device_type || 'host') === 'host'){
      meta.innerHTML += '<span>·</span><span class="inv-mono">' + escapeHtml(rec.architecture) + '</span>';
    }
  }'''
    ),

    # 23. Filter export to host-type records only. Per spec: peripherals are
    # UI-only, the existing XLSX format stays unchanged. Without this filter,
    # the export would include every type with mostly-empty CPU/RAM/OS columns.
    (
        '''        records = inventory_db.list_all()
        for row_idx, rec in enumerate(records, start=2):''',
        '''        # Per spec: export host-type records only. Peripherals are UI-only.
        records = [r for r in inventory_db.list_all()
                   if (r.get("device_type") or "host") == "host"]
        for row_idx, rec in enumerate(records, start=2):'''
    ),

    # 24. Bump version
    (
        'netwatch v3.15 - raspberry pi',
        'netwatch v3.16 - raspberry pi'
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

    if "export_inventory_to_xlsx" not in content:
        print("ERROR: This patch requires patch_inv_export first.")
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

    if 'VERSION = "3.15"' in content:
        content = content.replace('VERSION = "3.15"', 'VERSION = "3.16"', 1)

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
    print("  2. Open the Inventory tab. Your existing 22 records become Hosts.")
    print("     The metrics row now shows 'Total devices' alongside 'Hosts',")
    print("     and a new Type filter chip row appears above Categories.")
    print("  3. Click '+ Add' - the editor now starts with a Type dropdown.")
    print("     Pick UPS / Network / Disk / Peripheral to see type-specific")
    print("     fields appear.")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
