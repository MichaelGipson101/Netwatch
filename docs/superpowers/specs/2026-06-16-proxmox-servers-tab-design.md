# Proxmox Integration & Servers Tab — Design Spec

**Date:** 2026-06-16
**Status:** Approved

---

## Overview

Add Proxmox VE cluster monitoring with guest start/stop/reboot actions to Netwatch. The existing "Storage" tab is renamed to "Servers" and gains a pill toggle to switch between a new **Proxmox** sub-panel and the existing **TrueNAS** sub-panel (unchanged). A new optional `proxmox_vmid` inventory field lets existing hosts link to their Proxmox guest counterpart for unified status visibility.

---

## Architecture

A new `ProxmoxPoller` class mirrors `NASPoller`: background thread, fixed poll interval, in-memory cache, `get_cache()` accessor.

Two new endpoints:

- `GET /api/proxmox` — auth-gated, returns cached cluster state
- `POST /api/proxmox/action` — admin-only, proxies start/stop/reboot to the Proxmox API

The poller enumerates cluster nodes dynamically via `GET /api2/json/nodes` every poll cycle — no hardcoded node name. Works automatically as nodes are added.

**Poll interval:** 60 seconds. Guest state changes fast and the Proxmox API is local-network — load is negligible.

**SSL:** Proxmox uses a self-signed cert by default. The poller disables SSL verification (same approach as most homelab tools). No user-configurable option needed.

---

## Configuration

Four credential keys are used. All four are added to `_AUTH_STORED_KEYS` so they save to `auth.json` (alongside TrueNAS credentials), not `hosts.yaml`:

| Key | Example value |
|---|---|
| `proxmox_url` | `https://192.168.4.237:8006` |
| `proxmox_user` | `root@pam` |
| `proxmox_token_id` | `Netwatch` |
| `proxmox_token_secret` | UUID from PVE token creation |

The existing `proxmox_password` and `proxmox_node` keys remain in `SETTINGS_EDITABLE_KEYS` but are not used by the poller. `proxmox_node` is superseded by dynamic enumeration.

### Authorization header format

```
Authorization: PVEAPIToken=root@pam!Netwatch=<uuid-secret>
```

### Generating a PVE API token

1. **Datacenter → Permissions → API Tokens → Add**
2. Set **User** (e.g. `root@pam`)
3. Set **Token ID** (e.g. `Netwatch`)
4. **Uncheck "Privilege Separation"** — token inherits user permissions
5. Click **Add** — copy the UUID secret (shown once)

---

## Data Model

### `/api/proxmox` response

```json
{
  "last_updated": "2026-06-16T14:30:00",
  "reachable": true,
  "nodes": [
    {
      "name": "pve",
      "status": "online",
      "cpu_percent": 12.1,
      "mem_used_bytes": 11170574336,
      "mem_total_bytes": 16147808256,
      "uptime_seconds": 3891504,
      "guests": [
        {
          "vmid": 108,
          "name": "haos13.2",
          "type": "qemu",
          "status": "running",
          "cpu_percent": 0.0,
          "mem_used_bytes": 2046949088,
          "mem_total_bytes": 4294967296
        }
      ]
    }
  ]
}
```

**Key API details confirmed against live cluster:**

- The Proxmox API returns `cpu` as a **0–1 fraction** — the poller multiplies by 100 for `cpu_percent`
- QEMU guests have no `type` field in the API response — the poller sets `"type": "qemu"` based on which endpoint was called (`/qemu` vs `/lxc`)
- LXC guests on newer Proxmox nodes may include PSI pressure fields (`pressurememorysome` etc.) — these are ignored
- `status` mirrors Proxmox values: `"running"`, `"stopped"`, `"paused"`
- `cpu_percent` and `mem_used_bytes` are `0` when a guest is stopped — display as `—` in the UI

If unreachable: `reachable` is `false`, `nodes` retains last-known data, `last_updated` reflects the last successful poll.

### `/api/proxmox/action` request

```json
{ "node": "pve", "vmid": 108, "type": "qemu", "action": "stop" }
```

Valid actions: `start`, `stop`, `reboot`. Proxied to `POST /api2/json/nodes/{node}/{type}/{vmid}/status/{action}`.

---

## Frontend — Servers Tab

### Tab rename

"Storage" → "Servers". The tab button text, JS tab-key (`storage` → `servers`), and any `data-tab` attributes update accordingly. The TrueNAS panel code moves inside the new Servers tab wrapper untouched.

### Pill toggle

Two pills in the Servers tab header: **Proxmox** | **TrueNAS**. Defaults to Proxmox if configured, TrueNAS otherwise. Each pill shows its panel; the other hides. Each panel has its own Refresh button and last-updated line.

### Proxmox panel layout

**1. Action bar**
Refresh button + "Last updated X min ago · polls every 60s". If unreachable: amber warning banner "Proxmox unreachable · last data from X min ago."

**2. Node cards**
One card per node, displayed in a horizontal row. Each card: node name, `ONLINE`/`OFFLINE` badge, CPU% bar, RAM used/total + %, uptime formatted as days/hours. Read-only — no actions on nodes.

**3. Guest table**
All guests across all nodes sorted by node then VMID. Given ~28 guests across 2 nodes, the table is compact with no pagination needed but styled for density.

Columns:

| Column | Notes |
|---|---|
| **Node** | Node name |
| **VMID** | Integer |
| **Name** | Guest name |
| **Type** | `VM` or `LXC` pill |
| **Status** | `Running` (green) / `Stopped` (gray) / `Paused` (amber) badge |
| **CPU%** | Shown when running; `—` when stopped |
| **RAM** | Used / total when running; `—` when stopped |
| **Netwatch** | Up/down dot (green/red) if `proxmox_vmid` linked; empty otherwise. Clicking navigates to Inventory |
| **Actions** | Context-sensitive icon buttons: ▶ Start (stopped only); ■ Stop + ↺ Reboot (running only) |

ASCII sketch:

```
Node          VMID  Name              Type  Status   CPU%   RAM           Link  Actions
────────────────────────────────────────────────────────────────────────────────────────
pve            108  haos13.2          VM    Running  0.0%   2G / 4G       ●     ■ ↺
pve            101  VM 101            VM    Running  0.0%   1.2G / 8G     ○     ■ ↺
pve            104  Wordpress         VM    Running  0.0%   1G / 2G             ■ ↺
pve            103  Pi-Hole           VM    Running  0.0%   1.1G / 2G           ■ ↺
pve            100  windows-11        VM    Stopped  —      —             ○     ▶
pve            120  pihole            LXC   Running  0.0%   19M / 512M          ■ ↺
NASMachineV3   121  TRUENAS           VM    Running  0.0%   11G / 12G           ■ ↺
NASMachineV3   123  EmailArchive      LXC   Running  0.0%   24M / 512M          ■ ↺
```

### Action flow

Clicking an action button:
1. Button shows a spinner, other buttons on that row disable
2. `POST /api/proxmox/action` fires
3. On success: row status badge updates optimistically; full refresh happens on next 60s poll
4. On failure: button shows a red flash, restores to original state, tooltip shows error message

**Action-triggered stops do not alert.** When Netwatch fires a stop or reboot, the poller sets a 30-second VMID exemption so the expected state change doesn't trigger a false-alarm ntfy notification.

---

## Host Linking

A new optional `proxmox_vmid` integer field is added to the inventory schema (`InventoryDB`). It appears in the inventory editor for all host types (not restricted to `type: vm` — the user may want to link LXC containers too).

The join is **frontend-only**: on Proxmox panel load, the JS fetches `/api/proxmox` and reads host data already present from the last `/api/status` poll. It matches `guest.vmid` against any host with a matching `proxmox_vmid` value. No poller changes needed.

The Netwatch dot in the guest table:
- **Green** — linked host is UP
- **Red** — linked host is DOWN
- **Empty** — no `proxmox_vmid` set for any host matching this VMID
- Clicking navigates to that host's row in the Inventory tab

---

## Alerts

Three conditions, using the existing ntfy integration. De-duplication via `_alert_state` dict (same pattern as `NASPoller`): fires only on clear → triggered transition, re-arms on clear.

| Condition | Trigger | ntfy Body |
|---|---|---|
| Node offline | Node status not `"online"` | `Proxmox node "pve" is offline` |
| Guest stopped unexpectedly | Guest transitions `running → stopped` without a Netwatch action | `VM "haos13.2" (108) stopped unexpectedly on pve` |
| Guest paused | Any guest enters `"paused"` state | `VM "windows-11" (100) is paused on pve` |

ntfy title: `Netwatch · Proxmox Alert`

No alert for unreachability (Proxmox may be mid-reboot).

---

## Error Handling

| Condition | Behavior |
|---|---|
| Unreachable / connection error | `reachable: false`, retain last-known data, amber banner in UI, no ntfy |
| 401 / bad token | Log error, `reachable: false`. User fixes credentials in Settings |
| Missing config | Poller skips. `/api/proxmox` returns `{"reachable": false, "error": "Proxmox not configured"}`. Proxmox pill shows setup prompt pointing to Settings |
| Action failure (non-2xx) | Return error to frontend. Button red-flashes, row restores, tooltip shows error |

---

## Out of Scope

- Console / VNC access
- Snapshot or backup management
- Live CPU/RAM sparkline graphs (static current value only)
- Proxmox storage pool view (TrueNAS tab covers storage)
- Multi-cluster support (one Proxmox cluster per Netwatch instance)
- Migration or HA management
- Guest tags display / filtering (visible in API, deferred)
- PSI pressure metrics (`pressurememorysome` etc.)
