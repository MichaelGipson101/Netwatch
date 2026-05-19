# Inventory Export Scope Selection — Design Spec
_2026-05-18_

## Overview

The existing `GET /api/inventory-export` endpoint exports only host-type inventory records. This change adds a `scope` parameter so callers can request either hosts-only (current behaviour) or all device types (new multi-sheet workbook). The dashboard's Export XLSX button becomes a split dropdown offering both options.

The NAS cron job (a separate, out-of-repo convenience) is documented in the implementation plan.

---

## Change 1: `export_inventory_to_xlsx` — scope parameter

**File:** `monitor.py`

Add a `scope` keyword argument (`'hosts'` or `'all'`, default `'hosts'`) to `export_inventory_to_xlsx(inventory_db, scope='hosts')`.

**`scope='hosts'` (default):** Behaviour is identical to today — filters records to `device_type == 'host'`, produces a single-sheet workbook, filename `netwatch-inventory-hosts-{hostname}-{date}.xlsx`.

**`scope='all'`:** Skips the device-type filter. Groups all records by `device_type` and writes one sheet per type that has at least one record. Sheet names follow this mapping:

| `device_type` value | Sheet name |
|---|---|
| `host` | Hosts |
| `vm` | VMs |
| `network` | Network |
| `tablet` | Tablets |
| `phone` | Phones |
| `ups` | UPS |
| `disk` | Storage |
| `peripheral` | Peripherals |
| `printer` | Printers |
| _(any unknown value)_ | _(value as-is, title-cased)_ |

Each sheet uses the same column layout and formatting as the current single-sheet export (bold header, auto-width, freeze row 1). Sheet order follows `INV_TYPE_ORDER` from the dashboard. Filename: `netwatch-inventory-all-{hostname}-{date}.xlsx`.

---

## Change 2: `/api/inventory-export` endpoint

**File:** `monitor.py`

Read a `scope` query-string parameter from the request URL. Accept `hosts` (default if absent or unrecognised) and `all`. Pass the value through to `export_inventory_to_xlsx`. No other changes to the handler.

---

## Change 3: Export dropdown button in dashboard

**File:** `dashboard.html`

Replace the current single `<button onclick="downloadInventoryExport(this)">Export XLSX</button>` with a split dropdown button:

- **Left segment** ("Export Hosts") — triggers the hosts-only export directly on click, same as the current button
- **Right segment** (chevron `˅`) — opens a small two-item dropdown menu:
  - "Export Hosts" → `GET /api/inventory-export?scope=hosts`
  - "Export All Types" → `GET /api/inventory-export?scope=all`

The dropdown closes on outside click or after a selection. Style follows existing `.inv-toolbar` conventions (same height, border, font as the Import XLSX and other toolbar buttons).

`downloadInventoryExport(btn, scope)` gains a `scope` parameter (default `'hosts'`). It builds the URL as `/api/inventory-export?scope=${scope}` and triggers a file download via a temporary `<a>` element, identical to the current implementation.

---

## Non-goals

- Import is not changed — it continues to accept host-type records only
- No retention policy or file pruning on the NAS
- No changes to the import modal or any other toolbar button
- No new API endpoints

---

## Implementation order

1. Update `export_inventory_to_xlsx` — add `scope` parameter and multi-sheet logic
2. Update `/api/inventory-export` handler — read `?scope` query param
3. Update `downloadInventoryExport` JS — add `scope` argument, build URL
4. Replace export button with split dropdown HTML + CSS + JS
5. NAS setup (manual, out of repo): mount SMB share, backup script, cron job
