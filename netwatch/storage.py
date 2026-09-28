import os
import json
import time
import logging
import sqlite3
import threading

from netwatch import VERSION
from netwatch.connections import (
    fingerprint, plan_connections_migration, orient_edge, default_connection_type,
    normalize_port, resolve_ports, validate_parent_port, lint_edge, migration_drift_key,
    canonical_port, NETWORK_LINK_TYPES,
)


def _column_exists(conn: "sqlite3.Connection", table: str, column: str) -> bool:
    """Return True if `column` exists in `table`. Both must be code-controlled identifiers."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


# ============================================================================
# Persistent history (SQLite)
# ============================================================================

class HistoryDB:
    """Thread-safe SQLite store for ping results and incidents.

    Schema:
        pings(id, host_ip, timestamp, is_up, latency_ms)
        incidents(id, host_ip, host_name, host_group, started, ended, duration_seconds)

    All timestamps are unix epoch seconds (integer).
    Pruning runs nightly to drop data older than retention_days.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS pings (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        host_ip     TEXT NOT NULL,
        timestamp   INTEGER NOT NULL,
        is_up       INTEGER NOT NULL,
        latency_ms  REAL
    );
    CREATE INDEX IF NOT EXISTS idx_pings_host_time ON pings(host_ip, timestamp);
    CREATE INDEX IF NOT EXISTS idx_pings_time ON pings(timestamp);

    CREATE TABLE IF NOT EXISTS incidents (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        host_ip          TEXT NOT NULL,
        host_name        TEXT NOT NULL,
        host_group       TEXT NOT NULL,
        started          INTEGER NOT NULL,
        ended            INTEGER,
        duration_seconds INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_incidents_host ON incidents(host_ip);
    CREATE INDEX IF NOT EXISTS idx_incidents_started ON incidents(started);
    CREATE INDEX IF NOT EXISTS idx_incidents_ended ON incidents(ended);

    CREATE TABLE IF NOT EXISTS maintenance_windows (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        host_ip     TEXT NOT NULL,
        host_name   TEXT NOT NULL,
        started_at  INTEGER NOT NULL,
        expires_at  INTEGER NOT NULL,
        cleared_at  INTEGER,
        reason      TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_maintenance_host ON maintenance_windows(host_ip);

    CREATE TABLE IF NOT EXISTS ping_daily (
        day          TEXT NOT NULL,
        host_ip      TEXT NOT NULL,
        total        INTEGER NOT NULL,
        up           INTEGER NOT NULL,
        latency_avg  REAL,
        latency_min  REAL,
        latency_max  REAL,
        PRIMARY KEY (day, host_ip)
    );

    CREATE TABLE IF NOT EXISTS briefs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        created_ts    INTEGER NOT NULL,
        subject       TEXT    NOT NULL,
        stats_json    TEXT    NOT NULL,
        narrative     TEXT    NOT NULL,
        analysis_json TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_briefs_ts ON briefs(created_ts);

    CREATE TABLE IF NOT EXISTS power_readings (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp  INTEGER NOT NULL,
        watts      REAL,
        voltage    REAL,
        current_a  REAL,
        energy_kwh REAL
    );
    CREATE INDEX IF NOT EXISTS idx_power_ts ON power_readings(timestamp);
    """

    FLUSH_MAX = 200          # safety flush if the 30s flusher falls behind
    BUFFER_HARD_CAP = 5000   # drop oldest beyond this if SQLite is wedged

    def __init__(self, db_path, retention_days=30):
        self.db_path = db_path
        self.retention_days = retention_days
        self.lock = threading.Lock()
        self._ping_buffer = []
        # check_same_thread=False because we share the connection across threads,
        # and we serialize writes with self.lock.
        self.conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        # Cap the WAL file: without this SQLite never shrinks the -wal file
        # below its high-water mark (it hit 226MB via daily VACUUM).
        self.conn.execute("PRAGMA journal_size_limit=16777216")  # 16MB
        self.conn.executescript(self.SCHEMA)
        if not _column_exists(self.conn, "incidents", "alert_sent"):
            self.conn.execute("ALTER TABLE incidents ADD COLUMN alert_sent INTEGER DEFAULT 0")
            logging.info("HistoryDB: added alert_sent column to incidents")
        logging.info(f"HistoryDB: opened {db_path} (retention {retention_days} days)")

    def close(self):
        with self.lock:
            try:
                self._flush_pings_locked()
            except Exception:
                pass
            try:
                self.conn.close()
            except Exception:
                pass

    # ── Pings ───────────────────────────────────────────────────────────────

    def record_ping(self, host_ip, is_up, latency_ms):
        ts = int(time.time())
        with self.lock:
            self._ping_buffer.append((host_ip, ts, 1 if is_up else 0, latency_ms))
            if len(self._ping_buffer) >= self.FLUSH_MAX:
                self._flush_pings_locked()

    def _flush_pings_locked(self):
        """Write all buffered pings in one transaction. Caller holds self.lock.
        Batching cuts WAL write amplification ~10-30x vs per-ping commits."""
        if not self._ping_buffer:
            return
        if len(self._ping_buffer) > self.BUFFER_HARD_CAP:
            dropped = len(self._ping_buffer) - self.BUFFER_HARD_CAP
            del self._ping_buffer[:dropped]
            logging.warning(f"HistoryDB: dropped {dropped} buffered pings (DB unavailable?)")
        self.conn.execute("BEGIN")
        try:
            self.conn.executemany(
                "INSERT INTO pings (host_ip, timestamp, is_up, latency_ms) VALUES (?, ?, ?, ?)",
                self._ping_buffer,
            )
            self.conn.execute("COMMIT")
        except Exception:
            try: self.conn.execute("ROLLBACK")
            except Exception: pass
            raise
        self._ping_buffer.clear()

    def flush_pings(self):
        with self.lock:
            self._flush_pings_locked()

    def recent_pings(self, host_ip, limit=100):
        """Return up to `limit` most-recent pings for a host, oldest first.
        Used to repopulate the in-memory history deque on startup."""
        with self.lock:
            self._flush_pings_locked()
            cur = self.conn.execute(
                "SELECT is_up, latency_ms FROM pings "
                "WHERE host_ip = ? ORDER BY timestamp DESC LIMIT ?",
                (host_ip, limit),
            )
            rows = cur.fetchall()
        # Reverse so it's oldest-first (matches deque chronological order)
        return [(bool(r[0]), r[1]) for r in reversed(rows)]

    def latest_ping(self, host_ip):
        """Return (is_up, latency_ms, timestamp) of the most recent ping for
        the host, or None if no pings recorded."""
        with self.lock:
            self._flush_pings_locked()
            cur = self.conn.execute(
                "SELECT is_up, latency_ms, timestamp FROM pings "
                "WHERE host_ip = ? ORDER BY timestamp DESC LIMIT 1",
                (host_ip,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return (bool(row[0]), row[1], row[2])

    def history_series(self, host_ip, hours=24, target_points=180):
        """Bucketed latency/uptime series for charting, oldest first.

        Buckets are aligned absolute-time windows of span/target_points
        (min 60s). avg/min/max ignore down pings (NULL latency); up_pct is
        the fraction of up pings in the bucket."""
        hours = max(1, min(int(hours), 168))
        span = hours * 3600
        bucket = max(60, span // target_points)
        since = int(time.time()) - span
        with self.lock:
            self._flush_pings_locked()
            cur = self.conn.execute(
                "SELECT (timestamp / ?) * ? AS bucket_ts, "
                "AVG(latency_ms), MIN(latency_ms), MAX(latency_ms), AVG(is_up), COUNT(*) "
                "FROM pings WHERE host_ip = ? AND timestamp >= ? "
                "GROUP BY bucket_ts ORDER BY bucket_ts",
                (bucket, bucket, host_ip, since),
            )
            rows = cur.fetchall()
        points = [
            {"t": r[0],
             "avg": round(r[1], 2) if r[1] is not None else None,
             "min": round(r[2], 2) if r[2] is not None else None,
             "max": round(r[3], 2) if r[3] is not None else None,
             "up_pct": round((r[4] or 0) * 100, 1),
             "n": r[5]}
            for r in rows
        ]
        return {"bucket_seconds": bucket, "points": points}

    # ── Daily rollups ───────────────────────────────────────────────────────

    def rollup_days(self):
        """Aggregate complete (past, local-time) days into ping_daily.
        Idempotent (INSERT OR REPLACE re-rolls partial days). Runs daily
        from _prune_loop, BEFORE prune deletes the raw rows. ~29 rows/day,
        kept forever — months of uptime trends for pennies of storage."""
        with self.lock:
            self._flush_pings_locked()
            cur = self.conn.execute(
                "INSERT OR REPLACE INTO ping_daily "
                "(day, host_ip, total, up, latency_avg, latency_min, latency_max) "
                "SELECT date(timestamp, 'unixepoch', 'localtime'), host_ip, "
                "COUNT(*), SUM(is_up), AVG(latency_ms), MIN(latency_ms), MAX(latency_ms) "
                "FROM pings "
                "WHERE date(timestamp, 'unixepoch', 'localtime') < date('now', 'localtime') "
                "GROUP BY date(timestamp, 'unixepoch', 'localtime'), host_ip"
            )
            n = cur.rowcount
        if n:
            logging.info(f"HistoryDB: rolled up {n} host-day row(s)")
        return n

    def daily_history(self, host_ip, days=60):
        """Daily uptime/latency rollups for a host, oldest first."""
        days = max(1, min(int(days), 365))
        with self.lock:
            cur = self.conn.execute(
                "SELECT day, total, up, latency_avg, latency_min, latency_max "
                "FROM ping_daily WHERE host_ip = ? AND day >= date('now', 'localtime', ?) "
                "ORDER BY day",
                (host_ip, f"-{days} days"),
            )
            rows = cur.fetchall()
        return [
            {"day": r[0], "total": r[1], "up": r[2],
             "uptime_pct": round(r[2] / r[1] * 100, 2) if r[1] else None,
             "latency_avg": round(r[3], 2) if r[3] is not None else None,
             "latency_min": round(r[4], 2) if r[4] is not None else None,
             "latency_max": round(r[5], 2) if r[5] is not None else None}
            for r in rows
        ]

    # ── Incidents ───────────────────────────────────────────────────────────

    def open_incident(self, host_ip, host_name, host_group, started_at=None):
        """Open a new incident if there's no ongoing one for this host."""
        ts = int(started_at) if started_at is not None else int(time.time())
        with self.lock:
            # Check for an existing open incident
            cur = self.conn.execute(
                "SELECT id FROM incidents WHERE host_ip = ? AND ended IS NULL LIMIT 1",
                (host_ip,),
            )
            if cur.fetchone():
                return  # already an ongoing incident
            self.conn.execute(
                "INSERT INTO incidents (host_ip, host_name, host_group, started) "
                "VALUES (?, ?, ?, ?)",
                (host_ip, host_name, host_group, ts),
            )

    def close_incident(self, host_ip):
        """Close any open incident for this host."""
        ts = int(time.time())
        with self.lock:
            cur = self.conn.execute(
                "SELECT id, started FROM incidents WHERE host_ip = ? AND ended IS NULL LIMIT 1",
                (host_ip,),
            )
            row = cur.fetchone()
            if row is None:
                return
            inc_id, started = row
            duration = ts - started
            self.conn.execute(
                "UPDATE incidents SET ended = ?, duration_seconds = ? WHERE id = ?",
                (ts, duration, inc_id),
            )

    def start_maintenance(self, host_ip, host_name, expires_at, reason=""):
        """Open a maintenance window, closing any existing open one for this host."""
        ts = int(time.time())
        with self.lock:
            self.conn.execute(
                "UPDATE maintenance_windows SET cleared_at = ? "
                "WHERE host_ip = ? AND cleared_at IS NULL",
                (ts, host_ip),
            )
            self.conn.execute(
                "INSERT INTO maintenance_windows (host_ip, host_name, started_at, expires_at, reason) "
                "VALUES (?, ?, ?, ?, ?)",
                (host_ip, host_name, ts, int(expires_at), reason or ""),
            )

    def clear_maintenance(self, host_ip):
        """Close the open maintenance window for this host, if any."""
        ts = int(time.time())
        with self.lock:
            self.conn.execute(
                "UPDATE maintenance_windows SET cleared_at = ? "
                "WHERE host_ip = ? AND cleared_at IS NULL",
                (ts, host_ip),
            )

    def get_active_maintenance(self, host_ip):
        """Return {'started_at', 'expires_at', 'reason'} for this host's
        currently-active (unexpired, uncleared) window, or None."""
        now = int(time.time())
        with self.lock:
            cur = self.conn.execute(
                "SELECT started_at, expires_at, reason FROM maintenance_windows "
                "WHERE host_ip = ? AND cleared_at IS NULL AND expires_at > ? "
                "ORDER BY id DESC LIMIT 1",
                (host_ip, now),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {"started_at": row[0], "expires_at": row[1], "reason": row[2] or ""}

    def list_incidents(self, limit=100):
        """Return incidents most-recent-first as a list of dicts. Ongoing ones
        come first; resolved ones follow ordered by start time descending."""
        from datetime import datetime as _dt
        now = int(time.time())
        with self.lock:
            cur = self.conn.execute(
                "SELECT id, host_ip, host_name, host_group, started, ended, duration_seconds "
                "FROM incidents ORDER BY ended IS NULL DESC, started DESC LIMIT ?",
                (limit,),
            )
            rows = cur.fetchall()
        result = []
        for inc_id, host_ip, host_name, host_group, started, ended, duration in rows:
            ongoing = ended is None
            dur = (now - started) if ongoing else (duration or 0)
            st = _dt.fromtimestamp(started)
            # Time-only for today's events; month+day prefix once a midnight
            # has passed so the list never shows ambiguous bare times.
            # NOTE: started_str is SERVER-local; clients grouping by started_ts
            # (browser-local) should derive display labels from started_ts.
            fmt = "%H:%M:%S" if st.date() == _dt.now().date() else "%b %d %H:%M"
            result.append({
                "host_ip":          host_ip,
                "host_name":        host_name,
                "host_group":       host_group,
                "started_ts":       started,
                "started_str":      st.strftime(fmt),
                "started_iso":      st.isoformat(),
                "ended_iso":        _dt.fromtimestamp(ended).isoformat() if ended else None,
                "duration_seconds": dur,
                "ongoing":          ongoing,
            })
        return result

    def update_incident_host_info(self, host_ip, host_name, host_group):
        """Keep host_name/host_group up to date on existing open incidents
        when a host gets renamed or moved between groups."""
        with self.lock:
            self.conn.execute(
                "UPDATE incidents SET host_name = ?, host_group = ? "
                "WHERE host_ip = ? AND ended IS NULL",
                (host_name, host_group, host_ip),
            )

    def mark_incident_alerted(self, host_ip):
        """Set alert_sent=1 for the open incident for host_ip."""
        with self.lock:
            self.conn.execute(
                "UPDATE incidents SET alert_sent = 1 "
                "WHERE host_ip = ? AND ended IS NULL",
                (host_ip,),
            )

    def get_open_incident_alert_status(self, host_ip):
        """Return alert_sent (bool) for the open incident, or None."""
        with self.lock:
            cur = self.conn.execute(
                "SELECT alert_sent FROM incidents "
                "WHERE host_ip = ? AND ended IS NULL LIMIT 1",
                (host_ip,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return bool(row[0])

    def get_last_closed_incident_alert_status(self, host_ip):
        """Return alert_sent (bool) for the most recently closed incident."""
        with self.lock:
            cur = self.conn.execute(
                "SELECT alert_sent FROM incidents "
                "WHERE host_ip = ? AND ended IS NOT NULL "
                "ORDER BY ended DESC LIMIT 1",
                (host_ip,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return bool(row[0])

    # ── Briefs ──────────────────────────────────────────────────────────────

    def insert_brief(self, created_ts, subject, stats_json, narrative, analysis_json=None):
        with self.lock:
            self.conn.execute(
                "INSERT INTO briefs (created_ts, subject, stats_json, narrative, analysis_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (created_ts, subject, stats_json, narrative, analysis_json),
            )

    def get_briefs(self, days=7):
        cutoff = int(time.time()) - days * 86400
        with self.lock:
            cur = self.conn.execute(
                "SELECT id, created_ts, subject, stats_json, narrative "
                "FROM briefs WHERE created_ts >= ? ORDER BY created_ts DESC",
                (cutoff,),
            )
            rows = cur.fetchall()
        result = []
        for id_, created_ts, subject, stats_json, narrative in rows:
            try:
                stats = json.loads(stats_json)
            except Exception:
                stats = {}
            result.append({
                "id": id_,
                "created_ts": created_ts,
                "subject": subject,
                "stats": stats,
                "narrative": narrative,
            })
        return result

    # ── Power readings ───────────────────────────────────────────────────────

    def insert_power_reading(self, ts, watts, voltage, current_a, energy_kwh):
        with self.lock:
            self.conn.execute(
                "INSERT INTO power_readings (timestamp, watts, voltage, current_a, energy_kwh) "
                "VALUES (?, ?, ?, ?, ?)",
                (ts, watts, voltage, current_a, energy_kwh),
            )

    def get_power_readings(self, days=7):
        cutoff = int(time.time()) - days * 86400
        with self.lock:
            rows = self.conn.execute(
                "SELECT timestamp, watts, voltage, current_a, energy_kwh "
                "FROM power_readings WHERE timestamp >= ? ORDER BY timestamp ASC",
                (cutoff,),
            ).fetchall()
        return [
            {"timestamp": r[0], "watts": r[1], "voltage": r[2],
             "current_a": r[3], "energy_kwh": r[4]}
            for r in rows
        ]

    # ── Pruning ─────────────────────────────────────────────────────────────

    def prune(self):
        """Delete rows older than retention_days. Returns (pings_deleted, incidents_deleted)."""
        cutoff = int(time.time()) - self.retention_days * 86400
        with self.lock:
            r1 = self.conn.execute(
                "DELETE FROM pings WHERE timestamp < ?", (cutoff,)
            )
            pings_deleted = r1.rowcount
            r2 = self.conn.execute(
                "DELETE FROM incidents WHERE ended IS NOT NULL AND ended < ?", (cutoff,)
            )
            incidents_deleted = r2.rowcount
            r3 = self.conn.execute(
                "DELETE FROM briefs WHERE created_ts < ?",
                (int(time.time()) - 7 * 86400,),
            )
            briefs_deleted = r3.rowcount
            r4 = self.conn.execute(
                "DELETE FROM power_readings WHERE timestamp < ?", (cutoff,)
            )
            power_deleted = r4.rowcount
            # No VACUUM: with fixed retention the DB is steady-state and
            # freed pages get reused. Daily VACUUM rewrote the whole DB
            # through the WAL (~300MB/day of SD writes) for nothing.
            # Manual reclaim if ever needed: sqlite3 netwatch.db VACUUM.
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if pings_deleted or incidents_deleted or briefs_deleted or power_deleted:
            logging.info(
                f"HistoryDB: pruned {pings_deleted} pings, "
                f"{incidents_deleted} incidents, "
                f"{briefs_deleted} briefs, "
                f"{power_deleted} power readings"
            )
        return pings_deleted, incidents_deleted


def _flush_loop(history_db, stop_event):
    """Flush buffered pings every 30s; final flush on shutdown."""
    while not stop_event.wait(30):
        try:
            history_db.flush_pings()
        except Exception as e:
            logging.warning(f"HistoryDB flush failed: {e}")
    try:
        history_db.flush_pings()
    except Exception:
        pass


def _prune_loop(history_db, stop_event, inventory_db=None):
    """Run prune() once a day until stop_event is set."""
    SECONDS_PER_DAY = 86400
    # Run first prune ~60s after startup so the system isn't busy at boot
    elapsed = SECONDS_PER_DAY - 60
    while not stop_event.is_set():
        if elapsed >= SECONDS_PER_DAY:
            rollup_ok = True
            try:
                history_db.rollup_days()
            except Exception as e:
                logging.warning(f"HistoryDB rollup failed: {e}")
                rollup_ok = False
            if rollup_ok:
                try:
                    history_db.prune()
                except Exception as e:
                    logging.warning(f"HistoryDB prune failed: {e}")
            if inventory_db is not None:
                try:
                    n = inventory_db.suggestions.prune_decided()
                    if n:
                        logging.info(f"InventoryDB: pruned {n} decided suggestion(s)")
                except Exception as e:
                    logging.warning(f"Suggestion prune failed: {e}")
            elapsed = 0
        time.sleep(5)
        elapsed += 5


# ============================================================================
# Inventory (CMDB)
# ============================================================================

# Inventory type taxonomy. Each type has a list of "type-specific" properties
# stored in the properties JSON blob; common fields (system name, mac, ip,
# serial, notes, etc.) live as top-level columns and are shared across types.
INVENTORY_TYPES = ("host", "vm", "network", "ups", "disk", "peripheral", "tablet", "phone", "printer")

# properties keys that are server-managed (seeded by migration, discovery, etc.)
# rather than edited via the per-type field list in the inventory drawer. An
# update that doesn't explicitly mention one of these keeps whatever value is
# already stored, so a plain field-list save can't silently wipe it.
MANAGED_PROPERTY_KEYS = ("network_role", "mac_aliases", "proxmox_node", "guest_type")

INVENTORY_TYPE_PROPERTIES = {
    "host": [],  # all fields are top-level (cpu, ram, os, etc.)
    "vm": [
        # VMs use ALL the host fields (cpu, ram, os, etc. all top-level)
        # AND these VM-specific fields stored in the properties JSON.
        ("hypervisor",     "string", "Hypervisor (Proxmox/KVM/ESXi/etc.)"),
        ("vcpu_count",     "int",    "vCPU count"),
        ("ram_alloc_gb",   "int",    "Allocated RAM (GB)"),
        ("disk_alloc_gb",  "int",    "Allocated disk (GB)"),
        ("autostart",      "bool",   "Auto-starts with host"),
        ("proxmox_vmid",   "int",    "Proxmox VMID"),
    ],
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


class _SuggestionChanged(Exception):
    """The suggestion, or the data it refers to, changed since it was shown."""


class _SuggestionRejected(Exception):
    """The suggestion can't be accepted as asked; str(e) is user-facing."""


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
        "device_type", "properties",
    ]

    def __init__(self, history_db):
        """Reuses the connection from HistoryDB."""
        self.history_db = history_db
        self.lock = history_db.lock  # share the same lock to serialize writes
        self.conn = history_db.conn
        self.conn.executescript(self.SCHEMA)
        if not _column_exists(self.conn, "inventory", "device_type"):
            self.conn.execute("ALTER TABLE inventory ADD COLUMN device_type TEXT DEFAULT 'host' NOT NULL")
            logging.info("InventoryDB: added device_type column")
        if not _column_exists(self.conn, "inventory", "properties"):
            self.conn.execute("ALTER TABLE inventory ADD COLUMN properties TEXT")
            logging.info("InventoryDB: added properties column")
        # Connections table - records edges between inventory devices.
        # CREATE TABLE IF NOT EXISTS is idempotent so re-runs are safe.
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS inventory_connections (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                from_device_id  INTEGER NOT NULL,
                to_device_id    INTEGER NOT NULL,
                from_port       TEXT,
                to_port         TEXT,
                connection_type TEXT DEFAULT 'ethernet',
                notes           TEXT,
                created_at      INTEGER NOT NULL,
                FOREIGN KEY (from_device_id) REFERENCES inventory(id) ON DELETE CASCADE,
                FOREIGN KEY (to_device_id)   REFERENCES inventory(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_conn_from ON inventory_connections(from_device_id);
            CREATE INDEX IF NOT EXISTS idx_conn_to   ON inventory_connections(to_device_id);
        """)
        # Connections v2 (4.0): provenance + freshness columns. Direction is
        # a rule now: from_device_id = child (downstream), to_device_id =
        # parent (upstream). See netwatch/connections.py.
        for col, ddl in (
            ("source",       "TEXT NOT NULL DEFAULT 'manual'"),
            ("external_key", "TEXT"),
            ("last_seen",    "INTEGER"),
            ("updated_at",   "INTEGER"),
        ):
            if not _column_exists(self.conn, "inventory_connections", col):
                self.conn.execute(
                    f"ALTER TABLE inventory_connections ADD COLUMN {col} {ddl}")
                logging.info(f"InventoryDB: added inventory_connections.{col}")
        self.conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_conn_external_key
                ON inventory_connections(external_key);
            CREATE TABLE IF NOT EXISTS schema_meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        # Assigned by the discovery runner (plan 2):
        # callable(record) -> list of port dicts, or None.
        self.live_port_provider = None
        self.suggestions = SuggestionsDB(self.conn, self.lock)
        # SQLite needs PRAGMA foreign_keys=ON for CASCADE to actually work.
        # The HistoryDB connection might not have it on; flip it now.
        self.conn.execute("PRAGMA foreign_keys = ON")
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

    def get_device_type_map(self):
        """Return {ip: device_type} for all inventory records with a non-empty IP.
        When multiple records share an IP, the one with the highest id wins."""
        with self.lock:
            cur = self.conn.execute(
                "SELECT ip, device_type FROM inventory"
                " WHERE ip IS NOT NULL AND ip != ''"
                " ORDER BY id ASC"
            )
            return {row[0]: (row[1] or "host") for row in cur.fetchall()}

    def list_all(self):
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
            return rows

    def get(self, inv_id):
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
            return rec

    def find_by_mac(self, mac):
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
            rec["properties"] = {}

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
            try:
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
                return cur.lastrowid, None
            except sqlite3.IntegrityError:
                return None, f"a record with MAC {clean.get('mac')} already exists"

    def _merge_managed_properties(self, inv_id, incoming_properties):
        """A property update carries over MANAGED_PROPERTY_KEYS from the
        stored record when the incoming properties dict doesn't mention
        them, so a drawer save built from the per-type field list can't
        silently wipe a server-managed key like network_role. An explicit
        key in the incoming dict always wins. Returns the (possibly
        augmented) dict, or None when incoming_properties isn't a dict/
        JSON-object string (a clear should still clear everything)."""
        incoming = None
        if isinstance(incoming_properties, dict):
            incoming = dict(incoming_properties)
        elif isinstance(incoming_properties, str) and incoming_properties.strip():
            try:
                parsed = json.loads(incoming_properties)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                incoming = parsed
        if incoming is None:
            return None
        current = self.get(inv_id)
        stored_props = (current or {}).get("properties") or {}
        for key in MANAGED_PROPERTY_KEYS:
            if key in stored_props and key not in incoming:
                incoming[key] = stored_props[key]
        return incoming

    def update(self, inv_id, data):
        if "properties" in data:
            merged = self._merge_managed_properties(inv_id, data.get("properties"))
            if merged is not None:
                data = dict(data)
                data["properties"] = merged
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
        self._relint_around(inv_id)
        return True, None

    def _relint_around(self, inv_id):
        """After a record edit (port_count, device_type, network_role...),
        drift on edges touching it may be fixed. Best effort: a relint
        failure must never fail the edit itself."""
        try:
            parents = {inv_id} | {e["parent_id"]
                                  for e in self.list_connections_for_device(inv_id)}
            now = int(time.time())
            for pid in parents:
                self.relint_parent(pid, now)
        except Exception as e:
            logging.warning(f"InventoryDB: relint after edit failed: {type(e).__name__}")

    # ─── Connections (inventory_connections table) ──────────────────────────
    # Connection types we accept. Anything else gets coerced to "ethernet".
    CONNECTION_TYPES = ("ethernet", "fiber", "wifi", "virtual", "power", "usb", "console", "other")

    def _normalize_conn_type(self, t):
        if t is None: return "ethernet"
        s = str(t).strip().lower()
        return s if s in self.CONNECTION_TYPES else "ethernet"

    # LEFT JOINs + a WHERE on both names drops edges whose device no longer
    # exists (possible for rows written before foreign_keys was enabled).
    _CONN_SELECT = (
        "SELECT c.id, c.from_device_id, c.to_device_id, c.from_port, c.to_port, "
        "c.connection_type, c.notes, c.created_at, c.source, c.external_key, "
        "c.last_seen, c.updated_at, "
        "f.system AS from_name, f.device_type AS from_type, "
        "t.system AS to_name,   t.device_type AS to_type "
        "FROM inventory_connections c "
        "LEFT JOIN inventory f ON f.id = c.from_device_id "
        "LEFT JOIN inventory t ON t.id = c.to_device_id "
        "WHERE f.id IS NOT NULL AND t.id IS NOT NULL "
    )

    @staticmethod
    def _conn_rows(cur):
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows:
            r["child_id"], r["parent_id"] = r["from_device_id"], r["to_device_id"]
            r["child_port"], r["parent_port"] = r["from_port"], r["to_port"]
            r["child_name"], r["parent_name"] = r["from_name"], r["to_name"]
            r["child_type"], r["parent_type"] = r["from_type"], r["to_type"]
        return rows

    def list_connections_for_device(self, device_id):
        """All edges touching this device. `direction` is "out" when this
        device is the child end, "in" when it is the parent end."""
        with self.lock:
            cur = self.conn.execute(
                self._CONN_SELECT
                + "AND (c.from_device_id = ? OR c.to_device_id = ?) "
                "ORDER BY c.connection_type, c.created_at",
                (device_id, device_id),
            )
            rows = self._conn_rows(cur)
        for r in rows:
            r["direction"] = "out" if r["from_device_id"] == device_id else "in"
        return rows

    def list_all_connections(self):
        """Every edge. Used by the topology view and the connections API."""
        with self.lock:
            cur = self.conn.execute(
                self._CONN_SELECT + "ORDER BY c.connection_type, c.created_at")
            return self._conn_rows(cur)

    def get_connection(self, conn_id):
        with self.lock:
            cur = self.conn.execute(self._CONN_SELECT + "AND c.id = ?", (conn_id,))
            rows = self._conn_rows(cur)
        return rows[0] if rows else None

    def _live_ports_for(self, rec):
        """Live port table for `rec` from the discovery layer, if any."""
        fn = self.live_port_provider
        if fn is None:
            return None
        try:
            return fn(rec)
        except Exception as e:
            logging.warning(f"InventoryDB: live port provider failed: {type(e).__name__}")
            return None

    def connections_v2_ready(self):
        with self.lock:
            row = self.conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'connections_v2'").fetchone()
        return bool(row and row[0] == "done")

    def get_meta(self, key):
        with self.lock:
            row = self.conn.execute(
                "SELECT value FROM schema_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                (key, value))

    def migrate_connections_v2(self, backup_fn=None, now=None):
        """One-time data migration for the 4.0 connections model: seed
        properties.network_role, re-orient edges child -> parent (swapping
        their port fields), mark them manual, and file anything suspicious
        as drift suggestions. Never deletes or rewrites anything else.

        backup_fn runs first; if it raises, nothing is changed and the new
        connection endpoints stay disabled (they return 503)."""
        if self.connections_v2_ready():
            return True, "already migrated"
        if backup_fn is not None:
            try:
                backup_fn()
            except Exception as e:
                logging.error("InventoryDB: connections v2 migration aborted, "
                              f"backup failed: {type(e).__name__}: {e}")
                return False, "backup failed"
        now = int(now or time.time())
        records = {r["id"]: r for r in self.list_all()}
        plan = plan_connections_migration(
            records, self.list_all_connections(), self._live_ports_for)
        # Never overwrite a properties blob we can't parse: skip seeding
        # network_role for any record whose *raw* stored value is non-empty
        # but not valid JSON (a NULL/empty blob is fine to seed).
        network_roles = dict(plan["network_roles"])
        if network_roles:
            with self.lock:
                qmarks = ",".join("?" * len(network_roles))
                raw_by_id = dict(self.conn.execute(
                    f"SELECT id, properties FROM inventory WHERE id IN ({qmarks})",
                    tuple(network_roles)).fetchall())
            for rid in list(network_roles):
                raw = raw_by_id.get(rid)
                if not raw:
                    continue
                try:
                    parsed = json.loads(raw)
                except (ValueError, TypeError):
                    parsed = None
                if not isinstance(parsed, dict):
                    del network_roles[rid]
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                for rid, role in network_roles.items():
                    props = dict(records[rid].get("properties") or {})
                    props["network_role"] = role
                    self.conn.execute(
                        "UPDATE inventory SET properties = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(props), now, rid))
                for eid in plan["swaps"]:
                    # SQLite evaluates every right-hand side against the old
                    # row, so this is a true swap.
                    self.conn.execute(
                        "UPDATE inventory_connections SET "
                        "from_device_id = to_device_id, to_device_id = from_device_id, "
                        "from_port = to_port, to_port = from_port WHERE id = ?", (eid,))
                self.conn.execute(
                    "UPDATE inventory_connections SET source = 'manual', updated_at = ?",
                    (now,))
                for key, payload in plan["drift"]:
                    self.suggestions.upsert_locked(
                        "drift", "migration", key, payload, fingerprint(payload), now)
                self.conn.execute(
                    "INSERT OR REPLACE INTO schema_meta (key, value) "
                    "VALUES ('connections_v2', 'done')")
                self.conn.execute("COMMIT")
            except Exception as e:
                try: self.conn.execute("ROLLBACK")
                except Exception: pass
                logging.error("InventoryDB: connections v2 migration failed mid-"
                              f"transaction, rolled back: {type(e).__name__}: {e}")
                return False, "migration failed"
        msg = (f"re-oriented {len(plan['swaps'])} edge(s), seeded "
               f"{len(network_roles)} network role(s), flagged "
               f"{len(plan['drift'])} for review")
        logging.info(f"InventoryDB: connections v2 migration done: {msg}")
        return True, msg

    def ports_for_device(self, device_id):
        """(ports, record). ports is None when the device's ports are free
        text; each port dict gains an `occupants` list."""
        rec = self.get(device_id)
        if rec is None:
            return None, None
        ports = resolve_ports(rec, self._live_ports_for(rec))
        if ports is None:
            return None, rec
        occupants = {}
        with self.lock:
            rows = self.conn.execute(
                "SELECT c.id, c.to_port, i.id, i.system FROM inventory_connections c "
                "JOIN inventory i ON i.id = c.from_device_id WHERE c.to_device_id = ? "
                "AND c.connection_type != 'wifi'",
                (device_id,)).fetchall()
            # A device's own uplink occupies its port too: the edge where this
            # device is the CHILD (from_device_id), keyed by *its* from_port,
            # joined to the parent it uplinks to.
            uplink_rows = self.conn.execute(
                "SELECT c.id, c.from_port, i.id, i.system FROM inventory_connections c "
                "JOIN inventory i ON i.id = c.to_device_id WHERE c.from_device_id = ? "
                "AND c.connection_type != 'wifi'",
                (device_id,)).fetchall()
        for cid, port, iid, name in rows:
            p = canonical_port(port, ports)
            if p is not None:
                occupants.setdefault(p, []).append(
                    {"connection_id": cid, "device_id": iid, "name": name, "uplink": False})
        for cid, port, iid, name in uplink_rows:
            p = canonical_port(port, ports)
            if p is not None:
                occupants.setdefault(p, []).append(
                    {"connection_id": cid, "device_id": iid, "name": name, "uplink": True})
        for p in ports:
            p["occupants"] = occupants.get(p["name"], [])
        return ports, rec

    def preview_connection(self, a_id, b_id, connection_type=None):
        """How an edge between a and b would be stored. (preview, error)."""
        if a_id == b_id:
            return None, "cannot connect a device to itself"
        a, b = self.get(a_id), self.get(b_id)
        if a is None or b is None:
            return None, "one or both devices do not exist"
        child, parent, ambiguous = orient_edge(a, b, connection_type)
        ports, _ = self.ports_for_device(parent["id"])
        return {
            "child_id": child["id"], "child_name": child["system"],
            "child_type": child.get("device_type") or "host",
            "parent_id": parent["id"], "parent_name": parent["system"],
            "parent_type": parent.get("device_type") or "host",
            "ambiguous": ambiguous,
            "default_type": default_connection_type(child, parent),
            "ports": ports,
        }, None

    def _port_in_use(self, parent_id, port, exclude_conn_id=None, uplinks=True):
        """Another non-wifi edge already on this parent port? Ports are
        compared canonically, so "8" and "Port 8" are the same port. A
        device's own uplink (parent_id as the CHILD, from_port on that edge)
        occupies its port too, unless `uplinks=False` - relint_parent passes
        that to match migration lint, which only ever considered downlinks
        (plan_connections_migration's by_port grouping keys on to_device_id
        alone); it must keep resolving exactly the drift migration could
        have flagged, not surface new drift from a check migration never
        made."""
        parent = self.get(parent_id)
        ports = resolve_ports(parent, self._live_ports_for(parent)) if parent else None
        want = canonical_port(port, ports)
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, to_port FROM inventory_connections "
                "WHERE to_device_id = ? AND connection_type != 'wifi'",
                (parent_id,)).fetchall()
            uplink_rows = self.conn.execute(
                "SELECT id, from_port FROM inventory_connections "
                "WHERE from_device_id = ? AND connection_type != 'wifi'",
                (parent_id,)).fetchall() if uplinks else []
        return any(canonical_port(p, ports) == want and cid != exclude_conn_id
                   for cid, p in rows + uplink_rows)

    def _insert_connection_locked(self, child_id, parent_id, child_port, parent_port,
                                  ctype, notes, now, source="manual", external_key=None,
                                  last_seen=None):
        cur = self.conn.execute(
            "INSERT INTO inventory_connections "
            "(from_device_id, to_device_id, from_port, to_port, connection_type, "
            "notes, created_at, source, external_key, updated_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (child_id, parent_id, child_port, parent_port, ctype, notes,
             now, source, external_key, now, last_seen))
        return cur.lastrowid

    def _insert_connection(self, child_id, parent_id, child_port, parent_port,
                           ctype, notes, now, source="manual", external_key=None):
        with self.lock:
            return self._insert_connection_locked(
                child_id, parent_id, child_port, parent_port, ctype, notes, now,
                source, external_key)

    def quick_add_connection(self, data, now=None):
        """Order-agnostic create: the server decides which end is the child.
        Returns (id, warnings, error)."""
        try:
            a_id, b_id = int(data.get("a_id")), int(data.get("b_id"))
        except (TypeError, ValueError):
            return None, [], "a_id and b_id required"
        requested = data.get("connection_type")
        ctype = self._normalize_conn_type(requested) if requested else None
        preview, err = self.preview_connection(a_id, b_id, ctype)
        if err:
            return None, [], err
        child, parent = self.get(preview["child_id"]), self.get(preview["parent_id"])
        ports = preview["ports"]
        # The swap control only exists in the UI when orientation was
        # ambiguous - ignore it otherwise rather than let it force an
        # orientation the device types have already decided.
        if data.get("swap") and preview["ambiguous"]:
            child, parent = parent, child
            ports, _ = self.ports_for_device(parent["id"])
        ctype = ctype or default_connection_type(child, parent)
        parent_port = normalize_port(data.get("parent_port"))
        perr = validate_parent_port(parent, parent_port, ports)
        if perr:
            return None, [], perr
        parent_port = canonical_port(parent_port, ports)
        warnings = []
        if (parent_port is not None and ctype != "wifi"
                and self._port_in_use(parent["id"], parent_port)):
            warnings.append("port_in_use")
        now = int(now or time.time())
        raw_notes = data.get("notes")
        notes = (str(raw_notes).strip() if raw_notes is not None else "") or None
        new_id = self._insert_connection(
            child["id"], parent["id"], normalize_port(data.get("child_port")),
            parent_port, ctype, notes, now)
        return new_id, warnings, None

    def create_connection(self, data):
        """Legacy create (POST /api/inventory/<id>/connections): the caller's
        from/to order is a hint only; the edge is stored oriented, with the
        port fields following their devices. Returns (id, error)."""
        try:
            from_id = int(data.get("from_device_id"))
            to_id   = int(data.get("to_device_id"))
        except (TypeError, ValueError):
            return None, "from_device_id and to_device_id required"
        if from_id == to_id:
            return None, "cannot connect a device to itself"
        a, b = self.get(from_id), self.get(to_id)
        if a is None or b is None:
            return None, "one or both devices do not exist"
        ctype = self._normalize_conn_type(data.get("connection_type"))
        raw_from_port = data.get("from_port")
        from_port = (str(raw_from_port).strip() if raw_from_port is not None else "") or None
        raw_to_port = data.get("to_port")
        to_port = (str(raw_to_port).strip() if raw_to_port is not None else "") or None
        child, parent, _ambiguous = orient_edge(a, b, ctype)
        if child is a:
            child_port, parent_port = from_port, to_port
        else:
            child_port, parent_port = to_port, from_port
        raw_notes = data.get("notes")
        notes = (str(raw_notes).strip() if raw_notes is not None else "") or None
        new_id = self._insert_connection(
            child["id"], parent["id"], child_port, normalize_port(parent_port),
            ctype, notes, int(time.time()))
        return new_id, None

    def update_connection(self, conn_id, data, now=None):
        """Edit ports/type/notes, or swap ends. A hand edit of a discovered
        edge makes it manual. Returns (ok, error, warnings)."""
        existing = self.get_connection(conn_id)
        if existing is None:
            return False, "connection not found", []
        child_id, parent_id = existing["from_device_id"], existing["to_device_id"]
        child_port, parent_port = existing["from_port"], existing["to_port"]
        ctype, notes = existing["connection_type"], existing["notes"]
        changed = False
        port_touched = False
        if data.get("swap"):
            # The swap control only exists in the UI when orientation was
            # ambiguous - the pair's own device types decide otherwise.
            child_rec, parent_rec = self.get(child_id), self.get(parent_id)
            if child_rec is None or parent_rec is None:
                return False, "one or both devices do not exist", []
            if not orient_edge(child_rec, parent_rec, ctype)[2]:
                return False, ("orientation is decided by device types; "
                               "swap only applies to ambiguous pairs"), []
            child_id, parent_id = parent_id, child_id
            child_port, parent_port = parent_port, child_port
            changed = True
            port_touched = True
        for key in ("parent_port", "to_port"):
            if key in data:
                parent_port = normalize_port(data.get(key))
                changed = True
                port_touched = True
                break
        for key in ("child_port", "from_port"):
            if key in data:
                child_port = (str(data.get(key) or "")).strip() or None
                changed = True
                break
        if "connection_type" in data:
            ctype = self._normalize_conn_type(data.get("connection_type"))
            changed = True
        if "notes" in data:
            raw_notes = data.get("notes")
            notes = (str(raw_notes).strip() if raw_notes is not None else "") or None
            changed = True
        port_cleared = False
        if ("connection_type" in data and ctype == "wifi"
                and parent_port is not None and not port_touched):
            # A wifi link has no parent port: switching the type to wifi drops
            # the old switch port rather than leaving it behind. An edge that
            # was already wifi (legacy bad data) is untouched here - clearing
            # its stale port is a deliberate edit, not a side effect of
            # editing something else.
            parent_port = None
            port_cleared = True
        if not changed:
            return False, "no fields to update", []
        warnings = []
        if port_cleared:
            warnings.append("parent_port_cleared")
        if port_touched:
            if ctype == "wifi":
                if parent_port is not None:
                    return False, "wifi connections don't use a parent port", []
            else:
                ports, parent = self.ports_for_device(parent_id)
                perr = validate_parent_port(parent, parent_port, ports)
                if perr:
                    return False, perr, []
                parent_port = canonical_port(parent_port, ports)
            if (parent_port is not None and ctype != "wifi"
                    and self._port_in_use(parent_id, normalize_port(parent_port), conn_id)):
                warnings.append("port_in_use")
        now = int(now or time.time())
        with self.lock:
            self.conn.execute(
                "UPDATE inventory_connections SET from_device_id = ?, to_device_id = ?, "
                "from_port = ?, to_port = ?, connection_type = ?, notes = ?, "
                "source = 'manual', updated_at = ? WHERE id = ?",
                (child_id, parent_id, child_port, parent_port, ctype, notes, now, conn_id))
        self.relint_parent(parent_id, now,
                           confirmed={conn_id} if data.get("swap") else None)
        if parent_id != existing["to_device_id"]:
            self.relint_parent(existing["to_device_id"], now)
        return True, None, warnings

    def delete_connection(self, conn_id):
        existing = self.get_connection(conn_id)
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM inventory_connections WHERE id = ?", (conn_id,))
            if cur.rowcount == 0:
                return False, "connection not found"
        now = int(time.time())
        self.suggestions.resolve(migration_drift_key(conn_id), now)
        if existing is not None:
            self.relint_parent(existing["to_device_id"], now)
        return True, None

    def relint_parent(self, parent_id, now=None, confirmed=None):
        """Resolve migration drift for edges on this parent that are now
        clean. Only resolves; new drift comes from migration/discovery.

        `confirmed`: edge ids whose direction the user just chose explicitly
        (a swap) - an ambiguous orientation no longer counts against them."""
        now = int(now or time.time())
        parent = self.get(parent_id)
        if parent is None:
            return
        ports = resolve_ports(parent, self._live_ports_for(parent))
        for row in self.list_connections_for_device(parent_id):
            if row["to_device_id"] != parent_id:
                continue
            child = self.get(row["from_device_id"])
            issues = lint_edge(row, child, parent, ports)
            if (orient_edge(child, parent, row["connection_type"])[2]
                    and row["id"] not in (confirmed or ())):
                issues.append("ambiguous_direction")
            port = normalize_port(row["to_port"])
            if (port is not None and row["connection_type"] != "wifi"
                    and self._port_in_use(parent_id, port, row["id"], uplinks=False)):
                issues.append("duplicate_parent_port")
            if not issues:
                self.suggestions.resolve(migration_drift_key(row["id"]), now)

    # ─── Discovery (plan 2) ─────────────────────────────────────────────────

    def _rollback_quietly(self):
        try:
            self.conn.execute("ROLLBACK")
        except Exception:
            pass

    def apply_discovery_changes(self, changes, now=None):
        """Apply one scan's change set (netwatch.discovery.reconcile) in a
        single transaction. Touches never change a manual edge's port."""
        now = int(now or time.time())
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                for u in changes.get("upserts", []):
                    self.suggestions.upsert_locked(
                        u["kind"], u["source"], u["subject_key"], u["payload"], u["fp"], now)
                for key in changes.get("resolve", []):
                    self.suggestions.resolve_locked(key, now)
                for t in changes.get("touch", []):
                    if t["parent_port"] is None:
                        self.conn.execute(
                            "UPDATE inventory_connections SET last_seen = ? WHERE id = ?",
                            (now, t["id"]))
                    else:
                        self.conn.execute(
                            "UPDATE inventory_connections SET last_seen = ?, to_port = ?, "
                            "updated_at = ? WHERE id = ? AND source != 'manual'",
                            (now, t["parent_port"], now, t["id"]))
                for pf in changes.get("props", []):
                    # Spec §1.5: fill managed guest properties Proxmox knows,
                    # never overwrite anything already recorded.
                    row = self.conn.execute(
                        "SELECT properties FROM inventory WHERE id = ?", (pf["id"],)).fetchone()
                    if row is None:
                        continue
                    try:
                        props = json.loads(row[0]) if row[0] else {}
                    except ValueError:
                        continue
                    if not isinstance(props, dict):
                        continue
                    missing = {k: v for k, v in pf["set"].items() if props.get(k) in (None, "")}
                    if missing:
                        props.update(missing)
                        self.conn.execute(
                            "UPDATE inventory SET properties = ?, updated_at = ? WHERE id = ?",
                            (json.dumps(props), now, pf["id"]))
                for f in changes.get("ips", []):
                    # A guest's IP from Proxmox/ARP fills an empty field only.
                    self.conn.execute(
                        "UPDATE inventory SET ip = ?, updated_at = ? "
                        "WHERE id = ? AND (ip IS NULL OR ip = '')", (f["ip"], now, f["id"]))
                for f in changes.get("macs", []):
                    # A matched guest's net0 MAC fills an empty field only, and
                    # never one another record already uses (as mac or alias).
                    mac = self.normalize_mac(f["mac"])
                    if not mac or self._mac_conflict_locked(mac, exclude_id=f["id"]) is not None:
                        continue
                    self.conn.execute(
                        "UPDATE inventory SET mac = ?, updated_at = ? "
                        "WHERE id = ? AND (mac IS NULL OR mac = '')", (mac, now, f["id"]))
                self.conn.execute("COMMIT")
            except BaseException:
                self._rollback_quietly()
                raise

    def accept_suggestion(self, sid, fp, overrides=None, action=None, now=None):
        """Apply a pending suggestion atomically. Returns (ok, error, result);
        error is None, "not_found", "suggestion_changed" or "rejected" (then
        result["error"] says why).

        The fingerprint only covers the suggestion row, not the live inventory
        it was computed from - so every `_accept_*_locked` handler re-checks,
        inside this same transaction, that the live rows still look the way
        the payload assumed, and raises _SuggestionChanged if not."""
        now = int(now or time.time())
        overrides = overrides if isinstance(overrides, dict) else {}
        handlers = {
            "edge": self._accept_edge_locked,
            "device": self._accept_device_locked,
            "drift": self._accept_drift_locked,
            "identity": self._accept_identity_locked,
            "shared_port": self._accept_shared_port_locked,
        }
        # Live ports come from the discovery runner, which takes its own
        # lock - so fetch them before taking self.lock, never inside it.
        # A payload that changes in between fails the fingerprint check.
        peek = self.suggestions.get(sid)
        if peek is not None and peek["kind"] == "shared_port":
            switch = self.get((peek["payload"] or {}).get("switch_id"))
            live = self._live_ports_for(switch) if switch else None
            handlers["shared_port"] = (
                lambda *a: self._accept_shared_port_locked(*a, live_ports=live))
        relint = set()
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                cur = self.conn.execute(
                    "SELECT kind, payload, status, fingerprint FROM connection_suggestions "
                    "WHERE id = ?", (sid,)).fetchone()
                if cur is None:
                    self._rollback_quietly()
                    return False, "not_found", {}
                kind, raw_payload, status, stored_fp = cur
                if status != "pending" or stored_fp != fp:
                    raise _SuggestionChanged()
                try:
                    payload = json.loads(raw_payload)
                except (TypeError, ValueError):
                    payload = {}
                handler = handlers.get(kind)
                if handler is None:
                    raise _SuggestionRejected(f"unknown suggestion kind '{kind}'")
                result = handler(payload, overrides, action, now, relint)
                self.conn.execute(
                    "UPDATE connection_suggestions SET status = 'accepted', decided_at = ? "
                    "WHERE id = ?", (now, sid))
                self.conn.execute("COMMIT")
            except _SuggestionChanged:
                self._rollback_quietly()
                return False, "suggestion_changed", {}
            except _SuggestionRejected as e:
                self._rollback_quietly()
                return False, "rejected", {"error": str(e)}
            except BaseException:
                self._rollback_quietly()
                raise
        # The accept already committed; a relint failure here must not turn a
        # successful accept into an error response.
        for pid in relint:
            try:
                self.relint_parent(pid, now)
            except Exception as e:
                logging.warning(
                    f"InventoryDB: relint_parent failed after accept: {type(e).__name__}")
        return True, None, result

    def accept_suggestions(self, items, now=None):
        """Accept-all: each item independently; partial success is fine."""
        results = []
        for it in items:
            it = it if isinstance(it, dict) else {}
            try:
                sid = int(it.get("id"))
            except (TypeError, ValueError):
                results.append({"id": it.get("id"), "ok": False, "error": "invalid id"})
                continue
            ok, err, res = self.accept_suggestion(sid, it.get("fingerprint"), now=now)
            out = {"id": sid, "ok": ok, "error": None if ok else (res.get("error") or err)}
            if ok and res.get("device_id") is not None:
                out["device_id"] = res["device_id"]   # a created device (guest monitoring)
            results.append(out)
        return results

    def _device_exists_locked(self, inv_id):
        return self.conn.execute(
            "SELECT 1 FROM inventory WHERE id = ?", (inv_id,)).fetchone() is not None

    # SQL fragment for "any network-link edge" (an interface has at most one).
    _LINK_TYPE_PLACEHOLDERS = ",".join("?" * len(NETWORK_LINK_TYPES))

    def _has_network_link_locked(self, device_id, as_child=True):
        col = "from_device_id" if as_child else "to_device_id"
        return self.conn.execute(
            f"SELECT 1 FROM inventory_connections WHERE {col} = ? AND "
            f"connection_type IN ({self._LINK_TYPE_PLACEHOLDERS})",
            (device_id, *NETWORK_LINK_TYPES)).fetchone() is not None

    def _mac_conflict_locked(self, mac, exclude_id=None):
        """The system name of another record already using `mac` (as its
        primary mac or a properties.mac_aliases entry), or None."""
        if exclude_id is None:
            row = self.conn.execute(
                "SELECT system FROM inventory WHERE mac = ?", (mac,)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT system FROM inventory WHERE mac = ? AND id != ?",
                (mac, exclude_id)).fetchone()
        if row:
            return row[0]
        params = (f"%{mac}%",) if exclude_id is None else (exclude_id, f"%{mac}%")
        query = ("SELECT system, properties FROM inventory WHERE properties LIKE ?"
                 if exclude_id is None else
                 "SELECT system, properties FROM inventory WHERE id != ? AND properties LIKE ?")
        for system, raw in self.conn.execute(query, params).fetchall():
            try:
                props = json.loads(raw) if raw else {}
            except ValueError:
                continue
            if isinstance(props, dict) and mac in (props.get("mac_aliases") or []):
                return system
        return None

    def _accept_edge_locked(self, p, overrides, action, now, relint):
        if not (self._device_exists_locked(p["child_id"])
                and self._device_exists_locked(p["parent_id"])):
            raise _SuggestionChanged()
        # One edge per conflict family: a device has at most one network link
        # (a second would be a phantom uplink), and a guest runs on one node.
        if p["connection_type"] in NETWORK_LINK_TYPES:
            if self._has_network_link_locked(p["child_id"], as_child=True):
                raise _SuggestionChanged()
        elif self.conn.execute(
                "SELECT 1 FROM inventory_connections WHERE from_device_id = ? AND "
                "connection_type = ?", (p["child_id"], p["connection_type"])).fetchone():
            raise _SuggestionChanged()
        cid = self._insert_connection_locked(
            p["child_id"], p["parent_id"], p.get("child_port"), p.get("parent_port"),
            p["connection_type"], None, now, source=p["source"],
            external_key=p["external_key"], last_seen=now)
        relint.add(p["parent_id"])
        return {"connection_id": cid}

    def _accept_device_locked(self, p, overrides, action, now, relint):
        dev = dict(p["device"])
        for k in ("system", "device_type", "category"):
            if k in overrides:
                dev[k] = overrides[k]
        system = str(dev.get("system") or "").strip()
        if not system:
            raise _SuggestionRejected("system name is required")
        dtype = str(dev.get("device_type") or "host").strip().lower()
        if dtype not in INVENTORY_TYPES:
            raise _SuggestionRejected(f"unknown device type '{dtype}'")
        mac = self.normalize_mac(dev.get("mac")) or None
        if mac and self._mac_conflict_locked(mac) is not None:
            raise _SuggestionChanged()
        e = p["edge"]
        if not self._device_exists_locked(e["parent_id"]):
            raise _SuggestionChanged()
        category = str(dev.get("category") or "").strip() or None
        props = dev.get("properties")
        props_json = json.dumps(props) if isinstance(props, dict) and props else None
        cur = self.conn.execute(
            "INSERT INTO inventory (category, system, device_type, mac, ip, properties, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (category, system, dtype, mac, dev.get("ip"), props_json, now, now))
        device_id = cur.lastrowid
        cid = self._insert_connection_locked(
            device_id, e["parent_id"], e.get("child_port"), e.get("parent_port"),
            e["connection_type"], None, now, source=e["source"],
            external_key=e["external_key"], last_seen=now)
        relint.add(e["parent_id"])
        return {"device_id": device_id, "connection_id": cid}

    def _accept_drift_locked(self, p, overrides, action, now, relint):
        kind = p.get("action")
        if kind not in ("replace", "remove"):
            raise _SuggestionRejected("this suggestion can only be dismissed")
        if action is not None and action != kind:
            raise _SuggestionRejected(f"this suggestion's action is '{kind}'")
        row = self.conn.execute(
            "SELECT from_device_id, to_device_id, to_port, connection_type, source "
            "FROM inventory_connections WHERE id = ?",
            (p["connection_id"],)).fetchone()
        if row is None:
            raise _SuggestionChanged()
        from_id, to_id, to_port, ctype, source = row
        old_parent = to_id
        if kind == "remove":
            # Only remove an edge that's still discovery-sourced and still
            # connects the same two devices the suggestion was computed for.
            if (source == "manual" or from_id != p["child_id"]
                    or to_id != p["parent_id"]):
                raise _SuggestionChanged()
            self.conn.execute(
                "DELETE FROM inventory_connections WHERE id = ?", (p["connection_id"],))
            self.suggestions.resolve_locked(migration_drift_key(p["connection_id"]), now)
            relint.add(old_parent)
            return {"removed_connection_id": p["connection_id"]}
        # "replace": the live edge must still look like p["current"] (raw
        # equality - either side may be None) or the proposal no longer
        # applies to what's actually stored.
        cur_expected = p["current"]
        if (from_id != p["child_id"] or to_id != cur_expected["parent_id"]
                or to_port != cur_expected["parent_port"]
                or ctype != cur_expected["connection_type"]):
            raise _SuggestionChanged()
        q = p["proposed"]
        if not self._device_exists_locked(q["parent_id"]):
            raise _SuggestionChanged()
        self.conn.execute(
            "UPDATE inventory_connections SET to_device_id = ?, to_port = ?, "
            "from_port = COALESCE(?, from_port), connection_type = ?, source = ?, "
            "external_key = ?, last_seen = ?, updated_at = ? WHERE id = ?",
            (q["parent_id"], q.get("parent_port"), q.get("child_port"),
             q["connection_type"], q["source"], q["external_key"], now, now,
             p["connection_id"]))
        relint.update({old_parent, q["parent_id"]})
        return {"connection_id": p["connection_id"]}

    def _accept_identity_locked(self, p, overrides, action, now, relint):
        has_override = "device_id" in overrides
        target = overrides.get("device_id", p.get("candidate_id"))
        try:
            target = int(target)
        except (TypeError, ValueError):
            raise _SuggestionRejected("choose which device this is (overrides.device_id)")
        row = self.conn.execute(
            "SELECT properties, system, device_type FROM inventory WHERE id = ?",
            (target,)).fetchone()
        if row is None:
            # A device_id the caller typed in themselves that doesn't exist is
            # their mistake (rejected); the suggestion's own candidate having
            # vanished since the scan is the data changing under us (409).
            if has_override:
                raise _SuggestionRejected("that device does not exist")
            raise _SuggestionChanged()
        try:
            props = json.loads(row[0]) if row[0] else {}
        except ValueError:
            props = None
        if not isinstance(props, dict):
            raise _SuggestionRejected("that device's properties are unreadable; fix them first")
        if p.get("proxmox_node"):
            name = str(p["proxmox_node"])
            if row[2] == "vm":
                raise _SuggestionRejected("a VM can't be a Proxmox node")
            current = props.get("proxmox_node")
            if current not in (None, "") and str(current) != name:
                raise _SuggestionRejected(f"{row[1]} is already Proxmox node {current}")
            # A VM record carrying properties.proxmox_node means "runs on
            # node X", not "is node X" - it never blocks the node identity.
            for system, dtype, raw in self.conn.execute(
                    "SELECT system, device_type, properties FROM inventory WHERE id != ? AND "
                    "properties LIKE ?", (target, '%"proxmox_node"%')).fetchall():
                if dtype == "vm":
                    continue
                try:
                    other = json.loads(raw) if raw else {}
                except ValueError:
                    continue
                if isinstance(other, dict) and other.get("proxmox_node") == name:
                    raise _SuggestionRejected(f"{system} is already Proxmox node {name}")
            props["proxmox_node"] = name
            self.conn.execute(
                "UPDATE inventory SET properties = ?, updated_at = ? WHERE id = ?",
                (json.dumps(props), now, target))
            return {"device_id": target}
        mac = self.normalize_mac(p["chassis_mac"])
        conflict = self._mac_conflict_locked(mac, exclude_id=target)
        if conflict is not None:
            raise _SuggestionRejected(f"that MAC already belongs to {conflict}")
        aliases = [a for a in (props.get("mac_aliases") or []) if isinstance(a, str)]
        if mac not in aliases:
            aliases.append(mac)
        props["mac_aliases"] = aliases
        self.conn.execute(
            "UPDATE inventory SET properties = ?, updated_at = ? WHERE id = ?",
            (json.dumps(props), now, target))
        return {"device_id": target}

    def _accept_shared_port_locked(self, p, overrides, action, now, relint,
                                   live_ports=None):
        switch_row = self.conn.execute(
            "SELECT mac, properties FROM inventory WHERE id = ?",
            (p["switch_id"],)).fetchone()
        if switch_row is None:
            raise _SuggestionChanged()
        mac, raw_props = switch_row
        try:
            props = json.loads(raw_props) if raw_props else {}
        except ValueError:
            props = {}
        if not isinstance(props, dict):
            props = {}
        switch_rec = {"id": p["switch_id"], "mac": mac, "properties": props}
        ports = resolve_ports(switch_rec, live_ports)
        want_port = canonical_port(p["port"], ports) if ports else p["port"]
        rows = self.conn.execute(
            f"SELECT to_port FROM inventory_connections WHERE to_device_id = ? AND "
            f"connection_type IN ({self._LINK_TYPE_PLACEHOLDERS})",
            (p["switch_id"], *NETWORK_LINK_TYPES)).fetchall()
        for (to_port,) in rows:
            stored = canonical_port(to_port, ports) if ports else to_port
            if stored == want_port:
                # Something is already on this port (possibly a hand-wired
                # edge added since the scan) - don't shadow it with a
                # placeholder switch.
                raise _SuggestionChanged()
        name = (str(overrides.get("system") or "").strip()
                or f"Unmanaged switch ({p['switch_name']} · {p['port']})")
        cur = self.conn.execute(
            "INSERT INTO inventory (system, device_type, properties, created_at, updated_at) "
            "VALUES (?, 'network', ?, ?, ?)",
            (name, json.dumps({"network_role": "switch"}), now, now))
        placeholder = cur.lastrowid
        self._insert_connection_locked(
            placeholder, p["switch_id"], None, p["port"], "ethernet", None, now,
            source="unifi", external_key=f"unifi:shared:{p['switch_id']}:{p['port']}",
            last_seen=now)
        linked = []
        for m in p.get("matched", []):
            if not self._device_exists_locked(m["id"]):
                continue
            # A matched device that's already wired elsewhere (e.g. hand-wired
            # since the scan) is left alone rather than double-linked.
            if self._has_network_link_locked(m["id"], as_child=True):
                continue
            self._insert_connection_locked(
                m["id"], placeholder, None, None, "ethernet", None, now,
                source="unifi", external_key=f"unifi:port:{m['mac']}", last_seen=now)
            linked.append(m["id"])
        relint.add(p["switch_id"])
        return {"device_id": placeholder, "linked": linked}

    def delete(self, inv_id):
        # Collect before deleting: ON DELETE CASCADE removes the edges along
        # with the device, so this is our only chance to see which
        # connection ids and parents were touched.
        edges = self.list_connections_for_device(inv_id)
        with self.lock:
            cur = self.conn.execute("DELETE FROM inventory WHERE id = ?", (inv_id,))
            if cur.rowcount == 0:
                return False, "record not found"
        now = int(time.time())
        for e in edges:
            self.suggestions.resolve(migration_drift_key(e["id"]), now)
        for parent_id in {e["parent_id"] for e in edges if e["parent_id"] != inv_id}:
            self.relint_parent(parent_id, now)
        return True, None

    def replace_all(self, records):
        """Wipe inventory and bulk-insert atomically. Used by 'Replace all' import mode.

        DELETE and all INSERTs run inside a single BEGIN/COMMIT while holding
        self.lock so concurrent readers never see a partially-empty table.
        """
        ok, fail = 0, []
        ts = int(time.time())
        # Pre-validate and clean records before acquiring the lock so the
        # locked section is as short as possible.
        cleaned = []
        for rec in records:
            clean = self._clean_input(rec)
            if not clean.get("system"):
                fail.append({"system": rec.get("system", "?"), "error": "system name is required"})
            else:
                cleaned.append((rec, clean))
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                self.conn.execute("DELETE FROM inventory")
                for rec, clean in cleaned:
                    if clean.get("mac"):
                        row = self.conn.execute(
                            "SELECT system FROM inventory WHERE mac = ?", (clean["mac"],)
                        ).fetchone()
                        if row:
                            fail.append({"system": clean["system"],
                                         "error": f"MAC {clean['mac']} already used by '{row[0]}'"})
                            continue
                    self.conn.execute(
                        "INSERT INTO inventory (category, system, role, cpu, ram_gb, gpu, "
                        "architecture, os, cpu_score, tdp_watts, tpm, mac, ip, serial, notes, "
                        "device_type, properties, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (clean.get("category"), clean["system"], clean.get("role"),
                         clean.get("cpu"), clean.get("ram_gb"), clean.get("gpu"),
                         clean.get("architecture"), clean.get("os"), clean.get("cpu_score"),
                         clean.get("tdp_watts"), clean.get("tpm"), clean.get("mac"),
                         clean.get("ip"), clean.get("serial"), clean.get("notes"),
                         clean.get("device_type") or "host", clean.get("properties"),
                         ts, ts),
                    )
                    ok += 1
                # The whole inventory (and therefore every edge, via ON
                # DELETE CASCADE) was just replaced: any pending migration
                # drift suggestion no longer refers to a connection that
                # exists, so it can never be resolved by delete/relint.
                self.conn.execute(
                    "UPDATE connection_suggestions SET status = 'resolved', decided_at = ? "
                    "WHERE status = 'pending' AND subject_key LIKE 'drift:migration:conn:%'",
                    (ts,))
                self.conn.execute("COMMIT")
            except Exception:
                try: self.conn.execute("ROLLBACK")
                except Exception: pass
                raise
        return ok, fail

    def _clean_input(self, data):
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
        return out


class SuggestionsDB:
    """Discovery/migration suggestions (connection_suggestions table).

    Shares InventoryDB's connection and lock. `*_locked` methods assume the
    caller already holds the lock (so a migration or scan can write many
    rows inside one transaction); the plain methods take it themselves.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS connection_suggestions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        kind        TEXT NOT NULL,
        source      TEXT NOT NULL,
        subject_key TEXT NOT NULL UNIQUE,
        payload     TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'pending',
        first_seen  INTEGER NOT NULL,
        last_seen   INTEGER NOT NULL,
        decided_at  INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_sugg_status ON connection_suggestions(status);
    """

    _COLS = ("id, kind, source, subject_key, payload, fingerprint, status, "
             "first_seen, last_seen, decided_at")

    def __init__(self, conn, lock):
        self.conn = conn
        self.lock = lock
        self.conn.executescript(self.SCHEMA)

    @staticmethod
    def _decode(cols, row):
        rec = dict(zip(cols, row))
        try:
            rec["payload"] = json.loads(rec["payload"])
        except (TypeError, ValueError):
            rec["payload"] = {}
        return rec

    def upsert_locked(self, kind, source, subject_key, payload, fp, now):
        row = self.conn.execute(
            "SELECT id, status, fingerprint, decided_at FROM connection_suggestions "
            "WHERE subject_key = ?", (subject_key,)).fetchone()
        blob = json.dumps(payload, sort_keys=True)
        if row is None:
            cur = self.conn.execute(
                "INSERT INTO connection_suggestions (kind, source, subject_key, "
                "payload, fingerprint, status, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                (kind, source, subject_key, blob, fp, now, now))
            return cur.lastrowid
        sid, status, old_fp, decided_at = row
        # Only a dismissal is sticky at an unchanged fingerprint (spec §1.7).
        # An accepted proposal that is observed again means its result went
        # away (e.g. the edge was deleted), so it reopens - unless this scan
        # read its inputs before the accept (decided_at >= now), in which
        # case the observation predates the accept and proves nothing.
        if old_fp == fp and (status == "dismissed" or (
                status == "accepted" and (decided_at or 0) >= now)):
            self.conn.execute(
                "UPDATE connection_suggestions SET last_seen = ? WHERE id = ?",
                (now, sid))
        else:
            self.conn.execute(
                "UPDATE connection_suggestions SET kind = ?, source = ?, payload = ?, "
                "fingerprint = ?, status = 'pending', last_seen = ?, decided_at = NULL "
                "WHERE id = ?",
                (kind, source, blob, fp, now, sid))
        return sid

    def upsert(self, kind, source, subject_key, payload, fp, now=None):
        with self.lock:
            return self.upsert_locked(kind, source, subject_key, payload, fp,
                                      int(now or time.time()))

    def get(self, sid):
        with self.lock:
            cur = self.conn.execute(
                f"SELECT {self._COLS} FROM connection_suggestions WHERE id = ?", (sid,))
            row = cur.fetchone()
            if not row:
                return None
            return self._decode([d[0] for d in cur.description], row)

    def list(self, status="pending"):
        with self.lock:
            cur = self.conn.execute(
                f"SELECT {self._COLS} FROM connection_suggestions WHERE status = ? "
                "ORDER BY kind, first_seen, id", (status,))
            cols = [d[0] for d in cur.description]
            return [self._decode(cols, r) for r in cur.fetchall()]

    def count_pending(self):
        with self.lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM connection_suggestions WHERE status = 'pending'"
            ).fetchone()[0]

    def set_status(self, sid, status, now=None):
        with self.lock:
            cur = self.conn.execute(
                "UPDATE connection_suggestions SET status = ?, decided_at = ? WHERE id = ?",
                (status, int(now or time.time()), sid))
            return cur.rowcount > 0

    def resolve_locked(self, subject_key, now):
        cur = self.conn.execute(
            "UPDATE connection_suggestions SET status = 'resolved', decided_at = ? "
            "WHERE subject_key = ? AND status = 'pending'", (now, subject_key))
        return cur.rowcount > 0

    def resolve(self, subject_key, now=None):
        with self.lock:
            return self.resolve_locked(subject_key, int(now or time.time()))

    def prune_decided(self, now=None, max_age_days=90):
        """Delete decided rows that have been neither decided nor observed
        for max_age_days. Still-observed dismissals are kept on purpose:
        deleting them would resurface them as new pending suggestions."""
        cutoff = int(now or time.time()) - max_age_days * 86400
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM connection_suggestions WHERE status != 'pending' "
                "AND decided_at IS NOT NULL AND decided_at < ? AND last_seen < ?",
                (cutoff, cutoff))
            return cur.rowcount


class QuickLinksDB:
    """SQLite-backed quick-links list for the Overview tab. Lives in the same
    database as everything else (shares HistoryDB's connection/lock), so
    existing backup/restore covers it automatically with no extra plumbing."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS quick_links (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        label      TEXT NOT NULL,
        url        TEXT NOT NULL,
        icon       TEXT,
        sort_order INTEGER NOT NULL,
        created_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_quicklinks_sort ON quick_links(sort_order);
    """

    def __init__(self, history_db):
        self.history_db = history_db
        self.lock = history_db.lock
        self.conn = history_db.conn
        self.conn.executescript(self.SCHEMA)

    def list_links(self):
        with self.lock:
            cur = self.conn.execute(
                "SELECT id, label, url, icon, sort_order, created_at "
                "FROM quick_links ORDER BY sort_order"
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def create_link(self, label, url, icon):
        with self.lock:
            cur = self.conn.execute("SELECT MAX(sort_order) FROM quick_links")
            row = cur.fetchone()
            next_order = (row[0] + 1) if row and row[0] is not None else 0
            ts = int(time.time())
            cur = self.conn.execute(
                "INSERT INTO quick_links (label, url, icon, sort_order, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (label, url, icon, next_order, ts),
            )
            return cur.lastrowid

    def update_link(self, link_id, **fields):
        sets, vals = [], []
        for f in ("label", "url", "icon"):
            if f in fields:
                sets.append(f"{f} = ?")
                vals.append(fields[f])
        if not sets:
            return True  # nothing to update, id existing or not is irrelevant
        vals.append(link_id)
        with self.lock:
            cur = self.conn.execute(
                f"UPDATE quick_links SET {', '.join(sets)} WHERE id = ?", vals
            )
            return cur.rowcount > 0

    def delete_link(self, link_id):
        with self.lock:
            cur = self.conn.execute("DELETE FROM quick_links WHERE id = ?", (link_id,))
            return cur.rowcount > 0

    def move_link(self, link_id, direction):
        with self.lock:
            cur = self.conn.execute(
                "SELECT id, sort_order FROM quick_links ORDER BY sort_order"
            )
            rows = cur.fetchall()
            ids = [r[0] for r in rows]
            if link_id not in ids:
                return False
            idx = ids.index(link_id)
            if direction == "up":
                if idx == 0:
                    return True  # no neighbor above, no-op
                other_idx = idx - 1
            else:
                if idx == len(rows) - 1:
                    return True  # no neighbor below, no-op
                other_idx = idx + 1
            this_id, this_order = rows[idx]
            other_id, other_order = rows[other_idx]
            self.conn.execute("UPDATE quick_links SET sort_order = ? WHERE id = ?", (other_order, this_id))
            self.conn.execute("UPDATE quick_links SET sort_order = ? WHERE id = ?", (this_order, other_id))
            return True


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
# Backup
# ============================================================================

BACKUP_MANIFEST_VERSION = 1


def create_backup_tarball(config_path, auth_path):
    """Build a complete backup tarball in memory and return (bytes, filename).

    The SQLite snapshot uses sqlite3's online .backup API for consistency.
    Other files are read normally.

    `config_path` is the path to hosts.yaml; the netwatch directory and
    db path are derived from it. `auth_path` is the auth.json location
    (may not exist yet if no users are configured).
    """
    import io
    import json as _json
    import socket
    import tarfile
    import tempfile
    from datetime import datetime as _dt

    netwatch_dir = os.path.dirname(os.path.abspath(config_path))
    db_path      = os.path.join(netwatch_dir, "netwatch.db")
    monitor_path = os.path.join(netwatch_dir, "monitor.py")
    hostname     = socket.gethostname() or "unknown"
    iso_now      = _dt.now().strftime("%Y-%m-%dT%H-%M-%S")
    filename     = f"netwatch-backup-{hostname}-{iso_now}.tar.gz"

    manifest = {
        "manifest_version": BACKUP_MANIFEST_VERSION,
        "netwatch_version": VERSION,
        "created_at":       int(time.time()),
        "created_iso":      _dt.now().isoformat(),
        "source_hostname":  hostname,
        "files":            {},
    }

    # 1) Make a consistent SQLite snapshot to a temp file. We can't
    # tar.add() the live db directly because WAL writes might be active.
    snapshot_path = None
    if os.path.isfile(db_path):
        fd, snapshot_path = tempfile.mkstemp(prefix="nw_backup_", suffix=".db")
        os.close(fd)
        try:
            src = sqlite3.connect(db_path)
            try:
                dst = sqlite3.connect(snapshot_path)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
        except Exception:
            # If snapshot fails for any reason, clean up and re-raise
            try: os.unlink(snapshot_path)
            except OSError: pass
            raise

    # 2) Build the tarball in memory.
    try:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            def _add(real_path, arcname, mode_override=None):
                if not os.path.isfile(real_path):
                    return
                ti = tar.gettarinfo(real_path, arcname=arcname)
                if mode_override is not None:
                    ti.mode = mode_override
                # Strip uid/gid - they're meaningless across machines
                ti.uid = 0; ti.gid = 0
                ti.uname = ""; ti.gname = ""
                with open(real_path, "rb") as f:
                    tar.addfile(ti, f)
                manifest["files"][os.path.basename(arcname)] = os.path.getsize(real_path)

            _add(monitor_path, "netwatch/monitor.py")
            _add(config_path,  "netwatch/hosts.yaml")
            _add(auth_path,    "netwatch/auth.json", mode_override=0o600)
            if snapshot_path:
                # Snapshot lands under the original db filename
                ti = tar.gettarinfo(snapshot_path, arcname="netwatch/netwatch.db")
                ti.uid = 0; ti.gid = 0
                ti.uname = ""; ti.gname = ""
                with open(snapshot_path, "rb") as f:
                    tar.addfile(ti, f)
                manifest["files"]["netwatch.db"] = os.path.getsize(snapshot_path)

            # Manifest goes in last so all file sizes are populated
            manifest_bytes = _json.dumps(manifest, indent=2).encode("utf-8")
            ti = tarfile.TarInfo(name="netwatch/metadata.json")
            ti.size = len(manifest_bytes)
            ti.mtime = int(time.time())
            ti.mode = 0o644
            tar.addfile(ti, io.BytesIO(manifest_bytes))

        return buf.getvalue(), filename, manifest
    finally:
        # Always clean up the snapshot file, even if tar.add fails
        if snapshot_path:
            try: os.unlink(snapshot_path)
            except OSError: pass


def write_pre_migration_backup(config_path, auth_path, label):
    """Write a full backup tarball to backups/ next to hosts.yaml before a
    data migration. Returns the path. Mode 0600: it contains auth.json."""
    data, _filename, _manifest = create_backup_tarball(config_path, auth_path)
    backup_dir = os.path.join(os.path.dirname(os.path.abspath(config_path)), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    path = os.path.join(
        backup_dir, f"pre-{label}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(path, 0o600)
    return path


def restore_backup(tarball_path, config_path, force=False):
    """Restore hosts.yaml, auth.json, and netwatch.db from a backup tarball
    built by create_backup_tarball().

    Deliberately does NOT extract the tarball's bundled monitor.py: when
    --restore runs after a fresh git clone (the normal redeploy path),
    overwriting freshly-cloned code with whatever version made the backup
    would silently downgrade it. (The bundled monitor.py is a thin
    entrypoint shim as of the netwatch/ package split - it alone can't run
    the app without the rest of the netwatch/ package, which the tarball
    does not include; git clone is the supported way to get runnable code.)

    Returns (ok, message). Never raises for expected failure conditions
    (missing/invalid tarball, conflicting destination files) - callers can
    print the message and exit without a traceback.
    """
    import tarfile

    if not os.path.isfile(tarball_path):
        return False, f"Backup file not found: {tarball_path}"

    try:
        tar = tarfile.open(tarball_path, "r:gz")
    except (tarfile.TarError, OSError) as e:
        return False, f"Could not open backup tarball: {e}"

    with tar:
        try:
            manifest_member = tar.getmember("netwatch/metadata.json")
        except KeyError:
            return False, "Not a valid netwatch backup (missing netwatch/metadata.json)"

        manifest = json.loads(tar.extractfile(manifest_member).read().decode("utf-8"))

        warning = ""
        backup_version = manifest.get("manifest_version", 0)
        if backup_version > BACKUP_MANIFEST_VERSION:
            warning = (
                f"Warning: this backup was made by a newer netwatch version "
                f"(manifest v{backup_version}, this is v{BACKUP_MANIFEST_VERSION}) "
                f"- restore may be incomplete.\n"
            )

        config_dir = os.path.dirname(os.path.abspath(config_path))
        targets = {
            "netwatch/hosts.yaml":  os.path.join(config_dir, "hosts.yaml"),
            "netwatch/auth.json":   os.path.join(config_dir, "auth.json"),
            "netwatch/netwatch.db": os.path.join(config_dir, "netwatch.db"),
        }

        if not force:
            existing = [dest for dest in targets.values() if os.path.exists(dest)]
            if existing:
                listing = "\n".join(f"  - {p}" for p in existing)
                return False, (
                    "Refusing to overwrite existing files (use --force to overwrite):\n"
                    f"{listing}"
                )

        os.makedirs(config_dir, exist_ok=True)
        restored = []
        for arcname, dest in targets.items():
            try:
                member = tar.getmember(arcname)
            except KeyError:
                continue  # e.g. auth.json may be absent if no admin was ever set up
            with tar.extractfile(member) as src, open(dest, "wb") as out:
                out.write(src.read())
            if arcname == "netwatch/auth.json":
                os.chmod(dest, 0o600)
            restored.append(dest)

    files_listing = "\n".join(f"  - {p}" for p in restored)
    message = (
        f"{warning}"
        f"Restored backup from {manifest.get('source_hostname', 'unknown')}, "
        f"created {manifest.get('created_iso', 'unknown')}, "
        f"netwatch v{manifest.get('netwatch_version', 'unknown')}\n"
        f"Files written:\n{files_listing}"
    )
    return True, message

