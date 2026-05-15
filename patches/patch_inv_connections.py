#!/usr/bin/env python3
"""
netwatch patch: inventory connections data model.

Adds first-class tracking of physical/logical connections between
inventory devices. This is the foundation patch for the upcoming web
topology view - that visualization will consume this data to render
a force-directed network graph.

What this enables:
  - Record that "NAS Machine V3" is plugged into "Main Office Switch" port 4
  - See, from any device's drawer, both directions of its connections:
    "plugged into" (outbound) and "things plugged into me" (inbound)
  - Track connection types beyond just Ethernet: fiber, power, USB, console
  - Port-aware UX for switches: when adding a connection TO a network device
    with a port_count, the "to port" field becomes a numbered dropdown that
    indicates which ports are already in use.

What this patch does NOT do (yet):
  - The web topology visualization itself (next patch)
  - Bandwidth or utilization tracking (future arc, requires SNMP)
  - LLDP/CDP autodiscovery (most consumer gear doesn't support it)
  - XLSX import/export of connections (too relational for flat sheets)

Schema:
  CREATE TABLE inventory_connections (
    id INTEGER PRIMARY KEY,
    from_device_id INTEGER NOT NULL  -- "this end" of the cable (host side)
    to_device_id   INTEGER NOT NULL, -- "the other end" (switch/upstream)
    from_port      TEXT,             -- optional ("eth0", "WAN", etc.)
    to_port        TEXT,             -- recommended (port number on switch)
    connection_type TEXT DEFAULT 'ethernet',  -- ethernet/fiber/power/usb/console
    notes          TEXT,
    created_at     INTEGER NOT NULL,
    FOREIGN KEY (from_device_id) REFERENCES inventory(id) ON DELETE CASCADE,
    FOREIGN KEY (to_device_id)   REFERENCES inventory(id) ON DELETE CASCADE
  );
  CREATE INDEX idx_conn_from ON inventory_connections(from_device_id);
  CREATE INDEX idx_conn_to   ON inventory_connections(to_device_id);

API:
  GET    /api/inventory/<id>/connections     -- list both directions
  POST   /api/inventory/<id>/connections     -- create a new connection
  PUT    /api/connections/<conn_id>          -- update ports/type/notes
  DELETE /api/connections/<conn_id>          -- remove a connection

UI:
  New "Connections" section in the inventory drawer, after Specifications.
  Shows outbound + inbound, with inline add form (device picker + port).

Must be applied AFTER patch_inv_per_type_tables.py.

Run once from ~/netwatch/:
    python3 patch_inv_connections.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_connections.
Idempotent - safe to re-run.
"""

import os, shutil, sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_connections"
SENTINEL = "inventory_connections"


# ─── Backend: schema migration + InventoryDB methods ─────────────────────────

NEW_DB_METHODS = '''    # ─── Connections (inventory_connections table) ──────────────────────────
    # Connection types we accept. Anything else gets coerced to "ethernet".
    CONNECTION_TYPES = ("ethernet", "fiber", "power", "usb", "console", "other")

    def _normalize_conn_type(self, t):
        if t is None: return "ethernet"
        s = str(t).strip().lower()
        return s if s in self.CONNECTION_TYPES else "ethernet"

    def list_connections_for_device(self, device_id):
        """Return all connections involving this device, both directions.

        Each result includes the OTHER device\'s id and name (joined server-side
        so the UI doesn\'t need a second call), plus a "direction" field of
        either "out" (this device is the from end) or "in" (this device is
        the to end).
        """
        with self.lock:
            cur = self.conn.execute(
                "SELECT c.id, c.from_device_id, c.to_device_id, "
                "c.from_port, c.to_port, c.connection_type, c.notes, c.created_at, "
                "f.system AS from_name, f.device_type AS from_type, "
                "t.system AS to_name,   t.device_type AS to_type "
                "FROM inventory_connections c "
                "JOIN inventory f ON f.id = c.from_device_id "
                "JOIN inventory t ON t.id = c.to_device_id "
                "WHERE c.from_device_id = ? OR c.to_device_id = ? "
                "ORDER BY c.connection_type, c.created_at",
                (device_id, device_id),
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        for r in rows:
            r["direction"] = "out" if r["from_device_id"] == device_id else "in"
        return rows

    def list_all_connections(self):
        """Return all connections in the system. Used by the topology view."""
        with self.lock:
            cur = self.conn.execute(
                "SELECT id, from_device_id, to_device_id, from_port, to_port, "
                "connection_type, notes, created_at FROM inventory_connections "
                "ORDER BY connection_type, created_at"
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def create_connection(self, data):
        """Create a new connection row. Returns (id, error_msg)."""
        try:
            from_id = int(data.get("from_device_id"))
            to_id   = int(data.get("to_device_id"))
        except (TypeError, ValueError):
            return None, "from_device_id and to_device_id required"
        if from_id == to_id:
            return None, "cannot connect a device to itself"
        # Verify both ends exist
        with self.lock:
            cur = self.conn.execute(
                "SELECT id FROM inventory WHERE id IN (?, ?)", (from_id, to_id)
            )
            ids = {r[0] for r in cur.fetchall()}
            if from_id not in ids or to_id not in ids:
                return None, "one or both devices do not exist"
        ctype = self._normalize_conn_type(data.get("connection_type"))
        from_port = (data.get("from_port") or "").strip() or None
        to_port   = (data.get("to_port") or "").strip() or None
        notes     = (data.get("notes") or "").strip() or None
        ts = int(time.time())
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO inventory_connections "
                "(from_device_id, to_device_id, from_port, to_port, "
                "connection_type, notes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (from_id, to_id, from_port, to_port, ctype, notes, ts),
            )
            return cur.lastrowid, None

    def update_connection(self, conn_id, data):
        """Update fields of an existing connection. Returns (ok, error_msg)."""
        fields, values = [], []
        if "from_port" in data:
            fields.append("from_port = ?")
            values.append((data.get("from_port") or "").strip() or None)
        if "to_port" in data:
            fields.append("to_port = ?")
            values.append((data.get("to_port") or "").strip() or None)
        if "connection_type" in data:
            fields.append("connection_type = ?")
            values.append(self._normalize_conn_type(data.get("connection_type")))
        if "notes" in data:
            fields.append("notes = ?")
            values.append((data.get("notes") or "").strip() or None)
        if not fields:
            return False, "no fields to update"
        values.append(conn_id)
        with self.lock:
            cur = self.conn.execute(
                "UPDATE inventory_connections SET " + ", ".join(fields)
                + " WHERE id = ?", tuple(values)
            )
            if cur.rowcount == 0:
                return False, "connection not found"
        return True, None

    def delete_connection(self, conn_id):
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM inventory_connections WHERE id = ?", (conn_id,)
            )
            if cur.rowcount == 0:
                return False, "connection not found"
        return True, None

'''


PATCHES = [
    # ──────────────────────────────────────────────────────────────────────
    # 1. Schema migration: add the inventory_connections table.
    # We piggyback on the existing InventoryDB.__init__ which already runs
    # ALTER TABLE migrations. Adding a CREATE TABLE IF NOT EXISTS is safer
    # than ALTER since SQLite is happy to re-run it as a no-op.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''        try:
            self.conn.execute("ALTER TABLE inventory ADD COLUMN properties TEXT")
        except _sqlite3.OperationalError:
            pass
        logging.info("InventoryDB: schema ready")''',
        '''        try:
            self.conn.execute("ALTER TABLE inventory ADD COLUMN properties TEXT")
        except _sqlite3.OperationalError:
            pass
        # Connections table - records edges between inventory devices.
        # CREATE TABLE IF NOT EXISTS is idempotent so re-runs are safe.
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS inventory_connections (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                from_device_id  INTEGER NOT NULL,
                to_device_id    INTEGER NOT NULL,
                from_port       TEXT,
                to_port         TEXT,
                connection_type TEXT DEFAULT \'ethernet\',
                notes           TEXT,
                created_at      INTEGER NOT NULL,
                FOREIGN KEY (from_device_id) REFERENCES inventory(id) ON DELETE CASCADE,
                FOREIGN KEY (to_device_id)   REFERENCES inventory(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_conn_from ON inventory_connections(from_device_id);
            CREATE INDEX IF NOT EXISTS idx_conn_to   ON inventory_connections(to_device_id);
        """)
        # SQLite needs PRAGMA foreign_keys=ON for CASCADE to actually work.
        # The HistoryDB connection might not have it on; flip it now.
        self.conn.execute("PRAGMA foreign_keys = ON")
        logging.info("InventoryDB: schema ready")'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 2. Insert the new connection methods into InventoryDB. We anchor on
    # the delete() method, putting the new methods right before it for
    # logical grouping (single-record CRUD methods stay together).
    # ──────────────────────────────────────────────────────────────────────
    (
        '''    def delete(self, inv_id):
        with self.lock:
            cur = self.conn.execute("DELETE FROM inventory WHERE id = ?", (inv_id,))
            if cur.rowcount == 0:
                return False, "record not found"
        return True, None''',
        NEW_DB_METHODS + '''    def delete(self, inv_id):
        with self.lock:
            cur = self.conn.execute("DELETE FROM inventory WHERE id = ?", (inv_id,))
            if cur.rowcount == 0:
                return False, "record not found"
        return True, None'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 3. GET endpoints. Two new routes:
    #   /api/inventory/<id>/connections     - list connections for a device
    #   /api/connections                    - list ALL (used by topology viz)
    # We anchor on the existing /api/inventory/<id> handler, inserting BEFORE
    # it so the more specific route matches first. The catch-all
    # /api/inventory/<id>/ handler currently splits by / and takes [-1], so
    # we need to filter our /connections suffix out before it does.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''            if self.path.startswith("/api/inventory/") and self.path != "/api/inventory/":
                try:
                    inv_id = int(self.path.split("/")[-1])
                except ValueError:
                    self._send_json(400, {"error": "invalid id"})
                    return''',
        '''            if self.path == "/api/connections":
                # All-connections list (used by the topology viz)
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"})
                    return
                try:
                    self._send_json(200, {"items": inventory_db.list_all_connections()})
                except Exception as e:
                    logging.exception("connections list error")
                    self._send_json(500, {"error": str(e)})
                return

            if (self.path.startswith("/api/inventory/")
                    and self.path.endswith("/connections")):
                # /api/inventory/<id>/connections - per-device
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"})
                    return
                try:
                    parts = self.path.split("/")
                    inv_id = int(parts[-2])
                except (ValueError, IndexError):
                    self._send_json(400, {"error": "invalid id"})
                    return
                try:
                    items = inventory_db.list_connections_for_device(inv_id)
                    self._send_json(200, {"items": items})
                except Exception as e:
                    logging.exception("connections fetch error")
                    self._send_json(500, {"error": str(e)})
                return

            if self.path.startswith("/api/inventory/") and self.path != "/api/inventory/":
                try:
                    inv_id = int(self.path.split("/")[-1])
                except ValueError:
                    self._send_json(400, {"error": "invalid id"})
                    return'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 4. POST/PUT/DELETE endpoints in do_POST. We anchor on the existing
    # /api/inventory-import handler and insert our new routes before it.
    # The existing /api/inventory/<id> catch-all (line ~5830) needs us to
    # be ahead of it, but the catch-all is in the same do_POST and our
    # new routes have more specific suffixes, so anchor placement matters.
    # We piggyback on the catch-all block and insert before it.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''            if self.path.startswith("/api/inventory/") and self.path.endswith("/delete"):
                if not self._require_auth():
                    return
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"})
                    return
                try:
                    parts = self.path.split("/")
                    inv_id = int(parts[-2])
                except (ValueError, IndexError):
                    self._send_json(400, {"error": "invalid id"})
                    return
                ok, err = inventory_db.delete(inv_id)
                if not ok:
                    self._send_json(404, {"error": err})
                    return
                self._send_json(200, {"ok": True})
                return''',
        '''            if self.path.startswith("/api/inventory/") and self.path.endswith("/delete"):
                if not self._require_auth():
                    return
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"})
                    return
                try:
                    parts = self.path.split("/")
                    inv_id = int(parts[-2])
                except (ValueError, IndexError):
                    self._send_json(400, {"error": "invalid id"})
                    return
                ok, err = inventory_db.delete(inv_id)
                if not ok:
                    self._send_json(404, {"error": err})
                    return
                self._send_json(200, {"ok": True})
                return

            if (self.path.startswith("/api/inventory/")
                    and self.path.endswith("/connections")):
                # POST /api/inventory/<id>/connections - create
                if not self._require_auth():
                    return
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"})
                    return
                try:
                    parts = self.path.split("/")
                    inv_id = int(parts[-2])
                except (ValueError, IndexError):
                    self._send_json(400, {"error": "invalid id"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length).decode()
                    data = json.loads(body)
                    # Force the from_device_id to match the URL\'s inv_id so
                    # clients can\'t spoof the source device.
                    data["from_device_id"] = inv_id
                    new_id, err = inventory_db.create_connection(data)
                    if err:
                        self._send_json(400, {"error": err})
                        return
                    self._send_json(200, {"ok": True, "id": new_id})
                except json.JSONDecodeError:
                    self._send_json(400, {"error": "invalid JSON"})
                except Exception as e:
                    logging.exception("connection create error")
                    self._send_json(500, {"error": str(e)})
                return

            if (self.path.startswith("/api/connections/")
                    and self.path.endswith("/delete")):
                # DELETE-via-POST /api/connections/<id>/delete
                if not self._require_auth():
                    return
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"})
                    return
                try:
                    parts = self.path.split("/")
                    conn_id = int(parts[-2])
                except (ValueError, IndexError):
                    self._send_json(400, {"error": "invalid id"})
                    return
                ok, err = inventory_db.delete_connection(conn_id)
                if not ok:
                    self._send_json(404, {"error": err})
                    return
                self._send_json(200, {"ok": True})
                return

            if self.path.startswith("/api/connections/"):
                # POST /api/connections/<id> - update fields
                if not self._require_auth():
                    return
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"})
                    return
                try:
                    conn_id = int(self.path.split("/")[-1])
                except ValueError:
                    self._send_json(400, {"error": "invalid id"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length).decode()
                    data = json.loads(body)
                    ok, err = inventory_db.update_connection(conn_id, data)
                    if not ok:
                        self._send_json(400, {"error": err})
                        return
                    self._send_json(200, {"ok": True})
                except json.JSONDecodeError:
                    self._send_json(400, {"error": "invalid JSON"})
                except Exception as e:
                    logging.exception("connection update error")
                    self._send_json(500, {"error": str(e)})
                return'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 5. Frontend: drawer Connections section. Inserted into
    # renderInventoryDrawer right before the final assignment. We split the
    # render so the connections fetch is async without blocking the rest.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  document.getElementById('drawer-body').innerHTML = linkHtml + specsHtml + notesHtml + '<div class="d-section">' + editBtn + '</div>';
  document.getElementById('drawer-body').dataset.hostIp = 'inv:' + rec.id;
}''',
        '''  // Placeholder for the connections section - filled async after main render
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
    ethernet: '\u2500\u2500',  // box drawing horizontal
    fiber:    '\u2248',        // wave (suggestive of light/optical)
    power:    '\u26a1',        // lightning bolt
    usb:      '\u21cc',        // double arrow
    console:  '\u2192',        // arrow
    other:    '\u00b7',
  };

  let html = '';

  if(out.length){
    html += '<div class="conn-group"><div class="conn-group-label">Plugged into</div>';
    out.forEach(c => {
      const icon = typeIcon[c.connection_type] || typeIcon.other;
      const portInfo = [];
      if(c.from_port) portInfo.push('via ' + escapeHtml(c.from_port));
      if(c.to_port)   portInfo.push('port ' + escapeHtml(c.to_port));
      const portStr = portInfo.length ? ' <span class="conn-port">(' + portInfo.join(\', \') + \')</span>\' : \'\';
      html += \'<div class="conn-row" onclick="event.stopPropagation()">\'
        + \'<span class="conn-icon" title="\' + escapeHtml(c.connection_type) + \'">\' + icon + \'</span>\'
        + \'<span class="conn-target" onclick="openInventoryDrawer(\' + c.to_device_id + \')">\'
        + escapeHtml(c.to_name) + portStr + \'</span>\'
        + \'<button class="conn-del" title="Remove connection" onclick="deleteConnection(\' + c.id + \', \' + deviceId + \')">\u00d7</button>\'
        + \'</div>\';
    });
    html += \'</div>\';
  }

  if(inb.length){
    html += \'<div class="conn-group"><div class="conn-group-label">Things plugged into me</div>\';
    inb.forEach(c => {
      const icon = typeIcon[c.connection_type] || typeIcon.other;
      const portInfo = [];
      if(c.to_port)   portInfo.push(\'port \' + escapeHtml(c.to_port));
      if(c.from_port) portInfo.push(\'via \' + escapeHtml(c.from_port));
      const portStr = portInfo.length ? \' <span class="conn-port">(\' + portInfo.join(\', \') + \')</span>\' : \'\';
      html += \'<div class="conn-row" onclick="event.stopPropagation()">\'
        + \'<span class="conn-icon" title="\' + escapeHtml(c.connection_type) + \'">\' + icon + \'</span>\'
        + \'<span class="conn-target" onclick="openInventoryDrawer(\' + c.from_device_id + \')">\'
        + escapeHtml(c.from_name) + portStr + \'</span>\'
        + \'<button class="conn-del" title="Remove connection" onclick="deleteConnection(\' + c.id + \', \' + deviceId + \')">\u00d7</button>\'
        + \'</div>\';
    });
    html += \'</div>\';
  }

  if(!out.length && !inb.length){
    html += \'<div class="conn-empty">No connections recorded yet.</div>\';
  }

  // Inline add form
  html += \'<div class="conn-add">\';
  if(_connFormState.open && _connFormState.deviceId === deviceId){
    // Build the device dropdown excluding self
    const others = allInv.filter(i => i.id !== deviceId);
    others.sort((a, b) => (a.system || \'\').localeCompare(b.system || \'\'));
    const deviceOpts = \'<option value="">-- pick a device --</option>\'
      + others.map(i => {
          const dt = i.device_type || \'host\';
          const label = i.system + \' (\' + dt + \')\';
          return \'<option value="\' + i.id + \'">\' + escapeHtml(label) + \'</option>\';
        }).join(\'\');

    html += \'<div class="conn-form">\'
      + \'<div class="conn-form-row">\'
        + \'<label>Connect to<select class="conn-f-target" onchange="onConnTargetChange(this.value)">\' + deviceOpts + \'</select></label>\'
        + \'<label>Type<select class="conn-f-type"><option value="ethernet">Ethernet</option><option value="fiber">Fiber</option><option value="power">Power</option><option value="usb">USB</option><option value="console">Console</option><option value="other">Other</option></select></label>\'
      + \'</div>\'
      + \'<div class="conn-form-row">\'
        + \'<label>From port (this device, optional)<input type="text" class="conn-f-from-port" placeholder="e.g. eth0, WAN"></label>\'
        + \'<label>To port (target device)<span class="conn-f-to-port-wrap"><input type="text" class="conn-f-to-port" placeholder="e.g. 4"></span></label>\'
      + \'</div>\'
      + \'<div class="conn-form-actions">\'
        + \'<button class="btn" onclick="submitConnection(\' + deviceId + \')">Add connection</button>\'
        + \'<button class="btn btn-ghost" onclick="cancelConnection()">Cancel</button>\'
      + \'</div>\'
    + \'</div>\';
  } else {
    html += \'<button class="btn btn-ghost conn-add-btn" onclick="startAddConnection(\' + deviceId + \')">+ Add connection</button>\';
  }
  html += \'</div>\';

  return html;
}

function startAddConnection(deviceId){
  _connFormState = { open: true, deviceId: deviceId };
  loadInventoryConnections(deviceId);
}

function cancelConnection(){
  _connFormState = { open: false, deviceId: null };
  if(openDrawerIp && openDrawerIp.startsWith(\'inv:\')){
    const id = parseInt(openDrawerIp.split(\':\')[1], 10);
    loadInventoryConnections(id);
  }
}

async function onConnTargetChange(targetId){
  // If the target is a network device with a port_count, swap the to_port
  // input for a numbered dropdown showing which ports are taken.
  const wrap = document.querySelector(\'.conn-f-to-port-wrap\');
  if(!wrap) return;
  if(!targetId){
    wrap.innerHTML = \'<input type="text" class="conn-f-to-port" placeholder="e.g. 4">\';
    return;
  }
  try {
    const [recRes, connRes] = await Promise.all([
      fetch(\'/api/inventory/\' + targetId),
      fetch(\'/api/inventory/\' + targetId + \'/connections\'),
    ]);
    if(!recRes.ok){ return; }
    const rec = await recRes.json();
    const conns = connRes.ok ? (await connRes.json()).items : [];
    const props = rec.properties || {};
    const portCount = parseInt(props.port_count, 10);
    if(!portCount || portCount < 1 || portCount > 96){
      // Not a port-aware device: keep the free-text input
      wrap.innerHTML = \'<input type="text" class="conn-f-to-port" placeholder="port (optional)">\';
      return;
    }
    // Build map of port -> device using it
    const used = {};
    conns.filter(c => c.direction === \'in\' && c.to_port).forEach(c => {
      used[c.to_port] = c.from_name;
    });
    let opts = \'<option value="">-- select port --</option>\';
    for(let i = 1; i <= portCount; i++){
      const u = used[String(i)];
      opts += \'<option value="\' + i + \'"\' + (u ? \' disabled\' : \'\') + \'>Port \' + i
            + (u ? \' (in use by \' + escapeHtml(u) + \')\' : \'\')
            + \'</option>\';
    }
    wrap.innerHTML = \'<select class="conn-f-to-port">\' + opts + \'</select>\';
  } catch(e){
    /* fall back to text input - already there */
  }
}

async function submitConnection(deviceId){
  const targetEl = document.querySelector(\'.conn-f-target\');
  const typeEl   = document.querySelector(\'.conn-f-type\');
  const fpEl     = document.querySelector(\'.conn-f-from-port\');
  const tpEl     = document.querySelector(\'.conn-f-to-port\');
  if(!targetEl || !targetEl.value){
    alert(\'Please pick a device to connect to.\');
    return;
  }
  const data = {
    to_device_id:    parseInt(targetEl.value, 10),
    connection_type: typeEl ? typeEl.value : \'ethernet\',
    from_port:       fpEl ? fpEl.value.trim() : \'\',
    to_port:         tpEl ? (tpEl.value || \'\').trim() : \'\',
  };
  try {
    const res = await fetch(\'/api/inventory/\' + deviceId + \'/connections\', {
      method: \'POST\',
      headers: {\'Content-Type\': \'application/json\'},
      body: JSON.stringify(data),
    });
    if(!res.ok){
      let msg = \'Failed (HTTP \' + res.status + \')\';
      try { const j = await res.json(); if(j.error) msg = j.error; } catch(e){}
      alert(\'Could not add connection: \' + msg);
      return;
    }
    _connFormState = { open: false, deviceId: null };
    loadInventoryConnections(deviceId);
  } catch(e){
    alert(\'Network error: \' + e.message);
  }
}

async function deleteConnection(connId, deviceId){
  if(!confirm(\'Remove this connection?\')) return;
  try {
    const res = await fetch(\'/api/connections/\' + connId + \'/delete\', {method: \'POST\'});
    if(!res.ok){
      let msg = \'Failed (HTTP \' + res.status + \')\';
      try { const j = await res.json(); if(j.error) msg = j.error; } catch(e){}
      alert(\'Could not delete: \' + msg);
      return;
    }
    loadInventoryConnections(deviceId);
  } catch(e){
    alert(\'Network error: \' + e.message);
  }
}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 6. CSS for the connections section. Anchor on the existing
    # inv-link-host-card rule which is in the same conceptual neighborhood.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''.inv-link-host-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:11px 13px;cursor:pointer;transition:all .15s;display:flex;align-items:center;justify-content:space-between}
.inv-link-host-card:hover{border-color:var(--hint);background:var(--subtle)}''',
        '''.inv-link-host-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:11px 13px;cursor:pointer;transition:all .15s;display:flex;align-items:center;justify-content:space-between}
.inv-link-host-card:hover{border-color:var(--hint);background:var(--subtle)}
.conn-loading{padding:12px;color:var(--muted);font-size:12px;font-style:italic}
.conn-empty{padding:12px;color:var(--muted);font-size:12px;font-style:italic;background:var(--subtle);border:1px solid var(--border-light);border-radius:8px}
.conn-group{margin-bottom:10px}
.conn-group-label{font-family:'DM Mono',monospace;font-size:10px;letter-spacing:.07em;text-transform:uppercase;color:var(--hint);margin-bottom:6px;padding:0 2px}
.conn-row{display:grid;grid-template-columns:24px 1fr 28px;align-items:center;gap:8px;background:var(--subtle);border:1px solid var(--border-light);border-radius:8px;padding:8px 10px;margin-bottom:4px;font-size:13px}
.conn-icon{text-align:center;color:var(--hint);font-family:'DM Mono',monospace;font-size:14px}
.conn-target{cursor:pointer;color:var(--text)}
.conn-target:hover{color:var(--blue)}
.conn-port{color:var(--muted);font-size:11px;font-family:'DM Mono',monospace}
.conn-del{background:transparent;border:none;color:var(--muted);cursor:pointer;font-size:18px;line-height:1;padding:0;width:24px;height:24px;border-radius:4px;transition:all .15s}
.conn-del:hover{background:var(--red-bg);color:var(--red)}
.conn-add{margin-top:10px}
.conn-add-btn{width:100%;font-size:12px}
.conn-form{background:var(--subtle);border:1px solid var(--border-light);border-radius:8px;padding:12px;display:flex;flex-direction:column;gap:10px}
.conn-form-row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.conn-form label{display:flex;flex-direction:column;gap:4px;font-family:'DM Mono',monospace;font-size:10px;letter-spacing:.07em;text-transform:uppercase;color:var(--hint)}
.conn-form input,.conn-form select{padding:7px 9px;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--text);font-size:13px;font-family:inherit;text-transform:none;letter-spacing:0}
.conn-form input:focus,.conn-form select:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 2px var(--blue-bg)}
.conn-form-actions{display:flex;gap:8px;justify-content:flex-end}
@media (max-width:768px){.conn-form-row{grid-template-columns:1fr}}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 7. Bump version
    # ──────────────────────────────────────────────────────────────────────
    (
        'netwatch v3.17 - raspberry pi',
        'netwatch v3.18 - raspberry pi'
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

    if 'VERSION = "3.17"' in content:
        content = content.replace('VERSION = "3.17"', 'VERSION = "3.18"', 1)

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
    print("  2. Open any inventory record's drawer.")
    print("  3. Below the Specifications section is now 'Connections'.")
    print("     Click '+ Add connection', pick another device, set the type,")
    print("     fill in port info if applicable, click 'Add connection'.")
    print("  4. For switches/network devices that have port_count set in their")
    print("     properties, the 'To port' field auto-becomes a numbered dropdown")
    print("     showing which ports are already in use.")
    print()
    print("This is the foundation patch for the upcoming web topology view -")
    print("once you've recorded a few connections, the visualization patch will")
    print("have real data to render.")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
