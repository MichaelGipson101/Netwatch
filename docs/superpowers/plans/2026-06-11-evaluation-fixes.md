# Evaluation Fixes & Latency History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the operational issues (log rotation, WAL bloat, SD write amplification), security findings (session invalidation, settings leak, file perms), and doc drift from the 2026-06-11 evaluation — then ship the latency-history feature (bucketed `/api/history` endpoint, daily uptime rollups, drawer chart + 60-day uptime strip).

**Architecture:** All backend changes live in `monitor.py` (single-file app, module-level handler functions pattern). Frontend changes go in `static/core.js` + `static/main.css` + `dashboard.html`. New SQLite table `ping_daily` holds daily rollups; raw pings keep 7-day retention. Tests follow the existing plain-function style in `tests/test_netwatch.py`.

**Tech Stack:** Python 3.13 stdlib (http.server, sqlite3, logging.handlers), pytest, vanilla JS + hand-built SVG (no chart lib — D3 is not loaded globally).

**Deploy note:** The service is LIVE on this Pi (systemd or manual — Task 13 checks). Code edits don't affect the running process until restart; one restart at the end applies everything.

---

### Task 1: Git hygiene

**Files:**
- Modify: `.gitignore`
- Stage: `docs/superpowers/plans/2026-05-14-device-icons.md`, `docs/superpowers/plans/2026-05-15-topology-icons-replace-shapes.md` (untracked), deletion of `docs/superpowers/specs/2026-05-19-passmark-cpu-score-design.md`

- [ ] **Step 1: Add monitor.log to .gitignore** — append under the "Runtime data" section:

```
# Rotating app log (monitor.log, monitor.log.1, ...)
monitor.log*
```

- [ ] **Step 2: Stage and commit**

```bash
git add .gitignore docs/superpowers/plans/ && git add -A docs/superpowers/specs/
git commit -m "chore: ignore monitor.log, commit pending plan docs, drop stale passmark spec"
```

Expected: `git status` no longer shows `?? monitor.log`, plan docs committed, spec deletion committed.

---

### Task 2: Log rotation + transition-only INFO logging

**Files:**
- Modify: `monitor.py:3668-3671` (logging setup), `monitor.py:343-346` (per-ping log)
- Test: `tests/test_netwatch.py`

- [ ] **Step 1: Write failing tests** for the transition decision helper:

```python
from monitor import _should_log_transition

def test_should_log_transition_first_ping():
    assert _should_log_transition(None, True) is True
    assert _should_log_transition(None, False) is True

def test_should_log_transition_state_change():
    assert _should_log_transition(True, False) is True
    assert _should_log_transition(False, True) is True

def test_should_log_transition_steady_state():
    assert _should_log_transition(True, True) is False
    assert _should_log_transition(False, False) is False
```

- [ ] **Step 2: Run** `pytest tests/test_netwatch.py -k should_log -v` — expect FAIL (ImportError).

- [ ] **Step 3: Implement.** Add above `poll_host` in monitor.py:

```python
def _should_log_transition(was_up, is_up):
    """INFO-log only state changes (and the first ping). Steady-state pings
    go to DEBUG so monitor.log doesn't grow ~12MB/day."""
    return was_up is None or bool(is_up) != bool(was_up)
```

Replace the unconditional `logging.info(...)` at monitor.py:343-346 with:

```python
        line = (
            f"{'UP  ' if is_up else 'DOWN'} | {host.name:<20} | {host.ip:<16} | "
            f"{f'{latency:.1f}ms' if latency else '-':>8}"
        )
        if _should_log_transition(was_up, is_up):
            logging.info(line)
        else:
            logging.debug(line)
```

Replace `logging.basicConfig(...)` at monitor.py:3668-3671 with:

```python
    from logging.handlers import RotatingFileHandler
    _log_handler = RotatingFileHandler(args.log, maxBytes=10 * 1024 * 1024, backupCount=3)
    _log_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logging.basicConfig(level=logging.INFO, handlers=[_log_handler])
```

- [ ] **Step 4: Run** `pytest tests/test_netwatch.py -v` — expect ALL PASS.
- [ ] **Step 5: Commit** `git commit -m "fix: rotate monitor.log (10MB x3), log only status transitions at INFO"`

---

### Task 3: Cap WAL size, drop daily VACUUM

**Files:**
- Modify: `monitor.py:1016-1019` (HistoryDB.__init__ pragmas), `monitor.py:1182-1200` (prune)
- Test: `tests/test_netwatch.py`

- [ ] **Step 1: Write failing tests:**

```python
def test_journal_size_limit_pragma_set(tmp_path):
    from monitor import HistoryDB
    hdb = HistoryDB(str(tmp_path / "t.db"))
    assert hdb.conn.execute("PRAGMA journal_size_limit").fetchone()[0] == 16777216
    hdb.close()

def test_prune_deletes_old_keeps_recent_no_vacuum(tmp_path):
    from monitor import HistoryDB
    hdb = HistoryDB(str(tmp_path / "t.db"), retention_days=7)
    now = int(time.time())
    hdb.conn.execute("INSERT INTO pings (host_ip, timestamp, is_up, latency_ms) VALUES (?,?,?,?)",
                     ("10.0.0.1", now - 8 * 86400, 1, 1.0))
    hdb.conn.execute("INSERT INTO pings (host_ip, timestamp, is_up, latency_ms) VALUES (?,?,?,?)",
                     ("10.0.0.1", now, 1, 1.0))
    deleted, _ = hdb.prune()
    assert deleted == 1
    assert hdb.conn.execute("SELECT COUNT(*) FROM pings").fetchone()[0] == 1
    hdb.close()
```

- [ ] **Step 2: Run** `pytest -k "journal_size or no_vacuum" -v` — expect FAIL (pragma returns -1 default).

- [ ] **Step 3: Implement.** In `HistoryDB.__init__` after the `synchronous` pragma add:

```python
        # Cap the WAL file: without this SQLite never shrinks the -wal file
        # below its high-water mark (it hit 226MB via daily VACUUM).
        self.conn.execute("PRAGMA journal_size_limit=16777216")  # 16MB
```

In `prune()`: delete the `self.conn.execute("VACUUM")` line and add after the two DELETEs:

```python
            # No VACUUM: with fixed retention the DB is steady-state and
            # freed pages get reused. Daily VACUUM rewrote the whole DB
            # through the WAL (~300MB/day of SD writes) for nothing.
            # Manual reclaim if ever needed: sqlite3 netwatch.db VACUUM.
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
```

- [ ] **Step 4: Run** `pytest tests/test_netwatch.py -v` — expect ALL PASS.
- [ ] **Step 5: Commit** `git commit -m "fix: cap WAL at 16MB, drop daily VACUUM from prune"`

---

### Task 4: Batched ping inserts + clean shutdown

**Files:**
- Modify: `monitor.py` — HistoryDB (`record_ping`, new `flush_pings`/`_flush_pings_locked`, `recent_pings`, `latest_ping`, `close`), new `_flush_loop`, `main()` (flush thread, SIGTERM handler, `history_db.close()` in finally)
- Test: `tests/test_netwatch.py`

- [ ] **Step 1: Write failing tests:**

```python
def test_record_ping_buffers_until_flush(tmp_path):
    from monitor import HistoryDB
    db_path = str(tmp_path / "t.db")
    hdb = HistoryDB(db_path)
    hdb.record_ping("10.0.0.1", True, 5.0)
    other = sqlite3.connect(db_path)
    assert other.execute("SELECT COUNT(*) FROM pings").fetchone()[0] == 0
    hdb.flush_pings()
    assert other.execute("SELECT COUNT(*) FROM pings").fetchone()[0] == 1
    other.close(); hdb.close()

def test_buffer_threshold_autoflush(tmp_path):
    from monitor import HistoryDB
    db_path = str(tmp_path / "t.db")
    hdb = HistoryDB(db_path)
    hdb.FLUSH_MAX = 3
    for _ in range(3):
        hdb.record_ping("10.0.0.1", True, 1.0)
    other = sqlite3.connect(db_path)
    assert other.execute("SELECT COUNT(*) FROM pings").fetchone()[0] == 3
    other.close(); hdb.close()

def test_recent_pings_sees_buffered_rows(tmp_path):
    from monitor import HistoryDB
    hdb = HistoryDB(str(tmp_path / "t.db"))
    hdb.record_ping("10.0.0.1", True, 5.0)
    hdb.record_ping("10.0.0.1", False, None)
    assert hdb.recent_pings("10.0.0.1", limit=10) == [(True, 5.0), (False, None)]
    hdb.close()

def test_close_flushes_buffer(tmp_path):
    from monitor import HistoryDB
    db_path = str(tmp_path / "t.db")
    hdb = HistoryDB(db_path)
    hdb.record_ping("10.0.0.1", True, 5.0)
    hdb.close()
    other = sqlite3.connect(db_path)
    assert other.execute("SELECT COUNT(*) FROM pings").fetchone()[0] == 1
    other.close()
```

- [ ] **Step 2: Run** `pytest -k "buffer or close_flushes or recent_pings_sees" -v` — expect FAIL.

- [ ] **Step 3: Implement.** In `HistoryDB.__init__` add `self._ping_buffer = []` and class attr `FLUSH_MAX = 200`. Replace `record_ping` and add flush methods:

```python
    FLUSH_MAX = 200          # safety flush if the 30s flusher falls behind
    BUFFER_HARD_CAP = 5000   # drop oldest beyond this if SQLite is wedged

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
```

`recent_pings` and `latest_ping`: add `self._flush_pings_locked()` as the first line inside their `with self.lock:` blocks. `close()` becomes:

```python
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
```

Add next to `_prune_loop`:

```python
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
```

In `main()` after the prune thread:

```python
    ft = threading.Thread(target=_flush_loop, args=(history_db, stop_event), daemon=True, name="ping-flush")
    ft.start()
```

In `main()` before the try block (so systemd stop runs the KeyboardInterrupt path and flushes):

```python
    import signal
    def _sigterm(_sig, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _sigterm)
```

And in `main()`'s `finally:` add `history_db.close()` after `auth_manager.close()`.

- [ ] **Step 4: Run** `pytest tests/test_netwatch.py -v` — expect ALL PASS (existing HistoryDB tests must still pass; they call methods that now flush first).
- [ ] **Step 5: Commit** `git commit -m "fix: batch ping inserts (30s flush) to cut SD write amplification, clean SIGTERM shutdown"`

---

### Task 5: Session invalidation (deleted users, password change)

**Files:**
- Modify: `monitor.py:928-954` (`make_session_cookie`, `verify_session_cookie`), `monitor.py:849-861` (`change_password`)
- Test: `tests/test_netwatch.py`

- [ ] **Step 1: Write failing tests** (uses existing `_make_auth` helper):

```python
def test_deleted_user_cookie_rejected():
    with tempfile.TemporaryDirectory() as td:
        auth = _make_auth(td)
        auth.create_user("alice", "password123", admin=True)
        auth.create_user("bob", "password123")
        cookie = auth.make_session_cookie("bob")
        assert auth.verify_session_cookie(cookie)[0] == "bob"
        auth.delete_user("bob")
        assert auth.verify_session_cookie(cookie) == (None, False)

def test_password_change_invalidates_old_sessions():
    with tempfile.TemporaryDirectory() as td:
        auth = _make_auth(td)
        auth.create_user("alice", "password123", admin=True)
        old_cookie = auth.make_session_cookie("alice")
        assert auth.verify_session_cookie(old_cookie)[0] == "alice"
        auth.change_password("alice", "newpassword456")
        assert auth.verify_session_cookie(old_cookie) == (None, False)
        new_cookie = auth.make_session_cookie("alice")
        assert auth.verify_session_cookie(new_cookie) == ("alice", True)

def test_legacy_two_part_cookie_rejected():
    import hmac as _hmac, hashlib as _hashlib, base64 as _b64
    with tempfile.TemporaryDirectory() as td:
        auth = _make_auth(td)
        auth.create_user("alice", "password123", admin=True)
        payload = f"alice|{int(time.time()) + 3600}"  # old format: no generation
        secret = auth.data["secret_key"].encode()
        sig = _hmac.new(secret, payload.encode(), _hashlib.sha256).hexdigest()
        token = _b64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        assert auth.verify_session_cookie(f"{token}.{sig}") == (None, False)
```

- [ ] **Step 2: Run** `pytest -k "cookie_rejected or invalidates_old" -v` — expect FAIL.

- [ ] **Step 3: Implement.** Replace `make_session_cookie` / `verify_session_cookie`:

```python
    def make_session_cookie(self, username):
        expiry = int(time.time()) + self.SESSION_DAYS * 86400
        with self.lock:
            user = self.data["users"].get(username, {})
            gen = int(user.get("session_gen", 0))
        payload = f"{username}|{expiry}|{gen}"
        secret = self.data["secret_key"].encode()
        sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
        token = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        return f"{token}.{sig}"

    def verify_session_cookie(self, cookie_value):
        """Return (username, admin_bool) if valid, or (None, False).

        The payload carries a per-user session generation; bumping it on
        password change (or deleting the user) invalidates old cookies."""
        if not cookie_value or "." not in cookie_value:
            return None, False
        try:
            token, sig = cookie_value.rsplit(".", 1)
            padded = token + "=" * (-len(token) % 4)
            payload = base64.urlsafe_b64decode(padded.encode()).decode()
            username, expiry_s, gen_s = payload.split("|")
            expiry = int(expiry_s)
            gen = int(gen_s)
        except (ValueError, UnicodeDecodeError, base64.binascii.Error):
            return None, False
        if expiry < time.time():
            return None, False
        secret = self.data["secret_key"].encode()
        expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None, False
        with self.lock:
            user = self.data["users"].get(username)
            if not user or int(user.get("session_gen", 0)) != gen:
                return None, False
            return username, bool(user.get("admin"))
```

In `change_password`, inside the lock right before `self._save()`:

```python
            user["session_gen"] = int(user.get("session_gen", 0)) + 1
```

Note: deploying this logs everyone out once (old 2-part cookies become invalid). That's intended.

- [ ] **Step 4: Run** `pytest tests/test_netwatch.py -v` — expect ALL PASS.
- [ ] **Step 5: Commit** `git commit -m "fix: invalidate sessions for deleted users and on password change"`

---

### Task 6: Scrub secrets from /api/status settings

**Files:**
- Modify: `monitor.py:2673-2692` (`build_api_payload`)
- Test: `tests/test_netwatch.py`

- [ ] **Step 1: Write failing test:**

```python
def test_api_payload_settings_allowlist():
    from monitor import build_api_payload

    class _FakeHM:
        def list_hosts(self):
            return []

    settings = {"default_interval": 15, "openrouter_api_key": "sk-secret",
                "ntfy_topic": "secret-topic", "refresh_rate": 5}
    payload = build_api_payload(_FakeHM(), settings)
    assert payload["settings"] == {"default_interval": 15, "refresh_rate": 5}
    assert "openrouter_api_key" not in payload["settings"]
```

- [ ] **Step 2: Run** `pytest -k settings_allowlist -v` — expect FAIL.

- [ ] **Step 3: Implement.** Above `build_api_payload`:

```python
# Settings keys safe to expose via /api/status. Everything else (API keys,
# ntfy topic) stays server-side; the AI panel uses /api/ai-config instead.
SETTINGS_PUBLIC_KEYS = ("default_interval", "ping_timeout", "history_window",
                        "refresh_rate", "history_days")
```

In the returned dict change `"settings": settings,` to:

```python
        "settings":  {k: settings[k] for k in SETTINGS_PUBLIC_KEYS if k in settings},
```

(Frontend only reads `settings.default_interval` — static/core.js:264 — so nothing breaks.)

- [ ] **Step 4: Run** `pytest tests/test_netwatch.py -v` — expect ALL PASS.
- [ ] **Step 5: Commit** `git commit -m "fix: omit API key and ntfy topic from /api/status settings payload"`

---

### Task 7: hosts.yaml 0600 writes, admin-gate config edits, harden JSON body parse

**Files:**
- Modify: `monitor.py:666-693` (`save_hosts_config`), `monitor.py:2096-2108` (`_save_detected_mac` write), `monitor.py:3430-3435` (POST /api/hosts route), `monitor.py:3075-3093` (`_read_json_body`)
- Test: `tests/test_netwatch.py`

- [ ] **Step 1: Write failing tests:**

```python
def test_save_hosts_config_sets_0600(tmp_path):
    from monitor import save_hosts_config
    path = str(tmp_path / "hosts.yaml")
    save_hosts_config(path, [{"name": "a", "ip": "10.0.0.1"}])
    assert os.stat(path).st_mode & 0o777 == 0o600

def _start_server(handler):
    server = _THTS(("127.0.0.1", 0), handler)
    t = _threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, server.server_address[1]

def test_post_hosts_requires_admin(tmp_path):
    from monitor import make_handler
    auth = AuthManager(str(tmp_path / "auth.json"))
    auth.create_user("root", "password123", admin=True)
    auth.create_user("bob", "password123")
    handler = make_handler(None, {}, "/dev/null", auth_manager=auth)
    server, port = _start_server(handler)
    try:
        cookie = auth.make_session_cookie("bob")
        req = _urlreq.Request(f"http://127.0.0.1:{port}/api/hosts", data=b'{"hosts": []}',
                              method="POST", headers={"Cookie": f"nw_session={cookie}"})
        try:
            _urlreq.urlopen(req)
            assert False, "expected 403"
        except _urlerr.HTTPError as e:
            assert e.code == 403
    finally:
        server.shutdown(); server.server_close()

def test_non_dict_json_body_rejected(tmp_path):
    from monitor import make_handler
    auth = AuthManager(str(tmp_path / "auth.json"))
    auth.create_user("root", "password123", admin=True)
    handler = make_handler(None, {}, "/dev/null", auth_manager=auth)
    server, port = _start_server(handler)
    try:
        cookie = auth.make_session_cookie("root")
        req = _urlreq.Request(f"http://127.0.0.1:{port}/api/hosts", data=b'[1,2,3]',
                              method="POST", headers={"Cookie": f"nw_session={cookie}"})
        try:
            _urlreq.urlopen(req)
            assert False, "expected 400"
        except _urlerr.HTTPError as e:
            assert e.code == 400
    finally:
        server.shutdown(); server.server_close()
```

- [ ] **Step 2: Run** `pytest -k "0600 or requires_admin or non_dict" -v` — expect FAIL.

- [ ] **Step 3: Implement.**

`save_hosts_config` — after writing `tmp_path`, before `os.replace`:

```python
    os.chmod(tmp_path, 0o600)  # hosts.yaml carries the OpenRouter key + ntfy topic
```

`_save_detected_mac` — same `os.chmod(tmp, 0o600)` after the `yaml.safe_dump` write, before `os.replace`.

POST `/api/hosts` route: change `if not self._require_auth(): return` to `if not self._require_auth(admin_only=True): return`.

`_read_json_body` — replace body with:

```python
            try:
                length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                self._send_json(400, {"error": "invalid Content-Length"})
                return None, True
            if length > max_bytes:
                self._send_json(413, {"error": f"request body too large (max {max_bytes} bytes)"})
                return None, True
            try:
                body = self.rfile.read(length).decode()
                data = json.loads(body)
            except json.JSONDecodeError:
                self._send_json(400, {"error": "invalid JSON"})
                return None, True
            except (UnicodeDecodeError, ValueError):
                self._send_json(400, {"error": "invalid request body"})
                return None, True
            if not isinstance(data, dict):
                self._send_json(400, {"error": "expected a JSON object"})
                return None, True
            return data, None
```

- [ ] **Step 4: Run** `pytest tests/test_netwatch.py -v` — expect ALL PASS.
- [ ] **Step 5: Commit** `git commit -m "fix: 0600 hosts.yaml writes, admin-only host config edits, harden JSON body parsing"`

---

### Task 8: /api/history endpoint (bucketed latency series)

**Files:**
- Modify: `monitor.py` — new `HistoryDB.history_series`, new `_h_get_history`, route in `do_GET`, `make_handler` + `start_web_server` + `main()` gain `history_db` param
- Test: `tests/test_netwatch.py`

- [ ] **Step 1: Write failing tests:**

```python
def test_history_series_buckets_and_aggregates(tmp_path):
    from monitor import HistoryDB
    hdb = HistoryDB(str(tmp_path / "t.db"))
    now = int(time.time())
    rows = [("10.0.0.1", now - 30, 1, 10.0), ("10.0.0.1", now - 20, 1, 20.0),
            ("10.0.0.1", now - 10, 0, None), ("10.0.0.2", now - 10, 1, 99.0)]
    hdb.conn.executemany("INSERT INTO pings (host_ip, timestamp, is_up, latency_ms) VALUES (?,?,?,?)", rows)
    res = hdb.history_series("10.0.0.1", hours=1)
    assert res["bucket_seconds"] == 60  # 3600s / 180 points -> min clamp 60
    assert sum(p["n"] for p in res["points"]) == 3
    assert max(p["max"] for p in res["points"] if p["max"] is not None) == 20.0
    assert min(p["min"] for p in res["points"] if p["min"] is not None) == 10.0
    hdb.close()

def test_history_series_clamps_hours(tmp_path):
    from monitor import HistoryDB
    hdb = HistoryDB(str(tmp_path / "t.db"))
    assert hdb.history_series("10.0.0.1", hours=9999)["points"] == []
    hdb.close()

def test_h_get_history_requires_ip(tmp_path):
    from monitor import HistoryDB, _h_get_history
    hdb = HistoryDB(str(tmp_path / "t.db"))
    code, body = _h_get_history("/api/history", hdb)
    assert code == 400
    code, body = _h_get_history("/api/history?ip=10.0.0.1&hours=abc", hdb)
    assert code == 400
    hdb.close()

def test_h_get_history_returns_points(tmp_path):
    from monitor import HistoryDB, _h_get_history
    hdb = HistoryDB(str(tmp_path / "t.db"))
    hdb.record_ping("10.0.0.1", True, 12.0)
    code, body = _h_get_history("/api/history?ip=10.0.0.1&hours=1", hdb)
    assert code == 200
    assert body["ip"] == "10.0.0.1"
    assert sum(p["n"] for p in body["points"]) == 1
    hdb.close()
```

- [ ] **Step 2: Run** `pytest -k history_series -v` and `pytest -k h_get_history -v` — expect FAIL.

- [ ] **Step 3: Implement.** In `HistoryDB` after `latest_ping`:

```python
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
```

New handler next to `_h_get_discover`:

```python
def _h_get_history(path: str, history_db) -> tuple:
    if history_db is None:
        return 500, {"error": "history not available"}
    from urllib.parse import urlparse, parse_qs
    qs = parse_qs(urlparse(path).query)
    ip = (qs.get("ip", [""])[0] or "").strip()
    if not ip:
        return 400, {"error": "ip required"}
    try:
        hours = max(1, min(int(qs.get("hours", ["24"])[0]), 168))
    except ValueError:
        return 400, {"error": "hours must be an integer"}
    try:
        series = history_db.history_series(ip, hours=hours)
    except Exception as e:
        logging.exception("history fetch error")
        return 500, {"error": str(e)}
    return 200, {"ip": ip, "hours": hours, **series}
```

`make_handler` signature gains trailing `history_db=None`; same for `start_web_server` (pass through), and `main()` passes `history_db=history_db`. Route in `do_GET` before the `/api/inventory/` prefix routes:

```python
            if self.path.startswith("/api/history"):
                if not self._require_auth(): return
                self._send_json(*_h_get_history(self.path, history_db))
                return
```

- [ ] **Step 4: Run** `pytest tests/test_netwatch.py -v` — expect ALL PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: /api/history endpoint with bucketed latency series"`

---

### Task 9: Daily uptime rollups (ping_daily)

**Files:**
- Modify: `monitor.py` — `HistoryDB.SCHEMA` (new table), new `rollup_days` + `daily_history`, `_prune_loop` (rollup before prune), `_h_get_history` (add daily data)
- Test: `tests/test_netwatch.py`

- [ ] **Step 1: Write failing tests:**

```python
def _insert_ping(hdb, ip, ts, up, lat):
    hdb.conn.execute("INSERT INTO pings (host_ip, timestamp, is_up, latency_ms) VALUES (?,?,?,?)",
                     (ip, ts, 1 if up else 0, lat))

def test_rollup_aggregates_complete_days_only(tmp_path):
    from monitor import HistoryDB
    hdb = HistoryDB(str(tmp_path / "t.db"))
    now = int(time.time())
    _insert_ping(hdb, "10.0.0.1", now - 86400, True, 10.0)   # yesterday
    _insert_ping(hdb, "10.0.0.1", now - 86400 + 60, False, None)
    _insert_ping(hdb, "10.0.0.1", now, True, 5.0)            # today: excluded
    hdb.rollup_days()
    rows = hdb.conn.execute("SELECT day, total, up, latency_avg FROM ping_daily").fetchall()
    assert len(rows) == 1
    assert rows[0][1] == 2 and rows[0][2] == 1 and rows[0][3] == 10.0

def test_rollup_is_idempotent(tmp_path):
    from monitor import HistoryDB
    hdb = HistoryDB(str(tmp_path / "t.db"))
    _insert_ping(hdb, "10.0.0.1", int(time.time()) - 86400, True, 10.0)
    hdb.rollup_days(); hdb.rollup_days()
    assert hdb.conn.execute("SELECT COUNT(*) FROM ping_daily").fetchone()[0] == 1

def test_daily_history_survives_prune(tmp_path):
    from monitor import HistoryDB
    hdb = HistoryDB(str(tmp_path / "t.db"), retention_days=7)
    old = int(time.time()) - 8 * 86400
    _insert_ping(hdb, "10.0.0.1", old, True, 10.0)
    hdb.rollup_days()
    hdb.prune()  # deletes the raw ping
    assert hdb.conn.execute("SELECT COUNT(*) FROM pings").fetchone()[0] == 0
    daily = hdb.daily_history("10.0.0.1", days=30)
    assert len(daily) == 1 and daily[0]["uptime_pct"] == 100.0
```

- [ ] **Step 2: Run** `pytest -k rollup -v` and `pytest -k daily_history -v` — expect FAIL.

- [ ] **Step 3: Implement.** Append to `HistoryDB.SCHEMA`:

```sql
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
```

New methods after `history_series`:

```python
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
```

In `_prune_loop`, before the `history_db.prune()` try block:

```python
            try:
                history_db.rollup_days()
            except Exception as e:
                logging.warning(f"HistoryDB rollup failed: {e}")
```

In `_h_get_history`, parse `days` and include daily data — after the `hours` parse:

```python
    try:
        days = max(1, min(int(qs.get("days", ["60"])[0]), 365))
    except ValueError:
        return 400, {"error": "days must be an integer"}
```

and change the success return to:

```python
    return 200, {"ip": ip, "hours": hours, **series, "daily": history_db.daily_history(ip, days=days)}
```

(wrap inside the same try/except as `history_series`).

- [ ] **Step 4: Run** `pytest tests/test_netwatch.py -v` — expect ALL PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: daily uptime rollups in ping_daily, served via /api/history"`

---

### Task 10: Frontend — latency chart + daily uptime strip in host drawer

**Files:**
- Modify: `static/core.js` (drawer state ~line 48, `renderDrawer` ~lines 378-385 and 472-483, new functions at end of drawer section)
- Modify: `static/main.css` (after `.d-spark-axis` rules, ~line 653)

No JS test infra exists; server logic is covered by Tasks 8-9 tests. Verification is visual (Task 13).

- [ ] **Step 1: Add state + fetch/render functions to core.js.** Near `let openDrawerIp = null;` add:

```js
let drawerHistRange = 24;  // hours; persists across drawer opens this session
const HIST_RANGES = [['1h', 1], ['6h', 6], ['24h', 24], ['7d', 168]];
```

After `closeDrawer` (or near the sparkline code) add:

```js
async function loadDrawerHistory(ip){
  const el = document.getElementById('d-hist-section');
  if(!el) return;
  el.innerHTML = '<div class="d-section-hdr"><span>Latency history</span><span style="color:var(--muted)">loading…</span></div>';
  try{
    const res = await fetch('/api/history?ip=' + encodeURIComponent(ip) + '&hours=' + drawerHistRange + '&days=60');
    if(!res.ok) throw new Error('HTTP ' + res.status);
    renderDrawerHistory(el, ip, await res.json());
  }catch(e){
    el.innerHTML = '<div class="d-section-hdr"><span>Latency history</span></div>'
      + '<div class="d-hist-empty">history unavailable</div>';
  }
}

function setHistRange(btn){
  drawerHistRange = parseInt(btn.dataset.hours, 10) || 24;
  loadDrawerHistory(btn.dataset.ip);
}

function renderDrawerHistory(el, ip, data){
  const btns = HIST_RANGES.map(([label, hrs]) =>
    '<button class="d-range-btn' + (hrs === drawerHistRange ? ' active' : '') + '" data-hours="' + hrs
    + '" data-ip="' + escapeHtml(ip) + '" onclick="setHistRange(this)">' + label + '</button>'
  ).join('');
  const hdr = '<div class="d-section-hdr"><span>Latency history</span><span class="d-range-group">' + btns + '</span></div>';
  const chart = '<div class="d-spark-wrap">' + latencyChartSvg(data.points || [], data.bucket_seconds || 60) + '</div>';
  let daysHtml = '';
  if(data.daily && data.daily.length){
    daysHtml = '<div class="d-section-hdr" style="margin-top:10px"><span>Daily uptime</span>'
      + '<span style="color:var(--muted)">last ' + data.daily.length + ' day' + (data.daily.length > 1 ? 's' : '') + '</span></div>'
      + '<div class="d-spark-wrap">' + dayStripHtml(data.daily) + '</div>';
  }
  el.innerHTML = hdr + chart + daysHtml;
}

function fmtChartTime(ts, rangeHours){
  const d = new Date(ts * 1000);
  if(rangeHours <= 48) return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  return (d.getMonth() + 1) + '/' + d.getDate();
}

function latencyChartSvg(points, bucketSeconds){
  const pts = points.filter(p => p.avg !== null && p.avg !== undefined);
  if(pts.length < 2) return '<div class="d-hist-empty">not enough data for this range yet</div>';
  const W = 560, H = 130, L = 38, R = 6, T = 8, B = 18;
  const t0 = points[0].t, t1 = points[points.length - 1].t + bucketSeconds;
  const maxLat = Math.max(...pts.map(p => (p.max !== null && p.max !== undefined) ? p.max : p.avg));
  const yMax = Math.max(1, maxLat * 1.12);
  const x = t => L + (t - t0) / Math.max(1, t1 - t0) * (W - L - R);
  const y = v => T + (1 - v / yMax) * (H - T - B);
  const band = pts.map((p, i) => (i ? 'L' : 'M') + x(p.t).toFixed(1) + ',' + y(p.min ?? p.avg).toFixed(1)).join('')
    + pts.slice().reverse().map(p => 'L' + x(p.t).toFixed(1) + ',' + y(p.max ?? p.avg).toFixed(1)).join('') + 'Z';
  const line = pts.map((p, i) => (i ? 'L' : 'M') + x(p.t).toFixed(1) + ',' + y(p.avg).toFixed(1)).join('');
  const tickW = Math.max(2, (W - L - R) / Math.max(1, points.length));
  const downs = points.filter(p => p.up_pct < 100).map(p =>
    '<rect class="d-lat-down" x="' + x(p.t).toFixed(1) + '" y="' + (H - B + 4) + '" width="' + tickW.toFixed(1) + '" height="4" rx="1"/>'
  ).join('');
  const grid = [0.5, 1].map(f =>
    '<line class="d-lat-grid" x1="' + L + '" y1="' + y(yMax * f).toFixed(1) + '" x2="' + (W - R) + '" y2="' + y(yMax * f).toFixed(1) + '"/>'
  ).join('');
  const rangeHours = (t1 - t0) / 3600;
  return '<svg class="d-lat-chart" viewBox="0 0 ' + W + ' ' + H + '">'
    + grid
    + '<path class="d-lat-band" d="' + band + '"/>'
    + '<path class="d-lat-line" d="' + line + '"/>'
    + downs
    + '<text class="d-lat-label" x="' + (L - 5) + '" y="' + (y(yMax) + 3) + '" text-anchor="end">' + fmtLatency(yMax) + '</text>'
    + '<text class="d-lat-label" x="' + (L - 5) + '" y="' + (y(yMax * 0.5) + 3) + '" text-anchor="end">' + fmtLatency(yMax * 0.5) + '</text>'
    + '<text class="d-lat-label" x="' + L + '" y="' + (H - 4) + '">' + fmtChartTime(t0, rangeHours) + '</text>'
    + '<text class="d-lat-label" x="' + (W - R) + '" y="' + (H - 4) + '" text-anchor="end">' + fmtChartTime(t1, rangeHours) + '</text>'
    + '</svg>';
}

function dayStripHtml(daily){
  const cells = daily.map(d => {
    const pct = d.uptime_pct;
    let cls = 'nodata';
    if(pct !== null && pct !== undefined){
      cls = pct >= 99 ? 'ok' : (pct >= 80 ? 'warn' : 'bad');
    }
    const tip = d.day + ' — ' + (pct === null ? 'no data' : pct + '% up')
      + (d.latency_avg !== null && d.latency_avg !== undefined ? ' · ' + d.latency_avg + ' ms avg' : '');
    return '<div class="d-day ' + cls + '" title="' + escapeHtml(tip) + '"></div>';
  }).join('');
  return '<div class="d-days">' + cells + '</div>'
    + '<div class="d-spark-axis"><span>' + escapeHtml(daily[0].day) + '</span><span>' + escapeHtml(daily[daily.length - 1].day) + '</span></div>';
}
```

- [ ] **Step 2: Wire into renderDrawer.** After the `sparkHtml` block (~line 385) add:

```js
  const histHtml = '<div class="d-section" id="d-hist-section"></div>';
```

In the body-build line (~477) insert `histHtml` after `sparkHtml`:

```js
    drawerBody.innerHTML = statsHtml + linksHtml + '<div id="d-inv-section"></div>' + svcHtml + piHtml + sparkHtml + histHtml + specsHtml + notesHtml + incHtml + actionsHtml;
```

and inside that same `if(drawerBody.dataset.hostIp !== h.ip){` branch, after the innerHTML assignment, add:

```js
    loadDrawerHistory(h.ip);
```

- [ ] **Step 3: Add CSS to main.css** after the `.d-spark-axis` rule:

```css
.d-range-group{display:flex;gap:4px}
.d-range-btn{font-family:'DM Mono',monospace;font-size:10px;letter-spacing:.06em;text-transform:uppercase;padding:2px 7px;border-radius:5px;border:1px solid var(--border);background:var(--surface);color:var(--muted);cursor:pointer}
.d-range-btn:hover{color:var(--text);border-color:var(--muted)}
.d-range-btn.active{background:var(--text);color:var(--bg);border-color:var(--text)}
.d-lat-chart{display:block;width:100%;height:auto}
.d-lat-band{fill:var(--green);opacity:.14}
.d-lat-line{stroke:var(--green);stroke-width:1.8;fill:none;stroke-linejoin:round}
.d-lat-grid{stroke:var(--border-light);stroke-width:1}
.d-lat-down{fill:var(--red)}
.d-lat-label{font-family:'DM Mono',monospace;font-size:9px;fill:var(--hint)}
.d-hist-empty{font-size:12px;color:var(--hint);padding:10px 2px}
.d-days{display:flex;gap:2px;align-items:stretch}
.d-day{flex:1;height:20px;border-radius:2px;background:var(--green);opacity:.85;min-width:3px}
.d-day.warn{background:var(--amber)}
.d-day.bad{background:var(--red)}
.d-day.nodata{background:var(--border);opacity:.6}
```

- [ ] **Step 4: Sanity check** `node --check static/core.js` (syntax only). Expected: no output, exit 0.
- [ ] **Step 5: Commit** `git commit -m "feat: latency chart with range picker and 60-day uptime strip in host drawer"`

---

### Task 11: Cache busting + cache headers + version bump

**Files:**
- Modify: `dashboard.html:9,722-727` (7 static refs), `monitor.py:2606-2609` (`_load_dashboard_html`), `monitor.py:3103-3129` (GET `/` and `/static/` headers), `monitor.py:24` (VERSION)
- Test: `tests/test_netwatch.py`

- [ ] **Step 1: Write failing test:**

```python
def test_dashboard_html_version_substitution(tmp_path):
    from monitor import _load_dashboard_html, VERSION
    (tmp_path / "dashboard.html").write_text('<script src="/static/core.js?v={{VERSION}}"></script>')
    out = _load_dashboard_html(str(tmp_path))
    assert "{{VERSION}}" not in out
    assert f"?v={VERSION}" in out
```

- [ ] **Step 2: Run** `pytest -k version_substitution -v` — expect FAIL.

- [ ] **Step 3: Implement.**
  - `VERSION = "3.38"`.
  - `_load_dashboard_html`: `return f.read().replace("{{VERSION}}", VERSION)`.
  - dashboard.html: all 7 static refs get `?v={{VERSION}}`, e.g. `<link rel="stylesheet" href="/static/main.css?v={{VERSION}}">` and `<script src="/static/core.js?v={{VERSION}}"></script>` (×6 scripts).
  - In `do_GET` `/` route add `self.send_header("Cache-Control", "no-cache")`; in the `/static/` success path add `self.send_header('Cache-Control', 'public, max-age=86400')` (the handler already strips `?v=` when resolving files).

- [ ] **Step 4: Run** `pytest tests/test_netwatch.py -v` — expect ALL PASS.
- [ ] **Step 5: Commit** `git commit -m "feat: cache-bust static assets via VERSION, add cache headers; bump to 3.38"`

---

### Task 12: README + docstring drift

**Files:**
- Modify: `README.md`, `monitor.py:1-14` (docstring)

- [ ] **Step 1: Fix the four drift points + document new feature:**
  - Docstring line 2: `netwatch v2.2 - Homelab ping monitor...` → `netwatch - Homelab ping monitor with btop-style TUI, web dashboard, and web-based hosts.yaml editor.`
  - README "Files" section: `monitor.py — entire application (~8,600 lines)` → `monitor.py — application core (~3,800 lines)`; add `static/ — dashboard CSS/JS (split from dashboard.html)`.
  - README data location: `Data is stored in ~/.config/netwatch/ (SQLite: ping history, inventory, auth).` → `Data is stored next to monitor.py: netwatch.db (ping history, rollups, inventory, login lockouts), auth.json (users), monitor.log (rotating, 10MB × 3).`
  - README Security bullet `HTTP access logging to monitor.log` → `Rotating monitor.log records status transitions and warnings`.
  - README Monitoring features: add `- Latency history charts (1h-7d) and 60-day daily uptime strip per host`.

- [ ] **Step 2: Commit** `git commit -m "docs: fix README/docstring drift, document latency history"`

---

### Task 13: Deploy + verify (live service)

**Files:** none (operational)

- [ ] **Step 1:** `pytest tests/ -q` — full suite green before touching the service.
- [ ] **Step 2:** Discover how it runs: `systemctl status netwatch --no-pager; pgrep -af monitor.py`.
- [ ] **Step 3:** Stop service (`sudo systemctl stop netwatch` or kill the PID). Then clean up:

```bash
rm /home/mgipson/netwatch/monitor.py.bak_finalpolish /home/mgipson/netwatch/monitor.py.bak_security /home/mgipson/netwatch/monitor.py.bak_visual
rm /home/mgipson/netwatch/monitor.log   # 342MB; ping data lives in SQLite
chmod 600 /home/mgipson/netwatch/hosts.yaml
```

- [ ] **Step 4:** Start service. Verify:
  - `systemctl is-active netwatch` → `active` (or process running)
  - `curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/` → `200`
  - `curl -s http://localhost:8080/api/auth/status` → JSON with `logged_in:false`
  - `curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/api/status` → `401`
  - `curl -s -o /dev/null -w '%{http_code}' 'http://localhost:8080/api/history?ip=1.2.3.4'` → `401` (route wired, auth enforced)
  - After ~3 min: `ls -la netwatch.db-wal monitor.log` → WAL ≤ ~16MB, log in KB
- [ ] **Step 5:** User-facing check: open the dashboard, log in (sessions were reset by Task 5), open a host drawer → latency chart + range buttons render; daily strip appears once the first rollup runs (≤24h, or immediately if rollup_days has complete days available at first prune cycle ~60s after start).

---

## Self-review notes

- **Spec coverage:** log rotation ✓(T2) WAL/VACUUM ✓(T3) batching+SD wear ✓(T4) session bug ✓(T5) settings leak ✓(T6) perms+admin-gate+JSON nits ✓(T7) history endpoint ✓(T8) rollups ✓(T9) chart+strip ✓(T10) cache busting ✓(T11) README drift ✓(T12) .bak/log cleanup+chmod+deploy ✓(T13) git hygiene ✓(T1).
- **Type consistency:** `history_series` returns `{"bucket_seconds", "points"}`; `_h_get_history` spreads it and adds `ip/hours/daily` — frontend reads `data.points`, `data.bucket_seconds`, `data.daily` ✓. `make_handler(..., history_db=None)` keyword keeps existing test calls working ✓. Tests use existing imports (`sqlite3`, `time`, `tempfile`, `_THTS`, `_threading`, `_urlreq`, `_urlerr`, `AuthManager`, `_make_auth`) ✓.
- **Ordering:** T4 changes `recent_pings` used by T8/T9 flush-before-read; T8 must land before T9 (extends `_h_get_history`); T10 depends on T8+T9 response shape. Deploy last, single restart.
