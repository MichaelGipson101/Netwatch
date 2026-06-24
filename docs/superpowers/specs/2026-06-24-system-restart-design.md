# Restart Netwatch from Settings — Design Spec

## Goal

Add a "Restart netwatch" action to the Settings modal so an admin can restart the running
process from the dashboard, without SSH access or `sudo systemctl restart netwatch`.

## Architecture

The process restarts itself via `os.execv`, replacing its own image in place — same PID, same
listening socket, same systemd cgroup. Systemd never observes an exit, so this needs no `sudo`,
no `Restart=` policy change, and behaves identically whether netwatch is run under systemd or
manually during development (`python3 monitor.py --no-tui --port 8080`).

Before re-exec'ing, the handler performs the same graceful shutdown the existing SIGTERM path
already does in `main()`'s `finally` block:
- `history_db.close()` — flushes any buffered pings (`_flush_pings_locked`) and closes the
  shared SQLite connection used by `HistoryDB`, `InventoryDB`, and `IncidentLog`.
- `auth_manager.close()` — closes the login-attempts SQLite connection.

SQLite's WAL mode (already configured) means even a background poller thread caught mid-write
when `execv` fires is no worse than a process crash — WAL recovers automatically on the next
connection. No additional draining of `HostManager`/`NASPoller`/`ProxmoxPoller` threads is
needed; their state is in-memory cache only.

`os.execv(sys.executable, [sys.executable] + sys.argv)` is generic: it works regardless of how
the process was originally launched, since it re-invokes whatever interpreter and argv started
it. Config path resolution already uses `os.path.dirname(os.path.abspath(__file__))`, not cwd,
so a restart's cwd is irrelevant to config loading.

## Backend

**New endpoint:** `POST /api/system/restart`, admin-only (`_require_auth(admin_only=True)`).

**New handler:** `_h_post_system_restart(history_db, auth_manager) -> tuple` (near the other
`_h_post_*` handlers). Closes `history_db` and `auth_manager`, then calls `os.execv`. The
trailing `return 200, {"ok": True}` is unreachable in production (the process image is replaced
before it executes) but keeps the function's signature consistent with other handlers and gives
tests something to assert against when `os.execv` is mocked.

```python
def _h_post_system_restart(history_db, auth_manager) -> tuple:
    if history_db is not None:
        history_db.close()
    if auth_manager is not None:
        auth_manager.close()
    os.execv(sys.executable, [sys.executable] + sys.argv)
    return 200, {"ok": True}  # unreachable; satisfies callers/tests when os.execv is mocked
```

**Dispatch wiring (deliberate exception to the usual pattern):** every other POST handler is
dispatched as `self._send_json(*_h_post_X(data))` — call the handler, then send whatever it
returns. That doesn't work here: by the time `_h_post_system_restart` returns (in production,
never — `execv` replaces the process before the `return` statement runs), there is no process
left to send a response from. The dispatch block must send the response *first*, then call the
handler:

```python
if self.path == "/api/system/restart":
    if not self._require_auth(admin_only=True): return
    self._send_json(200, {"ok": True})
    _h_post_system_restart(history_db, auth_manager)
    return
```

This is safe because `BaseHTTPRequestHandler`'s response write happens synchronously and
unbuffered (`wbufsize = 0`), and already-written socket data survives `execv` regardless of the
listening socket's close-on-exec behavior (Python 3.4+ sockets default to non-inheritable, so
the old listening FD is cleanly closed as part of the exec syscall, leaving the new process free
to rebind the same port immediately).

## Frontend

**New "System" tab** in the Settings modal (`dashboard.html`), alongside General / Alerts /
Integrations / AI / Backups:

```html
<button class="stab-btn" data-tab="system" onclick="switchSettingsTab('system')" role="tab">System</button>
```

```html
<div class="stab-content" id="stab-system" style="display:none">
  <div class="form-stack">
    <p class="form-hint">Restarts the netwatch process. Ping monitoring and the dashboard will be briefly unavailable.</p>
    <button class="btn" id="restart-netwatch-btn" onclick="restartNetwatch()">Restart netwatch</button>
  </div>
</div>
```

**New `restartNetwatch()`** in `settings.js`, following the existing `confirm()`-gated
destructive-action pattern used elsewhere (e.g. inventory record deletion) — no special danger
button styling, just a confirm dialog:

```javascript
async function restartNetwatch() {
  if (!confirm('Restart netwatch now? The dashboard will be unavailable for a few seconds.')) return;
  const btn = document.getElementById('restart-netwatch-btn');
  btn.disabled = true;
  try {
    const res = await apiFetch('/api/system/restart', { method: 'POST' });
    if (!res.ok) { toast('Could not restart netwatch.', 'error'); btn.disabled = false; return; }
  } catch (e) {
    // A network error here is expected — the process may already be restarting.
  }
  toast('Restarting netwatch…', 'info');
  _pollForRestart();
}

function _pollForRestart() {
  let attempts = 0;
  const interval = setInterval(async () => {
    attempts++;
    try {
      const res = await fetch('/api/auth/status');
      if (res.ok) { clearInterval(interval); location.reload(); return; }
    } catch (e) { /* still down, keep polling */ }
    if (attempts >= 15) clearInterval(interval); // ~30s timeout, give up silently
  }, 2000);
}
```

Note: the `apiFetch` call to `/api/system/restart` may itself throw/reject (connection reset as
the process re-execs mid-response, or right after) — this is expected and handled, not an error
state worth surfacing to the user, since the toast and polling already communicate what's
happening.

`VERSION` in `monitor.py` must be bumped (next sequential value after whatever it is when this
plan is implemented) since `settings.js` and `dashboard.html` change.

## Testing

**Backend:** patch `os.execv` (same approach Task 1's tests used for `urllib.request.urlopen`)
and assert:
- `history_db.close()` and `auth_manager.close()` are called before `os.execv`.
- `os.execv` is called with `(sys.executable, [sys.executable] + sys.argv)`.
- Handler tolerates `history_db=None`/`auth_manager=None` (no crash, still calls `execv`).

No test can exercise the real `os.execv` call (it would replace the test process). This is an
accepted limitation, consistent with other irreversible-action handlers in this codebase.

**Frontend:** no automated test, consistent with other settings-tab actions. Manual
verification: click Restart, confirm dialog appears, dashboard goes briefly unavailable, then
auto-reloads once the server is back up, landing on a still-logged-in session. Session cookies
are stateless HMAC-SHA256 signatures keyed off `secret_key` in `auth.json` (`AuthManager`
"doesn't track sessions" server-side, per its own docstring) — the new process reloads the same
`auth.json`, so the same `secret_key` verifies the same cookie. `auth_manager.close()` only
closes the login-attempts SQLite connection, not anything session-related, so this holds
regardless.

## Out of scope

- No confirmation beyond the browser `confirm()` dialog — no "type RESTART to confirm" pattern.
- No way to schedule a restart for later, or to cancel one in flight.
- No change to systemd's `Restart=on-failure` policy — this feature is orthogonal to crash
  recovery.
