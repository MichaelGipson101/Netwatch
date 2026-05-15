#!/usr/bin/env python3
"""
netwatch patch: inventory management.

Adds a homelab-style CMDB (Configuration Management Database) to netwatch.
The inventory is separate from monitored hosts but cross-links by MAC address:
  - A monitored host whose MAC matches an inventory record shows the
    inventory data in its detail drawer.
  - An inventory record whose MAC matches a monitored host shows live ping
    status in its detail drawer.

Inventory fields (richer than netwatch's existing 'specs'):
    category, system, role, cpu, ram_gb, gpu, architecture, os, cpu_score,
    tdp_watts, tpm, mac, ip, serial, notes

Storage: a new 'inventory' table in the existing netwatch.db SQLite database.

UI: a new 'Inventory' tab next to Topology / Hosts / Events with:
  - Aggregate metrics (total systems, total estimated power, total CPU score)
  - Filter chips by category
  - Search box (matches system, role, OS, CPU)
  - Sortable table; click a row to open detail drawer
  - "+ Add" and "Import XLSX" buttons

XLSX import:
  - Upload your spreadsheet via the dashboard
  - Idempotent: re-running just adds new records
  - Optional "replace all" mode for clean re-imports
  - Requires python3-openpyxl on the Pi (auto-installed by this patch script)

Must be applied AFTER patch_dark_polish.py.

Run once from ~/netwatch/:
    python3 patch_inventory.py
    sudo systemctl restart netwatch

Backup saved to monitor.py.bak_inventory.
Idempotent - safe to re-run.
"""

import os
import shutil
import subprocess
import sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_inventory"
SENTINEL = "class InventoryDB"  # presence means already patched


# ─── Step 0: ensure openpyxl is installed ─────────────────────────────────────

def ensure_openpyxl():
    """Best-effort install of openpyxl. Returns (installed, message)."""
    try:
        import openpyxl  # noqa: F401
        return True, f"openpyxl already available"
    except ImportError:
        pass
    print("  openpyxl not found - attempting install...")
    # Try apt first (Debian/Pi OS native)
    try:
        r = subprocess.run(
            ["sudo", "apt-get", "install", "-y", "python3-openpyxl"],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0:
            try:
                import openpyxl  # noqa: F401
                return True, "openpyxl installed via apt"
            except ImportError:
                pass
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    # Fall back to pip
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "openpyxl", "--break-system-packages"],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0:
            try:
                import openpyxl  # noqa: F401
                return True, "openpyxl installed via pip"
            except ImportError:
                pass
        return False, f"pip install failed: {r.stderr.strip()[:200]}"
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return False, f"could not install openpyxl: {e}"


# ─── New backend module: InventoryDB class + import helpers ──────────────────

NEW_INVENTORY_MODULE = '''# ============================================================================
# Inventory (CMDB)
# ============================================================================

class InventoryDB:
    """SQLite-backed inventory store. Lives in the same database as ping history
    so we have one file to back up / one connection lifecycle to manage."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS inventory (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        category     TEXT,
        system       TEXT NOT NULL,
        role         TEXT,
        cpu          TEXT,
        ram_gb       REAL,
        gpu          TEXT,
        architecture TEXT,
        os           TEXT,
        cpu_score    INTEGER,
        tdp_watts    INTEGER,
        tpm          TEXT,
        mac          TEXT,
        ip           TEXT,
        serial       TEXT,
        notes        TEXT,
        created_at   INTEGER NOT NULL,
        updated_at   INTEGER NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_inv_mac ON inventory(mac) WHERE mac IS NOT NULL AND mac != '';
    CREATE INDEX IF NOT EXISTS idx_inv_category ON inventory(category);
    CREATE INDEX IF NOT EXISTS idx_inv_system ON inventory(system);
    """

    FIELDS = [
        "category", "system", "role", "cpu", "ram_gb", "gpu", "architecture",
        "os", "cpu_score", "tdp_watts", "tpm", "mac", "ip", "serial", "notes",
    ]

    def __init__(self, history_db):
        """Reuses the connection from HistoryDB."""
        self.history_db = history_db
        self.lock = history_db.lock  # share the same lock to serialize writes
        self.conn = history_db.conn
        self.conn.executescript(self.SCHEMA)
        logging.info("InventoryDB: schema ready")

    @staticmethod
    def normalize_mac(mac):
        """Lowercase + colon-separated. Returns '' for falsy input."""
        if not mac:
            return ""
        s = str(mac).strip().lower()
        # Strip out anything that isn't hex
        clean = "".join(c for c in s if c in "0123456789abcdef")
        if len(clean) != 12:
            # Don't reformat if it's not a valid 12-hex-char MAC
            return s
        return ":".join(clean[i:i+2] for i in range(0, 12, 2))

    def list_all(self):
        with self.lock:
            cur = self.conn.execute(
                "SELECT id, category, system, role, cpu, ram_gb, gpu, architecture, "
                "os, cpu_score, tdp_watts, tpm, mac, ip, serial, notes, "
                "created_at, updated_at FROM inventory ORDER BY system COLLATE NOCASE"
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get(self, inv_id):
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
            return dict(zip(cols, row))

    def find_by_mac(self, mac):
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
            return dict(zip(cols, row))

    def create(self, data):
        """Insert a new record. data is a dict of field values."""
        clean = self._clean_input(data)
        if not clean.get("system"):
            return None, "system name is required"
        # Check for MAC conflict
        if clean.get("mac"):
            existing = self.find_by_mac(clean["mac"])
            if existing:
                return None, f"a record with MAC {clean['mac']} already exists ({existing['system']})"
        ts = int(time.time())
        with self.lock:
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
            return cur.lastrowid, None

    def update(self, inv_id, data):
        clean = self._clean_input(data)
        if "system" in clean and not clean["system"]:
            return False, "system name cannot be empty"
        # MAC conflict check (if MAC is being set, ensure no other record has it)
        if clean.get("mac"):
            existing = self.find_by_mac(clean["mac"])
            if existing and existing["id"] != inv_id:
                return False, f"a different record with MAC {clean['mac']} already exists"
        ts = int(time.time())
        sets = []
        vals = []
        for f in self.FIELDS:
            if f in clean:
                sets.append(f"{f} = ?")
                vals.append(clean[f])
        if not sets:
            return True, None  # nothing to update
        sets.append("updated_at = ?")
        vals.append(ts)
        vals.append(inv_id)
        with self.lock:
            cur = self.conn.execute(
                f"UPDATE inventory SET {', '.join(sets)} WHERE id = ?", vals
            )
            if cur.rowcount == 0:
                return False, "record not found"
        return True, None

    def delete(self, inv_id):
        with self.lock:
            cur = self.conn.execute("DELETE FROM inventory WHERE id = ?", (inv_id,))
            if cur.rowcount == 0:
                return False, "record not found"
        return True, None

    def replace_all(self, records):
        """Wipe inventory and bulk-insert. Used by 'Replace all' import mode."""
        with self.lock:
            self.conn.execute("DELETE FROM inventory")
        ok, fail = 0, []
        for rec in records:
            inv_id, err = self.create(rec)
            if err:
                fail.append({"system": rec.get("system", "?"), "error": err})
            else:
                ok += 1
        return ok, fail

    def _clean_input(self, data):
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
        return out


def import_inventory_from_xlsx(inventory_db, xlsx_bytes, mode="add"):
    """Parse an uploaded xlsx and insert records.

    Expected columns (by header name, case-insensitive, flexible):
      Category, System, Role / Status, CPU, RAM_GB, GPU, Architecture, OS,
      Estimated_CPU_Score, Max_TDP_Watts, TPM_Version, MAC_Primary,
      IP_Address, Service_Tag_Serial

    mode='add'      -> insert new records, skip ones whose MAC or
                       (system+ram+cpu) tuple matches existing
    mode='replace'  -> wipe inventory first, then insert all
    Returns (added_count, skipped_count, errors_list).
    """
    try:
        import openpyxl
    except ImportError:
        return 0, 0, [{"row": 0, "error": "openpyxl not installed"}]

    import io
    try:
        wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
    except Exception as e:
        return 0, 0, [{"row": 0, "error": f"could not parse xlsx: {e}"}]

    ws = wb.active
    # Read first row as headers
    rows = ws.iter_rows(values_only=True)
    try:
        header_row = next(rows)
    except StopIteration:
        return 0, 0, [{"row": 0, "error": "spreadsheet is empty"}]

    # Map header names to our field names (case-insensitive, flexible)
    HEADER_MAP = {
        "category": "category",
        "system": "system",
        "role / status": "role",
        "role/status": "role",
        "role": "role",
        "cpu": "cpu",
        "ram_gb": "ram_gb",
        "ram (gb)": "ram_gb",
        "ram": "ram_gb",
        "gpu": "gpu",
        "architecture": "architecture",
        "arch": "architecture",
        "os": "os",
        "estimated_cpu_score": "cpu_score",
        "cpu_score": "cpu_score",
        "cpu score": "cpu_score",
        "max_tdp_watts": "tdp_watts",
        "tdp": "tdp_watts",
        "tdp_watts": "tdp_watts",
        "tpm_version": "tpm",
        "tpm": "tpm",
        "mac_primary": "mac",
        "mac": "mac",
        "mac address": "mac",
        "ip_address": "ip",
        "ip": "ip",
        "service_tag_serial": "serial",
        "serial": "serial",
        "service tag": "serial",
        "notes": "notes",
    }

    col_to_field = {}
    for idx, header in enumerate(header_row):
        if header is None: continue
        key = str(header).strip().lower()
        field = HEADER_MAP.get(key)
        if field:
            col_to_field[idx] = field

    if "system" not in col_to_field.values():
        return 0, 0, [{"row": 0, "error": "no 'System' column found in spreadsheet"}]

    # Build list of records
    records = []
    for row_idx, row in enumerate(rows, start=2):  # start=2 because row 1 was header
        rec = {}
        for col_idx, value in enumerate(row):
            field = col_to_field.get(col_idx)
            if field and value is not None:
                rec[field] = value
        if not rec.get("system"):
            continue  # skip blank rows
        records.append(rec)

    if mode == "replace":
        ok, fail = inventory_db.replace_all(records)
        return ok, 0, fail

    # 'add' mode: skip duplicates by MAC, otherwise insert
    added = 0
    skipped = 0
    errors = []
    existing = inventory_db.list_all()
    existing_macs = {InventoryDB.normalize_mac(r.get("mac")) for r in existing if r.get("mac")}
    existing_systems = {(r.get("system") or "").lower() for r in existing}
    for rec in records:
        mac_norm = InventoryDB.normalize_mac(rec.get("mac"))
        sys_lower = (rec.get("system") or "").lower()
        if mac_norm and mac_norm in existing_macs:
            skipped += 1
            continue
        if not mac_norm and sys_lower in existing_systems:
            skipped += 1
            continue
        inv_id, err = inventory_db.create(rec)
        if err:
            errors.append({"system": rec.get("system"), "error": err})
        else:
            added += 1
            if mac_norm: existing_macs.add(mac_norm)
            existing_systems.add(sys_lower)
    return added, skipped, errors


# ============================================================================
# Wake-on-LAN
# ============================================================================'''


def main():
    if not os.path.isfile(TARGET):
        print(f"ERROR: {TARGET} not found.")
        sys.exit(1)

    content = open(TARGET).read()

    if SENTINEL in content:
        print(f"NOTE: '{SENTINEL}' found - patch already applied.")
        sys.exit(0)

    if "--text-inverse-muted" not in content:
        print("ERROR: This patch requires patch_dark_polish first.")
        sys.exit(1)

    # ── Step 0: try to install openpyxl
    print("Checking for openpyxl...")
    ok, msg = ensure_openpyxl()
    print(f"  {msg}")
    if not ok:
        print("  WARNING: openpyxl is not available. The patch will still apply,")
        print("  but XLSX import will fail. Install manually:")
        print("    sudo apt-get install python3-openpyxl  # or:")
        print("    python3 -m pip install openpyxl --break-system-packages")

    shutil.copy2(TARGET, BACKUP)
    print(f"[OK] Backed up {TARGET} -> {BACKUP}")

    # ── 1. Insert InventoryDB class right before "# Wake-on-LAN" (the existing anchor)
    OLD = '''# ============================================================================
# Wake-on-LAN
# ============================================================================'''
    if content.count(OLD) != 1:
        print(f"[FAIL] WoL anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW_INVENTORY_MODULE, 1)
    print("[OK] Inserted InventoryDB module")

    # ── 2. main(): instantiate InventoryDB after HistoryDB
    OLD = '''    history_db = HistoryDB(db_path, retention_days=retention_days)
    print(f"[netwatch] History DB -> {db_path} (retention {retention_days} days)")'''
    NEW = '''    history_db = HistoryDB(db_path, retention_days=retention_days)
    print(f"[netwatch] History DB -> {db_path} (retention {retention_days} days)")
    inventory_db = InventoryDB(history_db)
    inv_count = len(inventory_db.list_all())
    print(f"[netwatch] Inventory  -> {inv_count} record(s)")'''
    if content.count(OLD) != 1:
        print(f"[FAIL] HistoryDB instantiation anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] InventoryDB instantiated in main()")

    # ── 3. start_web_server signature: accept inventory_db
    OLD = '''def start_web_server(host_manager, settings, config_path, port, stop_event, incident_log=None, auth_manager=None):
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(host_manager, settings, config_path, incident_log, auth_manager))'''
    NEW = '''def start_web_server(host_manager, settings, config_path, port, stop_event, incident_log=None, auth_manager=None, inventory_db=None):
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(host_manager, settings, config_path, incident_log, auth_manager, inventory_db))'''
    if content.count(OLD) != 1:
        print(f"[FAIL] start_web_server signature: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] start_web_server accepts inventory_db")

    # ── 4. make_handler signature
    OLD = '''def make_handler(host_manager, settings, config_path, incident_log=None, auth_manager=None):'''
    NEW = '''def make_handler(host_manager, settings, config_path, incident_log=None, auth_manager=None, inventory_db=None):'''
    if content.count(OLD) != 1:
        print(f"[FAIL] make_handler signature: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] make_handler accepts inventory_db")

    # ── 5. main(): pass inventory_db to web server thread
    OLD = '''        wt = threading.Thread(
            target=start_web_server,
            args=(host_manager, web_settings, config_path, args.port, stop_event, incident_log, auth_manager),
            daemon=True
        )'''
    NEW = '''        wt = threading.Thread(
            target=start_web_server,
            args=(host_manager, web_settings, config_path, args.port, stop_event, incident_log, auth_manager, inventory_db),
            daemon=True
        )'''
    if content.count(OLD) != 1:
        print(f"[FAIL] web server thread args: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] inventory_db threaded through to web server")

    # ── 6. Add inventory API endpoints. We anchor on /api/auth/users GET.
    OLD = '''            if self.path == "/api/auth/users":
                if not self._require_auth(admin_only=True):
                    return
                self._send_json(200, {"users": auth_manager.list_users()})
                return'''
    NEW = '''            if self.path == "/api/auth/users":
                if not self._require_auth(admin_only=True):
                    return
                self._send_json(200, {"users": auth_manager.list_users()})
                return

            if self.path == "/api/inventory":
                # Inventory list is OPEN to viewers (matches the dashboard's view-mode)
                try:
                    items = inventory_db.list_all() if inventory_db else []
                    # Annotate each with linked monitored host info if MAC matches
                    host_map = {}
                    if host_manager:
                        for h in [h.to_dict() for h in host_manager.list_hosts()]:
                            mac = (h.get("specs", {}) or {}).get("mac")
                            if mac:
                                key = InventoryDB.normalize_mac(mac)
                                if key:
                                    host_map[key] = {
                                        "name":       h.get("name"),
                                        "ip":         h.get("ip"),
                                        "is_up":      h.get("is_up"),
                                        "status":     h.get("status"),
                                        "uptime_pct": h.get("uptime_pct"),
                                    }
                    for item in items:
                        m = InventoryDB.normalize_mac(item.get("mac"))
                        item["linked_host"] = host_map.get(m) if m else None
                    self._send_json(200, {"items": items})
                except Exception as e:
                    logging.exception("inventory list error")
                    self._send_json(500, {"error": str(e)})
                return

            if self.path.startswith("/api/inventory/") and self.path != "/api/inventory/":
                try:
                    inv_id = int(self.path.split("/")[-1])
                except ValueError:
                    self._send_json(400, {"error": "invalid id"})
                    return
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"})
                    return
                rec = inventory_db.get(inv_id)
                if not rec:
                    self._send_json(404, {"error": "not found"})
                    return
                # Annotate linked host
                m = InventoryDB.normalize_mac(rec.get("mac"))
                rec["linked_host"] = None
                if m and host_manager:
                    for h in [h.to_dict() for h in host_manager.list_hosts()]:
                        h_mac = (h.get("specs", {}) or {}).get("mac")
                        if InventoryDB.normalize_mac(h_mac) == m:
                            rec["linked_host"] = {
                                "name":       h.get("name"),
                                "ip":         h.get("ip"),
                                "is_up":      h.get("is_up"),
                                "status":     h.get("status"),
                                "uptime_pct": h.get("uptime_pct"),
                            }
                            break
                self._send_json(200, rec)
                return'''
    if content.count(OLD) != 1:
        print(f"[FAIL] auth/users GET anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Added inventory GET endpoints")

    # ── 7. Add POST endpoints for inventory CRUD + xlsx import.
    # Anchor: existing inventory-adjacent POST handler for /api/auth/users.
    OLD = '''            if self.path == "/api/auth/users":
                # Create a new user (admin only)'''
    NEW = '''            if self.path == "/api/inventory":
                # Create a new inventory record (auth required)
                if not self._require_auth():
                    return
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length).decode()
                    data = json.loads(body)
                    inv_id, err = inventory_db.create(data)
                    if err:
                        self._send_json(400, {"error": err})
                        return
                    self._send_json(200, {"ok": True, "id": inv_id})
                except json.JSONDecodeError:
                    self._send_json(400, {"error": "invalid JSON"})
                except Exception as e:
                    logging.exception("inventory create error")
                    self._send_json(500, {"error": str(e)})
                return

            if self.path.startswith("/api/inventory/") and self.path.endswith("/delete"):
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

            if self.path.startswith("/api/inventory/"):
                # Update existing record - POST /api/inventory/<id>
                if not self._require_auth():
                    return
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"})
                    return
                try:
                    inv_id = int(self.path.split("/")[-1])
                except ValueError:
                    self._send_json(400, {"error": "invalid id"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length).decode()
                    data = json.loads(body)
                    ok, err = inventory_db.update(inv_id, data)
                    if not ok:
                        self._send_json(400, {"error": err})
                        return
                    self._send_json(200, {"ok": True})
                except json.JSONDecodeError:
                    self._send_json(400, {"error": "invalid JSON"})
                except Exception as e:
                    logging.exception("inventory update error")
                    self._send_json(500, {"error": str(e)})
                return

            if self.path == "/api/inventory-import":
                # Multipart upload of an xlsx
                if not self._require_auth():
                    return
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"})
                    return
                try:
                    ctype = self.headers.get("Content-Type", "")
                    length = int(self.headers.get("Content-Length", 0))
                    if length > 10 * 1024 * 1024:
                        self._send_json(400, {"error": "file too large (10MB max)"})
                        return
                    body = self.rfile.read(length)
                    # Parse multipart manually - we expect ONE file part named "file"
                    if "multipart/form-data" not in ctype:
                        self._send_json(400, {"error": "expected multipart/form-data"})
                        return
                    boundary = None
                    for part in ctype.split(";"):
                        part = part.strip()
                        if part.startswith("boundary="):
                            boundary = part[9:].strip('"')
                    if not boundary:
                        self._send_json(400, {"error": "missing boundary"})
                        return
                    delimiter = ("--" + boundary).encode()
                    parts = body.split(delimiter)
                    file_bytes = None
                    mode = "add"
                    for p in parts:
                        if not p or p == b"--" or p.strip() == b"--\\r\\n" or p.strip() == b"":
                            continue
                        # Each part: headers + \\r\\n\\r\\n + content + trailing \\r\\n
                        sep = p.find(b"\\r\\n\\r\\n")
                        if sep < 0:
                            continue
                        headers_blob = p[:sep].decode("latin-1", errors="replace")
                        content = p[sep + 4:]
                        if content.endswith(b"\\r\\n"):
                            content = content[:-2]
                        # Parse Content-Disposition to get name
                        name = None
                        for hline in headers_blob.split("\\r\\n"):
                            if hline.lower().startswith("content-disposition"):
                                for piece in hline.split(";"):
                                    piece = piece.strip()
                                    if piece.startswith("name="):
                                        name = piece[5:].strip('"')
                        if name == "file":
                            file_bytes = content
                        elif name == "mode":
                            try:
                                mode = content.decode().strip()
                                if mode not in ("add", "replace"):
                                    mode = "add"
                            except Exception:
                                mode = "add"
                    if file_bytes is None:
                        self._send_json(400, {"error": "no file uploaded"})
                        return
                    added, skipped, errors = import_inventory_from_xlsx(inventory_db, file_bytes, mode=mode)
                    self._send_json(200, {
                        "ok":      True,
                        "added":   added,
                        "skipped": skipped,
                        "errors":  errors[:20],  # cap to 20
                        "mode":    mode,
                    })
                except Exception as e:
                    logging.exception("inventory import error")
                    self._send_json(500, {"error": str(e)})
                return

            if self.path == "/api/auth/users":
                # Create a new user (admin only)'''
    if content.count(OLD) != 1:
        print(f"[FAIL] auth/users POST anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Added inventory POST endpoints")

    # ── 8. Frontend: add Inventory tab to the tabs row.
    # The actual tab format uses data-tab + role="tab" with click delegation.
    OLD = '''    <button class="tab" data-tab="events" role="tab">Events <span class="tab-count" id="events-count" style="display:none">0</span></button>'''
    NEW = '''    <button class="tab" data-tab="events" role="tab">Events <span class="tab-count" id="events-count" style="display:none">0</span></button>
    <button class="tab" data-tab="inventory" role="tab">Inventory <span class="tab-count" id="inv-count" style="display:none">0</span></button>'''
    if content.count(OLD) != 1:
        print(f"[FAIL] Events tab anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Added Inventory tab button")

    # ── 9. Add the Inventory view pane HTML (after the Events view pane).
    # Matches the real .view / id=view-X pattern.
    OLD = '''  <div class="view" id="view-events">
    <div class="events-empty" id="events-empty">No incidents recorded yet. When a monitored host goes down, it'll appear here.</div>
    <div class="events-list" id="events-list" style="display:none"></div>
  </div>'''
    NEW = '''  <div class="view" id="view-events">
    <div class="events-empty" id="events-empty">No incidents recorded yet. When a monitored host goes down, it'll appear here.</div>
    <div class="events-list" id="events-list" style="display:none"></div>
  </div>

  <div class="view" id="view-inventory">
    <div class="inv-toolbar">
      <input type="text" id="inv-search" placeholder="Search systems, roles, OS, CPU..." class="inv-search">
      <div class="inv-toolbar-actions">
        <button class="btn" onclick="openInventoryEditor()">+ Add</button>
        <button class="btn" onclick="openImportModal()">Import XLSX</button>
      </div>
    </div>
    <div class="inv-metrics" id="inv-metrics"></div>
    <div class="inv-chips" id="inv-chips"></div>
    <div class="inv-table-wrap">
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
    </div>
    <div id="inv-empty" class="inv-empty" style="display:none">
      No inventory yet. Click "Import XLSX" to bootstrap from a spreadsheet, or "+ Add" to create records one at a time.
    </div>
  </div>'''
    if content.count(OLD) != 1:
        print(f"[FAIL] Events view pane anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Added Inventory view pane HTML")

    # ── 10. Add CSS for inventory components
    OLD = '''/* Pi system health */'''
    NEW = '''/* Inventory */
.inv-toolbar{display:flex;align-items:center;gap:12px;margin-bottom:14px;flex-wrap:wrap}
.inv-search{flex:1;min-width:220px;padding:8px 12px;border:1px solid var(--border);border-radius:8px;background:var(--surface);color:var(--text);font-family:'DM Sans',sans-serif;font-size:13px}
.inv-search:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px var(--blue-bg)}
.inv-toolbar-actions{display:flex;gap:8px}
.inv-metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px}
.inv-metric{background:var(--subtle);border:1px solid var(--border-light);border-radius:8px;padding:11px 14px}
.inv-metric-label{font-family:'DM Mono',monospace;font-size:10px;letter-spacing:.07em;text-transform:uppercase;color:var(--hint);margin-bottom:5px}
.inv-metric-val{font-family:'DM Sans',sans-serif;font-size:20px;font-weight:600;color:var(--text)}
.inv-metric-val .unit{font-size:11px;font-weight:400;color:var(--muted);margin-left:3px}
.inv-metric-sub{font-size:11px;color:var(--muted);margin-top:2px}
.inv-chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px}
.inv-chip{padding:5px 11px;border:1px solid var(--border);border-radius:14px;background:var(--surface);color:var(--muted);font-size:11px;font-family:'DM Sans',sans-serif;cursor:pointer;transition:all .15s}
.inv-chip:hover{border-color:var(--hint);color:var(--text)}
.inv-chip.active{background:var(--text);color:var(--surface);border-color:var(--text)}
.inv-chip-count{margin-left:5px;opacity:.7;font-family:'DM Mono',monospace;font-size:10px}
.inv-table-wrap{background:var(--surface);border:1px solid var(--border-light);border-radius:8px;overflow:hidden;overflow-x:auto}
.inv-table{width:100%;border-collapse:collapse;font-size:13px}
.inv-th{text-align:left;padding:10px 12px;background:var(--subtle);border-bottom:1px solid var(--border-light);font-family:'DM Mono',monospace;font-size:10px;letter-spacing:.07em;text-transform:uppercase;color:var(--hint);font-weight:500;cursor:pointer;user-select:none;white-space:nowrap}
.inv-th:hover{color:var(--text)}
.inv-th.sort-asc::after{content:' \u2191';color:var(--text)}
.inv-th.sort-desc::after{content:' \u2193';color:var(--text)}
.inv-row{border-bottom:1px solid var(--border-light);cursor:pointer;transition:background .1s}
.inv-row:last-child{border-bottom:none}
.inv-row:hover{background:var(--subtle)}
.inv-row td{padding:11px 12px;vertical-align:middle}
.inv-system{font-weight:500;color:var(--text)}
.inv-role{font-size:11px;color:var(--muted);margin-top:2px}
.inv-mono{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted)}
.inv-cat-tag{display:inline-block;padding:2px 7px;border-radius:3px;font-size:10px;font-family:'DM Mono',monospace;background:var(--subtle);color:var(--muted);border:1px solid var(--border)}
.inv-link-pill{display:inline-flex;align-items:center;gap:5px;padding:3px 8px;border-radius:10px;font-size:10px;font-family:'DM Mono',monospace}
.inv-link-pill.up{background:var(--green-bg);color:var(--green-text)}
.inv-link-pill.down{background:var(--red-bg);color:var(--red-text)}
.inv-link-pill.idle{background:var(--subtle);color:var(--muted);border:1px solid var(--border)}
.inv-link-pill.degraded{background:var(--amber-bg);color:var(--amber-text)}
.inv-link-pill.none{color:var(--hint)}
.inv-empty{padding:32px 20px;text-align:center;color:var(--muted);font-size:13px;background:var(--subtle);border:1px dashed var(--border);border-radius:8px}

/* Inventory drawer specs grid */
.inv-drawer-section{background:var(--subtle);border:1px solid var(--border-light);border-radius:8px;overflow:hidden}
.inv-drawer-row{display:grid;grid-template-columns:110px 1fr;gap:10px;padding:9px 12px;border-bottom:1px solid var(--border-light);font-size:13px;align-items:start}
.inv-drawer-row:last-child{border-bottom:none}
.inv-drawer-key{color:var(--hint);font-family:'DM Mono',monospace;font-size:11px;letter-spacing:.04em;text-transform:uppercase}
.inv-drawer-val{color:var(--text);word-break:break-word}
.inv-drawer-val.empty{color:var(--hint);font-style:italic}
.inv-link-host-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:11px 13px;cursor:pointer;transition:all .15s;display:flex;align-items:center;justify-content:space-between}
.inv-link-host-card:hover{border-color:var(--hint);background:var(--subtle)}

/* Import modal */
.import-modal-body{font-size:13px;color:var(--text);line-height:1.5}
.import-modal-body p{margin-bottom:10px}
.import-mode-toggle{display:flex;gap:8px;margin:14px 0}
.import-mode-toggle label{flex:1;display:flex;align-items:flex-start;gap:8px;padding:10px;border:1px solid var(--border);border-radius:8px;cursor:pointer;transition:all .15s}
.import-mode-toggle label:hover{border-color:var(--hint)}
.import-mode-toggle input{margin:0;cursor:pointer}
.import-mode-toggle .mode-label{font-weight:500;font-size:13px}
.import-mode-toggle .mode-desc{font-size:11px;color:var(--muted);margin-top:2px}
.import-mode-toggle input:checked + div .mode-label{color:var(--blue)}
.import-result{margin-top:10px;padding:10px;border-radius:6px;font-size:12px}
.import-result.ok{background:var(--green-bg);color:var(--green-text)}
.import-result.err{background:var(--red-bg);color:var(--red-text)}

/* Pi system health */'''
    if content.count(OLD) != 1:
        print(f"[FAIL] CSS Pi-health anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Added inventory CSS")

    # ── 11. Add inventory + import modals before drawer
    OLD = '''<aside class="drawer" id="drawer" role="dialog" aria-modal="true" aria-label="Host details">'''
    NEW = '''<div class="modal-overlay" id="inv-edit-overlay">
  <form class="modal" style="max-width:640px" onsubmit="submitInventory(event)">
    <div class="modal-hdr">
      <div class="modal-title" id="inv-edit-title">New inventory record</div>
      <button type="button" class="btn btn-ghost" onclick="closeInventoryEditor()">X</button>
    </div>
    <div class="modal-body">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
        <label class="full" style="grid-column:1/-1">System name <span style="color:var(--red)">*</span><input type="text" class="inv-f-system" required></label>
        <label>Category<input type="text" class="inv-f-category" placeholder="e.g. Homelab Server & Edge Infrastructure"></label>
        <label>Architecture<input type="text" class="inv-f-architecture" placeholder="x86_64, ARM64, ..."></label>
        <label class="full" style="grid-column:1/-1">Role / Status<input type="text" class="inv-f-role" placeholder="What this device does"></label>
        <label>CPU<input type="text" class="inv-f-cpu" placeholder="e.g. Ryzen 5 5600X"></label>
        <label>GPU<input type="text" class="inv-f-gpu" placeholder="e.g. RTX 3070 Ti"></label>
        <label>RAM (GB)<input type="number" step="0.001" class="inv-f-ram_gb" placeholder="16"></label>
        <label>OS<input type="text" class="inv-f-os" placeholder="e.g. Proxmox VE"></label>
        <label>CPU score<input type="number" class="inv-f-cpu_score" placeholder="PassMark or similar"></label>
        <label>Max TDP (W)<input type="number" class="inv-f-tdp_watts" placeholder="65"></label>
        <label>TPM version<input type="text" class="inv-f-tpm" placeholder="2 / None / etc."></label>
        <label>MAC address<input type="text" class="inv-f-mac" placeholder="aa:bb:cc:dd:ee:ff (used for cross-linking)"></label>
        <label>IP address<input type="text" class="inv-f-ip" placeholder="optional"></label>
        <label>Service tag / serial<input type="text" class="inv-f-serial"></label>
        <label class="full" style="grid-column:1/-1">Notes<textarea class="inv-f-notes" placeholder="Anything else worth remembering"></textarea></label>
      </div>
      <div id="inv-edit-error" style="font-size:12px;color:var(--red-text);min-height:16px;margin-top:8px"></div>
    </div>
    <div class="modal-foot">
      <button type="button" class="btn" id="inv-delete-btn" onclick="deleteInventory()" style="display:none;color:var(--red-text);border-color:var(--red-bg)">Delete</button>
      <div style="display:flex;gap:8px;margin-left:auto">
        <button type="button" class="btn" onclick="closeInventoryEditor()">Cancel</button>
        <button type="submit" class="btn btn-primary">Save</button>
      </div>
    </div>
  </form>
</div>

<div class="modal-overlay" id="import-overlay">
  <div class="modal" style="max-width:520px">
    <div class="modal-hdr">
      <div class="modal-title">Import inventory from XLSX</div>
      <button type="button" class="btn btn-ghost" onclick="closeImportModal()">X</button>
    </div>
    <div class="modal-body import-modal-body">
      <p>Upload an Excel file. Columns are auto-detected by header name. Expected columns:</p>
      <p style="font-family:'DM Mono',monospace;font-size:11px;color:var(--muted);background:var(--subtle);padding:8px;border-radius:6px;margin-bottom:14px">Category, System, Role / Status, CPU, RAM_GB, GPU, Architecture, OS, Estimated_CPU_Score, Max_TDP_Watts, TPM_Version, MAC_Primary, IP_Address, Service_Tag_Serial</p>
      <div class="import-mode-toggle">
        <label><input type="radio" name="import-mode" value="add" checked><div><div class="mode-label">Add new</div><div class="mode-desc">Insert records that don\'t already exist (matched by MAC or system name).</div></div></label>
        <label><input type="radio" name="import-mode" value="replace"><div><div class="mode-label">Replace all</div><div class="mode-desc">Delete the entire current inventory and reimport everything from scratch.</div></div></label>
      </div>
      <input type="file" id="import-file" accept=".xlsx,.xlsm">
      <div id="import-result" style="display:none"></div>
    </div>
    <div class="modal-foot">
      <button type="button" class="btn" onclick="closeImportModal()">Cancel</button>
      <button type="button" class="btn btn-primary" onclick="submitImport()" id="import-submit-btn">Upload</button>
    </div>
  </div>
</div>

<aside class="drawer" id="drawer" role="dialog" aria-modal="true" aria-label="Host details">'''
    if content.count(OLD) != 1:
        print(f"[FAIL] drawer anchor for modals: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Added inventory + import modal HTML")

    # ── 12. Add inventory JS - the bulk of the frontend logic.
    # Insert before refresh() call near the bottom.
    OLD = '''// Initial auth state load
fetchAuthState();
// Re-check auth state every minute (in case session expired)
setInterval(fetchAuthState, 60000);

refresh();'''
    NEW = '''// ── Inventory state ──
let _inventoryData = [];
let _inventorySort = { col: 'system', dir: 'asc' };
let _inventoryFilter = { category: null, search: '' };
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
}

function renderInventoryChips(){
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
}

function setInvCategoryFilter(cat){
  _inventoryFilter.category = cat;
  renderInventoryChips();
  renderInventoryRows();
}

function renderInventoryRows(){
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
}

// Wire up sort headers + search
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
});
document.addEventListener('input', e => {
  if(e.target.id === 'inv-search'){
    _inventoryFilter.search = e.target.value;
    renderInventoryRows();
  }
});

// Editor
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
  if(existingId){
    try {
      const res = await fetch('/api/inventory/' + existingId);
      if(res.ok){
        const rec = await res.json();
        ['system','category','role','cpu','ram_gb','gpu','architecture','os','cpu_score','tdp_watts','tpm','mac','ip','serial','notes'].forEach(f => {
          const el = document.querySelector('.inv-f-' + f);
          if(el && rec[f] !== null && rec[f] !== undefined) el.value = rec[f];
        });
      }
    } catch(e){}
  }
  document.getElementById('inv-edit-overlay').classList.add('open');
}
function closeInventoryEditor(){
  document.getElementById('inv-edit-overlay').classList.remove('open');
}
async function submitInventory(ev){
  if(ev) ev.preventDefault();
  const data = {};
  ['system','category','role','cpu','ram_gb','gpu','architecture','os','cpu_score','tdp_watts','tpm','mac','ip','serial','notes'].forEach(f => {
    const el = document.querySelector('.inv-f-' + f);
    if(el) data[f] = el.value.trim();
  });
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
  if(dotEl){ dotEl.className = 'drawer-name-dot idle'; }
  document.getElementById('d-name').textContent = rec.system || 'Inventory record';
  const meta = document.getElementById('d-meta');
  if(meta){
    meta.innerHTML = '<span>' + escapeHtml(rec.category || '-') + '</span>';
    if(rec.architecture){
      meta.innerHTML += '<span>·</span><span class="inv-mono">' + escapeHtml(rec.architecture) + '</span>';
    }
  }
  const fields = [
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
  let specsHtml = '<div class="d-section"><div class="d-section-hdr"><span>Specifications</span></div><div class="inv-drawer-section">';
  fields.forEach(([k, v]) => {
    if(v === null || v === undefined || v === '') return;
    specsHtml += '<div class="inv-drawer-row"><div class="inv-drawer-key">' + escapeHtml(k.toUpperCase()) + '</div><div class="inv-drawer-val">' + escapeHtml(String(v)) + '</div></div>';
  });
  specsHtml += '</div></div>';

  let linkHtml = '';
  if(rec.linked_host){
    const lh = rec.linked_host;
    const cls = lh.status === 'DEGRADED' ? 'degraded' : lh.is_up ? 'up' : (lh.status === 'IDLE' ? 'idle' : 'down');
    linkHtml = '<div class="d-section"><div class="d-section-hdr"><span>Linked monitored host</span></div>'
      + '<div class="inv-link-host-card" onclick="openHostDrawerByIp(' + JSON.stringify(lh.ip) + ')">'
      + '<div><div style="font-weight:500">' + escapeHtml(lh.name) + '</div>'
      + '<div class="inv-mono" style="font-size:11px;color:var(--muted);margin-top:2px">' + escapeHtml(lh.ip) + '</div></div>'
      + '<span class="inv-link-pill ' + cls + '">' + escapeHtml(lh.status) + '</span>'
      + '</div></div>';
  }

  let notesHtml = '';
  if(rec.notes){
    notesHtml = '<div class="d-section"><div class="d-section-hdr"><span>Notes</span></div>'
      + '<div style="background:var(--subtle);border:1px solid var(--border-light);border-radius:8px;padding:11px 13px;font-size:13px;line-height:1.5;white-space:pre-wrap">' + escapeHtml(rec.notes) + '</div></div>';
  }

  const editBtn = '<button class="d-action-btn" onclick="openInventoryEditor(' + rec.id + ')">'
    + '<span>Edit record</span><span class="arrow">->></span></button>';

  document.getElementById('drawer-body').innerHTML = linkHtml + specsHtml + notesHtml + '<div class="d-section">' + editBtn + '</div>';
  document.getElementById('drawer-body').dataset.hostIp = 'inv:' + rec.id;
}

function openHostDrawerByIp(ip){
  // openDrawer takes an IP and looks up the host in lastData itself
  closeDrawer();
  openDrawer(ip);
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

// Hook into the existing tab buttons to lazy-load inventory data
document.addEventListener('click', e => {
  const tab = e.target.closest('.tab[data-tab="inventory"]');
  if(tab) fetchInventory();
});

// Initial inventory fetch on page load if tab is restored
if(localStorage.getItem('nw-tab') === 'inventory') fetchInventory();

// Initial auth state load
fetchAuthState();
// Re-check auth state every minute (in case session expired)
setInterval(fetchAuthState, 60000);

refresh();'''
    if content.count(OLD) != 1:
        print(f"[FAIL] refresh() bottom anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Added inventory JS")

    # ── 13. Host drawer: show inventory cross-link if MAC matches.
    # Add fetch + section to renderDrawer. Insert in the host drawer's renderDrawer.
    # We'll place it right after the links section calculation but before the drawer
    # body assembly. Simpler approach: add a new section variable that gets fetched
    # on drawer open.
    # We do this by modifying the drawer body assembly to include an inv-link section.
    # The fetching happens async after drawer opens.
    OLD = '''    drawerBody.innerHTML = statsHtml + linksHtml + svcHtml + piHtml + sparkHtml + specsHtml + notesHtml + incHtml + actionsHtml;'''
    NEW = '''    drawerBody.innerHTML = statsHtml + linksHtml + '<div id="d-inv-section"></div>' + svcHtml + piHtml + sparkHtml + specsHtml + notesHtml + incHtml + actionsHtml;
    fetchHostInventoryLink(h);'''
    if content.count(OLD) != 1:
        print(f"[FAIL] drawer-body assembly anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Inventory cross-link slot added to host drawer")

    # ── 14. Add the helper that fetches and renders the host->inventory link.
    OLD = '''function openHostDrawerByIp(ip){'''
    NEW = '''async function fetchHostInventoryLink(h){
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
      + '<span class="d-link-arrow" style="color:var(--hint);font-family:DM Mono,monospace">->></span>'
      + '</div></div>';
  } catch(e){ slot.innerHTML = ''; }
}

function openHostDrawerByIp(ip){'''
    if content.count(OLD) != 1:
        print(f"[FAIL] openHostDrawerByIp anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Added fetchHostInventoryLink helper")

    # ── 15. Bump version
    if 'VERSION = "3.10"' in content:
        content = content.replace('VERSION = "3.10"', 'VERSION = "3.11"', 1)
    content = content.replace('netwatch v3.10 - raspberry pi', 'netwatch v3.11 - raspberry pi', 1)

    open(TARGET, "w").write(content)

    import ast
    try:
        ast.parse(open(TARGET).read())
        print("[OK] Resulting Python is valid")
    except SyntaxError as e:
        print(f"[FAIL] Syntax error: {e}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)

    print()
    print("Next steps:")
    print("  1. sudo systemctl restart netwatch")
    print("  2. Open the dashboard - new 'Inventory' tab next to Events")
    print("  3. Click 'Import XLSX' to bootstrap from your spreadsheet")
    print("  4. Click any inventory row to see full details")
    print("  5. For monitored hosts whose MAC matches an inventory record,")
    print("     the host's drawer will show an 'Inventory record' section")
    print()
    if not ok:
        print("  IMPORTANT: openpyxl was not installed. To enable XLSX import:")
        print("    sudo apt-get install python3-openpyxl")
        print("    OR: python3 -m pip install openpyxl --break-system-packages")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")
    print("(After rollback, the inventory table remains in netwatch.db - safe to leave)")


if __name__ == "__main__":
    main()
