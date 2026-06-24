# Restart Netwatch from Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Restart netwatch" button to a new System tab in the Settings modal, so an admin can restart the running process from the dashboard without SSH/sudo access.

**Architecture:** The process restarts itself via `os.execv`, replacing its own image in place (same PID, same listening socket, same systemd cgroup) — so no `sudo`, no systemd `Restart=` policy involvement, and identical behavior whether run under systemd or manually. Before re-exec'ing, the handler performs the same graceful shutdown the existing SIGTERM path already does in `main()`'s `finally` block: `history_db.close()` then `auth_manager.close()`.

**Tech Stack:** Python stdlib (`os.execv`) for the backend — no new dependencies. Plain JS matching `static/settings.js`'s existing style.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-24-system-restart-design.md`
- The endpoint is admin-only (`_require_auth(admin_only=True)`), matching every other settings-mutating endpoint's precedent.
- The dispatch block must send the HTTP response **before** calling the handler — by design, the handler never returns in production (`os.execv` replaces the process), so the usual `self._send_json(*_h_post_X(data))` pattern is inverted here only.
- `VERSION` in `monitor.py` must be bumped whenever a `static/*.js` file or `dashboard.html` changes (cache-busting via `?v={{VERSION}}`). Current value at plan-writing time is `"3.49"` — verify with `grep -n '^VERSION' monitor.py` before bumping, since other work may have changed it since.
- No automated test can call the real `os.execv` (it would replace the test process) — `os.execv` is mocked in all backend tests.

---

### Task 1: Backend — `_h_post_system_restart` handler and endpoint

**Files:**
- Modify: `monitor.py` (new handler function after `_h_post_nas_acknowledge_alert`, ~line 3842; new dispatch block in `do_POST` after the `/api/nas/acknowledge-alert` block, ~line 4754)
- Test: `tests/test_netwatch.py`

**Interfaces:**
- Consumes: `history_db.close()` and `auth_manager.close()` (both already exist — `HistoryDB.close` at `monitor.py:1076`, `AuthManager.close` at `monitor.py:932`), `os.execv` (stdlib), `sys.executable`/`sys.argv` (stdlib).
- Produces: `_h_post_system_restart(history_db, auth_manager) -> tuple`. Used by Task 1's own dispatch wiring. No later task consumes this directly (Task 2 is the frontend calling the HTTP endpoint, not this Python function).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_netwatch.py`, anywhere after the existing `from unittest.mock import patch, MagicMock` import (line 1456) — e.g. directly after the acknowledge-alert tests block:

```python
from monitor import _h_post_system_restart


def test_system_restart_closes_resources_then_execs_in_order():
    history_db = MagicMock()
    auth_manager = MagicMock()
    manager = MagicMock()
    manager.attach_mock(history_db.close, "history_close")
    manager.attach_mock(auth_manager.close, "auth_close")
    with patch("os.execv") as mock_execv:
        manager.attach_mock(mock_execv, "execv")
        _h_post_system_restart(history_db, auth_manager)
    assert [c[0] for c in manager.mock_calls] == ["history_close", "auth_close", "execv"]


def test_system_restart_execs_with_current_interpreter_and_argv():
    with patch("os.execv") as mock_execv, \
         patch.object(_mon.sys, "argv", ["monitor.py", "--no-tui", "--port", "8080"]):
        _h_post_system_restart(MagicMock(), MagicMock())
    mock_execv.assert_called_once_with(
        _mon.sys.executable,
        [_mon.sys.executable, "monitor.py", "--no-tui", "--port", "8080"],
    )


def test_system_restart_tolerates_missing_history_db_and_auth_manager():
    with patch("os.execv") as mock_execv:
        _h_post_system_restart(None, None)
    mock_execv.assert_called_once()
```

`_mon` (an `import monitor as _mon` alias) and `MagicMock`/`patch` are already imported at the top of `tests/test_netwatch.py` (lines 14 and 1456) — no new imports needed beyond the `_h_post_system_restart` import shown above.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_netwatch.py -k system_restart -v`
Expected: FAIL — `ImportError: cannot import name '_h_post_system_restart' from 'monitor'`

- [ ] **Step 3: Implement the handler**

Add directly after `_h_post_nas_acknowledge_alert` (monitor.py, after its closing `return 200, {"ok": True}` line, before `_h_get_hosts`):

```python
def _h_post_system_restart(history_db, auth_manager) -> tuple:
    if history_db is not None:
        history_db.close()
    if auth_manager is not None:
        auth_manager.close()
    os.execv(sys.executable, [sys.executable] + sys.argv)
    return 200, {"ok": True}  # unreachable; satisfies callers/tests when os.execv is mocked
```

- [ ] **Step 4: Wire the dispatch endpoint**

In `monitor.py`'s `do_POST`, directly after the existing `/api/nas/acknowledge-alert` block. This block intentionally does NOT follow the `self._send_json(*_h_post_X(data))` pattern used elsewhere — the response must be sent before the handler runs, since the handler's `os.execv` call replaces the process before any code after it can execute:

```python
            if self.path == "/api/system/restart":
                if not self._require_auth(admin_only=True): return
                self._send_json(200, {"ok": True})
                _h_post_system_restart(history_db, auth_manager)
                return
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_netwatch.py -k system_restart -v`
Expected: 3 passed

- [ ] **Step 6: Run the full suite**

Run: `python3 -m pytest tests/test_netwatch.py -v 2>&1 | tail -10`
Expected: 195 passed (192 existing + 3 new)

- [ ] **Step 7: Commit**

```bash
git add monitor.py tests/test_netwatch.py
git commit -m "feat: add /api/system/restart that re-execs the process in place"
```

---

### Task 2: Frontend — System tab with Restart button

**Files:**
- Modify: `dashboard.html` (new `<button class="stab-btn" data-tab="system">` after the Backups tab button, ~line 729; new `<div class="stab-content" id="stab-system">` after the Backups tab content block, ~line 805)
- Modify: `static/settings.js` (new `restartNetwatch()` and `_pollForRestart()` functions, after `switchSettingsTab`, ~line 91)
- Modify: `monitor.py` (`VERSION` bump)

**Interfaces:**
- Consumes: `POST /api/system/restart` (Task 1), `GET /api/auth/status` (pre-existing endpoint, already used elsewhere in the codebase for session checks), the existing `apiFetch`/`toast` helpers from `static/utils.js`, the existing `switchSettingsTab` tab-registration pattern (no special wiring beyond matching `data-tab`/`id="stab-<name>"` — confirmed generic in `static/settings.js:83-91`).

No automated test applies — frontend rendering and a manual restart action, consistent with other settings-tab buttons (e.g. the Backups tab's "Refresh" button) having no test. Verified manually in Step 4.

- [ ] **Step 1: Add the System tab button**

In `dashboard.html`, directly after the Backups tab button (~line 729):

```html
        <button class="stab-btn" data-tab="system" onclick="switchSettingsTab('system')" role="tab">System</button>
```

So the full `stab-bar` block reads:

```html
      <div class="stab-bar" role="tablist">
        <button class="stab-btn active" data-tab="general" onclick="switchSettingsTab('general')" role="tab">General</button>
        <button class="stab-btn" data-tab="alerts" onclick="switchSettingsTab('alerts')" role="tab">Alerts</button>
        <button class="stab-btn" data-tab="integrations" onclick="switchSettingsTab('integrations')" role="tab">Integrations</button>
        <button class="stab-btn" data-tab="ai" onclick="switchSettingsTab('ai')" role="tab">AI</button>
        <button class="stab-btn" data-tab="backups" onclick="switchSettingsTab('backups')" role="tab">Backups</button>
        <button class="stab-btn" data-tab="system" onclick="switchSettingsTab('system')" role="tab">System</button>
      </div>
```

- [ ] **Step 2: Add the System tab content**

In `dashboard.html`, directly after the closing `</div>` of `stab-content" id="stab-backups"` (~line 805), before the `<div id="settings-error">` line:

```html
      <div class="stab-content" id="stab-system" style="display:none">
        <div class="form-stack">
          <p class="form-hint">Restarts the netwatch process. Ping monitoring and the dashboard will be briefly unavailable.</p>
          <button type="button" class="btn" id="restart-netwatch-btn" onclick="restartNetwatch()" style="align-self:flex-start">Restart netwatch</button>
        </div>
      </div>
```

- [ ] **Step 3: Add `restartNetwatch()` and `_pollForRestart()`**

In `static/settings.js`, directly after the closing `}` of `switchSettingsTab` (~line 91):

```javascript
async function restartNetwatch() {
  if (!confirm('Restart netwatch now? The dashboard will be unavailable for a few seconds.')) return;
  const btn = document.getElementById('restart-netwatch-btn');
  btn.disabled = true;
  try {
    const res = await apiFetch('/api/system/restart', { method: 'POST' });
    if (!res.ok) { toast('Could not restart netwatch.', 'error'); btn.disabled = false; return; }
  } catch (e) {
    // A network error here is expected - the process may already be restarting.
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

- [ ] **Step 4: Verify JS syntax**

```bash
node --check static/settings.js
```

Expected: no output (clean).

- [ ] **Step 5: Bump `VERSION` and verify manually**

In `monitor.py`, find the current `VERSION = "..."` line and increment it by one (check the actual current value first with `grep -n '^VERSION' monitor.py`, since other work may have bumped it since this plan was written).

```bash
python3 monitor.py --no-tui --port 18080
```

Log in as an admin, open Settings → System. Confirm:
- The System tab button and content render, matching the other tabs' visual style.
- Clicking "Restart netwatch" shows the `confirm()` dialog; cancelling does nothing.
- Confirming disables the button, shows the "Restarting…" toast, and the dev server process actually exits-and-relaunches (watch the terminal running `python3 monitor.py --no-tui --port 18080` — it should print its startup banner again with the same PID, without you restarting it manually).
- After a few seconds, the page automatically reloads and you land on a still-logged-in dashboard (not bounced to the login screen) — this confirms the session cookie's HMAC signature, keyed off `secret_key` in `auth.json`, verifies correctly against the freshly-restarted process per the spec's "Out of scope"/Testing section reasoning.
- A non-admin session shows the System tab content but POSTing `/api/system/restart` is rejected with 401/403 by `_require_auth(admin_only=True)` (same as any other admin-only settings action) — verify via the Network tab in devtools, not by building separate UI gating (none of the other Settings tabs hide their content for non-admins either; the `Save` button at the bottom is the actual gate, consistent with existing UX).

- [ ] **Step 6: Commit**

```bash
git add dashboard.html static/settings.js monitor.py
git commit -m "feat: add Restart netwatch button to a new Settings > System tab"
```

---

## Self-Review Notes

- **Spec coverage:** Task 1 covers the spec's Backend section exactly (handler signature, close-then-close-then-execv ordering, the deliberate dispatch-pattern exception, admin-only gating). Task 2 covers the Frontend section exactly (tab markup, button, confirm dialog, toast, polling-then-reload). The spec's "Out of scope" items (typed confirmation, scheduled/cancelable restart, systemd policy changes) correctly have no corresponding task.
- **Placeholder scan:** none found — every step has complete, runnable code.
- **Type consistency:** `_h_post_system_restart(history_db, auth_manager) -> tuple` matches between Task 1's implementation, its three tests, and its dispatch wiring. The frontend's `restartNetwatch()` → `POST /api/system/restart` round-trips to Task 1's exact endpoint path. `_pollForRestart()`'s use of `GET /api/auth/status` matches that endpoint's existing pre-existing shape (no new backend work needed for it — confirmed pre-existing via `grep` during plan research).
