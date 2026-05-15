#!/usr/bin/env python3
"""
netwatch patch: SQLite-backed history.

Replaces the volatile in-memory state with a SQLite database so that:
  - Uptime % survives restarts (rolling window restored from DB)
  - Latency sparkline survives restarts
  - Incident log survives restarts (Events tab keeps history across reboots)
  - Foundation laid for future features (charts, weekly stats, etc.)

Database file: ~/netwatch/netwatch.db (created on first run)
Retention:     30 days (configurable via 'history_days' setting)
Schema:        2 tables (pings, incidents) plus indexes

Behaviour:
  - In-memory deque & IncidentLog still exist (for fast UI reads)
  - On every ping, the result is also written to the DB
  - On startup, the in-memory state is restored from the DB
  - A daily background task prunes data older than retention
  - WAL mode enables concurrent reads/writes without locking issues

Must be applied AFTER patch_polish_drawer.py.

Run once from ~/netwatch/:
    python3 patch_sqlite_history.py
    sudo systemctl restart netwatch

Backup saved to monitor.py.bak_sqlite.
Idempotent - safe to re-run.
"""

import os
import shutil
import sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_sqlite"
SENTINEL = "class HistoryDB"  # presence means already patched


# ─── New module-level class: HistoryDB ────────────────────────────────────────

NEW_HISTORY_DB_CLASS = '''# ============================================================================
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
    """

    def __init__(self, db_path, retention_days=30):
        import sqlite3
        self.db_path = db_path
        self.retention_days = retention_days
        self.lock = threading.Lock()
        # check_same_thread=False because we share the connection across threads,
        # and we serialize writes with self.lock.
        self.conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(self.SCHEMA)
        logging.info(f"HistoryDB: opened {db_path} (retention {retention_days} days)")

    def close(self):
        with self.lock:
            try:
                self.conn.close()
            except Exception:
                pass

    # ── Pings ───────────────────────────────────────────────────────────────

    def record_ping(self, host_ip, is_up, latency_ms):
        ts = int(time.time())
        with self.lock:
            self.conn.execute(
                "INSERT INTO pings (host_ip, timestamp, is_up, latency_ms) VALUES (?, ?, ?, ?)",
                (host_ip, ts, 1 if is_up else 0, latency_ms),
            )

    def recent_pings(self, host_ip, limit=100):
        """Return up to `limit` most-recent pings for a host, oldest first.
        Used to repopulate the in-memory history deque on startup."""
        with self.lock:
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
            cur = self.conn.execute(
                "SELECT is_up, latency_ms, timestamp FROM pings "
                "WHERE host_ip = ? ORDER BY timestamp DESC LIMIT 1",
                (host_ip,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return (bool(row[0]), row[1], row[2])

    # ── Incidents ───────────────────────────────────────────────────────────

    def open_incident(self, host_ip, host_name, host_group):
        """Open a new incident if there's no ongoing one for this host."""
        ts = int(time.time())
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
            result.append({
                "host_ip":          host_ip,
                "host_name":        host_name,
                "host_group":       host_group,
                "started_str":      _dt.fromtimestamp(started).strftime("%H:%M:%S"),
                "started_iso":      _dt.fromtimestamp(started).isoformat(),
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
            self.conn.execute("VACUUM")
        if pings_deleted or incidents_deleted:
            logging.info(
                f"HistoryDB: pruned {pings_deleted} pings, "
                f"{incidents_deleted} incidents older than {self.retention_days}d"
            )
        return pings_deleted, incidents_deleted


def _prune_loop(history_db, stop_event):
    """Run prune() once a day until stop_event is set."""
    SECONDS_PER_DAY = 86400
    # Run first prune ~60s after startup so the system isn't busy at boot
    elapsed = SECONDS_PER_DAY - 60
    while not stop_event.is_set():
        if elapsed >= SECONDS_PER_DAY:
            try:
                history_db.prune()
            except Exception as e:
                logging.warning(f"HistoryDB prune failed: {e}")
            elapsed = 0
        time.sleep(5)
        elapsed += 5


'''


def main():
    if not os.path.isfile(TARGET):
        print(f"ERROR: {TARGET} not found.")
        sys.exit(1)

    content = open(TARGET).read()

    if SENTINEL in content:
        print(f"NOTE: '{SENTINEL}' found - patch already applied.")
        sys.exit(0)

    if "updatePiHealth" not in content:
        print("ERROR: This patch requires patch_polish_drawer first.")
        sys.exit(1)

    shutil.copy2(TARGET, BACKUP)
    print(f"[OK] Backed up {TARGET} -> {BACKUP}")

    # ── 1. Insert HistoryDB class right before "# Wake-on-LAN" section anchor.
    OLD = '''# ============================================================================
# Wake-on-LAN
# ============================================================================'''
    if content.count(OLD) != 1:
        print(f"[FAIL] WoL anchor not unique: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW_HISTORY_DB_CLASS + OLD, 1)
    print("[OK] Inserted HistoryDB class")

    # ── 2. IncidentLog: change record_down/record_up to forward to HistoryDB,
    # and keep the in-memory cache for fast reads. We also let it accept an
    # optional history_db reference set at construction time.
    OLD = '''class IncidentLog:
    """Tracks down/up incidents. Thread-safe, in-memory, capped at MAX entries."""

    MAX = 100

    def __init__(self):
        self._open = {}        # ip -> open incident dict (no end_time yet)
        self._closed = []      # list of closed incidents (most recent last)
        self.lock = threading.Lock()

    def record_down(self, host):
        """Open a new incident for host if there isn't one already."""
        with self.lock:
            if host.ip in self._open:
                return  # already tracking
            self._open[host.ip] = {
                "host_name":  host.name,
                "host_ip":    host.ip,
                "host_group": host.group,
                "started":    datetime.now(),
                "ended":      None,
            }

    def record_up(self, host):
        """Close any open incident for this host."""
        with self.lock:
            inc = self._open.pop(host.ip, None)
            if inc is None:
                return
            inc["ended"] = datetime.now()
            self._closed.append(inc)
            # Cap memory: keep only the most recent MAX incidents
            if len(self._closed) > self.MAX:
                self._closed = self._closed[-self.MAX:]

    def list_incidents(self):
        """Returns most-recent-first list of dicts suitable for JSON."""
        with self.lock:
            now = datetime.now()
            ongoing = []
            for inc in self._open.values():
                duration = int((now - inc["started"]).total_seconds())
                ongoing.append({
                    "host_name":        inc["host_name"],
                    "host_ip":          inc["host_ip"],
                    "host_group":       inc["host_group"],
                    "started_str":      inc["started"].strftime("%H:%M:%S"),
                    "started_iso":      inc["started"].isoformat(),
                    "duration_seconds": duration,
                    "ongoing":          True,
                })
            resolved = []
            for inc in self._closed:
                duration = int((inc["ended"] - inc["started"]).total_seconds())
                resolved.append({
                    "host_name":        inc["host_name"],
                    "host_ip":          inc["host_ip"],
                    "host_group":       inc["host_group"],
                    "started_str":      inc["started"].strftime("%H:%M:%S"),
                    "started_iso":      inc["started"].isoformat(),
                    "ended_iso":        inc["ended"].isoformat(),
                    "duration_seconds": duration,
                    "ongoing":          False,
                })
        # Ongoing first, then resolved most-recent-first
        return ongoing + list(reversed(resolved))'''

    NEW = '''class IncidentLog:
    """Records down/up incidents to the HistoryDB. Thread-safe.

    With SQLite-backed history, this becomes a thin wrapper around HistoryDB.
    The previous in-memory implementation was capped at 100 incidents and
    reset on restart - the DB-backed version preserves history forever
    (subject to the retention_days setting)."""

    def __init__(self, history_db=None):
        self.history_db = history_db
        self.lock = threading.Lock()

    def record_down(self, host):
        if self.history_db:
            self.history_db.open_incident(host.ip, host.name, host.group)

    def record_up(self, host):
        if self.history_db:
            self.history_db.close_incident(host.ip)

    def update_host_info(self, host):
        """Called when a host is renamed/regrouped so existing open incidents
        keep matching the host's current display values."""
        if self.history_db:
            self.history_db.update_incident_host_info(host.ip, host.name, host.group)

    def list_incidents(self):
        if not self.history_db:
            return []
        return self.history_db.list_incidents(limit=100)'''

    if content.count(OLD) != 1:
        print(f"[FAIL] IncidentLog match: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Replaced IncidentLog with DB-backed version")

    # ── 3. poll_host: also write each ping to the HistoryDB
    OLD = '''def poll_host(host, timeout, global_stop, incident_log=None):
    while not global_stop.is_set() and not host.stop_event.is_set():
        was_up = host.is_up if host.history else None
        is_up, latency = ping_host(host.ip, timeout)
        with host.lock:
            host.history.append(is_up)
            host.last_latency_ms = latency if is_up else None
            host.last_checked = datetime.now()
            if is_up:
                host.last_seen_up = datetime.now()
                host.consecutive_down = 0
            else:
                host.consecutive_down += 1

        # Track incidents (only for always_on hosts)
        if incident_log is not None and host.always_on:'''

    NEW = '''def poll_host(host, timeout, global_stop, incident_log=None, history_db=None):
    while not global_stop.is_set() and not host.stop_event.is_set():
        was_up = host.is_up if host.history else None
        is_up, latency = ping_host(host.ip, timeout)
        with host.lock:
            host.history.append(is_up)
            host.last_latency_ms = latency if is_up else None
            host.last_checked = datetime.now()
            if is_up:
                host.last_seen_up = datetime.now()
                host.consecutive_down = 0
            else:
                host.consecutive_down += 1

        # Persist to DB if available
        if history_db is not None:
            try:
                history_db.record_ping(host.ip, is_up, latency)
            except Exception as e:
                logging.warning(f"HistoryDB record_ping failed: {e}")

        # Track incidents (only for always_on hosts)
        if incident_log is not None and host.always_on:'''

    if content.count(OLD) != 1:
        print(f"[FAIL] poll_host match: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] poll_host now writes to HistoryDB")

    # ── 4. HostManager: hold history_db reference, pass to threads
    OLD = '''class HostManager:
    def __init__(self, config_path, ping_timeout, history_window, global_stop, incident_log=None):
        self.config_path = config_path
        self.ping_timeout = ping_timeout
        self.history_window = history_window
        self.global_stop = global_stop
        self.incident_log = incident_log
        self.hosts = []
        self.lock = threading.Lock()'''

    NEW = '''class HostManager:
    def __init__(self, config_path, ping_timeout, history_window, global_stop, incident_log=None, history_db=None):
        self.config_path = config_path
        self.ping_timeout = ping_timeout
        self.history_window = history_window
        self.global_stop = global_stop
        self.incident_log = incident_log
        self.history_db = history_db
        self.hosts = []
        self.lock = threading.Lock()'''

    if content.count(OLD) != 1:
        print(f"[FAIL] HostManager init match: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] HostManager now holds history_db reference")

    # ── 5. _spawn: thread history_db into the poll thread + restore deque from DB
    OLD = '''    def _spawn(self, name, ip, group, interval, always_on=True, specs=None, notes="", links=None):
        host = HostState(
            name=name, ip=ip, group=group, interval=interval,
            always_on=always_on,
            specs=specs or {},
            notes=notes or "",
            links=links or {},
            history=deque(maxlen=self.history_window),
            stop_event=threading.Event(),
        )
        host.thread = threading.Thread(
            target=poll_host,
            args=(host, self.ping_timeout, self.global_stop, self.incident_log),
            daemon=True, name=f"ping-{name}",
        )
        host.thread.start()
        return host'''

    NEW = '''    def _spawn(self, name, ip, group, interval, always_on=True, specs=None, notes="", links=None):
        history_window = self.history_window
        host = HostState(
            name=name, ip=ip, group=group, interval=interval,
            always_on=always_on,
            specs=specs or {},
            notes=notes or "",
            links=links or {},
            history=deque(maxlen=history_window),
            stop_event=threading.Event(),
        )
        # Restore history from DB if available
        if self.history_db:
            try:
                recent = self.history_db.recent_pings(ip, limit=history_window)
                for is_up, latency_ms in recent:
                    host.history.append(is_up)
                # Also restore last_latency_ms / last_seen_up from latest ping
                latest = self.history_db.latest_ping(ip)
                if latest is not None:
                    last_up, last_lat, last_ts = latest
                    if last_up:
                        host.last_seen_up = datetime.fromtimestamp(last_ts)
                        host.last_latency_ms = last_lat
            except Exception as e:
                logging.warning(f"HistoryDB restore failed for {ip}: {e}")
        host.thread = threading.Thread(
            target=poll_host,
            args=(host, self.ping_timeout, self.global_stop, self.incident_log, self.history_db),
            daemon=True, name=f"ping-{name}",
        )
        host.thread.start()
        return host'''

    if content.count(OLD) != 1:
        print(f"[FAIL] _spawn match: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] _spawn restores history from DB on creation")

    # ── 6. reload_from_config: when an existing host gets renamed/regrouped,
    # update existing open incidents in the DB
    OLD = '''                links = h.get("links") if isinstance(h.get("links"), dict) else {}
                if ip in current_by_ip:
                    existing = current_by_ip[ip]
                    existing.name = name
                    existing.group = group
                    existing.interval = interval
                    existing.always_on = always_on
                    existing.specs = specs
                    existing.notes = notes
                    existing.links = links
                    rebuilt.append(existing)
                else:
                    rebuilt.append(self._spawn(name, ip, group, interval, always_on, specs, notes, links))'''

    NEW = '''                links = h.get("links") if isinstance(h.get("links"), dict) else {}
                if ip in current_by_ip:
                    existing = current_by_ip[ip]
                    name_changed = existing.name != name or existing.group != group
                    existing.name = name
                    existing.group = group
                    existing.interval = interval
                    existing.always_on = always_on
                    existing.specs = specs
                    existing.notes = notes
                    existing.links = links
                    if name_changed and self.incident_log:
                        self.incident_log.update_host_info(existing)
                    rebuilt.append(existing)
                else:
                    rebuilt.append(self._spawn(name, ip, group, interval, always_on, specs, notes, links))'''

    if content.count(OLD) != 1:
        print(f"[FAIL] reload_from_config match: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] reload_from_config updates open incidents on rename")

    # ── 7. main(): create HistoryDB, prune thread, pass to HostManager + IncidentLog
    OLD = '''    stop_event = threading.Event()
    incident_log = IncidentLog()
    host_manager = HostManager(config_path, ping_timeout, history_window, stop_event, incident_log)
    host_manager.load_initial(config.get("hosts", []), default_interval)'''

    NEW = '''    stop_event = threading.Event()

    # SQLite-backed history (persists pings & incidents across restarts)
    db_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "netwatch.db")
    retention_days = int(settings.get("history_days", 30))
    history_db = HistoryDB(db_path, retention_days=retention_days)
    print(f"[netwatch] History DB -> {db_path} (retention {retention_days} days)")

    # Daily prune task
    pt = threading.Thread(target=_prune_loop, args=(history_db, stop_event), daemon=True, name="prune")
    pt.start()

    incident_log = IncidentLog(history_db=history_db)
    host_manager = HostManager(config_path, ping_timeout, history_window, stop_event, incident_log, history_db)
    host_manager.load_initial(config.get("hosts", []), default_interval)'''

    if content.count(OLD) != 1:
        print(f"[FAIL] main() init match: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] main() creates HistoryDB and prune thread")

    # ── 8. Bump version
    if 'VERSION = "3.5"' in content:
        content = content.replace('VERSION = "3.5"', 'VERSION = "3.6"', 1)
    content = content.replace('netwatch v3.5 - raspberry pi', 'netwatch v3.6 - raspberry pi', 1)

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
    print("  2. The first restart creates ~/netwatch/netwatch.db automatically.")
    print("  3. Verify it's working:")
    print("       ls -l ~/netwatch/netwatch.db")
    print("       sqlite3 ~/netwatch/netwatch.db 'SELECT COUNT(*) FROM pings;'")
    print("  4. Restart again - your uptime % and incidents should now persist!")
    print()
    print("Optional: change retention by adding to hosts.yaml under settings:")
    print("    history_days: 7      # or 60, or whatever you prefer")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")
    print("(Note: rolling back leaves netwatch.db on disk - safe to delete if unwanted)")


if __name__ == "__main__":
    main()
