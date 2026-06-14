# NAS Storage Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Storage" tab to the Netwatch dashboard showing TrueNAS pool health, scrub status, and replication task results, with ntfy alerts on failure or staleness.

**Architecture:** A new `NASPoller` class in `monitor.py` runs a background thread that polls the TrueNAS REST API every 15 minutes and caches results in memory. A new `/api/nas` endpoint returns that cache to the frontend. The Storage tab in `dashboard.html` + `static/nas.js` fetches `/api/nas` on tab activation and manual refresh.

**Tech Stack:** Python stdlib (`urllib.request`, `datetime`, `threading`), vanilla JS, existing Netwatch ntfy integration. No new dependencies.

---

## File map

| File | Change |
|---|---|
| `monitor.py` | Add `NASPoller` class; add `_h_get_nas()`; add `/api/nas` route in `make_handler()`; wire `nas_poller` through `start_web_server()` and `run()` |
| `dashboard.html` | Add Storage tab button; add `<div class="view" id="view-storage">`; add `<script src="/static/nas.js">` |
| `static/nas.js` | New file — all Storage tab JS (fetch, render, badge logic, helpers) |
| `static/main.css` | Add NAS component styles (metrics grid, card, VDEV rows, badges, action bar) |
| `static/core.js` | Add `fetchNas()` call in `setTab()` for the `storage` tab |
| `tests/test_netwatch.py` | Add tests for `NASPoller` parse methods, alert de-dupe, and `_h_get_nas()` |

---

## Task 1: NASPoller class — parse and cache

No threading, no network calls in this task. Just the class skeleton, parse helpers, and cache.

**Files:**
- Modify: `monitor.py` (insert after line ~2366, after `_send_alert_async`)
- Modify: `tests/test_netwatch.py`

- [ ] **Step 1: Write failing tests for parse methods**

Add to `tests/test_netwatch.py` (after existing imports):

```python
from monitor import NASPoller
import monitor as _mon

# --- NASPoller parse tests ---

_POOL_RAW = {
    "name": "tank",
    "status": "ONLINE",
    "size": 4000000000000,
    "allocated": 2000000000000,
    "topology": {
        "data": [
            {
                "type": "MIRROR",
                "name": "mirror-0",
                "status": "ONLINE",
                "children": [
                    {"disk": "ada0", "status": "ONLINE", "type": "DISK"},
                    {"disk": "ada1", "status": "ONLINE", "type": "DISK"},
                ],
            }
        ]
    },
    "scan": {
        "state": "FINISHED",
        "end_time": "2026-06-01T04:22:00",
        "errors": 0,
    },
}

_SCRUB_TASKS = [
    {"pool": "tank", "schedule": {"minute": "0", "hour": "0", "dom": "1", "month": "*", "dow": "*"}}
]

_REP_RAW = {
    "id": 1,
    "name": "tank → backup",
    "state": {"state": "FINISHED", "datetime": "2026-06-14T02:00:00"},
}


def test_parse_pool_basic():
    pool = NASPoller._parse_pool(_POOL_RAW, _SCRUB_TASKS)
    assert pool["name"] == "tank"
    assert pool["status"] == "ONLINE"
    assert pool["capacity_total_bytes"] == 4000000000000
    assert pool["capacity_used_bytes"] == 2000000000000
    assert pool["last_scrub"]["errors"] == 0
    assert pool["last_scrub"]["status"] == "FINISHED"
    assert pool["next_scrub"] is not None  # cron produced a date


def test_parse_pool_null_scan():
    raw = dict(_POOL_RAW)
    raw["scan"] = None
    pool = NASPoller._parse_pool(raw, [])
    assert pool["last_scrub"]["status"] is None
    assert pool["last_scrub"]["errors"] == 0


def test_parse_vdevs_mirror():
    vdevs = NASPoller._parse_vdevs(_POOL_RAW["topology"]["data"])
    assert len(vdevs) == 1
    assert vdevs[0]["type"] == "MIRROR"
    assert len(vdevs[0]["disks"]) == 2
    assert vdevs[0]["disks"][0]["name"] == "ada0"


def test_parse_replication_basic():
    task = NASPoller._parse_replication(_REP_RAW)
    assert task["id"] == 1
    assert task["name"] == "tank → backup"
    assert task["last_state"] == "FINISHED"
    assert task["last_run"] == "2026-06-14T02:00:00"


def test_next_cron_run_monthly():
    # dom=1 means 1st of each month — result must be in the future
    from datetime import datetime, timezone
    result = NASPoller._next_cron_run("0", "0", "1", "*", "*")
    assert result is not None
    dt = datetime.fromisoformat(result.rstrip("Z"))
    assert dt > datetime.now(tz=timezone.utc).replace(tzinfo=None)
```

- [ ] **Step 2: Run tests to verify they fail with ImportError**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py -k "nas or parse_pool or parse_replication or next_cron" -v 2>&1 | tail -20
```

Expected: `ImportError: cannot import name 'NASPoller'`

- [ ] **Step 3: Add NASPoller class to monitor.py**

Insert after line ~2366 (after `_send_alert_async` function, before the `# Wake-on-LAN` comment block):

```python
# ============================================================================
# NAS Poller (TrueNAS REST API)
# ============================================================================

class NASPoller:
    POLL_INTERVAL_SECONDS = 900    # 15 minutes
    REPLICATION_STALE_HOURS = 25   # grace window for daily replication tasks

    def __init__(self, auth_manager, alert_settings=None, alert_port=None):
        self._auth_manager = auth_manager
        self._alert_settings = alert_settings or {}
        self._alert_port = alert_port
        self._cache = {
            "reachable": False,
            "last_updated": None,
            "error": None,
            "pools": [],
            "replication_tasks": [],
        }
        self._lock = threading.Lock()
        self._alert_state = {}  # condition_id -> bool, True = currently alerting

    def get_cache(self):
        with self._lock:
            import copy
            return copy.copy(self._cache)

    def _get_config(self):
        data = self._auth_manager.data if self._auth_manager else {}
        return data.get("truenas_url", ""), data.get("truenas_api_key", "")

    @staticmethod
    def _fetch(url, api_key, path):
        import urllib.request
        req = urllib.request.Request(
            url.rstrip("/") + path,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())

    @staticmethod
    def _parse_vdevs(data_vdevs):
        vdevs = []
        for v in data_vdevs:
            vdev = {
                "type": v.get("type", "DISK"),
                "name": v.get("name", ""),
                "status": v.get("status", "UNKNOWN"),
                "disks": [],
            }
            for child in v.get("children", []):
                vdev["disks"].append({
                    "name": child.get("disk") or child.get("name", ""),
                    "status": child.get("status", "UNKNOWN"),
                })
            if not vdev["disks"] and v.get("disk"):
                vdev["disks"].append({"name": v["disk"], "status": v.get("status", "UNKNOWN")})
            vdevs.append(vdev)
        return vdevs

    @staticmethod
    def _parse_scrub(scan):
        if not scan:
            return {"status": None, "end_time": None, "errors": 0}
        end_raw = scan.get("end_time")
        end_str = None
        if isinstance(end_raw, str):
            end_str = end_raw
        elif isinstance(end_raw, dict):
            ms = end_raw.get("$date", {})
            if isinstance(ms, dict):
                ms = ms.get("$numberLong")
            if ms:
                from datetime import timezone
                end_str = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
        return {"status": scan.get("state"), "end_time": end_str, "errors": scan.get("errors", 0)}

    @staticmethod
    def _next_cron_run(minute, hour, dom, month, dow):
        """Return next ISO datetime string for a simple cron expression (no ranges/steps/lists)."""
        from datetime import timedelta, timezone
        now = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
        t = now + timedelta(minutes=1)
        for _ in range(366 * 24 * 60):
            if (month == "*" or t.month == int(month)) and \
               (dom == "*" or t.day == int(dom)) and \
               (dow == "*" or t.weekday() == int(dow) % 7) and \
               (hour == "*" or t.hour == int(hour)) and \
               (minute == "*" or t.minute == int(minute)):
                return t.isoformat()
            t += timedelta(minutes=1)
        return None

    @classmethod
    def _parse_pool(cls, raw, scrub_tasks):
        name = raw.get("name", "")
        next_scrub = None
        for st in scrub_tasks:
            if st.get("pool") == name:
                sch = st.get("schedule", {})
                next_scrub = cls._next_cron_run(
                    sch.get("minute", "*"), sch.get("hour", "*"),
                    sch.get("dom", "*"), sch.get("month", "*"), sch.get("dow", "*"),
                )
                break
        return {
            "name": name,
            "status": raw.get("status", "UNKNOWN"),
            "capacity_used_bytes": raw.get("allocated", 0),
            "capacity_total_bytes": raw.get("size", 0),
            "vdevs": cls._parse_vdevs(raw.get("topology", {}).get("data", [])),
            "last_scrub": cls._parse_scrub(raw.get("scan")),
            "next_scrub": next_scrub,
        }

    @staticmethod
    def _parse_replication(raw):
        state = raw.get("state") or {}
        dt_raw = state.get("datetime") or state.get("time_finished")
        last_run = None
        if isinstance(dt_raw, str):
            last_run = dt_raw
        elif isinstance(dt_raw, dict):
            last_run = dt_raw.get("$date") or dt_raw.get("$numberLong")
        return {
            "id": raw.get("id"),
            "name": raw.get("name", ""),
            "last_run": last_run,
            "last_state": state.get("state") or "UNKNOWN",
        }
```

- [ ] **Step 4: Run tests — expect them to pass**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py -k "nas or parse_pool or parse_replication or next_cron" -v 2>&1 | tail -20
```

Expected: all 5 new tests PASS

- [ ] **Step 5: Commit**

```bash
cd /home/mgipson/netwatch && git add monitor.py tests/test_netwatch.py && git commit -m "feat: add NASPoller class with parse methods and cache"
```

---

## Task 2: NASPoller polling loop and alert logic

**Files:**
- Modify: `monitor.py` (add methods to `NASPoller`)
- Modify: `tests/test_netwatch.py`

- [ ] **Step 1: Write failing tests for alert de-dupe and poll loop**

Add to `tests/test_netwatch.py`:

```python
from unittest.mock import patch, MagicMock

def _make_nas_poller():
    am = MagicMock()
    am.data = {"truenas_url": "http://truenas.test", "truenas_api_key": "testkey"}
    return NASPoller(am, alert_settings={}, alert_port=8080)


def test_alert_fires_once_on_repeated_degraded():
    poller = _make_nas_poller()
    pools = [{"name": "tank", "status": "DEGRADED", "last_scrub": {"errors": 0}, "next_scrub": None}]
    with patch("monitor._send_alert_async") as mock_send:
        poller._check_alerts(pools, [])
        poller._check_alerts(pools, [])
    assert mock_send.call_count == 1  # fired once, not twice


def test_alert_rearmed_after_clear():
    poller = _make_nas_poller()
    pools_degraded = [{"name": "tank", "status": "DEGRADED", "last_scrub": {"errors": 0}, "next_scrub": None}]
    pools_ok = [{"name": "tank", "status": "ONLINE", "last_scrub": {"errors": 0}, "next_scrub": None}]
    with patch("monitor._send_alert_async") as mock_send:
        poller._check_alerts(pools_degraded, [])  # fires
        poller._check_alerts(pools_ok, [])         # clears
        poller._check_alerts(pools_degraded, [])  # re-arms → fires again
    assert mock_send.call_count == 2


def test_replication_stale_alert():
    poller = _make_nas_poller()
    # last_run is 30 hours ago
    from datetime import datetime, timezone, timedelta
    old_run = (datetime.now(tz=timezone.utc) - timedelta(hours=30)).isoformat()
    tasks = [{"id": 1, "name": "tank→backup", "last_run": old_run, "last_state": "FINISHED"}]
    with patch("monitor._send_alert_async") as mock_send:
        poller._check_alerts([], tasks)
    assert mock_send.call_count == 1


def test_get_cache_returns_copy():
    poller = _make_nas_poller()
    cache1 = poller.get_cache()
    cache1["reachable"] = True
    cache2 = poller.get_cache()
    assert cache2["reachable"] is False  # mutation of copy didn't affect internal cache


def test_poll_skipped_when_unconfigured():
    am = MagicMock()
    am.data = {}  # no truenas_url or api_key
    poller = NASPoller(am)
    poller._poll()
    assert poller.get_cache()["reachable"] is False
    assert poller.get_cache()["error"] == "NAS not configured"
```

- [ ] **Step 2: Run tests — expect them to fail**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py -k "alert or stale or cache_returns or poll_skipped" -v 2>&1 | tail -20
```

Expected: `AttributeError: 'NASPoller' object has no attribute '_check_alerts'`

- [ ] **Step 3: Add poll loop and alert methods to NASPoller**

Add these methods inside the `NASPoller` class, after `_parse_replication`:

```python
    def start(self, stop_event):
        t = threading.Thread(target=self._loop, args=(stop_event,), daemon=True, name="nas-poller")
        t.start()
        return t

    def _loop(self, stop_event):
        self._poll()
        while not stop_event.is_set():
            stop_event.wait(timeout=self.POLL_INTERVAL_SECONDS)
            if not stop_event.is_set():
                self._poll()

    def _poll(self):
        url, api_key = self._get_config()
        if not url or not api_key:
            with self._lock:
                self._cache.update({"reachable": False, "error": "NAS not configured"})
            return
        try:
            pools_raw = self._fetch(url, api_key, "/api/v2.0/pool")
            scrub_tasks = self._fetch(url, api_key, "/api/v2.0/pool/scrub")
            replication_raw = self._fetch(url, api_key, "/api/v2.0/replication")
            pools = [self._parse_pool(p, scrub_tasks) for p in pools_raw]
            tasks = [self._parse_replication(r) for r in replication_raw]
            with self._lock:
                self._cache = {
                    "reachable": True,
                    "last_updated": datetime.utcnow().isoformat(),
                    "error": None,
                    "pools": pools,
                    "replication_tasks": tasks,
                }
            self._check_alerts(pools, tasks)
        except Exception as e:
            logging.warning(f"NASPoller: poll failed: {e}")
            with self._lock:
                self._cache["reachable"] = False

    def _fire_alert(self, condition_id, title, message):
        if not self._alert_state.get(condition_id, False):
            self._alert_state[condition_id] = True
            click_url = _get_dashboard_url(self._alert_settings, self._alert_port or 8080)
            _send_alert_async(
                self._alert_settings, title, message,
                priority="high", tags="warning", click_url=click_url,
            )

    def _clear_alert(self, condition_id):
        self._alert_state[condition_id] = False

    def _check_alerts(self, pools, tasks):
        from datetime import timezone, timedelta
        for pool in pools:
            cid = f"pool_health_{pool['name']}"
            if pool["status"] != "ONLINE":
                self._fire_alert(cid, "Netwatch · NAS Alert",
                                 f"Pool \"{pool['name']}\" is {pool['status']}")
            else:
                self._clear_alert(cid)
            cid_scrub = f"scrub_errors_{pool['name']}"
            errors = (pool.get("last_scrub") or {}).get("errors", 0) or 0
            if int(errors) > 0:
                self._fire_alert(cid_scrub, "Netwatch · NAS Alert",
                                 f"Scrub on \"{pool['name']}\" found {errors} error(s)")
            else:
                self._clear_alert(cid_scrub)

        now = datetime.now(tz=timezone.utc)
        stale_delta = timedelta(hours=self.REPLICATION_STALE_HOURS)
        for task in tasks:
            cid = f"replication_{task['id']}"
            ok_states = ("SUCCESS", "FINISHED", "PENDING")
            failed = task["last_state"] not in ok_states
            stale = False
            last = None
            if task.get("last_run"):
                try:
                    last = datetime.fromisoformat(task["last_run"].rstrip("Z"))
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    stale = (now - last) > stale_delta
                except (ValueError, TypeError):
                    pass
            if failed or stale:
                hours_old = int((now - last).total_seconds() // 3600) if last else 0
                reason = "failed" if failed else f"stale ({hours_old}h)"
                self._fire_alert(cid, "Netwatch · NAS Alert",
                                 f"Replication \"{task['name']}\" {reason}")
            else:
                self._clear_alert(cid)
```

- [ ] **Step 4: Run tests — expect them to pass**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py -k "alert or stale or cache_returns or poll_skipped" -v 2>&1 | tail -20
```

Expected: all 5 new tests PASS

- [ ] **Step 5: Run full test suite to check for regressions**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/ -v 2>&1 | tail -30
```

Expected: all tests PASS

- [ ] **Step 6: Commit**

```bash
cd /home/mgipson/netwatch && git add monitor.py tests/test_netwatch.py && git commit -m "feat: add NASPoller poll loop and ntfy alert de-dupe logic"
```

---

## Task 3: `/api/nas` endpoint and server wiring

**Files:**
- Modify: `monitor.py` (handler function, route, `make_handler` signature, `start_web_server` signature, `run()`)
- Modify: `tests/test_netwatch.py`

- [ ] **Step 1: Write failing tests for _h_get_nas**

Add to `tests/test_netwatch.py`:

```python
from monitor import _h_get_nas

def test_h_get_nas_when_poller_is_none():
    status, body = _h_get_nas(None)
    assert status == 503
    assert body["reachable"] is False


def test_h_get_nas_returns_cache():
    poller = _make_nas_poller()
    # Manually inject a known cache state
    with poller._lock:
        poller._cache = {
            "reachable": True,
            "last_updated": "2026-06-14T02:00:00",
            "error": None,
            "pools": [{"name": "tank", "status": "ONLINE"}],
            "replication_tasks": [],
        }
    status, body = _h_get_nas(poller)
    assert status == 200
    assert body["reachable"] is True
    assert body["pools"][0]["name"] == "tank"
```

- [ ] **Step 2: Run tests — expect ImportError**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py -k "h_get_nas" -v 2>&1 | tail -10
```

Expected: `ImportError: cannot import name '_h_get_nas'`

- [ ] **Step 3: Add `_h_get_nas` handler function to monitor.py**

Insert near the other `_h_get_*` functions (around line 2930, after `_h_get_pi_health`):

```python
def _h_get_nas(nas_poller) -> tuple:
    if nas_poller is None:
        return 503, {"reachable": False, "error": "NAS poller not available"}
    return 200, nas_poller.get_cache()
```

- [ ] **Step 4: Run tests — expect them to pass**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py -k "h_get_nas" -v 2>&1 | tail -10
```

Expected: both tests PASS

- [ ] **Step 5: Add `nas_poller` parameter to `make_handler` and wire the route**

Find `make_handler` at line ~3251. Change the signature from:

```python
def make_handler(host_manager, settings, config_path, incident_log=None, auth_manager=None, inventory_db=None, dashboard_html="", history_db=None):
```

to:

```python
def make_handler(host_manager, settings, config_path, incident_log=None, auth_manager=None, inventory_db=None, dashboard_html="", history_db=None, nas_poller=None):
```

Then find the `/api/pi-health` route block (around line 3374):

```python
            if self.path == "/api/pi-health":
                if not self._require_auth(): return
                self._send_json(*_h_get_pi_health())
                return
```

Add immediately after it:

```python
            if self.path == "/api/nas":
                if not self._require_auth(): return
                self._send_json(*_h_get_nas(nas_poller))
                return
```

- [ ] **Step 6: Add `nas_poller` parameter to `start_web_server` and pass it through**

Find `start_web_server` at line ~3679. Change:

```python
def start_web_server(host_manager, settings, config_path, port, stop_event, incident_log=None, auth_manager=None, inventory_db=None, dashboard_html="", history_db=None):
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(host_manager, settings, config_path, incident_log, auth_manager, inventory_db, dashboard_html, history_db))
```

to:

```python
def start_web_server(host_manager, settings, config_path, port, stop_event, incident_log=None, auth_manager=None, inventory_db=None, dashboard_html="", history_db=None, nas_poller=None):
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(host_manager, settings, config_path, incident_log, auth_manager, inventory_db, dashboard_html, history_db, nas_poller=nas_poller))
```

- [ ] **Step 7: Create and start NASPoller in `run()`, pass to `start_web_server`**

In `run()`, find the block that starts `if not args.no_web:` (around line 3962). Just before it, add:

```python
    nas_poller = NASPoller(auth_manager, alert_settings=settings, alert_port=args.port)
    nas_poller.start(stop_event)
    print(f"[netwatch] NAS poller -> polling TrueNAS every {NASPoller.POLL_INTERVAL_SECONDS}s")
```

Then update the `start_web_server` call (around line 3964) to pass `nas_poller`:

```python
        wt = threading.Thread(
            target=start_web_server,
            args=(host_manager, web_settings, config_path, args.port, stop_event, incident_log, auth_manager, inventory_db, dashboard_html, history_db),
            kwargs={"nas_poller": nas_poller},
            daemon=True
        )
```

- [ ] **Step 8: Run full test suite**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/ -v 2>&1 | tail -30
```

Expected: all tests PASS

- [ ] **Step 9: Commit**

```bash
cd /home/mgipson/netwatch && git add monitor.py tests/test_netwatch.py && git commit -m "feat: add /api/nas endpoint and wire NASPoller into server startup"
```

---

## Task 4: Storage tab HTML, CSS, and JS

**Files:**
- Modify: `dashboard.html`
- Create: `static/nas.js`
- Modify: `static/main.css`
- Modify: `static/core.js`

- [ ] **Step 1: Add the Storage tab button to `dashboard.html`**

Find the tab bar (line ~222):

```html
  <div class="tabs" role="tablist" aria-label="Views">
    <button class="tab" data-tab="topology" role="tab">Topology</button>
    <button class="tab" data-tab="hosts" role="tab">Hosts</button>
    <button class="tab" data-tab="events" role="tab">Events <span class="tab-count" id="events-count" style="display:none">0</span></button>
    <button class="tab" data-tab="inventory" role="tab">Inventory <span class="tab-count" id="inv-count" style="display:none">0</span></button>
  </div>
```

Change to:

```html
  <div class="tabs" role="tablist" aria-label="Views">
    <button class="tab" data-tab="topology" role="tab">Topology</button>
    <button class="tab" data-tab="hosts" role="tab">Hosts</button>
    <button class="tab" data-tab="events" role="tab">Events <span class="tab-count" id="events-count" style="display:none">0</span></button>
    <button class="tab" data-tab="inventory" role="tab">Inventory <span class="tab-count" id="inv-count" style="display:none">0</span></button>
    <button class="tab" data-tab="storage" role="tab">Storage</button>
  </div>
```

- [ ] **Step 2: Add the Storage view div to `dashboard.html`**

Find the inventory view div (line ~368):

```html
  <div class="view" id="view-inventory">
```

After the closing `</div>` of the inventory view, add:

```html
  <div class="view" id="view-storage">
    <div id="nas-content"></div>
  </div>
```

- [ ] **Step 3: Add the nas.js script tag to `dashboard.html`**

Find the script tags near the end (line ~698):

```html
<script src="/static/utils.js?v={{VERSION}}"></script>
<script src="/static/core.js?v={{VERSION}}"></script>
<script src="/static/topology.js?v={{VERSION}}"></script>
<script src="/static/auth.js?v={{VERSION}}"></script>
<script src="/static/inventory.js?v={{VERSION}}"></script>
<script src="/static/ai-panel.js?v={{VERSION}}"></script>
```

Change to:

```html
<script src="/static/utils.js?v={{VERSION}}"></script>
<script src="/static/core.js?v={{VERSION}}"></script>
<script src="/static/topology.js?v={{VERSION}}"></script>
<script src="/static/auth.js?v={{VERSION}}"></script>
<script src="/static/inventory.js?v={{VERSION}}"></script>
<script src="/static/ai-panel.js?v={{VERSION}}"></script>
<script src="/static/nas.js?v={{VERSION}}"></script>
```

- [ ] **Step 4: Add NAS tab trigger to `setTab()` in `static/core.js`**

Find `setTab` in `core.js` (line ~49):

```javascript
function setTab(tab){
  document.querySelectorAll('.tab').forEach(t => {
    t.classList.toggle('active', t.dataset.tab === tab);
    t.setAttribute('aria-selected', t.dataset.tab === tab ? 'true' : 'false');
  });
  // Web-overlay metrics only apply when topology tab is active in web mode
  document.body.classList.toggle('nw-topo-web',
    tab === 'topology' && _topoView === 'web');
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + tab));
  localStorage.setItem('nw-tab', tab);
  if(tab === 'inventory' && typeof fetchInventory === 'function') fetchInventory();
}
```

Change the last line:

```javascript
  if(tab === 'inventory' && typeof fetchInventory === 'function') fetchInventory();
  if(tab === 'storage' && typeof fetchNas === 'function') fetchNas();
```

- [ ] **Step 5: Add NAS CSS to `static/main.css`**

Append to `static/main.css`:

```css
/* NAS Storage tab */
.nas-action-bar{display:flex;align-items:center;gap:14px;margin-bottom:1.25rem}
.nas-meta{font-size:12px;color:var(--hint)}
.nas-warn{font-size:12px;color:var(--amber-text);background:var(--amber-bg);padding:3px 10px;border-radius:6px}
.nas-unavailable{padding:2rem;color:var(--muted);font-size:13px}
.nas-section-label{font-size:11px;font-weight:500;color:var(--hint);text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px}
.nas-metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:1.25rem}
.nas-metric{background:var(--subtle);border:1px solid var(--border-light);border-radius:8px;padding:14px 16px}
.nas-metric-label{font-size:12px;color:var(--muted);margin-bottom:6px}
.nas-metric-value{font-size:22px;font-weight:500;line-height:1}
.nas-metric-sub{font-size:11px;color:var(--hint);margin-top:4px}
.nas-status-ok{color:var(--green)}
.nas-status-err{color:var(--red)}
.nas-card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px 18px;margin-bottom:12px}
.nas-card-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.nas-card-title{font-size:14px;font-weight:500}
.nas-badge{font-size:11px;padding:3px 10px;border-radius:6px;font-weight:500}
.nas-badge-ok{background:var(--green-bg);color:var(--green-text)}
.nas-badge-warn{background:var(--amber-bg);color:var(--amber-text)}
.nas-badge-err{background:var(--red-bg);color:var(--red-text)}
.nas-vdev-row{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--border-light);font-size:13px}
.nas-vdev-row:last-child{border-bottom:none}
.nas-vdev-indent{padding-left:20px}
.nas-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.nas-dot-ok{background:var(--green)}
.nas-dot-err{background:var(--red)}
.nas-vdev-name{flex:1;color:var(--muted)}
.nas-rep-row{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border-light);font-size:13px}
.nas-rep-row:last-child{border-bottom:none}
.nas-rep-name{flex:1;font-weight:500}
.nas-rep-meta{font-size:11px;color:var(--hint);margin-right:8px}
.nas-muted{font-size:12px;color:var(--hint)}
.nas-refresh-btn{font-size:12px;padding:5px 12px}
@media(max-width:600px){.nas-metrics{grid-template-columns:repeat(2,1fr)}}
```

- [ ] **Step 6: Create `static/nas.js`**

Create `/home/mgipson/netwatch/static/nas.js` with this content:

```javascript
function fetchNas() {
  fetch('/api/nas')
    .then(r => r.json())
    .then(renderNas)
    .catch(() => {
      var el = document.getElementById('nas-content');
      if (el) el.innerHTML = '<div class="nas-unavailable">Could not reach Netwatch server.</div>';
    });
}

function renderNas(data) {
  var el = document.getElementById('nas-content');
  if (!el) return;

  if (!data.reachable && !(data.pools && data.pools.length)) {
    el.innerHTML = renderNasUnavailable(data);
    return;
  }

  var html = renderNasActionBar(data);
  (data.pools || []).forEach(function(pool) { html += renderPoolSection(pool); });
  if (data.replication_tasks && data.replication_tasks.length) {
    html += renderReplicationSection(data.replication_tasks);
  }
  el.innerHTML = html;
}

function renderNasUnavailable(data) {
  var msg = data.error === 'NAS not configured'
    ? 'TrueNAS is not configured. Add <code>truenas_url</code> and <code>truenas_api_key</code> to <code>auth.json</code>.'
    : 'TrueNAS is unreachable. Check connection and API key.';
  return '<div class="nas-unavailable">' + msg + '</div>';
}

function renderNasActionBar(data) {
  var ago = data.last_updated ? nasTimeAgo(new Date(data.last_updated)) : 'never';
  var info = data.reachable
    ? '<span class="nas-meta">Last updated ' + ago + ' \xB7 polls every 15 min</span>'
    : '<span class="nas-warn">TrueNAS unreachable \xB7 last data ' + ago + '</span>';
  return '<div class="nas-action-bar"><button class="btn nas-refresh-btn" onclick="fetchNas()">&#8635; Refresh now</button>' + info + '</div>';
}

function renderPoolSection(pool) {
  var used = pool.capacity_used_bytes || 0;
  var total = pool.capacity_total_bytes || 0;
  var pct = total ? Math.round(used / total * 100) : 0;
  var statusCls = pool.status === 'ONLINE' ? 'nas-status-ok' : 'nas-status-err';
  var scrub = pool.last_scrub || {};
  var scrubLabel = scrub.status === 'FINISHED' ? (scrub.errors ? 'Errors' : 'Clean') : (scrub.status || '—');
  var scrubCls = scrub.errors ? 'nas-status-err' : 'nas-status-ok';
  var scrubDate = scrub.end_time ? nasFmtDate(scrub.end_time) : '—';
  var nextScrub = pool.next_scrub ? nasFmtDate(pool.next_scrub) : '—';
  var nextDays = pool.next_scrub ? nasDaysAway(pool.next_scrub) : null;
  var nextSub = nextDays !== null ? nextDays + ' day' + (nextDays === 1 ? '' : 's') + ' away' : '';
  var badgeCls = pool.status === 'ONLINE' ? 'nas-badge-ok' : 'nas-badge-err';

  return '<div class="nas-section-label">Pool health</div>' +
    '<div class="nas-metrics">' +
      '<div class="nas-metric"><div class="nas-metric-label">Pool status</div>' +
        '<div class="nas-metric-value ' + statusCls + '">' + escapeHtml(pool.status) + '</div>' +
        '<div class="nas-metric-sub">' + escapeHtml(pool.name) + '</div></div>' +
      '<div class="nas-metric"><div class="nas-metric-label">Capacity used</div>' +
        '<div class="nas-metric-value">' + nasFmtBytes(used) + '</div>' +
        '<div class="nas-metric-sub">of ' + nasFmtBytes(total) + ' (' + pct + '%)</div></div>' +
      '<div class="nas-metric"><div class="nas-metric-label">Last scrub</div>' +
        '<div class="nas-metric-value ' + scrubCls + '">' + escapeHtml(scrubLabel) + '</div>' +
        '<div class="nas-metric-sub">' + scrubDate + ' \xB7 ' + (scrub.errors || 0) + ' error(s)</div></div>' +
      '<div class="nas-metric"><div class="nas-metric-label">Next scrub</div>' +
        '<div class="nas-metric-value">' + nextScrub + '</div>' +
        '<div class="nas-metric-sub">' + nextSub + '</div></div>' +
    '</div>' +
    '<div class="nas-card">' +
      '<div class="nas-card-hdr"><span class="nas-card-title">VDEV layout</span>' +
        '<span class="nas-badge ' + badgeCls + '">' + escapeHtml(pool.status) + '</span></div>' +
      renderVdevs(pool.vdevs || []) +
    '</div>';
}

function renderVdevs(vdevs) {
  if (!vdevs.length) return '<div class="nas-vdev-row"><span class="nas-muted">No VDEV data</span></div>';
  return vdevs.map(function(v) {
    var dotCls = v.status === 'ONLINE' ? 'nas-dot-ok' : 'nas-dot-err';
    var disks = (v.disks || []).map(function(d) {
      var dDot = d.status === 'ONLINE' ? 'nas-dot-ok' : 'nas-dot-err';
      return '<div class="nas-vdev-row nas-vdev-indent">' +
        '<span class="nas-dot ' + dDot + '"></span>' +
        '<span class="nas-vdev-name">' + escapeHtml(d.name) + '</span>' +
        '<span class="nas-muted">' + escapeHtml(d.status) + '</span></div>';
    }).join('');
    return '<div class="nas-vdev-row">' +
      '<span class="nas-dot ' + dotCls + '"></span>' +
      '<span class="nas-vdev-name">' + escapeHtml(v.type.toLowerCase()) + '-' + escapeHtml(v.name) + '</span>' +
      '<span class="nas-muted">' + escapeHtml(v.status) + '</span></div>' + disks;
  }).join('');
}

function renderReplicationSection(tasks) {
  var rows = tasks.map(function(t) {
    var badge = nasRepBadge(t);
    return '<div class="nas-rep-row">' +
      '<span class="nas-rep-name">' + escapeHtml(t.name) + '</span>' +
      '<span class="nas-rep-meta">Last run: ' + (t.last_run ? nasFmtDate(t.last_run) : '—') + '</span>' +
      '<span class="nas-badge ' + badge.cls + '">' + escapeHtml(badge.label) + '</span></div>';
  }).join('');
  return '<div class="nas-section-label" style="margin-top:1.5rem">Replication tasks</div>' +
    '<div class="nas-card">' + rows + '</div>';
}

function nasRepBadge(task) {
  var okStates = ['SUCCESS', 'FINISHED', 'PENDING'];
  if (task.last_state && okStates.indexOf(task.last_state) === -1) {
    return { label: 'Failed', cls: 'nas-badge-err' };
  }
  if (task.last_run) {
    var diffH = (Date.now() - new Date(task.last_run).getTime()) / 3600000;
    if (diffH > 25) return { label: 'Stale (' + Math.floor(diffH) + 'h)', cls: 'nas-badge-warn' };
  }
  return { label: 'Success', cls: 'nas-badge-ok' };
}

function nasFmtBytes(b) {
  if (!b) return '0 B';
  var units = ['B', 'KB', 'MB', 'GB', 'TB'];
  var i = 0;
  while (b >= 1024 && i < units.length - 1) { b /= 1024; i++; }
  return (i > 0 ? b.toFixed(1) : Math.round(b)) + ' ' + units[i];
}

function nasFmtDate(iso) {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  } catch(e) { return iso; }
}

function nasTimeAgo(date) {
  var diffMin = Math.round((Date.now() - date.getTime()) / 60000);
  if (diffMin < 1) return 'just now';
  if (diffMin < 60) return diffMin + ' min ago';
  return Math.round(diffMin / 60) + 'h ago';
}

function nasDaysAway(iso) {
  try {
    var diff = new Date(iso).getTime() - Date.now();
    return Math.max(0, Math.ceil(diff / 86400000));
  } catch(e) { return null; }
}
```

- [ ] **Step 7: Verify static file is served**

Restart netwatch and confirm the file is accessible:

```bash
sudo systemctl restart netwatch && sleep 2 && curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/static/nas.js
```

Expected: `200`

- [ ] **Step 8: Commit**

```bash
cd /home/mgipson/netwatch && git add dashboard.html static/nas.js static/main.css static/core.js && git commit -m "feat: add Storage tab UI with pool health metrics and replication status"
```

---

## Task 5: Configure TrueNAS API key and smoke test

**Files:**
- Modify: `auth.json` (manual step)

- [ ] **Step 1: Generate a TrueNAS API key**

1. Open TrueNAS web UI
2. Click your username (top right) → API Keys
3. Click Add, name it `netwatch`, click Generate
4. Copy the key (shown only once)

- [ ] **Step 2: Add TrueNAS config to auth.json**

```bash
# Find TrueNAS IP first
grep -i truenas /home/mgipson/netwatch/hosts.yaml || echo "check your known hosts"
```

Then edit `/home/mgipson/netwatch/auth.json` — add two keys alongside the existing `secret_key` and `users` keys:

```json
{
  "secret_key": "<existing value — do not change>",
  "users": { ... },
  "truenas_url": "http://<truenas-ip>",
  "truenas_api_key": "<paste key here>"
}
```

- [ ] **Step 3: Restart netwatch and verify the poller starts**

```bash
sudo systemctl restart netwatch && sleep 3 && journalctl -u netwatch -n 30 --no-pager
```

Expected: log line containing `[netwatch] NAS poller -> polling TrueNAS every 900s`

- [ ] **Step 4: Verify /api/nas returns data**

```bash
# Get a session cookie first (replace with your actual credentials)
COOKIE=$(curl -s -c - -X POST http://localhost:8080/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"YOUR_PASSWORD"}' | grep nw_session | awk '{print $NF}')
curl -s -b "nw_session=$COOKIE" http://localhost:8080/api/nas | python3 -m json.tool | head -30
```

Expected: JSON with `"reachable": true` and `"pools": [...]`

- [ ] **Step 5: Open dashboard in browser, click Storage tab**

Navigate to `http://192.168.6.90:8080`, log in, click Storage tab.

Expected: pool health metrics cards visible, VDEV layout card showing mirror, replication tasks listed with Success/Stale/Failed badges.

- [ ] **Step 6: Commit**

```bash
cd /home/mgipson/netwatch && git commit --allow-empty -m "chore: NAS storage page complete — TrueNAS API key configured"
```
