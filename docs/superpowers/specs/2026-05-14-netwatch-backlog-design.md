# Netwatch Backlog — Design Spec
**Date:** 2026-05-14
**Scope:** Four independent improvements to `monitor.py` and the repository.

---

## 1. Schema Migrations — PRAGMA Introspection

### Problem
Three `ALTER TABLE` statements are guarded by bare `try/except sqlite3.OperationalError` blocks (lines 962, 1254, 1259 of `monitor.py`). This pattern silently swallows any `OperationalError` — including real errors unrelated to "column already exists."

### Design
Add a module-level helper function immediately below the imports:

```python
def _column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)
```

Replace each try/except block with a guarded `ALTER TABLE`:

```python
if not _column_exists(self.conn, "incidents", "alert_sent"):
    self.conn.execute(
        "ALTER TABLE incidents ADD COLUMN alert_sent INTEGER DEFAULT 0"
    )
    logging.info("HistoryDB: added alert_sent column to incidents")
```

Apply the same pattern to the two `inventory` migrations in `InventoryDB.__init__`.

### Invariants
- `PRAGMA table_info` is safe to call on any existing table; returns empty for unknown tables.
- The helper is read-only and takes no lock — safe to call before acquiring `self.lock`.
- Real `OperationalError`s now propagate instead of being silently dropped.

---

## 2. Brute-Force Persistence — SQLite

### Problem
`AuthManager._failed_attempts` is an in-memory dict (line 728). Every service restart resets the lockout window, allowing an attacker to bypass the 5-attempt limit by triggering a restart.

### Design

**Table:** Add to `HistoryDB.SCHEMA` (so it's created when the DB is first initialised):

```sql
CREATE TABLE IF NOT EXISTS login_attempts (
    ip        TEXT    NOT NULL,
    timestamp INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_ip ON login_attempts(ip);
```

**AuthManager changes:**

`__init__` gains an optional `db_path: str = None` parameter. When provided, it opens its own WAL-mode `sqlite3` connection to `netwatch.db` and calls a private `_load_attempts()` method that reads rows within the current lockout window into `_failed_attempts`.

```python
def _open_db(self, db_path):
    import sqlite3
    conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS login_attempts (
            ip TEXT NOT NULL, timestamp INTEGER NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attempts_ip ON login_attempts(ip)"
    )
    return conn

def _load_attempts(self):
    cutoff = time.time() - self.LOCKOUT_MINUTES * 60
    rows = self._db.execute(
        "SELECT ip, timestamp FROM login_attempts WHERE timestamp > ?", (cutoff,)
    ).fetchall()
    for ip, ts in rows:
        self._failed_attempts.setdefault(ip, []).append(ts)
```

**Mutations** (all called inside `self.lock`):

| Method | SQLite action |
|---|---|
| `record_failed_attempt(ip)` | `INSERT INTO login_attempts VALUES (?, ?)` |
| `record_successful_login(ip)` | `DELETE FROM login_attempts WHERE ip = ?` |
| `_prune_old_attempts(ip, now)` | `DELETE FROM login_attempts WHERE ip = ? AND timestamp <= ?` |

**Wiring in `main()`:** Pass `db_path` to `AuthManager`. No init-order change needed — `AuthManager` opens its own connection independently.

```python
auth_manager = AuthManager(auth_path, db_path=db_path)
```

### Invariants
- `AuthManager` uses its own `sqlite3` connection; no lock sharing with `HistoryDB`.
- WAL mode allows concurrent readers — the separate connection is safe.
- On startup, only attempts within the lockout window are loaded (stale rows are ignored and will be pruned on first `_prune_old_attempts` call).

---

## 3. patches/ Cleanup

### Problem
The `patches/` directory contains 46 historical patch scripts from before the project moved to direct commits. They are dead code and add noise to the repository. Three `.bak_*` files also exist in the project root.

### Design
Remove via `git rm`:

```bash
git rm -r patches/
git rm monitor.py.bak_finalpolish monitor.py.bak_security monitor.py.bak_visual
git commit -m "Remove historical patch scripts and .bak files"
```

No code changes to `monitor.py`. The patches directory is not referenced anywhere in the running code.

---

## 4. Netwatch Logo — Inline SVG

### Design
A pulse/waveform mark: a horizontal line that flatlines, spikes upward (representing a successful ping), then flatlines again. Placed to the left of the "NETWATCH" wordmark. Accent colour: `#00bcd4` (matches existing dashboard cyan).

**Placement:**
- Nav bar: replaces the plain "NETWATCH" text in the `<nav>` element in `dashboard.html`.
- Landing page: replaces the `<h1>NETWATCH</h1>` heading in the login page HTML block inside `monitor.py`.

**Implementation:** A single inline `<svg>` snippet, approximately:

```html
<svg class="nw-logo" viewBox="0 0 120 32" ...>
  <!-- pulse waveform path -->
  <polyline points="0,16 30,16 38,4 46,28 54,16 120,16"
            stroke="#00bcd4" stroke-width="2.5" fill="none"
            stroke-linecap="round" stroke-linejoin="round"/>
  <!-- wordmark -->
  <text x="62" y="22" font-family="monospace" font-size="14"
        font-weight="700" fill="#e0e0e0" letter-spacing="2">NETWATCH</text>
</svg>
```

CSS class `.nw-logo` sets `height: 32px; width: auto; vertical-align: middle;`.

The exact SVG path will be tuned during implementation for visual balance. No external image files. No new assets added to the repo.

---

## Out of Scope
- Access logging: current implementation (silenced `log_message`) is acceptable. Security-relevant events are already captured via `logging.info/warning`.
- New patch infrastructure: no new patch mechanism, all changes go directly into `monitor.py` and `dashboard.html`.
