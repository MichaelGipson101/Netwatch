# Proxmox Integration & Servers Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Proxmox VE cluster monitoring with guest start/stop/reboot actions; rename the Storage tab to Servers with a pill toggle between Proxmox and TrueNAS panels; link inventory VM records to their Proxmox guest via a `proxmox_vmid` property.

**Architecture:** New `ProxmoxPoller` class (mirrors `NASPoller`) polls the PVE REST API every 60 s, caches cluster state, and fires ntfy alerts on node offline / unexpected guest stop / guest paused. Two new endpoints (`GET /api/proxmox`, `POST /api/proxmox/action`) expose the cache and proxy start/stop/reboot commands. Frontend lives in a new `static/proxmox.js` file; the existing `static/nas.js` TrueNAS panel is untouched.

**Tech Stack:** Python 3 (stdlib only — `urllib.request`, `ssl`, `threading`, `json`), SQLite (existing), pytest (existing), vanilla JS (no framework), CSS custom properties (existing design system).

**Spec:** `docs/superpowers/specs/2026-06-16-proxmox-servers-tab-design.md`

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `monitor.py` | Modify | `_AUTH_STORED_KEYS` extension, `ProxmoxPoller` class, `_h_get_proxmox`, `_h_post_proxmox_action`, wiring in `make_handler` / `start_web_server` / `main`, `INVENTORY_TYPE_PROPERTIES["vm"]` |
| `static/proxmox.js` | **Create** | All Proxmox tab JS: fetchProxmox, renderProxmox, node cards, guest table, action buttons, host linking, pill toggle |
| `dashboard.html` | Modify | Rename Storage → Servers tab, add pill toggle + panel structure, add `proxmox.js` script tag |
| `static/core.js` | Modify | Update `setTab` Storage → Servers trigger; expose `window.nwLastData` for host join |
| `static/inventory.js` | Modify | Add `proxmox_vmid` to `INVENTORY_TYPE_PROPERTIES.vm` editor fields |
| `static/main.css` | Modify | Pill toggle, node cards, guest table, badges, action buttons, host link dot |
| `tests/test_netwatch.py` | Modify | Tests for all new backend behavior |

---

## Task 1: Auth routing patch + Proxmox creds to `_AUTH_STORED_KEYS`

**Files:**
- Modify: `monitor.py:3118`
- Modify: `tests/test_netwatch.py` (append at end)

**Context:** The working tree already has uncommitted changes that add `_AUTH_STORED_KEYS` and route TrueNAS keys through `auth_manager`. This task extends those changes to also cover the four Proxmox credential keys, then commits everything together.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_netwatch.py`:

```python
# ============================================================================
# Auth routing — Proxmox credential keys
# ============================================================================

from monitor import _AUTH_STORED_KEYS, _h_get_settings, _h_post_settings


def _make_am_with_proxmox():
    am = MagicMock()
    am.data = {
        "proxmox_url": "https://pve.test:8006",
        "proxmox_user": "root@pam",
        "proxmox_token_id": "Netwatch",
        "proxmox_token_secret": "test-uuid",
    }
    am.lock = MagicMock()
    am.lock.__enter__ = MagicMock(return_value=None)
    am.lock.__exit__ = MagicMock(return_value=False)
    return am


def test_proxmox_keys_in_auth_stored_keys():
    for k in ("proxmox_url", "proxmox_user", "proxmox_token_id", "proxmox_token_secret"):
        assert k in _AUTH_STORED_KEYS, f"{k} not in _AUTH_STORED_KEYS"


def test_h_get_settings_reads_proxmox_creds_from_auth_manager():
    am = _make_am_with_proxmox()
    status, body = _h_get_settings({}, auth_manager=am)
    assert status == 200
    assert body["proxmox_url"] == "https://pve.test:8006"
    assert body["proxmox_token_secret"] == "test-uuid"


def test_h_post_settings_saves_proxmox_secret_to_auth_manager():
    import tempfile, os
    am = MagicMock()
    am.data = {}
    am.lock = MagicMock()
    am.lock.__enter__ = MagicMock(return_value=None)
    am.lock.__exit__ = MagicMock(return_value=False)
    am._save = MagicMock()
    with tempfile.TemporaryDirectory() as d:
        cfg = os.path.join(d, "hosts.yaml")
        status, body = _h_post_settings(
            {"proxmox_token_secret": "new-uuid"}, cfg, {}, auth_manager=am
        )
    assert am.data.get("proxmox_token_secret") == "new-uuid"
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py::test_proxmox_keys_in_auth_stored_keys tests/test_netwatch.py::test_h_get_settings_reads_proxmox_creds_from_auth_manager tests/test_netwatch.py::test_h_post_settings_saves_proxmox_secret_to_auth_manager -v
```

Expected: FAIL — `proxmox_url` not found in `_AUTH_STORED_KEYS`.

- [ ] **Step 3: Extend `_AUTH_STORED_KEYS`**

In `monitor.py`, replace line 3118:

```python
_AUTH_STORED_KEYS = {"truenas_url", "truenas_api_key"}
```

with:

```python
_AUTH_STORED_KEYS = {
    "truenas_url", "truenas_api_key",
    "proxmox_url", "proxmox_user", "proxmox_token_id", "proxmox_token_secret",
}
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py::test_proxmox_keys_in_auth_stored_keys tests/test_netwatch.py::test_h_get_settings_reads_proxmox_creds_from_auth_manager tests/test_netwatch.py::test_h_post_settings_saves_proxmox_secret_to_auth_manager -v
```

Expected: PASS (3 tests).

- [ ] **Step 5: Run full test suite**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py -v --tb=short 2>&1 | tail -20
```

Expected: all previously passing tests still pass.

- [ ] **Step 6: Commit**

```bash
cd /home/mgipson/netwatch && git add monitor.py tests/test_netwatch.py && git commit -m "feat: route Proxmox + TrueNAS credentials through auth_manager (auth.json)"
```

---

## Task 2: Add `proxmox_vmid` to VM inventory type properties

**Files:**
- Modify: `monitor.py:1404–1412` (`INVENTORY_TYPE_PROPERTIES["vm"]`)
- Modify: `static/inventory.js:504–510` (`INVENTORY_TYPE_PROPERTIES.vm`)
- Modify: `tests/test_netwatch.py` (append)

The `proxmox_vmid` is stored in the `properties` JSON blob alongside other VM-specific fields (`hypervisor`, `vcpu_count`, etc.). No database migration is required — the blob is already flexible. The existing inventory editor and API routes pick it up automatically.

- [ ] **Step 1: Write failing test**

Append to `tests/test_netwatch.py`:

```python
# ============================================================================
# proxmox_vmid in VM inventory properties
# ============================================================================

def test_proxmox_vmid_in_vm_type_properties():
    from monitor import INVENTORY_TYPE_PROPERTIES
    vm_keys = [p[0] for p in INVENTORY_TYPE_PROPERTIES.get("vm", [])]
    assert "proxmox_vmid" in vm_keys
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py::test_proxmox_vmid_in_vm_type_properties -v
```

Expected: FAIL.

- [ ] **Step 3: Add to `INVENTORY_TYPE_PROPERTIES["vm"]` in `monitor.py`**

In `monitor.py` around line 1404, the `"vm"` entry currently reads:

```python
    "vm": [
        # VMs use ALL the host fields (cpu, ram, os, etc. all top-level)
        # AND these VM-specific fields stored in the properties JSON.
        ("hypervisor",     "string", "Hypervisor (Proxmox/KVM/ESXi/etc.)"),
        ("vcpu_count",     "int",    "vCPU count"),
        ("ram_alloc_gb",   "int",    "Allocated RAM (GB)"),
        ("disk_alloc_gb",  "int",    "Allocated disk (GB)"),
        ("autostart",      "bool",   "Auto-starts with host"),
    ],
```

Replace with:

```python
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
```

- [ ] **Step 4: Run test to confirm pass**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py::test_proxmox_vmid_in_vm_type_properties -v
```

Expected: PASS.

- [ ] **Step 5: Add to `INVENTORY_TYPE_PROPERTIES.vm` in `static/inventory.js`**

In `static/inventory.js` around line 504, the `vm` entry currently reads:

```javascript
  vm: [
    {key:"hypervisor",    type:"string", label:"Hypervisor (Proxmox/KVM/ESXi/etc.)"},
    {key:"vcpu_count",    type:"int",    label:"vCPU count"},
    {key:"ram_alloc_gb",  type:"int",    label:"Allocated RAM (GB)"},
    {key:"disk_alloc_gb", type:"int",    label:"Allocated disk (GB)"},
    {key:"autostart",     type:"bool",   label:"Auto-starts with host"},
  ],
```

Replace with:

```javascript
  vm: [
    {key:"hypervisor",    type:"string", label:"Hypervisor (Proxmox/KVM/ESXi/etc.)"},
    {key:"vcpu_count",    type:"int",    label:"vCPU count"},
    {key:"ram_alloc_gb",  type:"int",    label:"Allocated RAM (GB)"},
    {key:"disk_alloc_gb", type:"int",    label:"Allocated disk (GB)"},
    {key:"autostart",     type:"bool",   label:"Auto-starts with host"},
    {key:"proxmox_vmid",  type:"int",    label:"Proxmox VMID"},
  ],
```

- [ ] **Step 6: Run full test suite**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py -v --tb=short 2>&1 | tail -10
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
cd /home/mgipson/netwatch && git add monitor.py static/inventory.js tests/test_netwatch.py && git commit -m "feat: add proxmox_vmid field to VM inventory type"
```

---

## Task 3: ProxmoxPoller — data fetch and transformation

**Files:**
- Modify: `monitor.py` — insert `ProxmoxPoller` class after `NASPoller` (after line ~2598, before the `# Wake-on-LAN` section)
- Modify: `tests/test_netwatch.py` (append)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_netwatch.py`:

```python
# ============================================================================
# ProxmoxPoller — data fetch and transformation
# ============================================================================

from monitor import ProxmoxPoller


def _make_proxmox_poller():
    am = MagicMock()
    am.data = {
        "proxmox_url": "https://pve.test:8006",
        "proxmox_user": "root@pam",
        "proxmox_token_id": "Netwatch",
        "proxmox_token_secret": "test-uuid",
    }
    return ProxmoxPoller(am, alert_settings={}, alert_port=8080)


_RAW_QEMU = {
    "vmid": 108, "name": "haos13.2", "status": "running",
    "cpu": 0.0243, "mem": 2046949088, "maxmem": 4294967296,
    "cpus": 2, "uptime": 3891377,
}
_RAW_LXC = {
    "vmid": 120, "name": "pihole", "type": "lxc", "status": "running",
    "cpu": 0.003, "mem": 20336640, "maxmem": 536870912,
    "cpus": 1, "uptime": 3891350,
}
_RAW_STOPPED = {
    "vmid": 100, "name": "windows-11", "status": "stopped",
    "cpu": 0, "mem": 0, "maxmem": 8589934592,
    "cpus": 4, "uptime": 0,
}
_RAW_NODE = {
    "node": "pve", "status": "online",
    "cpu": 0.1209, "mem": 11170574336, "maxmem": 16147808256,
    "uptime": 3891504,
}


def test_build_guest_qemu_type_inferred():
    poller = _make_proxmox_poller()
    g = poller._build_guest(_RAW_QEMU, "qemu")
    assert g["type"] == "qemu"
    assert g["vmid"] == 108
    assert g["name"] == "haos13.2"
    assert g["status"] == "running"


def test_build_guest_lxc_type_inferred():
    poller = _make_proxmox_poller()
    g = poller._build_guest(_RAW_LXC, "lxc")
    assert g["type"] == "lxc"


def test_build_guest_cpu_fraction_to_percent():
    poller = _make_proxmox_poller()
    g = poller._build_guest(_RAW_QEMU, "qemu")
    # 0.0243 * 100 = 2.4 (rounded to 1 decimal)
    assert g["cpu_percent"] == round(0.0243 * 100, 1)


def test_build_guest_stopped_has_zero_cpu():
    poller = _make_proxmox_poller()
    g = poller._build_guest(_RAW_STOPPED, "qemu")
    assert g["cpu_percent"] == 0.0
    assert g["mem_used_bytes"] == 0


def test_build_node_contains_guests():
    poller = _make_proxmox_poller()
    node = poller._build_node(_RAW_NODE, [_RAW_QEMU, _RAW_STOPPED], [_RAW_LXC])
    assert node["name"] == "pve"
    assert node["status"] == "online"
    assert len(node["guests"]) == 3
    # Guests sorted by vmid
    assert node["guests"][0]["vmid"] == 100
    assert node["guests"][1]["vmid"] == 108
    assert node["guests"][2]["vmid"] == 120


def test_build_node_cpu_fraction_to_percent():
    poller = _make_proxmox_poller()
    node = poller._build_node(_RAW_NODE, [], [])
    assert node["cpu_percent"] == round(0.1209 * 100, 1)


def test_get_cache_returns_deepcopy():
    poller = _make_proxmox_poller()
    c1 = poller.get_cache()
    c1["reachable"] = True
    c2 = poller.get_cache()
    assert c2["reachable"] is False  # mutation of copy didn't affect internal cache


def test_poll_skipped_when_unconfigured():
    am = MagicMock()
    am.data = {}
    poller = ProxmoxPoller(am)
    poller._poll()
    cache = poller.get_cache()
    assert cache["reachable"] is False
    assert cache["error"] == "Proxmox not configured"
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py -k "proxmox_poller or build_guest or build_node or poll_skipped_when_unconf" -v 2>&1 | tail -20
```

Expected: ImportError — `ProxmoxPoller` does not exist yet.

- [ ] **Step 3: Add `ProxmoxPoller` class to `monitor.py`**

Insert the following block immediately after the `NASPoller` class ends (after the `_clear_alert` / stale replication block, before the `# Wake-on-LAN` comment, around line 2598):

```python
# ============================================================================
# Proxmox Poller (Proxmox VE REST API)
# ============================================================================

class ProxmoxPoller:
    POLL_INTERVAL_SECONDS = 60

    def __init__(self, auth_manager, alert_settings=None, alert_port=None):
        self._auth_manager = auth_manager
        self._alert_settings = alert_settings or {}
        self._alert_port = alert_port
        self._cache = {
            "reachable": False,
            "last_updated": None,
            "error": None,
            "nodes": [],
        }
        self._lock = threading.Lock()
        self._alert_state = {}    # condition_id -> bool (True = currently alerting)
        self._exemptions = {}     # vmid (int) -> float timestamp (exempt until)

    def get_cache(self):
        with self._lock:
            import copy
            return copy.deepcopy(self._cache)

    def exempt_vmid(self, vmid, seconds=30):
        """Suppress unexpected-stop alert for vmid for the next N seconds."""
        self._exemptions[int(vmid)] = time.time() + seconds

    def _get_config(self):
        data = self._auth_manager.data if self._auth_manager else {}
        return (
            data.get("proxmox_url", ""),
            data.get("proxmox_user", ""),
            data.get("proxmox_token_id", ""),
            data.get("proxmox_token_secret", ""),
        )

    def _make_ssl_ctx(self):
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _fetch(self, base_url, user, token_id, token_secret, path):
        import urllib.request
        url = base_url.rstrip("/") + path
        token = f"{user}!{token_id}={token_secret}"
        req = urllib.request.Request(
            url, headers={"Authorization": f"PVEAPIToken={token}"}
        )
        with urllib.request.urlopen(req, context=self._make_ssl_ctx(), timeout=10) as r:
            return json.loads(r.read().decode())["data"]

    def _build_guest(self, raw, guest_type):
        return {
            "vmid":          raw.get("vmid"),
            "name":          raw.get("name", ""),
            "type":          guest_type,
            "status":        raw.get("status", "stopped"),
            "cpu_percent":   round((raw.get("cpu") or 0.0) * 100, 1),
            "mem_used_bytes":  raw.get("mem", 0),
            "mem_total_bytes": raw.get("maxmem", 0),
        }

    def _build_node(self, raw_node, qemu_list, lxc_list):
        guests = (
            [self._build_guest(g, "qemu") for g in qemu_list]
            + [self._build_guest(g, "lxc")  for g in lxc_list]
        )
        guests.sort(key=lambda g: g["vmid"] or 0)
        return {
            "name":            raw_node.get("node", ""),
            "status":          raw_node.get("status", "unknown"),
            "cpu_percent":     round((raw_node.get("cpu") or 0.0) * 100, 1),
            "mem_used_bytes":  raw_node.get("mem", 0),
            "mem_total_bytes": raw_node.get("maxmem", 0),
            "uptime_seconds":  raw_node.get("uptime", 0),
            "guests":          guests,
        }

    def _poll(self):
        url, user, token_id, token_secret = self._get_config()
        if not all([url, user, token_id, token_secret]):
            with self._lock:
                self._cache["error"] = "Proxmox not configured"
            return
        try:
            raw_nodes = self._fetch(url, user, token_id, token_secret, "/api2/json/nodes")
            nodes = []
            for raw in raw_nodes:
                name = raw.get("node", "")
                qemu = self._fetch(url, user, token_id, token_secret,
                                   f"/api2/json/nodes/{name}/qemu")
                lxc  = self._fetch(url, user, token_id, token_secret,
                                   f"/api2/json/nodes/{name}/lxc")
                nodes.append(self._build_node(raw, qemu, lxc))
            now_str = datetime.now().isoformat(timespec="seconds")
            with self._lock:
                prev_nodes = self._cache.get("nodes", [])
                self._cache.update({
                    "reachable":    True,
                    "last_updated": now_str,
                    "error":        None,
                    "nodes":        nodes,
                })
            self._check_alerts(nodes, prev_nodes)
        except Exception as e:
            logging.warning(f"ProxmoxPoller: poll failed: {e}")
            with self._lock:
                self._cache["reachable"] = False

    def _check_alerts(self, nodes, prev_nodes):
        pass  # implemented in Task 4

    def start(self, stop_event):
        def _loop():
            while not stop_event.is_set():
                try:
                    self._poll()
                except Exception as e:
                    logging.warning(f"ProxmoxPoller: unexpected error in loop: {e}")
                stop_event.wait(self.POLL_INTERVAL_SECONDS)
        threading.Thread(target=_loop, daemon=True, name="proxmox-poller").start()
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py -k "build_guest or build_node or get_cache_returns_deepcopy or poll_skipped_when_unconfigured" -v
```

Expected: PASS (8 tests).

- [ ] **Step 5: Run full test suite**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py --tb=short 2>&1 | tail -10
```

Expected: all passing.

- [ ] **Step 6: Commit**

```bash
cd /home/mgipson/netwatch && git add monitor.py tests/test_netwatch.py && git commit -m "feat: add ProxmoxPoller with data fetch and transformation"
```

---

## Task 4: ProxmoxPoller — alert logic and background thread

**Files:**
- Modify: `monitor.py` — add `_check_alerts`, `_fire_alert`, `_clear_alert` methods to `ProxmoxPoller`
- Modify: `tests/test_netwatch.py` (append)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_netwatch.py`:

```python
# ============================================================================
# ProxmoxPoller — alert logic
# ============================================================================

def _make_nodes(node_status="online", guest_status="running", vmid=108):
    return [{
        "name": "pve", "status": node_status,
        "cpu_percent": 1.0, "mem_used_bytes": 0, "mem_total_bytes": 0, "uptime_seconds": 0,
        "guests": [{"vmid": vmid, "name": "haos", "type": "qemu",
                    "status": guest_status, "cpu_percent": 0.0,
                    "mem_used_bytes": 0, "mem_total_bytes": 0}],
    }]


def test_pve_node_offline_alert_fires_once():
    poller = _make_proxmox_poller()
    nodes_offline = _make_nodes(node_status="offline")
    with patch("monitor._send_alert_async") as mock_send:
        poller._check_alerts(nodes_offline, [])
        poller._check_alerts(nodes_offline, [])
    assert mock_send.call_count == 1


def test_pve_node_alert_rearmed_after_clear():
    poller = _make_proxmox_poller()
    offline = _make_nodes(node_status="offline")
    online  = _make_nodes(node_status="online")
    with patch("monitor._send_alert_async") as mock_send:
        poller._check_alerts(offline, [])  # fires
        poller._check_alerts(online, [])   # clears
        poller._check_alerts(offline, [])  # re-arms → fires
    assert mock_send.call_count == 2


def test_pve_unexpected_stop_fires_alert():
    poller = _make_proxmox_poller()
    prev   = _make_nodes(guest_status="running")
    now    = _make_nodes(guest_status="stopped")
    with patch("monitor._send_alert_async") as mock_send:
        poller._check_alerts(now, prev)
    assert mock_send.call_count == 1
    args = mock_send.call_args[0]
    assert "stopped unexpectedly" in args[2]


def test_pve_exempted_stop_does_not_alert():
    poller = _make_proxmox_poller()
    poller.exempt_vmid(108, seconds=60)
    prev = _make_nodes(guest_status="running")
    now  = _make_nodes(guest_status="stopped")
    with patch("monitor._send_alert_async") as mock_send:
        poller._check_alerts(now, prev)
    assert mock_send.call_count == 0


def test_pve_paused_guest_fires_alert():
    poller = _make_proxmox_poller()
    nodes = _make_nodes(guest_status="paused")
    with patch("monitor._send_alert_async") as mock_send:
        poller._check_alerts(nodes, [])
    assert mock_send.call_count == 1
    args = mock_send.call_args[0]
    assert "paused" in args[2]
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py -k "pve_node_offline or pve_node_alert or pve_unexpected or pve_exempted or pve_paused" -v 2>&1 | tail -20
```

Expected: FAIL — `_check_alerts` not defined.

- [ ] **Step 3: Add alert methods to `ProxmoxPoller` in `monitor.py`**

Inside the `ProxmoxPoller` class, add these methods after `start`:

```python
    def _fire_alert(self, condition_id, message):
        if not self._alert_state.get(condition_id, False):
            self._alert_state[condition_id] = True
            click_url = _get_dashboard_url(self._alert_settings, self._alert_port or 8080)
            _send_alert_async(
                self._alert_settings, "Netwatch · Proxmox Alert", message,
                priority="high", tags="rotating_light", click_url=click_url,
            )

    def _clear_alert(self, condition_id):
        self._alert_state[condition_id] = False

    def _check_alerts(self, nodes, prev_nodes):
        now = time.time()
        # Build vmid → previous status map
        prev_states = {}
        for n in prev_nodes:
            for g in n.get("guests", []):
                prev_states[g["vmid"]] = g["status"]

        for node in nodes:
            name = node["name"]

            # Node offline
            cid_node = f"node:{name}"
            if node["status"] != "online":
                self._fire_alert(cid_node, f'Proxmox node "{name}" is offline')
            else:
                self._clear_alert(cid_node)

            for guest in node.get("guests", []):
                vmid   = guest["vmid"]
                gname  = guest["name"]
                status = guest["status"]

                # Unexpected stop (not caused by a Netwatch action)
                cid_stop = f"stop:{vmid}"
                if (prev_states.get(vmid) == "running"
                        and status == "stopped"
                        and now > self._exemptions.get(vmid, 0)):
                    self._fire_alert(cid_stop,
                                     f'VM "{gname}" ({vmid}) stopped unexpectedly on {name}')
                elif status == "running":
                    self._clear_alert(cid_stop)

                # Paused
                cid_pause = f"pause:{vmid}"
                if status == "paused":
                    self._fire_alert(cid_pause,
                                     f'VM "{gname}" ({vmid}) is paused on {name}')
                else:
                    self._clear_alert(cid_pause)
```

- [ ] **Step 4: Run alert tests to confirm they pass**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py -k "pve_node_offline or pve_node_alert or pve_unexpected or pve_exempted or pve_paused" -v
```

Expected: PASS (5 tests).

- [ ] **Step 5: Run full test suite**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py --tb=short 2>&1 | tail -10
```

Expected: all passing.

- [ ] **Step 6: Commit**

```bash
cd /home/mgipson/netwatch && git add monitor.py tests/test_netwatch.py && git commit -m "feat: add ProxmoxPoller alert logic and background poll loop"
```

---

## Task 5: `/api/proxmox` GET endpoint + startup wiring

**Files:**
- Modify: `monitor.py` — add `_h_get_proxmox` (after `_h_get_nas` at line ~3289), extend `make_handler` and `start_web_server` signatures, wire in `main()`
- Modify: `tests/test_netwatch.py` (append)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_netwatch.py`:

```python
# ============================================================================
# /api/proxmox handler
# ============================================================================

from monitor import _h_get_proxmox


def test_h_get_proxmox_when_poller_is_none():
    status, body = _h_get_proxmox(None)
    assert status == 503
    assert body["reachable"] is False


def test_h_get_proxmox_returns_cache():
    poller = _make_proxmox_poller()
    with poller._lock:
        poller._cache = {
            "reachable": True,
            "last_updated": "2026-06-16T14:00:00",
            "error": None,
            "nodes": [{"name": "pve"}],
        }
    status, body = _h_get_proxmox(poller)
    assert status == 200
    assert body["reachable"] is True
    assert body["nodes"][0]["name"] == "pve"
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py::test_h_get_proxmox_when_poller_is_none tests/test_netwatch.py::test_h_get_proxmox_returns_cache -v 2>&1 | tail -10
```

Expected: ImportError — `_h_get_proxmox` not defined.

- [ ] **Step 3: Add `_h_get_proxmox` to `monitor.py`**

Immediately after `_h_get_nas` (around line 3290), add:

```python
def _h_get_proxmox(proxmox_poller) -> tuple:
    if proxmox_poller is None:
        return 503, {"reachable": False, "error": "Proxmox poller not running"}
    return 200, proxmox_poller.get_cache()
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py::test_h_get_proxmox_when_poller_is_none tests/test_netwatch.py::test_h_get_proxmox_returns_cache -v
```

Expected: PASS.

- [ ] **Step 5: Add `proxmox_poller` to `make_handler` signature and register the endpoint**

In `monitor.py` at line 3610, `make_handler` signature currently reads:

```python
def make_handler(host_manager, settings, config_path, incident_log=None, auth_manager=None, inventory_db=None, dashboard_html="", history_db=None, nas_poller=None):
```

Replace with:

```python
def make_handler(host_manager, settings, config_path, incident_log=None, auth_manager=None, inventory_db=None, dashboard_html="", history_db=None, nas_poller=None, proxmox_poller=None):
```

In the GET handler block inside `make_handler`, directly after the `/api/nas` handler (around line 3743):

```python
            if self.path == "/api/nas":
                if not self._require_auth(): return
                self._send_json(*_h_get_nas(nas_poller))
                return
```

Add immediately after:

```python
            if self.path == "/api/proxmox":
                if not self._require_auth(): return
                self._send_json(*_h_get_proxmox(proxmox_poller))
                return
```

- [ ] **Step 6: Update `start_web_server` and `main()` to pass `proxmox_poller`**

At line 4053, `start_web_server` currently reads:

```python
def start_web_server(host_manager, settings, config_path, port, stop_event, incident_log=None, auth_manager=None, inventory_db=None, dashboard_html="", history_db=None, nas_poller=None):
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(host_manager, settings, config_path, incident_log, auth_manager, inventory_db, dashboard_html, history_db, nas_poller=nas_poller))
```

Replace with:

```python
def start_web_server(host_manager, settings, config_path, port, stop_event, incident_log=None, auth_manager=None, inventory_db=None, dashboard_html="", history_db=None, nas_poller=None, proxmox_poller=None):
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(host_manager, settings, config_path, incident_log, auth_manager, inventory_db, dashboard_html, history_db, nas_poller=nas_poller, proxmox_poller=proxmox_poller))
```

In `main()`, after the `nas_poller` block (around line 4340), add:

```python
    proxmox_poller = ProxmoxPoller(auth_manager, alert_settings=settings, alert_port=args.port)
    _pve_url, _, _, _ = proxmox_poller._get_config()
    if _pve_url:
        proxmox_poller.start(stop_event)
        print(f"[netwatch] Proxmox poller -> polling cluster every {ProxmoxPoller.POLL_INTERVAL_SECONDS}s")
```

And update the `start_web_server` kwargs at line ~4347:

```python
            kwargs={"nas_poller": nas_poller, "proxmox_poller": proxmox_poller},
```

- [ ] **Step 7: Run full test suite**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py --tb=short 2>&1 | tail -10
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
cd /home/mgipson/netwatch && git add monitor.py tests/test_netwatch.py && git commit -m "feat: add /api/proxmox endpoint and wire ProxmoxPoller into server startup"
```

---

## Task 6: `/api/proxmox/action` POST endpoint

**Files:**
- Modify: `monitor.py` — add `_h_post_proxmox_action`, register in `make_handler` (POST branch, admin-only)
- Modify: `tests/test_netwatch.py` (append)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_netwatch.py`:

```python
# ============================================================================
# /api/proxmox/action handler
# ============================================================================

from monitor import _h_post_proxmox_action


def _make_auth_manager_with_pve():
    am = MagicMock()
    am.data = {
        "proxmox_url": "https://pve.test:8006",
        "proxmox_user": "root@pam",
        "proxmox_token_id": "Netwatch",
        "proxmox_token_secret": "test-uuid",
    }
    return am


def test_pve_action_missing_node_returns_400():
    am = _make_auth_manager_with_pve()
    status, body = _h_post_proxmox_action(
        {"vmid": 108, "type": "qemu", "action": "stop"}, None, am
    )
    assert status == 400


def test_pve_action_invalid_action_returns_400():
    am = _make_auth_manager_with_pve()
    status, body = _h_post_proxmox_action(
        {"node": "pve", "vmid": 108, "type": "qemu", "action": "destroy"}, None, am
    )
    assert status == 400


def test_pve_action_invalid_type_returns_400():
    am = _make_auth_manager_with_pve()
    status, body = _h_post_proxmox_action(
        {"node": "pve", "vmid": 108, "type": "openvz", "action": "stop"}, None, am
    )
    assert status == 400


def test_pve_action_stop_exempts_vmid():
    import time
    am = _make_auth_manager_with_pve()
    poller = _make_proxmox_poller()
    with patch("urllib.request.urlopen") as mock_open:
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b'{"data": "UPID:pve:..."}'
        mock_open.return_value = mock_resp
        status, body = _h_post_proxmox_action(
            {"node": "pve", "vmid": 108, "type": "qemu", "action": "stop"},
            poller, am
        )
    # Exemption should now be set
    assert poller._exemptions.get(108, 0) > time.time()


def test_pve_action_unconfigured_returns_503():
    am = MagicMock()
    am.data = {}
    status, body = _h_post_proxmox_action(
        {"node": "pve", "vmid": 108, "type": "qemu", "action": "stop"}, None, am
    )
    assert status == 503
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py -k "pve_action" -v 2>&1 | tail -15
```

Expected: ImportError — `_h_post_proxmox_action` not defined.

- [ ] **Step 3: Add `_h_post_proxmox_action` to `monitor.py`**

Immediately after `_h_get_proxmox`, add:

```python
def _h_post_proxmox_action(data, proxmox_poller, auth_manager) -> tuple:
    import ssl, urllib.request, urllib.error as _urlerr
    node    = (data.get("node") or "").strip()
    vmid    = data.get("vmid")
    gtype   = (data.get("type") or "").strip()
    action  = (data.get("action") or "").strip()

    if not node or not vmid or gtype not in ("qemu", "lxc") \
            or action not in ("start", "stop", "reboot"):
        return 400, {"error": "Required: node, vmid, type (qemu/lxc), action (start/stop/reboot)"}

    try:
        vmid = int(vmid)
    except (TypeError, ValueError):
        return 400, {"error": "vmid must be an integer"}

    if action in ("stop", "reboot") and proxmox_poller:
        proxmox_poller.exempt_vmid(vmid, 30)

    auth_data     = auth_manager.data if auth_manager else {}
    base_url      = auth_data.get("proxmox_url", "")
    user          = auth_data.get("proxmox_user", "")
    token_id      = auth_data.get("proxmox_token_id", "")
    token_secret  = auth_data.get("proxmox_token_secret", "")

    if not all([base_url, user, token_id, token_secret]):
        return 503, {"error": "Proxmox not configured"}

    url = (f"{base_url.rstrip('/')}/api2/json/nodes"
           f"/{node}/{gtype}/{vmid}/status/{action}")
    token = f"{user}!{token_id}={token_secret}"
    req = urllib.request.Request(
        url, data=b"", method="POST",
        headers={"Authorization": f"PVEAPIToken={token}"},
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10):
            return 200, {"ok": True}
    except _urlerr.HTTPError as e:
        body = e.read().decode(errors="replace")
        return e.code, {"error": body}
    except Exception as e:
        return 500, {"error": str(e)}
```

- [ ] **Step 4: Register the action endpoint in `make_handler`**

In the POST handler block inside `make_handler`, after the existing `/api/nas` POST handler area (look for the block that handles `self.command == "POST"`), add:

```python
            if self.path == "/api/proxmox/action":
                if not self._require_auth(admin_only=True): return
                data, err = self._read_json_body()
                if err: return
                self._send_json(*_h_post_proxmox_action(data, proxmox_poller, auth_manager))
                return
```

- [ ] **Step 5: Run action tests**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py -k "pve_action" -v
```

Expected: PASS (5 tests).

- [ ] **Step 6: Run full test suite**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py --tb=short 2>&1 | tail -10
```

Expected: all passing.

- [ ] **Step 7: Commit**

```bash
cd /home/mgipson/netwatch && git add monitor.py tests/test_netwatch.py && git commit -m "feat: add /api/proxmox/action POST endpoint for guest start/stop/reboot"
```

---

## Task 7: Rename Storage → Servers tab + pill toggle

**Files:**
- Modify: `dashboard.html:227` (tab button), `dashboard.html:391–393` (view div)
- Modify: `static/core.js:60` (`setTab` storage trigger)
- Modify: `static/core.js:323` (`refresh` function — expose `window.nwLastData`)
- Modify: `static/main.css` (append pill styles)

- [ ] **Step 1: Update `dashboard.html` tab button**

Find and replace (line 227):

```html
    <button class="tab" data-tab="storage" role="tab">Storage</button>
```

Replace with:

```html
    <button class="tab" data-tab="servers" role="tab">Servers</button>
```

- [ ] **Step 2: Replace the Storage view div with the Servers view**

Find and replace (lines 391–393):

```html
  <div class="view" id="view-storage">
    <div id="nas-content"></div>
  </div>
```

Replace with:

```html
  <div class="view" id="view-servers">
    <div class="servers-pills" role="tablist">
      <button class="servers-pill active" data-panel="proxmox"
              onclick="switchServersPanel('proxmox')" role="tab">Proxmox</button>
      <button class="servers-pill" data-panel="truenas"
              onclick="switchServersPanel('truenas')" role="tab">TrueNAS</button>
    </div>
    <div data-servers-panel="proxmox">
      <div id="proxmox-content"></div>
    </div>
    <div data-servers-panel="truenas" style="display:none">
      <div id="nas-content"></div>
    </div>
  </div>
```

- [ ] **Step 3: Update `core.js` `setTab` function**

Find in `static/core.js` (line 60):

```javascript
  if(tab === 'storage' && typeof fetchNas === 'function') fetchNas();
```

Replace with:

```javascript
  if(tab === 'servers' && typeof initServersTab === 'function') initServersTab();
```

- [ ] **Step 4: Expose `window.nwLastData` in `core.js` `refresh` function**

Find in `static/core.js` (around line 324):

```javascript
    const data = await res.json();
    lastData = data;
```

Replace with:

```javascript
    const data = await res.json();
    lastData = data;
    window.nwLastData = data;
```

- [ ] **Step 5: Add pill toggle styles to `static/main.css`**

Append to `static/main.css`:

```css
/* ── Servers tab pill toggle ─────────────────────────────────────────────── */
.servers-pills {
  display: flex;
  gap: 4px;
  padding: 12px 0 16px;
}
.servers-pill {
  padding: 5px 18px;
  border-radius: 20px;
  border: 1.5px solid var(--border);
  background: transparent;
  color: var(--text-muted);
  font: 500 13px/1 var(--font-sans);
  cursor: pointer;
  transition: background 0.15s, color 0.15s, border-color 0.15s;
}
.servers-pill:hover { background: var(--surface2); color: var(--text); }
.servers-pill.active {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}
```

- [ ] **Step 6: Verify the app still starts (no import errors)**

```bash
cd /home/mgipson/netwatch && python -c "import monitor; print('OK')"
```

Expected: `OK`.

- [ ] **Step 7: Run full test suite**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py --tb=short 2>&1 | tail -10
```

Expected: all passing.

- [ ] **Step 8: Commit**

```bash
cd /home/mgipson/netwatch && git add dashboard.html static/core.js static/main.css && git commit -m "feat: rename Storage tab to Servers with Proxmox/TrueNAS pill toggle"
```

---

## Task 8: `proxmox.js` — skeleton, action bar, and node cards

**Files:**
- Create: `static/proxmox.js`
- Modify: `dashboard.html:886` (add script tag after `nas.js`)
- Modify: `static/main.css` (append node card styles)

- [ ] **Step 1: Create `static/proxmox.js`**

```javascript
/* Proxmox VE panel — Servers tab */
(function () {
  'use strict';

  /* ── State ──────────────────────────────────────────────────────────────── */
  var _hostsVmidMap = {};  // proxmox_vmid (int) -> {name, is_up}

  /* ── Public API ─────────────────────────────────────────────────────────── */

  window.fetchProxmox = function () {
    fetch('/api/proxmox')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        _buildHostsMap();
        _renderProxmox(data);
      })
      .catch(function () {
        var el = document.getElementById('proxmox-content');
        if (el) el.innerHTML = '<div class="pve-unavailable">Could not reach Netwatch server.</div>';
      });
  };

  window.initServersTab = function () {
    var saved = localStorage.getItem('nw-servers-panel') || 'proxmox';
    window.switchServersPanel(saved, false);  // false = don't re-save
  };

  window.switchServersPanel = function (panel, save) {
    document.querySelectorAll('.servers-pill').forEach(function (btn) {
      btn.classList.toggle('active', btn.dataset.panel === panel);
    });
    document.querySelectorAll('[data-servers-panel]').forEach(function (div) {
      div.style.display = div.dataset.serversPanel === panel ? '' : 'none';
    });
    if (save !== false) localStorage.setItem('nw-servers-panel', panel);
    if (panel === 'proxmox') window.fetchProxmox();
    if (panel === 'truenas' && typeof fetchNas === 'function') fetchNas();
  };

  /* ── Hosts map (for Netwatch link dot) ─────────────────────────────────── */

  function _buildHostsMap () {
    _hostsVmidMap = {};
    var status = window.nwLastData;
    var hosts = (status && status.hosts) ? status.hosts : [];
    // We also need inventory to find proxmox_vmid mappings
    fetch('/api/inventory')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (inv) {
        if (!inv || !inv.items) return;
        inv.items.forEach(function (rec) {
          var vmid = rec.properties && rec.properties.proxmox_vmid;
          if (!vmid) return;
          vmid = parseInt(vmid, 10);
          if (isNaN(vmid)) return;
          // Look up live status by IP
          var ip = rec.ip;
          var host = hosts.find(function (h) { return h.ip === ip; });
          _hostsVmidMap[vmid] = {
            name:  rec.system,
            is_up: host ? !!host.is_up : null,
          };
        });
      })
      .catch(function () {});  // silent — link dots are non-critical
  }

  /* ── Render ─────────────────────────────────────────────────────────────── */

  function _renderProxmox (data) {
    var el = document.getElementById('proxmox-content');
    if (!el) return;
    if (!data.reachable && data.error === 'Proxmox not configured') {
      el.innerHTML = '<div class="pve-unavailable">Proxmox is not configured.'
        + ' Add credentials in <strong>Settings → Integrations</strong>.</div>';
      return;
    }
    el.innerHTML = _renderActionBar(data)
      + _renderNodeCards(data.nodes || [])
      + _renderGuestTable(data.nodes || []);
  }

  function _renderActionBar (data) {
    var ago  = data.last_updated ? _timeAgo(new Date(data.last_updated)) : 'never';
    var info = data.reachable
      ? '<span class="pve-meta">Last updated ' + ago + ' \xB7 polls every 60s</span>'
      : '<span class="pve-warn">Proxmox unreachable \xB7 last data ' + ago + '</span>';
    return '<div class="pve-action-bar">'
      + '<button class="btn pve-refresh-btn" onclick="fetchProxmox()">↻ Refresh now</button>'
      + info + '</div>';
  }

  function _renderNodeCards (nodes) {
    if (!nodes.length) return '';
    var cards = nodes.map(function (n) {
      var okCls    = n.status === 'online' ? 'pve-node-badge-ok' : 'pve-node-badge-err';
      var label    = n.status === 'online' ? 'ONLINE' : 'OFFLINE';
      var cpuPct   = n.cpu_percent.toFixed(1);
      var memPct   = n.mem_total_bytes
        ? Math.round(n.mem_used_bytes / n.mem_total_bytes * 100) : 0;
      var memUsed  = _fmtBytes(n.mem_used_bytes);
      var memTotal = _fmtBytes(n.mem_total_bytes);
      var uptime   = _fmtUptime(n.uptime_seconds);
      return '<div class="pve-node-card">'
        + '<div class="pve-node-name">' + escapeHtml(n.name) + '</div>'
        + '<div class="pve-node-badge ' + okCls + '">' + label + '</div>'
        + '<div class="pve-node-stat"><span class="pve-stat-lbl">CPU</span>'
        +   '<div class="pve-bar"><div class="pve-bar-fill" style="width:' + cpuPct + '%"></div></div>'
        +   '<span class="pve-stat-val">' + cpuPct + '%</span></div>'
        + '<div class="pve-node-stat"><span class="pve-stat-lbl">RAM</span>'
        +   '<div class="pve-bar"><div class="pve-bar-fill" style="width:' + memPct + '%"></div></div>'
        +   '<span class="pve-stat-val">' + memUsed + ' / ' + memTotal + '</span></div>'
        + '<div class="pve-node-uptime">Up ' + uptime + '</div>'
        + '</div>';
    }).join('');
    return '<div class="pve-node-cards">' + cards + '</div>';
  }

  /* ── Stubs for tasks 9–11 ──────────────────────────────────────────────── */
  function _renderGuestTable (nodes) { return ''; }

  /* ── Utilities ──────────────────────────────────────────────────────────── */

  function _fmtBytes (bytes) {
    if (!bytes) return '0 B';
    var units = ['B', 'KB', 'MB', 'GB', 'TB'];
    var i = 0; var v = bytes;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return v.toFixed(i > 0 ? 1 : 0) + ' ' + units[i];
  }

  function _fmtUptime (s) {
    if (!s) return '0s';
    var d = Math.floor(s / 86400);
    var h = Math.floor((s % 86400) / 3600);
    var m = Math.floor((s % 3600) / 60);
    if (d > 0) return d + 'd ' + h + 'h';
    return h + 'h ' + m + 'm';
  }

  function _timeAgo (date) {
    var diff = Math.floor((Date.now() - date.getTime()) / 1000);
    if (diff < 60)   return diff + 's ago';
    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
    return Math.floor(diff / 3600) + 'h ago';
  }

})();
```

- [ ] **Step 2: Add `proxmox.js` script tag to `dashboard.html`**

Find in `dashboard.html` (line 886):

```html
<script src="/static/nas.js?v={{VERSION}}"></script>
```

Add immediately after:

```html
<script src="/static/proxmox.js?v={{VERSION}}"></script>
```

- [ ] **Step 3: Add node card styles to `static/main.css`**

Append to `static/main.css`:

```css
/* ── Proxmox panel — shared & action bar ─────────────────────────────────── */
.pve-unavailable {
  padding: 32px;
  text-align: center;
  color: var(--text-muted);
  font-size: 14px;
}
.pve-action-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-bottom: 16px;
}
.pve-meta  { font-size: 12px; color: var(--text-muted); }
.pve-warn  { font-size: 12px; color: var(--warn, #e8a000); font-weight: 500; }

/* ── Node cards ──────────────────────────────────────────────────────────── */
.pve-node-cards {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 24px;
}
.pve-node-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 18px;
  min-width: 220px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.pve-node-name  { font: 600 14px/1 var(--font-mono); color: var(--text); }
.pve-node-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font: 600 11px/1 var(--font-mono);
  letter-spacing: .5px;
  width: fit-content;
}
.pve-node-badge-ok  { background: #1a4a2e; color: #4caf50; }
.pve-node-badge-err { background: #4a1a1a; color: #f44336; }
.pve-node-stat {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}
.pve-stat-lbl { color: var(--text-muted); width: 30px; flex-shrink: 0; }
.pve-stat-val { color: var(--text-muted); font-size: 11px; white-space: nowrap; }
.pve-bar {
  flex: 1;
  height: 5px;
  background: var(--border);
  border-radius: 3px;
  overflow: hidden;
}
.pve-bar-fill {
  height: 100%;
  background: var(--accent);
  border-radius: 3px;
  transition: width 0.3s;
  max-width: 100%;
}
.pve-node-uptime { font-size: 11px; color: var(--text-muted); }
```

- [ ] **Step 4: Verify the app starts and no console errors**

```bash
cd /home/mgipson/netwatch && python -c "import monitor; print('OK')"
```

Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
cd /home/mgipson/netwatch && git add static/proxmox.js dashboard.html static/main.css && git commit -m "feat: add proxmox.js skeleton with node cards UI"
```

---

## Task 9: `proxmox.js` — guest table, status/type badges, helpers

**Files:**
- Modify: `static/proxmox.js` — replace `_renderGuestTable` stub with full implementation
- Modify: `static/main.css` (append table + badge styles)

- [ ] **Step 1: Replace the `_renderGuestTable` stub in `static/proxmox.js`**

Find and replace in `static/proxmox.js`:

```javascript
  /* ── Stubs for tasks 9–11 ──────────────────────────────────────────────── */
  function _renderGuestTable (nodes) { return ''; }
```

Replace with:

```javascript
  /* ── Guest table ─────────────────────────────────────────────────────────── */

  function _renderGuestTable (nodes) {
    var rows = [];
    nodes.forEach(function (node) {
      (node.guests || []).forEach(function (g) {
        rows.push({ nodeName: node.name, guest: g });
      });
    });
    if (!rows.length) {
      return '<div class="pve-unavailable">No guests found.</div>';
    }

    var head = '<thead><tr>'
      + '<th>Node</th><th>VMID</th><th>Name</th><th>Type</th>'
      + '<th>Status</th><th>CPU%</th><th>RAM</th>'
      + '<th class="pve-col-nw" title="Netwatch link">NW</th>'
      + '<th>Actions</th></tr></thead>';

    var body = '<tbody>' + rows.map(function (r) {
      var g = r.guest;
      var running = g.status === 'running';
      var cpu = running ? g.cpu_percent.toFixed(1) + '%' : '—';
      var ram = running
        ? _fmtBytes(g.mem_used_bytes) + ' / ' + _fmtBytes(g.mem_total_bytes)
        : '—';
      return '<tr id="pve-row-' + g.vmid + '">'
        + '<td class="pve-td-mono">' + escapeHtml(r.nodeName) + '</td>'
        + '<td class="pve-td-mono">' + g.vmid + '</td>'
        + '<td>' + escapeHtml(g.name) + '</td>'
        + '<td>' + _typePill(g.type) + '</td>'
        + '<td>' + _statusBadge(g.status) + '</td>'
        + '<td class="pve-td-num">' + cpu + '</td>'
        + '<td class="pve-td-num">' + ram + '</td>'
        + '<td class="pve-td-nw">' + _nwLink(g.vmid) + '</td>'
        + '<td class="pve-td-actions">' + _actionButtons(r.nodeName, g.vmid, g.type, g.status) + '</td>'
        + '</tr>';
    }).join('') + '</tbody>';

    return '<table class="pve-guest-table">' + head + body + '</table>';
  }

  function _statusBadge (status) {
    var map = {
      running: ['pve-badge-running', 'Running'],
      stopped: ['pve-badge-stopped', 'Stopped'],
      paused:  ['pve-badge-paused',  'Paused'],
    };
    var pair = map[status] || ['pve-badge-stopped', status];
    return '<span class="pve-badge ' + pair[0] + '">' + pair[1] + '</span>';
  }

  function _typePill (type) {
    return type === 'qemu'
      ? '<span class="pve-type-vm">VM</span>'
      : '<span class="pve-type-lxc">LXC</span>';
  }

  /* ── Stubs for tasks 10–11 ──────────────────────────────────────────────── */
  function _nwLink (vmid)                              { return ''; }
  function _actionButtons (node, vmid, type, status)   { return ''; }
```

- [ ] **Step 2: Add table + badge styles to `static/main.css`**

Append to `static/main.css`:

```css
/* ── Proxmox guest table ─────────────────────────────────────────────────── */
.pve-guest-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.pve-guest-table th {
  text-align: left;
  padding: 6px 10px;
  border-bottom: 2px solid var(--border);
  font: 600 11px/1 var(--font-sans);
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: .5px;
  white-space: nowrap;
}
.pve-guest-table td {
  padding: 7px 10px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}
.pve-guest-table tr:last-child td { border-bottom: none; }
.pve-td-mono { font-family: var(--font-mono); font-size: 12px; }
.pve-td-num  { text-align: right; font-family: var(--font-mono); font-size: 12px; }
.pve-td-nw, .pve-col-nw { text-align: center; width: 36px; }
.pve-td-actions { white-space: nowrap; }

/* Status badges */
.pve-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font: 600 11px/1.4 var(--font-mono);
  letter-spacing: .3px;
}
.pve-badge-running { background: #1a4a2e; color: #4caf50; }
.pve-badge-stopped { background: var(--surface2); color: var(--text-muted); }
.pve-badge-paused  { background: #4a3b1a; color: #ffb300; }

/* Type pills */
.pve-type-vm, .pve-type-lxc {
  display: inline-block;
  padding: 2px 7px;
  border-radius: 4px;
  font: 600 10px/1.4 var(--font-mono);
  letter-spacing: .5px;
}
.pve-type-vm  { background: #1a2e4a; color: #64b5f6; }
.pve-type-lxc { background: #2e1a4a; color: #ce93d8; }
```

- [ ] **Step 3: Verify import**

```bash
cd /home/mgipson/netwatch && python -c "import monitor; print('OK')"
```

Expected: `OK`.

- [ ] **Step 4: Run full test suite**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py --tb=short 2>&1 | tail -10
```

Expected: all passing.

- [ ] **Step 5: Commit**

```bash
cd /home/mgipson/netwatch && git add static/proxmox.js static/main.css && git commit -m "feat: add Proxmox guest table with status and type badges"
```

---

## Task 10: `proxmox.js` — action buttons

**Files:**
- Modify: `static/proxmox.js` — replace `_actionButtons` stub; add `window.proxmoxAction`
- Modify: `static/main.css` (append button styles)

- [ ] **Step 1: Replace `_actionButtons` stub and add `proxmoxAction` in `static/proxmox.js`**

Find and replace in `static/proxmox.js`:

```javascript
  /* ── Stubs for tasks 10–11 ──────────────────────────────────────────────── */
  function _nwLink (vmid)                              { return ''; }
  function _actionButtons (node, vmid, type, status)   { return ''; }
```

Replace with:

```javascript
  /* ── Stub for task 11 ───────────────────────────────────────────────────── */
  function _nwLink (vmid) { return ''; }

  /* ── Action buttons ─────────────────────────────────────────────────────── */

  function _actionButtons (node, vmid, type, status) {
    var n  = escapeHtml(node);
    var t  = escapeHtml(type);
    if (status === 'running') {
      return '<button class="pve-btn pve-btn-stop" title="Stop"'
        + ' onclick="proxmoxAction(\'' + n + '\',' + vmid + ',\'' + t + '\',\'stop\')">■</button>'
        + '<button class="pve-btn pve-btn-reboot" title="Reboot"'
        + ' onclick="proxmoxAction(\'' + n + '\',' + vmid + ',\'' + t + '\',\'reboot\')">↺</button>';
    }
    if (status === 'stopped') {
      return '<button class="pve-btn pve-btn-start" title="Start"'
        + ' onclick="proxmoxAction(\'' + n + '\',' + vmid + ',\'' + t + '\',\'start\')">▶</button>';
    }
    return '';
  }

  window.proxmoxAction = function (node, vmid, type, action) {
    var row  = document.getElementById('pve-row-' + vmid);
    var btns = row ? Array.from(row.querySelectorAll('.pve-btn')) : [];
    btns.forEach(function (b) { b.disabled = true; b.classList.add('pve-btn-loading'); });

    fetch('/api/proxmox/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ node: node, vmid: vmid, type: type, action: action }),
    })
      .then(function (r) {
        return r.json().then(function (d) { return { ok: r.ok, data: d }; });
      })
      .then(function (res) {
        if (res.ok) {
          window.fetchProxmox();
        } else {
          _flashBtns(btns, 'pve-btn-err');
        }
      })
      .catch(function () { _flashBtns(btns, 'pve-btn-err'); });
  };

  function _flashBtns (btns, cls) {
    btns.forEach(function (b) {
      b.disabled = false;
      b.classList.remove('pve-btn-loading');
      b.classList.add(cls);
    });
    setTimeout(function () {
      btns.forEach(function (b) { b.classList.remove(cls); });
    }, 2000);
  }
```

- [ ] **Step 2: Add action button styles to `static/main.css`**

Append to `static/main.css`:

```css
/* ── Proxmox action buttons ──────────────────────────────────────────────── */
.pve-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--surface);
  color: var(--text-muted);
  font-size: 13px;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
  margin-right: 3px;
}
.pve-btn:hover:not(:disabled) { background: var(--surface2); color: var(--text); }
.pve-btn:disabled { opacity: 0.4; cursor: default; }
.pve-btn-start:hover:not(:disabled) { color: #4caf50; border-color: #4caf50; }
.pve-btn-stop:hover:not(:disabled)  { color: #f44336; border-color: #f44336; }
.pve-btn-loading { opacity: 0.5; }
.pve-btn-err { border-color: #f44336 !important; color: #f44336 !important; }
```

- [ ] **Step 3: Verify import**

```bash
cd /home/mgipson/netwatch && python -c "import monitor; print('OK')"
```

Expected: `OK`.

- [ ] **Step 4: Commit**

```bash
cd /home/mgipson/netwatch && git add static/proxmox.js static/main.css && git commit -m "feat: add Proxmox guest action buttons (start/stop/reboot)"
```

---

## Task 11: Host linking + Netwatch dot

**Files:**
- Modify: `static/proxmox.js` — replace `_nwLink` stub with live implementation
- Modify: `static/main.css` (append dot styles)

The `_buildHostsMap()` function already runs on each `fetchProxmox()` call (Task 8). It populates `_hostsVmidMap` keyed by `proxmox_vmid`. This task only needs to replace the `_nwLink` stub with a real renderer.

- [ ] **Step 1: Replace `_nwLink` stub in `static/proxmox.js`**

Find and replace in `static/proxmox.js`:

```javascript
  /* ── Stub for task 11 ───────────────────────────────────────────────────── */
  function _nwLink (vmid) { return ''; }
```

Replace with:

```javascript
  /* ── Netwatch host link dot ─────────────────────────────────────────────── */

  function _nwLink (vmid) {
    var entry = _hostsVmidMap[vmid];
    if (!entry) return '';
    var upCls = entry.is_up === true  ? 'pve-nw-up'
              : entry.is_up === false ? 'pve-nw-down'
              : 'pve-nw-unknown';
    var title = escapeHtml(entry.name) + (entry.is_up === true ? ' • UP' : entry.is_up === false ? ' • DOWN' : '');
    return '<span class="pve-nw-dot ' + upCls + '" title="' + title + '"'
      + ' onclick="if(typeof showTab===\'function\')showTab(\'inventory\')"'
      + ' style="cursor:pointer" tabindex="0" role="link"'
      + ' onkeydown="if(event.key===\'Enter\'&&typeof showTab===\'function\')showTab(\'inventory\')"></span>';
  }
```

- [ ] **Step 2: Add dot styles to `static/main.css`**

Append to `static/main.css`:

```css
/* ── Proxmox Netwatch host link dot ─────────────────────────────────────── */
.pve-nw-dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  border: 1.5px solid var(--border);
  transition: transform 0.1s;
}
.pve-nw-dot:hover { transform: scale(1.3); }
.pve-nw-up      { background: #4caf50; border-color: #4caf50; }
.pve-nw-down    { background: #f44336; border-color: #f44336; }
.pve-nw-unknown { background: var(--surface2); }
```

- [ ] **Step 3: Run full test suite**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py --tb=short 2>&1 | tail -10
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
cd /home/mgipson/netwatch && git add static/proxmox.js static/main.css && git commit -m "feat: add Proxmox host link dot with inventory proxmox_vmid join"
```

---

## Final verification

- [ ] **Step 1: Run full test suite one last time**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py -v 2>&1 | tail -30
```

Expected: all tests pass, no warnings.

- [ ] **Step 2: Restart the service**

```bash
sudo systemctl restart netwatch.service && sudo systemctl status netwatch.service | head -15
```

Expected: `active (running)`.

- [ ] **Step 3: Verify the new endpoints respond**

```bash
curl -sk -u admin:$(cat /home/mgipson/netwatch/auth.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(list(d.get('users',{}).values())[0].get('password',''))" 2>/dev/null || echo "password") http://localhost:8080/api/proxmox | python3 -m json.tool | head -10
```

(Replace credentials as needed.) Expected: JSON with `reachable` key.
