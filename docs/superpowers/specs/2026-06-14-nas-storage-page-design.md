# NAS Storage Page — Design Spec

**Date:** 2026-06-14
**Status:** Approved

---

## Overview

Add a dedicated "Storage" tab to the Netwatch dashboard that surfaces TrueNAS pool health and replication task status. The goal is to make NAS health visible at a glance without logging into the TrueNAS web UI, with ntfy alerts when something goes wrong.

---

## Architecture

A new `NASPoller` class is added to `monitor.py` alongside the existing Pi-health poller. It:

- Spawns a background thread on startup
- Wakes every 15 minutes and calls the TrueNAS REST API (`/api/v2.0/pool` and `/api/v2.0/replication`)
- Caches the result in memory as a plain dict
- Tracks last-known alert states to de-duplicate notifications

A new `/api/nas` GET endpoint (auth-gated, same as all other API endpoints) returns the cached dict to the frontend.

The Storage tab in `dashboard.html` fetches `/api/nas` on tab load and on manual refresh. No new files, no new dependencies, no new config files beyond two entries in `auth.json`.

---

## Configuration

Two new keys are added to `auth.json`:

```json
"truenas_url": "http://192.168.x.x",
"truenas_api_key": "your-key-here"
```

### Generating a TrueNAS API key

1. Log into the TrueNAS web UI
2. Click your username (top right) → API Keys
3. Click Add, give it a name (e.g. `netwatch`), copy the key
4. Paste into `auth.json`

### Hardcoded constants (in `NASPoller`)

```python
POLL_INTERVAL_SECONDS = 900       # 15 minutes
REPLICATION_STALE_HOURS = 25      # grace window for daily tasks
```

---

## Data Model

The `/api/nas` response shape:

```json
{
  "last_updated": "2026-06-14T02:15:00",
  "reachable": true,
  "pools": [
    {
      "name": "tank",
      "status": "ONLINE",
      "capacity_used_bytes": 2254857830400,
      "capacity_total_bytes": 3954220032000,
      "vdevs": [
        {
          "type": "mirror",
          "name": "mirror-0",
          "status": "ONLINE",
          "disks": [
            { "name": "ada0", "status": "ONLINE" },
            { "name": "ada1", "status": "ONLINE" }
          ]
        }
      ],
      "last_scrub": {
        "status": "FINISHED",
        "end_time": "2026-06-01T04:22:00",
        "errors": 0
      },
      "next_scrub": "2026-07-01T04:00:00"
    }
  ],
  "replication_tasks": [
    {
      "id": 1,
      "name": "tank → backup-drive",
      "last_run": "2026-06-14T02:00:00",
      "last_state": "SUCCESS"
    }
  ]
}
```

If TrueNAS is unreachable, `reachable` is `false` and `last_updated` reflects the last successful poll. Pool and replication arrays retain their last-known values.

---

## Frontend — Storage Tab

**Tab bar:** New "Storage" button added after Inventory.

**Page layout (top to bottom):**

1. **Action bar** — "Refresh now" button + "Last updated X min ago · polls every 15 min" timestamp. If unreachable, replaces timestamp with a warning banner: "TrueNAS unreachable · last data from X min ago".

2. **Pool health metrics** (4-up metric cards):
   - Pool status (`ONLINE` / `DEGRADED` / `FAULTED`) — colored green/amber/red
   - Capacity used (bytes formatted + percentage)
   - Last scrub — result + date + error count
   - Next scrub — date + days away

3. **VDEV layout card** — tree view of mirror/raidz groups and their member disks with per-disk status dot and name. Badge shows overall health.

4. **Replication tasks card** — one row per task: name, last run time, badge (`Success` / `Failed` / `Stale (Xh)`). Stale threshold: last_run more than 25 hours ago.

**Status badge logic:**

| Condition | Badge | Color |
|---|---|---|
| Last state SUCCESS, ran within 25h | Success | Green |
| Last state SUCCESS, ran > 25h ago | Stale (Xh) | Amber |
| Last state FAILED | Failed | Red |

**Staleness** is computed client-side from `last_run` timestamp vs. current time.

---

## Alerts (ntfy)

Three alert conditions, using the existing Netwatch ntfy integration:

| Condition | Trigger |
|---|---|
| Pool degraded/faulted | Any pool status not `ONLINE` |
| Scrub errors | Last scrub `errors` > 0 |
| Replication failure/stale | Task `last_state == FAILED` or last run > 25h ago |

De-duplication: the poller keeps a `_alert_state` dict keyed by condition ID. An alert fires only on the transition from clear → triggered. It re-arms when the condition clears.

ntfy message format:
- **Title:** `Netwatch · NAS Alert`
- **Body:** e.g. `Pool "tank" is DEGRADED` / `Replication "tank → backup-drive" failed` / `Scrub on "tank" found 3 errors`

---

## Error Handling

- **TrueNAS unreachable:** Poller catches connection errors, sets `reachable: false` in cache, does not overwrite last-known pool/replication data. Dashboard shows unreachable banner. No ntfy alert for unreachability itself (TrueNAS may be rebooting briefly).
- **Invalid API key:** Returns 401 — poller logs the error and sets `reachable: false`. User is expected to fix `auth.json`.
- **Missing config keys:** If `truenas_url` or `truenas_api_key` are absent from `auth.json`, the poller logs a warning and skips polling entirely. The `/api/nas` endpoint returns `{"reachable": false, "error": "NAS not configured"}` and the Storage tab shows a setup prompt.

---

## Out of Scope

- S.M.A.R.T. disk health (future enhancement)
- Dataset/share browsing
- Snapshot management
- Multi-pool support beyond what TrueNAS already returns (handled automatically via the pools array)
- Hearthboard NAS tile (separate feature, reads from `/api/nas` when ready)
