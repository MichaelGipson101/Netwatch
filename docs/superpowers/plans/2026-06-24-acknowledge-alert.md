# Per-Instance Alert Acknowledge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a second, lighter "Acknowledge" action to each TrueNAS alert row that proxies TrueNAS's own per-alert dismiss API, hiding just that occurrence without silencing the whole category the way the existing "Dismiss" button does.

**Architecture:** A new handler, `_h_post_nas_acknowledge_alert`, builds a one-off authenticated POST to TrueNAS's `/api/v2.0/alert/dismiss` (mirroring the existing pattern in `_h_post_proxmox_action` for one-off authenticated requests), then forces an immediate re-poll so the change is visible without waiting for the next scheduled poll. No new persisted state — TrueNAS already removes dismissed alerts from `/api/v2.0/alert/list` entirely, and the existing filter/cache/clear pipeline already does the right thing once that happens.

**Tech Stack:** Python stdlib (`urllib.request`) for the backend call — no new dependencies. Plain JS matching `static/nas.js`'s existing style.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-24-acknowledge-alert-design.md`
- No new persisted setting, no new state tracked by netwatch — this is a thin proxy to TrueNAS's own dismiss API plus a forced re-poll.
- TrueNAS's `/api/v2.0/alert/dismiss` expects the request body to be the bare JSON-encoded alert ID **string**, not a JSON object (confirmed via live testing: `data=json.dumps(alert_id).encode()`, not `json.dumps({"id": alert_id})`).
- The new endpoint is admin-only (`_require_auth(admin_only=True)`), matching `/api/nas/ignore-alert`'s precedent exactly.
- `VERSION` in `monitor.py` must be bumped whenever a `static/*.js` file changes — confirmed today this is a real, recurring cache-staleness problem in this project (browsers won't refetch `?v={{VERSION}}`-tagged assets otherwise).

---

### Task 1: Backend — `_h_post_nas_acknowledge_alert` handler and endpoint

**Files:**
- Modify: `monitor.py` (new handler function near `_h_post_nas_unignore_alert`, ~line 3814; new dispatch block in `do_POST` near the existing `/api/nas/unignore-alert` block, ~line 4718)
- Test: `tests/test_netwatch.py`

**Interfaces:**
- Consumes: `NASPoller._get_config() -> (truenas_url: str, truenas_api_key: str)` (already exists), `NASPoller._poll()` (already exists, called to force an immediate re-poll).
- Produces: `_h_post_nas_acknowledge_alert(data: dict, nas_poller) -> tuple`. Used by Task 2's dispatch wiring (already part of this task) and the frontend's new `POST /api/nas/acknowledge-alert` call.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_netwatch.py`, in the NAS poller test section (after the existing `test_h_get_proxmox_without_force_does_not_poll` block, or anywhere in the NAS-related tests — exact placement doesn't matter as long as `_make_nas_poller` is already in scope):

```python
from monitor import _h_post_nas_acknowledge_alert


def test_acknowledge_alert_requires_id():
    code, body = _h_post_nas_acknowledge_alert({}, _make_nas_poller())
    assert code == 400
    assert "id" in body["error"]


def test_acknowledge_alert_poller_none():
    code, body = _h_post_nas_acknowledge_alert({"id": "abc"}, None)
    assert code == 503


def test_acknowledge_alert_not_configured():
    am = MagicMock()
    am.data = {}
    poller = NASPoller(am, alert_settings={}, alert_port=8080)
    code, body = _h_post_nas_acknowledge_alert({"id": "abc"}, poller)
    assert code == 503


def test_acknowledge_alert_calls_truenas_dismiss_and_repolls():
    poller = _make_nas_poller()
    fake_response = MagicMock()
    fake_response.__enter__.return_value = fake_response
    fake_response.__exit__.return_value = False
    with patch("urllib.request.urlopen", return_value=fake_response) as mock_urlopen, \
         patch.object(poller, "_poll") as mock_poll:
        code, body = _h_post_nas_acknowledge_alert({"id": "bf046f61-72b1-43a3-9192-3047833abf1b"}, poller)
    assert code == 200
    assert body["ok"] is True
    mock_poll.assert_called_once()
    sent_req = mock_urlopen.call_args[0][0]
    assert sent_req.data == _json.dumps("bf046f61-72b1-43a3-9192-3047833abf1b").encode()
    assert sent_req.get_method() == "POST"
    assert sent_req.full_url == "http://truenas.test/api/v2.0/alert/dismiss"


def test_acknowledge_alert_handles_http_error_and_skips_repoll():
    poller = _make_nas_poller()
    import urllib.error as _urlerr
    err = _urlerr.HTTPError(url="x", code=404, msg="not found", hdrs=None,
                             fp=io.BytesIO(b'{"error":"no such alert"}'))
    with patch("urllib.request.urlopen", side_effect=err), patch.object(poller, "_poll") as mock_poll:
        code, body = _h_post_nas_acknowledge_alert({"id": "bad-id"}, poller)
    assert code == 404
    mock_poll.assert_not_called()
```

`io`, `MagicMock`/`patch`, and `json as _json` (confirmed: `tests/test_netwatch.py` imports it under that alias, not bare `json` — the test code above already uses `_json.dumps(...)` to match) are all already imported at the top of `tests/test_netwatch.py` — no new imports needed.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_netwatch.py -k acknowledge_alert -v`
Expected: FAIL — `ImportError: cannot import name '_h_post_nas_acknowledge_alert' from 'monitor'`

- [ ] **Step 3: Implement the handler**

Add directly after `_h_post_nas_unignore_alert` (monitor.py, after its closing `return _h_post_settings(...)` line, before `_h_get_hosts`):

```python
def _h_post_nas_acknowledge_alert(data: dict, nas_poller) -> tuple:
    if nas_poller is None:
        return 503, {"error": "NAS poller not available"}
    alert_id = (data.get("id") or "").strip()
    if not alert_id:
        return 400, {"error": "id is required"}
    url, api_key = nas_poller._get_config()
    if not url or not api_key:
        return 503, {"error": "NAS not configured"}
    import urllib.request, urllib.error as _urlerr
    req = urllib.request.Request(
        url.rstrip("/") + "/api/v2.0/alert/dismiss",
        data=json.dumps(alert_id).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except _urlerr.HTTPError as e:
        return e.code, {"error": e.read().decode(errors="replace")}
    except Exception as e:
        return 500, {"error": str(e)}
    # Reflect the change immediately rather than waiting up to 15 minutes
    # for the next scheduled poll - same force-repoll pattern as "Refresh now".
    nas_poller._poll()
    return 200, {"ok": True}
```

- [ ] **Step 4: Wire the dispatch endpoint**

In `monitor.py`'s `do_POST`, directly after the existing `/api/nas/unignore-alert` block:

```python
            if self.path == "/api/nas/acknowledge-alert":
                if not self._require_auth(admin_only=True): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_nas_acknowledge_alert(data, nas_poller))
                return
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_netwatch.py -k acknowledge_alert -v`
Expected: 5 passed

- [ ] **Step 6: Run the full suite**

Run: `python3 -m pytest tests/test_netwatch.py -v 2>&1 | tail -10`
Expected: 192 passed (187 existing + 5 new)

- [ ] **Step 7: Commit**

```bash
git add monitor.py tests/test_netwatch.py
git commit -m "feat: add /api/nas/acknowledge-alert proxying TrueNAS's own per-alert dismiss"
```

---

### Task 2: Frontend — Acknowledge button in the alert row

**Files:**
- Modify: `static/nas.js` (`renderNasAlerts()`, new `acknowledgeNasAlert()` function)
- Modify: `monitor.py` (`VERSION` bump)

**Interfaces:**
- Consumes: `POST /api/nas/acknowledge-alert` (Task 1), the existing `apiFetch`/`escapeHtml`/`toast` helpers, the existing `_authState.admin` gate (same pattern `dismissNasAlert` already uses).

No automated test applies — frontend rendering, consistent with `dismissNasAlert`'s existing lack of one. Verified manually in Step 4.

- [ ] **Step 1: Add the Acknowledge button to `renderNasAlerts()`**

In `static/nas.js`, replace the `renderNasAlerts` function:

```javascript
function renderNasAlerts(alerts) {
  if (!alerts || !alerts.length) return '';
  var rows = alerts.map(function(a) {
    var badgeCls = (a.level === 'WARNING') ? 'nas-badge-warn' : 'nas-badge-err';
    var actions = '';
    if (typeof _authState !== 'undefined' && _authState.admin) {
      actions =
        '<button class="btn" data-id="' + escapeHtml(a.id) +
        '" onclick="acknowledgeNasAlert(this)">Acknowledge</button>' +
        '<button class="btn" style="margin-left:8px" data-klass="' + escapeHtml(a.klass) +
        '" onclick="dismissNasAlert(this)">Dismiss</button>';
    }
    return '<div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border)">' +
      '<span class="nas-badge ' + badgeCls + '">' + escapeHtml(a.level) + '</span>' +
      '<span style="flex:1;font-size:13px">' + escapeHtml(a.message) + '</span>' +
      '<span style="margin-left:auto;display:flex">' + actions + '</span>' +
      '</div>';
  }).join('');
  return '<div class="nas-section-label">TrueNAS Alerts</div>' +
    '<div class="nas-card">' + rows + '</div>';
}
```

(The previous version had `dismissBtn` alone with `style="margin-left:auto"` directly on the button; this version wraps both buttons in a `margin-left:auto` flex container instead, since there are now two buttons that need to sit together at the row's right edge. `dismissNasAlert`'s own row-removal logic — `btn.closest('div[style*="border-bottom"]')` — still works unchanged, since the closest matching ancestor is still the same outer row div.)

- [ ] **Step 2: Add `acknowledgeNasAlert()`**

Add directly after `dismissNasAlert` in `static/nas.js`:

```javascript
async function acknowledgeNasAlert(btn) {
  var id = btn.dataset.id;
  btn.disabled = true;
  try {
    const res = await apiFetch('/api/nas/acknowledge-alert', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id: id}),
    });
    if (!res.ok) { toast('Could not acknowledge alert.', 'error'); btn.disabled = false; return; }
    var row = btn.closest('div[style*="border-bottom"]');
    if (row) row.remove();
    toast('Alert acknowledged.', 'success');
  } catch (e) { toast('Network error', 'error'); btn.disabled = false; }
}
```

- [ ] **Step 3: Verify JS syntax**

```bash
node --check static/nas.js
```

Expected: no output (clean).

- [ ] **Step 4: Bump `VERSION` and verify manually**

In `monitor.py`, find the current `VERSION = "..."` line and increment it by one (e.g. `"3.47"` → `"3.48"` — check the actual current value first with `grep -n '^VERSION' monitor.py`, since other work may have bumped it since this plan was written).

```bash
python3 monitor.py --no-tui --port 18080
```

Log in as an admin, open Servers → TrueNAS (with at least one WARNING+ alert present — if none exist right now, TrueNAS's `/api/v2.0/alert/list` may need a real condition, or temporarily lower `NASPoller._ALERT_MIN_LEVEL` for local testing only, reverting before commit). Confirm:
- Both "Acknowledge" and "Dismiss" buttons render side by side for an admin.
- A non-admin session (or `_authState.admin = false` in devtools console, then re-render via `renderNas(window.nwLastNas)`) shows neither button.
- Clicking "Acknowledge" removes that row and shows a success toast; the alert does not reappear on refresh (assuming TrueNAS's own condition has actually cleared, or the same instance was genuinely dismissed — a recurring condition may raise a new alert with a new ID, which is expected per the design).
- Clicking "Dismiss" still works as before (whole-category ignore).

- [ ] **Step 5: Commit**

```bash
git add static/nas.js monitor.py
git commit -m "feat: add Acknowledge button for per-instance TrueNAS alert dismissal"
```

---

## Self-Review Notes

- **Spec coverage:** the handler + endpoint (Task 1) covers the architecture section exactly (proxy to TrueNAS's dismiss API, body is the bare ID string, forced re-poll, no new persisted state); the UI (Task 2) covers the button naming/ordering/admin-gating from the spec's UI section. The spec's "Out of scope" items (re-show UI, bulk acknowledge) correctly have no corresponding task.
- **Placeholder scan:** none found.
- **Type consistency:** `_h_post_nas_acknowledge_alert(data, nas_poller) -> tuple` matches between Task 1's definition, its own tests, and Task 1's dispatch wiring. The frontend's `data-id`/`btn.dataset.id` round-trip matches the backend's `data.get("id")` exactly, and follows the existing `data-klass`/`dismissNasAlert` pattern precisely (same row-removal selector, same admin-gate check, same `apiFetch` usage).
