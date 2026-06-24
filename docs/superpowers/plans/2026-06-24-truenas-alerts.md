# TrueNAS Alerts in the NAS Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface TrueNAS's own alert feed (WARNING and above) in the NAS panel, with a per-klass dismiss mechanism so a specific alert category (e.g. the user's intentional USB-enclosure pool) can be permanently silenced without hardcoding it for every netwatch deployment.

**Architecture:** `NASPoller` gains a pure `_filter_alerts()` function (severity + ignore-list filtering), wired into `_poll()` and cached alongside pools/replication data. `_check_alerts` fires/clears ntfy alerts keyed by TrueNAS's own alert `id`. A new `truenas_ignored_alert_klasses` setting (comma-separated string) persists the ignore list through the existing settings machinery. Two new admin-only endpoints let the UI dismiss/undismiss a klass. `static/nas.js` renders the filtered list with a Dismiss button.

**Tech Stack:** Python stdlib only (no new dependencies). Plain JS matching `static/nas.js`'s existing style (no framework, global functions).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-24-truenas-alerts-design.md`
- Only alerts at `WARNING` severity or above are shown; TrueNAS's full level order is `INFO < NOTICE < WARNING < ERROR < CRITICAL < ALERT < EMERGENCY`. An unrecognized level string is kept (fail open), never silently hidden.
- The ignore list is per-deployment, stored in `hosts.yaml` (not hardcoded), so dismissing a klass on one netwatch installation never affects another.
- Dismissing/undismissing requires admin (`_require_auth(admin_only=True)`); viewing the filtered alert list only requires login, matching `/api/nas`'s existing requirement.
- This work depends on the dict-identity fix already committed in `00480af` (`settings` is now the same object everywhere) — without it, dismissing an alert would persist to `hosts.yaml` but not take effect until a restart. No separate task needed for that; it's already done.
- `_check_alerts`'s existing call signature `(pools, tasks)` must stay backward compatible — the new `alerts` parameter is added with a default so the 7 existing tests calling it with 2 positional args keep working unmodified.

---

### Task 1: `_filter_alerts()` pure function

**Files:**
- Modify: `monitor.py` (new staticmethod + two new class constants on `NASPoller`, placed near `_parse_replication`, ~line 2628)
- Test: `tests/test_netwatch.py`

**Interfaces:**
- Produces: `NASPoller._ALERT_SEVERITY_ORDER` (list of strings, ascending severity), `NASPoller._ALERT_MIN_LEVEL = "WARNING"`, `NASPoller._filter_alerts(raw_alerts: list, ignored_klasses: str) -> list[dict]` — each returned dict has exactly `{"id", "klass", "level", "message"}`. Used by Task 2's `_poll()` wiring.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_netwatch.py`, in a new section after the existing replication tests (after `test_replication_cid_uses_explicit_none_check_for_id_zero`):

```python
# ── NASPoller._filter_alerts ──────────────────────────────────────────────────

_RAW_ALERTS = [
    {"id": "a1", "klass": "PoolUSBDisks", "level": "WARNING",
     "formatted": "'ArchiveBackup' is consuming USB devices 'sde'.", "text": "fallback"},
    {"id": "a2", "klass": "AppUpdate", "level": "INFO",
     "formatted": "An update is available for \"plex\".", "text": "fallback"},
    {"id": "a3", "klass": "PoolDegraded", "level": "CRITICAL",
     "formatted": None, "text": "Pool tank is DEGRADED"},
]


def test_filter_alerts_excludes_info_level():
    result = NASPoller._filter_alerts(_RAW_ALERTS, "")
    klasses = [a["klass"] for a in result]
    assert "AppUpdate" not in klasses
    assert "PoolUSBDisks" in klasses
    assert "PoolDegraded" in klasses


def test_filter_alerts_excludes_ignored_klass():
    result = NASPoller._filter_alerts(_RAW_ALERTS, "PoolUSBDisks")
    klasses = [a["klass"] for a in result]
    assert "PoolUSBDisks" not in klasses
    assert "PoolDegraded" in klasses


def test_filter_alerts_ignore_list_handles_multiple_comma_separated():
    result = NASPoller._filter_alerts(_RAW_ALERTS, "PoolUSBDisks, PoolDegraded")
    assert result == []


def test_filter_alerts_falls_back_to_text_when_formatted_missing():
    result = NASPoller._filter_alerts(_RAW_ALERTS, "")
    degraded = next(a for a in result if a["klass"] == "PoolDegraded")
    assert degraded["message"] == "Pool tank is DEGRADED"


def test_filter_alerts_unrecognized_level_is_kept():
    raw = [{"id": "x", "klass": "SomethingNew", "level": "WEIRD_FUTURE_LEVEL",
            "formatted": "something", "text": "fallback"}]
    result = NASPoller._filter_alerts(raw, "")
    assert len(result) == 1


def test_filter_alerts_empty_ignore_list_string():
    result = NASPoller._filter_alerts(_RAW_ALERTS, None)
    assert len(result) == 2  # WARNING + CRITICAL, INFO excluded
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_netwatch.py -k filter_alerts -v`
Expected: FAIL — `AttributeError: type object 'NASPoller' has no attribute '_filter_alerts'`

- [ ] **Step 3: Implement**

Add directly after `_parse_replication` (monitor.py, after the `return {...}` block ending at line ~2628, before `def start(self, stop_event):`):

```python
    _ALERT_SEVERITY_ORDER = ["INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL", "ALERT", "EMERGENCY"]
    _ALERT_MIN_LEVEL = "WARNING"

    @staticmethod
    def _filter_alerts(raw_alerts, ignored_klasses):
        """Keep only WARNING-and-above TrueNAS alerts, excluding any klass
        the user has chosen to permanently ignore (comma-separated string).
        An unrecognized level is kept rather than dropped - better to show
        something unexpected than silently hide it."""
        ignored = {k.strip() for k in (ignored_klasses or "").split(",") if k.strip()}
        min_idx = NASPoller._ALERT_SEVERITY_ORDER.index(NASPoller._ALERT_MIN_LEVEL)
        kept = []
        for a in raw_alerts:
            klass = a.get("klass", "")
            if klass in ignored:
                continue
            level = (a.get("level") or "").upper()
            if level in NASPoller._ALERT_SEVERITY_ORDER:
                if NASPoller._ALERT_SEVERITY_ORDER.index(level) < min_idx:
                    continue
            kept.append({
                "id": a.get("id"),
                "klass": klass,
                "level": level or "UNKNOWN",
                "message": a.get("formatted") or a.get("text") or "",
            })
        return kept
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_netwatch.py -k filter_alerts -v`
Expected: 6 passed

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest tests/test_netwatch.py -v 2>&1 | tail -10`
Expected: 172 passed (166 existing + 6 new)

- [ ] **Step 6: Commit**

```bash
git add monitor.py tests/test_netwatch.py
git commit -m "feat: add NASPoller._filter_alerts for severity+ignore-list filtering"
```

---

### Task 2: Wire `/api/v2.0/alert/list` into `_poll()` and `_check_alerts`

This task is deliberately one unit, not two: changing `_poll()` to call `_check_alerts(pools, tasks, alerts)` and changing `_check_alerts`'s signature to accept that third argument have to land together — `_poll()`'s new call wouldn't actually run otherwise (a 3rd positional arg to a function that only accepts 2 is a `TypeError`), so there's no point at which a reviewer could sensibly approve one half without the other.

**Files:**
- Modify: `monitor.py` (`NASPoller._poll`, ~line 2649-2670; `NASPoller._check_alerts`, ~line 2682-2722)
- Test: `tests/test_netwatch.py`

**Interfaces:**
- Consumes: `NASPoller._filter_alerts(raw_alerts, ignored_klasses) -> list[dict]` (Task 1).
- Produces: `self._cache["alerts"]` — a list of `{"id","klass","level","message"}` dicts, present in whatever `_h_get_nas`/`/api/nas` returns (no changes needed to `_h_get_nas` itself, since it just returns `nas_poller.get_cache()` directly). `_check_alerts(self, pools, tasks, alerts=None)` — the new third parameter defaults to `None` (treated as empty), so all 7 existing calls with 2 positional arguments keep working unmodified.

- [ ] **Step 1: Write the failing tests for the `_check_alerts` extension**

Add to `tests/test_netwatch.py`, after `test_replication_cid_uses_explicit_none_check_for_id_zero` (or after Task 1's new tests if those were added first):

```python
def test_truenas_alert_fires_once():
    poller = _make_nas_poller()
    alerts = [{"id": "a1", "klass": "PoolUSBDisks", "level": "WARNING", "message": "USB disk warning"}]
    with patch("monitor._send_alert_async") as mock_send:
        poller._check_alerts([], [], alerts)
        poller._check_alerts([], [], alerts)
    assert mock_send.call_count == 1


def test_truenas_alert_clears_when_no_longer_present():
    poller = _make_nas_poller()
    alerts = [{"id": "a1", "klass": "PoolUSBDisks", "level": "WARNING", "message": "USB disk warning"}]
    with patch("monitor._send_alert_async"):
        poller._check_alerts([], [], alerts)  # fires, sets alert_state["truenas_alert_a1"] = True
    assert poller._alert_state.get("truenas_alert_a1") is True
    with patch("monitor._send_alert_async"):
        poller._check_alerts([], [], [])  # alert resolved/disappeared from TrueNAS's list
    assert poller._alert_state.get("truenas_alert_a1") is False


def test_truenas_alert_rearmed_after_clear():
    poller = _make_nas_poller()
    alerts = [{"id": "a1", "klass": "PoolUSBDisks", "level": "WARNING", "message": "USB disk warning"}]
    with patch("monitor._send_alert_async") as mock_send:
        poller._check_alerts([], [], alerts)   # fires
        poller._check_alerts([], [], [])        # clears
        poller._check_alerts([], [], alerts)   # re-arms -> fires again
    assert mock_send.call_count == 2


def test_check_alerts_without_alerts_arg_still_works():
    """Backward compatibility: existing call sites pass only (pools, tasks)."""
    poller = _make_nas_poller()
    with patch("monitor._send_alert_async") as mock_send:
        poller._check_alerts([], [])
    assert mock_send.call_count == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_netwatch.py -k truenas_alert -v`
Expected: FAIL — `TypeError: _check_alerts() takes 3 positional arguments but 4 were given`

- [ ] **Step 3: Implement the `_check_alerts` extension**

In `monitor.py`, change the `_check_alerts` signature (currently `def _check_alerts(self, pools, tasks):`) to:

```python
    def _check_alerts(self, pools, tasks, alerts=None):
```

Then add this block at the end of the method body, after the existing replication loop (after the line `self._clear_alert(cid)` that closes the `for task in tasks:` loop, before the method ends):

```python
        alerts = alerts or []
        current_alert_cids = set()
        for alert in alerts:
            cid = f"truenas_alert_{alert['id']}"
            current_alert_cids.add(cid)
            self._fire_alert(cid, "Netwatch · NAS Alert", f"TrueNAS: {alert['message']}")
        # An alert that resolved (or got newly ignored) simply disappears from
        # TrueNAS's own list rather than arriving with a "resolved" state, so
        # clear any previously-firing TrueNAS alert no longer present here.
        for cid in list(self._alert_state.keys()):
            if cid.startswith("truenas_alert_") and cid not in current_alert_cids:
                self._clear_alert(cid)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_netwatch.py -k "truenas_alert or check_alerts" -v`
Expected: all passing (4 new)

- [ ] **Step 5: Add the fetch and wire the cache in `_poll()`**

In `monitor.py`, `NASPoller._poll` currently reads:

```python
        try:
            pools_raw = self._fetch(url, api_key, "/api/v2.0/pool")
            scrub_tasks = self._fetch(url, api_key, "/api/v2.0/pool/scrub")
            replication_raw = self._fetch(url, api_key, "/api/v2.0/replication")
            system_info = self._fetch(url, api_key, "/api/v2.0/system/info")
            tz_name = system_info.get("timezone") or "UTC"
            pools = [self._parse_pool(p, scrub_tasks, tz_name) for p in pools_raw]
            tasks = [self._parse_replication(r) for r in replication_raw]
            with self._lock:
                self._cache = {
                    "reachable": True,
                    "last_updated": datetime.now(tz=timezone.utc).isoformat(),
                    "error": None,
                    "pools": pools,
                    "replication_tasks": tasks,
                }
            self._check_alerts(pools, tasks)
```

Replace it with:

```python
        try:
            pools_raw = self._fetch(url, api_key, "/api/v2.0/pool")
            scrub_tasks = self._fetch(url, api_key, "/api/v2.0/pool/scrub")
            replication_raw = self._fetch(url, api_key, "/api/v2.0/replication")
            system_info = self._fetch(url, api_key, "/api/v2.0/system/info")
            alerts_raw = self._fetch(url, api_key, "/api/v2.0/alert/list")
            tz_name = system_info.get("timezone") or "UTC"
            pools = [self._parse_pool(p, scrub_tasks, tz_name) for p in pools_raw]
            tasks = [self._parse_replication(r) for r in replication_raw]
            ignored_klasses = self._alert_settings.get("truenas_ignored_alert_klasses", "")
            alerts = self._filter_alerts(alerts_raw, ignored_klasses)
            with self._lock:
                self._cache = {
                    "reachable": True,
                    "last_updated": datetime.now(tz=timezone.utc).isoformat(),
                    "error": None,
                    "pools": pools,
                    "replication_tasks": tasks,
                    "alerts": alerts,
                }
            self._check_alerts(pools, tasks, alerts)
```

- [ ] **Step 6: No automated test for the fetch wiring itself**

Skip — consistent with how `pools_raw`/`replication_raw`/`system_info` fetches aren't unit-tested at the `_poll()` level either (only their downstream parsing functions are). This is verified end-to-end in Task 5's manual check.

- [ ] **Step 7: Run the full suite**

Run: `python3 -m pytest tests/test_netwatch.py -v 2>&1 | tail -10`
Expected: 176 passed (172 from Task 1 + 4 new)

- [ ] **Step 8: Commit**

```bash
git add monitor.py tests/test_netwatch.py
git commit -m "feat: fetch and alert on TrueNAS's own alert list (WARNING+)"
```

---

### Task 3: `truenas_ignored_alert_klasses` setting

**Files:**
- Modify: `monitor.py` (`SETTINGS_EDITABLE_KEYS`, ~line 3420)

**Interfaces:**
- Produces: `truenas_ignored_alert_klasses` as a valid key in `SETTINGS_EDITABLE_KEYS` with type `str`, readable/writable through the existing `_h_get_settings`/`_h_post_settings` machinery unchanged. Used by Task 4's new endpoints.

No test needed for this step alone — `_h_post_settings`'s existing generic tests already cover every `str`-typed key uniformly via `SETTINGS_EDITABLE_KEYS` iteration; Task 4's tests exercise this key specifically through the new endpoints.

- [ ] **Step 1: Add the key**

In `monitor.py`, `SETTINGS_EDITABLE_KEYS` (~line 3420-3440), add one line after `"setup_wizard_complete": bool,`:

```python
SETTINGS_EDITABLE_KEYS = {
    "default_interval":     int,
    "ping_timeout":         int,
    "history_window":       int,
    "refresh_rate":         int,
    "history_days":         int,
    "ntfy_topic":           str,
    "ntfy_server":          str,
    "truenas_url":          str,
    "truenas_api_key":      str,
    "proxmox_url":          str,
    "proxmox_user":         str,
    "proxmox_password":     str,
    "proxmox_token_id":     str,
    "proxmox_token_secret": str,
    "proxmox_node":         str,
    "proxmox_verify_ssl":   bool,
    "proxmox_ca_cert":      str,
    "openrouter_api_key":   str,
    "ai_model":             str,
    "setup_wizard_complete": bool,
    "truenas_ignored_alert_klasses": str,
}
```

- [ ] **Step 2: Verify nothing else needs to change**

`truenas_ignored_alert_klasses` is not in `_AUTH_STORED_KEYS` (it's not a credential, so it correctly lives in `hosts.yaml`, not `auth.json`) and not in `_SETTINGS_URL_KEYS` (not a URL). No further edits needed.

```bash
grep -n "_AUTH_STORED_KEYS\|_SETTINGS_URL_KEYS" monitor.py
```

Expected: confirm `truenas_ignored_alert_klasses` does not appear in either set's definition.

- [ ] **Step 3: Commit**

```bash
git add monitor.py
git commit -m "feat: add truenas_ignored_alert_klasses setting"
```

---

### Task 4: Ignore/unignore endpoints

**Files:**
- Modify: `monitor.py` (two new handler functions near `_h_post_settings`, ~line 3746; two new dispatch blocks in `do_POST`, near the existing `/api/settings` POST dispatch ~line 4628)
- Test: `tests/test_netwatch.py`

**Interfaces:**
- Consumes: `_h_post_settings(data, config_path, settings, auth_manager) -> tuple` (already exists) - both new handlers are thin wrappers that compute an updated `truenas_ignored_alert_klasses` string and delegate to it for actual persistence, so they get the exact same validation/atomic-write/in-place-mutation behavior for free.
- Produces: `_h_post_nas_ignore_alert(data, config_path, settings, auth_manager) -> tuple`, `_h_post_nas_unignore_alert(data, config_path, settings, auth_manager) -> tuple`. Used by Task 5's frontend Dismiss button via `POST /api/nas/ignore-alert`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_netwatch.py`, after the `_filter_alerts`/`_check_alerts` tests:

```python
# ── _h_post_nas_ignore_alert / _h_post_nas_unignore_alert ────────────────────

from monitor import _h_post_nas_ignore_alert, _h_post_nas_unignore_alert


def test_ignore_alert_requires_klass():
    code, body = _h_post_nas_ignore_alert({}, "/dev/null", {})
    assert code == 400
    assert "klass" in body["error"]


def test_ignore_alert_adds_to_empty_list(tmp_path):
    config_path = str(tmp_path / "hosts.yaml")
    with open(config_path, "w") as f:
        f.write("settings: {}\nhosts: []\n")
    settings = {}
    code, body = _h_post_nas_ignore_alert({"klass": "PoolUSBDisks"}, config_path, settings)
    assert code == 200
    assert settings["truenas_ignored_alert_klasses"] == "PoolUSBDisks"


def test_ignore_alert_dedupes_repeated_calls(tmp_path):
    config_path = str(tmp_path / "hosts.yaml")
    with open(config_path, "w") as f:
        f.write("settings: {}\nhosts: []\n")
    settings = {}
    _h_post_nas_ignore_alert({"klass": "PoolUSBDisks"}, config_path, settings)
    code, body = _h_post_nas_ignore_alert({"klass": "PoolUSBDisks"}, config_path, settings)
    assert code == 200
    assert settings["truenas_ignored_alert_klasses"] == "PoolUSBDisks"


def test_ignore_alert_appends_to_existing_list(tmp_path):
    config_path = str(tmp_path / "hosts.yaml")
    with open(config_path, "w") as f:
        f.write("settings: {}\nhosts: []\n")
    settings = {"truenas_ignored_alert_klasses": "SomeOtherKlass"}
    code, body = _h_post_nas_ignore_alert({"klass": "PoolUSBDisks"}, config_path, settings)
    assert code == 200
    assert "SomeOtherKlass" in settings["truenas_ignored_alert_klasses"]
    assert "PoolUSBDisks" in settings["truenas_ignored_alert_klasses"]


def test_unignore_alert_requires_klass():
    code, body = _h_post_nas_unignore_alert({}, "/dev/null", {})
    assert code == 400


def test_unignore_alert_removes_klass(tmp_path):
    config_path = str(tmp_path / "hosts.yaml")
    with open(config_path, "w") as f:
        f.write("settings: {}\nhosts: []\n")
    settings = {"truenas_ignored_alert_klasses": "PoolUSBDisks,SomeOtherKlass"}
    code, body = _h_post_nas_unignore_alert({"klass": "PoolUSBDisks"}, config_path, settings)
    assert code == 200
    assert "PoolUSBDisks" not in settings.get("truenas_ignored_alert_klasses", "")
    assert "SomeOtherKlass" in settings["truenas_ignored_alert_klasses"]


def test_unignore_alert_clears_key_when_list_becomes_empty(tmp_path):
    config_path = str(tmp_path / "hosts.yaml")
    with open(config_path, "w") as f:
        f.write("settings: {}\nhosts: []\n")
    settings = {"truenas_ignored_alert_klasses": "PoolUSBDisks"}
    code, body = _h_post_nas_unignore_alert({"klass": "PoolUSBDisks"}, config_path, settings)
    assert code == 200
    assert "truenas_ignored_alert_klasses" not in settings
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_netwatch.py -k "ignore_alert" -v`
Expected: FAIL — `ImportError: cannot import name '_h_post_nas_ignore_alert' from 'monitor'`

- [ ] **Step 3: Implement the handlers**

Add directly after `_h_post_settings` (monitor.py, after its closing `return 200, {"ok": True, "settings": result}` at ~line 3746, before `_h_get_hosts`):

```python
def _h_post_nas_ignore_alert(data: dict, config_path: str, settings: dict, auth_manager=None) -> tuple:
    klass = (data.get("klass") or "").strip()
    if not klass:
        return 400, {"error": "klass is required"}
    current = [k.strip() for k in (settings.get("truenas_ignored_alert_klasses") or "").split(",") if k.strip()]
    if klass not in current:
        current.append(klass)
    return _h_post_settings({"truenas_ignored_alert_klasses": ",".join(current)},
                             config_path, settings, auth_manager)


def _h_post_nas_unignore_alert(data: dict, config_path: str, settings: dict, auth_manager=None) -> tuple:
    klass = (data.get("klass") or "").strip()
    if not klass:
        return 400, {"error": "klass is required"}
    current = [k.strip() for k in (settings.get("truenas_ignored_alert_klasses") or "").split(",") if k.strip()]
    current = [k for k in current if k != klass]
    return _h_post_settings({"truenas_ignored_alert_klasses": ",".join(current)},
                             config_path, settings, auth_manager)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_netwatch.py -k "ignore_alert" -v`
Expected: 7 passed

- [ ] **Step 5: Wire the dispatch endpoints**

In `monitor.py`'s `do_POST`, directly before the existing `/api/settings` POST block (~line 4628):

```python
            if self.path == "/api/nas/ignore-alert":
                if not self._require_auth(admin_only=True): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_nas_ignore_alert(data, config_path, settings, auth_manager))
                return

            if self.path == "/api/nas/unignore-alert":
                if not self._require_auth(admin_only=True): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_nas_unignore_alert(data, config_path, settings, auth_manager))
                return

            if self.path == "/api/settings":
                if not self._require_auth(admin_only=True): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_settings(data, config_path, settings, auth_manager))
                return
```

- [ ] **Step 6: Run the full suite**

Run: `python3 -m pytest tests/test_netwatch.py -v 2>&1 | tail -10`
Expected: 183 passed (176 from Task 2 + 7 new)

- [ ] **Step 7: Commit**

```bash
git add monitor.py tests/test_netwatch.py
git commit -m "feat: add admin-only /api/nas/ignore-alert and /api/nas/unignore-alert endpoints"
```

---

### Task 5: Frontend — Alerts block in the NAS panel

**Files:**
- Modify: `static/nas.js` (new `renderNasAlerts()` function, new `dismissNasAlert()` function, wired into `renderNas()`)
- Modify: `static/main.css` (small additions for the alert row layout)

**Interfaces:**
- Consumes: `data.alerts` (array of `{"id","klass","level","message"}`, from Task 2's cache wiring, arriving via the existing `/api/nas` fetch `renderNas()` already does), the global `_authState.admin` (already set by `static/auth.js`, loaded before `nas.js` per `dashboard.html`'s script order), the global `apiFetch` helper (already exists in `static/utils.js`).

No automated test applies — frontend rendering logic isn't covered by the Python test suite in this codebase (consistent with every other `static/*.js` change in this project's history). Verified manually in Step 4.

- [ ] **Step 1: Add `renderNasAlerts()` and `dismissNasAlert()` to `static/nas.js`**

Add this directly above the existing `function renderPoolSection(pool) {` in `static/nas.js`:

```js
function renderNasAlerts(alerts) {
  if (!alerts || !alerts.length) return '';
  var rows = alerts.map(function(a) {
    var badgeCls = (a.level === 'WARNING') ? 'nas-badge-warn' : 'nas-badge-err';
    var dismissBtn = (typeof _authState !== 'undefined' && _authState.admin)
      ? '<button class="btn" style="margin-left:auto" data-klass="' + escapeHtml(a.klass) +
        '" onclick="dismissNasAlert(this)">Dismiss</button>'
      : '';
    return '<div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border)">' +
      '<span class="nas-badge ' + badgeCls + '">' + escapeHtml(a.level) + '</span>' +
      '<span style="flex:1;font-size:13px">' + escapeHtml(a.message) + '</span>' +
      dismissBtn +
      '</div>';
  }).join('');
  return '<div class="nas-section-label">TrueNAS Alerts</div>' +
    '<div class="nas-card">' + rows + '</div>';
}

async function dismissNasAlert(btn) {
  var klass = btn.dataset.klass;
  btn.disabled = true;
  try {
    const res = await apiFetch('/api/nas/ignore-alert', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({klass: klass}),
    });
    if (!res.ok) { toast('Could not dismiss alert.', 'error'); btn.disabled = false; return; }
    var row = btn.closest('div[style*="border-bottom"]');
    if (row) row.remove();
    toast('Alert category dismissed.', 'success');
  } catch (e) { toast('Network error', 'error'); btn.disabled = false; }
}
```

- [ ] **Step 2: Wire it into `renderNas()`**

In `static/nas.js`, `renderNas()` currently builds:

```js
  var html = renderNasActionBar(data);
  if (data.reachable && (!data.pools || !data.pools.length)) {
    html += '<div class="nas-unavailable">No pools found on TrueNAS.</div>';
  }
  (data.pools || []).forEach(function(pool) { html += renderPoolSection(pool); });
```

Change it to:

```js
  var html = renderNasActionBar(data);
  html += renderNasAlerts(data.alerts);
  if (data.reachable && (!data.pools || !data.pools.length)) {
    html += '<div class="nas-unavailable">No pools found on TrueNAS.</div>';
  }
  (data.pools || []).forEach(function(pool) { html += renderPoolSection(pool); });
```

- [ ] **Step 3: Verify JS syntax**

```bash
node --check static/nas.js
```

Expected: no output (clean).

- [ ] **Step 4: Manual verification**

```bash
python3 monitor.py --port 18080
```

Log in as an admin user, open the Servers tab → TrueNAS panel. Expected: if your TrueNAS instance has any WARNING+ alerts not yet ignored, a "TrueNAS Alerts" card appears above the pool sections, each row showing a severity badge, the message, and a "Dismiss" button. Click Dismiss on one — it should disappear immediately and a success toast should appear. Refresh the page (or click "Refresh now") — confirm the dismissed alert category stays gone (proving the ignore-list persisted and `_filter_alerts` is excluding it on the next poll/cache read).

Also check as a non-admin logged-in user (or open devtools and temporarily fake `_authState.admin = false` then call `renderNas(window.nwLastNas)` in the console) — confirm the Dismiss button doesn't render, but the alert itself still shows (viewing requires only login, not admin, per the design).

- [ ] **Step 5: Commit**

```bash
git add static/nas.js
git commit -m "feat: render TrueNAS alerts in the NAS panel with an admin-only dismiss action"
```

---

## Self-Review Notes

- **Spec coverage:** `_filter_alerts` (Task 1) covers the severity-threshold + ignore-list filtering section; `_poll()`/cache wiring and the `_check_alerts` extension (both Task 2, deliberately one unit) cover the architecture section's ntfy-firing requirement; the setting (Task 3) and endpoints (Task 4) cover the ignore-list storage section exactly as specified (comma-separated string, `hosts.yaml`, admin-only mutation, login-only viewing); the UI (Task 5) covers the spec's rendering requirements (severity badge, message, admin-gated Dismiss, empty-state silence). The spec's "Out of scope" items (un-ignore UI, configurable severity threshold, alerts outside the NAS panel) correctly have no corresponding task.
- **Placeholder scan:** none found.
- **Type consistency:** `_filter_alerts(raw_alerts, ignored_klasses) -> list[dict]`'s output shape (`id`/`klass`/`level`/`message`) is used identically in Task 2's cache assignment, Task 2's own `_check_alerts` loop (`alert['id']`, `alert['message']`), and Task 5's frontend (`a.level`, `a.message`, `a.klass`) — no drift. `_h_post_nas_ignore_alert`/`_h_post_nas_unignore_alert`'s signature `(data, config_path, settings, auth_manager=None)` matches `_h_post_settings`'s own signature exactly, and Task 4's dispatch wiring passes the same four arguments `make_handler`'s closure already has in scope for the existing `/api/settings` block.
- **Dependency on the settings dict-identity fix:** explicitly called out in Global Constraints rather than re-implemented as a task, since it's already committed (`00480af`) - this plan's Task 4 endpoints rely on it for the Dismiss button to take effect without a restart, and it would be a silent correctness gap if a reader assumed it still needed doing.
