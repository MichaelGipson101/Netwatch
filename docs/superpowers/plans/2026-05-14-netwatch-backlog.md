# Netwatch Backlog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the silent-swallow schema migration bug, persist brute-force attempt data across restarts, remove 46 stale patch files, and add an inline SVG logo to the nav and landing page.

**Architecture:** All Python changes go into `monitor.py` (single-file project). HTML/CSS changes go into `dashboard.html`. No new Python modules. Tests live in `tests/test_netwatch.py`.

**Tech Stack:** Python 3 stdlib (sqlite3, hmac, threading), pytest, inline SVG.

---

## Task 1: Add `_column_exists` helper and fix schema migrations

**Files:**
- Create: `tests/test_netwatch.py`
- Modify: `monitor.py` (insert helper after line 24; replace lines 962–966 and 1252–1262)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_netwatch.py`:

```python
import sqlite3
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from monitor import _column_exists


def test_column_exists_returns_true_for_existing_column():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (a TEXT, b INTEGER)")
    assert _column_exists(conn, "t", "a") is True
    assert _column_exists(conn, "t", "b") is True


def test_column_exists_returns_false_for_missing_column():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (a TEXT)")
    assert _column_exists(conn, "t", "x") is False


def test_column_exists_returns_false_for_unknown_table():
    conn = sqlite3.connect(":memory:")
    assert _column_exists(conn, "nonexistent", "col") is False


def test_real_operationalerror_propagates_on_bad_sql():
    """ALTER TABLE on a non-existent table raises OperationalError — not swallowed."""
    import sqlite3 as _sq
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("ALTER TABLE ghost ADD COLUMN x TEXT")
        assert False, "Should have raised"
    except _sq.OperationalError:
        pass  # expected — real errors propagate
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py -v
```

Expected: `ImportError: cannot import name '_column_exists' from 'monitor'`

- [ ] **Step 3: Add `_column_exists` to monitor.py**

Insert after line 24 (`VERSION = "3.37"`), before the next blank line:

```python
def _column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py -v
```

Expected: 4 tests PASSED.

- [ ] **Step 5: Replace try/except in HistoryDB.__init__ (lines 962–966)**

Current code:
```python
        try:
            self.conn.execute("ALTER TABLE incidents ADD COLUMN alert_sent INTEGER DEFAULT 0")
            logging.info("HistoryDB: added alert_sent column to incidents")
        except sqlite3.OperationalError:
            pass
```

Replace with:
```python
        if not _column_exists(self.conn, "incidents", "alert_sent"):
            self.conn.execute("ALTER TABLE incidents ADD COLUMN alert_sent INTEGER DEFAULT 0")
            logging.info("HistoryDB: added alert_sent column to incidents")
```

- [ ] **Step 6: Replace try/excepts in InventoryDB.__init__ (lines 1252–1262)**

Current code:
```python
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
```

Replace with:
```python
        if not _column_exists(self.conn, "inventory", "device_type"):
            self.conn.execute("ALTER TABLE inventory ADD COLUMN device_type TEXT DEFAULT 'host' NOT NULL")
            logging.info("InventoryDB: added device_type column")
        if not _column_exists(self.conn, "inventory", "properties"):
            self.conn.execute("ALTER TABLE inventory ADD COLUMN properties TEXT")
```

- [ ] **Step 7: Smoke-test the app starts cleanly**

```bash
cd /home/mgipson/netwatch && python monitor.py --no-tui --no-web &
sleep 2 && kill %1
```

Expected: No tracebacks; `monitor.log` shows `HistoryDB: opened` and `InventoryDB: schema ready`.

- [ ] **Step 8: Commit**

```bash
cd /home/mgipson/netwatch
git add monitor.py tests/test_netwatch.py
git commit -m "Replace schema migration try/except with PRAGMA table_info guards"
```

---

## Task 2: Persist brute-force attempts to SQLite

**Files:**
- Modify: `tests/test_netwatch.py` (append new tests)
- Modify: `monitor.py` — `AuthManager.__init__`, add `_open_attempts_db`/`_load_attempts` methods, update `record_failed_attempt`, `record_successful_login`, `_prune_old_attempts`, and `main()`

- [ ] **Step 1: Add failing tests to tests/test_netwatch.py**

Append to the end of `tests/test_netwatch.py`:

```python
import tempfile, time
from monitor import AuthManager


def _make_auth(tmpdir, db_path=None):
    auth_path = os.path.join(tmpdir, "auth.json")
    return AuthManager(auth_path, db_path=db_path)


def test_failed_attempts_persist_across_restart():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        am1 = _make_auth(tmpdir, db_path=db_path)
        for _ in range(AuthManager.LOCKOUT_AFTER):
            am1.record_failed_attempt("1.2.3.4")
        assert am1.is_locked_out("1.2.3.4")

        # New instance, same DB — lockout must survive
        am2 = _make_auth(tmpdir, db_path=db_path)
        assert am2.is_locked_out("1.2.3.4")


def test_successful_login_clears_persisted_attempts():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        am1 = _make_auth(tmpdir, db_path=db_path)
        for _ in range(AuthManager.LOCKOUT_AFTER):
            am1.record_failed_attempt("10.0.0.1")
        am1.record_successful_login("10.0.0.1")

        am2 = _make_auth(tmpdir, db_path=db_path)
        assert not am2.is_locked_out("10.0.0.1")


def test_no_db_path_works_as_before():
    """AuthManager without db_path still works (in-memory only)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        am = _make_auth(tmpdir, db_path=None)
        for _ in range(AuthManager.LOCKOUT_AFTER):
            am.record_failed_attempt("192.168.1.1")
        assert am.is_locked_out("192.168.1.1")
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py::test_failed_attempts_persist_across_restart tests/test_netwatch.py::test_successful_login_clears_persisted_attempts tests/test_netwatch.py::test_no_db_path_works_as_before -v
```

Expected: `TypeError: AuthManager.__init__() got an unexpected keyword argument 'db_path'`

- [ ] **Step 3: Update AuthManager.__init__ signature and body**

Current `__init__` (around line 725):
```python
    def __init__(self, auth_path):
        self.path = auth_path
        self.lock = threading.Lock()
        self._failed_attempts = {}  # ip -> [(timestamp, ...), ...]
        self.data = self._load()
```

Replace with:
```python
    def __init__(self, auth_path, db_path=None):
        self.path = auth_path
        self.lock = threading.Lock()
        self._failed_attempts = {}  # ip -> [timestamp, ...]
        self._attempts_db = self._open_attempts_db(db_path) if db_path else None
        self.data = self._load()
        if self._attempts_db:
            self._load_attempts()
```

- [ ] **Step 4: Add `_open_attempts_db` and `_load_attempts` methods**

Insert immediately after `__init__`, before `_load`:

```python
    def _open_attempts_db(self, db_path):
        import sqlite3
        conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS login_attempts "
            "(ip TEXT NOT NULL, timestamp INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_attempts_ip ON login_attempts(ip)"
        )
        return conn

    def _load_attempts(self):
        cutoff = time.time() - self.LOCKOUT_MINUTES * 60
        rows = self._attempts_db.execute(
            "SELECT ip, timestamp FROM login_attempts WHERE timestamp > ?", (cutoff,)
        ).fetchall()
        for ip, ts in rows:
            self._failed_attempts.setdefault(ip, []).append(ts)
```

- [ ] **Step 5: Update `_prune_old_attempts` to also prune SQLite**

Current (around line 843):
```python
    def _prune_old_attempts(self, ip, now):
        cutoff = now - self.LOCKOUT_MINUTES * 60
        self._failed_attempts[ip] = [
            t for t in self._failed_attempts.get(ip, []) if t > cutoff
        ]
        if not self._failed_attempts[ip]:
            del self._failed_attempts[ip]
```

Replace with:
```python
    def _prune_old_attempts(self, ip, now):
        cutoff = now - self.LOCKOUT_MINUTES * 60
        self._failed_attempts[ip] = [
            t for t in self._failed_attempts.get(ip, []) if t > cutoff
        ]
        if not self._failed_attempts[ip]:
            del self._failed_attempts[ip]
        if self._attempts_db:
            self._attempts_db.execute(
                "DELETE FROM login_attempts WHERE ip = ? AND timestamp <= ?",
                (ip, cutoff),
            )
```

- [ ] **Step 6: Update `record_failed_attempt` to also write to SQLite**

Current (around line 858):
```python
    def record_failed_attempt(self, ip):
        now = time.time()
        with self.lock:
            self._prune_old_attempts(ip, now)
            self._failed_attempts.setdefault(ip, []).append(now)
```

Replace with:
```python
    def record_failed_attempt(self, ip):
        now = time.time()
        with self.lock:
            self._prune_old_attempts(ip, now)
            self._failed_attempts.setdefault(ip, []).append(now)
            if self._attempts_db:
                self._attempts_db.execute(
                    "INSERT INTO login_attempts (ip, timestamp) VALUES (?, ?)",
                    (ip, int(now)),
                )
```

- [ ] **Step 7: Update `record_successful_login` to also clear SQLite**

Current (around line 864):
```python
    def record_successful_login(self, ip):
        with self.lock:
            self._failed_attempts.pop(ip, None)
```

Replace with:
```python
    def record_successful_login(self, ip):
        with self.lock:
            self._failed_attempts.pop(ip, None)
            if self._attempts_db:
                self._attempts_db.execute(
                    "DELETE FROM login_attempts WHERE ip = ?", (ip,)
                )
```

- [ ] **Step 8: Wire `db_path` into `AuthManager` in `main()`**

Current block (lines 3653–3665):
```python
    # Authentication
    auth_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "auth.json")
    auth_manager = AuthManager(auth_path)
    if auth_manager.has_users:
        print(f"[netwatch] Auth enabled - {len(auth_manager.list_users())} user(s) configured")
    else:
        print(f"[netwatch] Auth NOT YET CONFIGURED - visit the dashboard to set up the first admin user")

    # SQLite-backed history (persists pings & incidents across restarts)
    db_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "netwatch.db")
    retention_days = int(settings.get("history_days", 30))
    history_db = HistoryDB(db_path, retention_days=retention_days)
    print(f"[netwatch] History DB -> {db_path} (retention {retention_days} days)")
```

Replace with:
```python
    # Authentication
    config_dir = os.path.dirname(os.path.abspath(config_path))
    auth_path = os.path.join(config_dir, "auth.json")
    db_path = os.path.join(config_dir, "netwatch.db")
    auth_manager = AuthManager(auth_path, db_path=db_path)
    if auth_manager.has_users:
        print(f"[netwatch] Auth enabled - {len(auth_manager.list_users())} user(s) configured")
    else:
        print(f"[netwatch] Auth NOT YET CONFIGURED - visit the dashboard to set up the first admin user")

    # SQLite-backed history (persists pings & incidents across restarts)
    retention_days = int(settings.get("history_days", 30))
    history_db = HistoryDB(db_path, retention_days=retention_days)
    print(f"[netwatch] History DB -> {db_path} (retention {retention_days} days)")
```

- [ ] **Step 9: Run all tests**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py -v
```

Expected: All tests PASSED (7 total).

- [ ] **Step 10: Smoke-test the app starts cleanly**

```bash
cd /home/mgipson/netwatch && python monitor.py --no-tui --no-web &
sleep 2 && kill %1
```

Expected: No tracebacks. `monitor.log` shows normal startup messages.

- [ ] **Step 11: Commit**

```bash
cd /home/mgipson/netwatch
git add monitor.py tests/test_netwatch.py
git commit -m "Persist brute-force login attempts to SQLite across restarts"
```

---

## Task 3: Remove patches/ directory and .bak files

**Files:**
- Delete: `patches/` (46 files, all git-tracked)
- Note: `monitor.py.bak_*` files exist on disk but are NOT git-tracked — leave them (or `rm` manually if desired, but no `git rm` needed)

- [ ] **Step 1: Remove patches/ from git**

```bash
cd /home/mgipson/netwatch && git rm -r patches/
```

Expected: `rm 'patches/patch_always_on.py'` … (46 lines)

- [ ] **Step 2: Commit**

```bash
cd /home/mgipson/netwatch
git commit -m "Remove historical patch scripts (development now uses direct commits)"
```

---

## Task 4: Add Netwatch logo SVG

The logo is a pulse/waveform mark (ping line: flatline → spike → flatline) beside the "NETWATCH" wordmark. Implemented as inline SVG in two places in `dashboard.html`: the nav bar (line 1089) and the landing page brand block (lines 1050–1052).

**Files:**
- Modify: `dashboard.html` (CSS additions + two HTML replacements)

- [ ] **Step 1: Add `.nw-logo` CSS**

Find the existing `.logo` CSS rule (line 54):
```css
.logo{font-family:'DM Mono',monospace;font-size:16px;font-weight:500;letter-spacing:.06em}
.logo span{color:var(--green)}
```

Append `.nw-logo` immediately after `.logo span{...}`:
```css
.nw-logo{height:26px;width:auto;vertical-align:middle}
```

- [ ] **Step 2: Replace the nav logo (line 1089)**

Current:
```html
    <div class="logo">net<span>watch</span></div>
```

Replace with:
```html
    <div class="logo">
      <svg class="nw-logo" viewBox="0 0 168 32" xmlns="http://www.w3.org/2000/svg" aria-label="Netwatch">
        <polyline points="0,16 22,16 30,5 38,27 46,16 68,16" stroke="#00bcd4" stroke-width="2.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
        <text x="76" y="22" font-family="'DM Mono','Courier New',monospace" font-size="14" font-weight="500" fill="currentColor" letter-spacing="2">NETWATCH</text>
      </svg>
    </div>
```

- [ ] **Step 3: Add `.landing-nw-logo` CSS for the larger landing page variant**

Find the `.landing-brand-name` rule (line 1036):
```css
.landing-brand-name{font-family:'DM Mono',monospace;font-size:20px;font-weight:500;letter-spacing:.12em;color:var(--text);}
```

Append immediately after:
```css
.landing-nw-logo{height:40px;width:auto}
```

- [ ] **Step 4: Replace the landing page brand block (lines 1050–1052)**

Current:
```html
    <div class="landing-brand">
      <div class="landing-brand-name">NETWATCH</div>
      <div class="landing-brand-sub">homelab monitor</div>
    </div>
```

Replace with:
```html
    <div class="landing-brand">
      <svg class="landing-nw-logo" viewBox="0 0 168 32" xmlns="http://www.w3.org/2000/svg" aria-label="Netwatch">
        <polyline points="0,16 22,16 30,5 38,27 46,16 68,16" stroke="#00bcd4" stroke-width="2.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
        <text x="76" y="22" font-family="'DM Mono','Courier New',monospace" font-size="14" font-weight="500" fill="currentColor" letter-spacing="2">NETWATCH</text>
      </svg>
      <div class="landing-brand-sub">homelab monitor</div>
    </div>
```

- [ ] **Step 5: Visual test — start the server and check both locations**

```bash
cd /home/mgipson/netwatch && python monitor.py --no-tui &
```

Open `http://192.168.6.90:8080` in a browser. Verify:
- Landing page shows the waveform + NETWATCH logo (not plain text)
- After login, the nav shows the waveform + NETWATCH logo
- Logo renders correctly in both light and dark mode (toggle the theme switcher)
- Logo is legible at nav size; landing page version is comfortably larger

If the waveform feels off-balance visually, adjust the `points` attribute: `30,5` controls the spike peak (lower = taller spike) and `38,27` controls the trough.

Kill the server when done:
```bash
kill %1
```

- [ ] **Step 6: Commit**

```bash
cd /home/mgipson/netwatch
git add dashboard.html
git commit -m "Add inline SVG pulse/waveform logo to nav and landing page"
```
