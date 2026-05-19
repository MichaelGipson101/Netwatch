# Inventory Polish & Add Host FAB — Design Spec
_2026-05-18_

## Overview

Three areas of improvement to `dashboard.html`. All changes are contained to that single file (CSS + HTML + JS). No backend changes required except that the Add Host modal issues a `GET /api/hosts` then `POST /api/hosts` using the existing endpoint.

---

## Change 1: Add Host FAB

### Floating Action Button

A persistent floating action button (FAB) sits fixed at the bottom-right of the viewport:

- **Position:** `position: fixed; bottom: 28px; right: 28px; z-index: 35`
- **Style:** Extended pill — `+` glyph followed by "Add host" label. Green fill using `--green` background with white text. Subtle drop shadow for elevation.
- **Responsive:** Below 600px, hides the label and collapses to a circle-only button to avoid obscuring content.
- **Visibility:** Always visible on all tabs.

### Dedicated Add Host Modal

The FAB opens a dedicated single-record modal — not the existing Edit hosts modal. No host list is shown.

**Fields:** Identical to a single row in the Edit hosts modal:
- Name (required)
- IP address (required)
- Group (pre-filled "General")
- Interval
- Always on (checkbox, default checked)
- Alert (checkbox, default checked)
- Collapsible "More fields" section: CPU, RAM, Storage, OS, MAC (with Detect button), Services, Primary URL, Extra links, Notes

**Save path:** `GET /api/hosts` → append new host object → `POST /api/hosts` with full updated list → close modal + trigger `refresh()`.

**Auth:** Gated identically to `openEditor()` — checks `_authState.logged_in` / `_authState.setup_required`, redirects to login/setup if needed.

**Scope:** New `#add-host-overlay` modal HTML, ~60 lines JS (`openAddHostModal`, `saveAddHost`), ~10 CSS rules for the FAB and any modal overrides.

---

## Change 2: Icon Replacement

### Replace `deviceIcon()` throughout

The `deviceIcon(type, size)` function currently renders thin-stroke Lucide-style `#icon-{type}` sprites. Replace it to use the dimensional `#topo-icon-{type}` sprites instead — consistent with the topology view, drawer header, and topology legend.

**Updated function:**

```js
function deviceIcon(type, size){
  return '<svg width="' + size + '" height="' + size + '" viewBox="0 0 32 32"'
    + ' style="vertical-align:middle;flex-shrink:0" aria-hidden="true">'
    + '<use href="#topo-icon-' + (type || 'host') + '"/></svg>';
}
```

**Size change:** Callers currently pass 13–14px. Update call sites to pass 18px so the detail in the dimensional icons remains readable.

**Affected call sites:**
- Inventory table first column: `deviceIcon(rec.device_type, 14)` → `deviceIcon(rec.device_type, 18)`
- Host list rows: `deviceIcon(h.device_type, 13)` → `deviceIcon(h.device_type, 18)`

**Scope:** 1 JS function change + 2 call-site size updates. The `#icon-*` sprite `<symbol>` definitions can remain in the HTML (they're inert if unused) or be removed — leave them for now.

---

## Change 3: Inventory Improvements

### 3a. Empty state — smarter messaging

The `#inv-empty` element is shown when `renderInventoryTablesByType()` finds zero rows. Currently it always shows the "No inventory yet" message regardless of whether records exist but are filtered out.

**Fix:** Pass a boolean `isFiltered` to the empty-state render path. If `isFiltered` is true (search, type chip, category chip, or status chip is active), show a filter-specific message. If false (no records at all), show the onboarding message.

- **Filtered empty:** Icon (`⊘` or similar), heading "No results", subtitle "Try clearing the filter."
- **Truly empty:** Structured empty state — checkmark/box icon in a colored circle, bold "No inventory yet" heading, subtitle "Import an XLSX or add records one at a time."

### 3b. Status filter chips

Add a status filter row above the existing type chips. Options: **All · Up · Down · Unlinked**

- "Up" — `linked_host` exists and `linked_host.is_up === true` and `linked_host.status !== 'DEGRADED'`
- "Down" — `linked_host` exists and (`!linked_host.is_up` or `linked_host.status === 'DEGRADED'`)
- "Unlinked" — `linked_host` is null/undefined

Filter combines with type chip, category chip, and search (AND logic). Applied in `renderInventoryTablesByType()` before the type/category filters.

State stored in `_inventoryFilter.status` (default `null` = All).

### 3c. Chip counts respect search filter

Currently, type and category chip counts are calculated from `_inventoryData` (full dataset) or the type-filtered subset, ignoring any active search string.

**Fix:** Calculate chip counts from the search-filtered subset — apply `_inventoryFilter.search` first, then count by type and category. Chips always reflect what's actually visible in the table.

### 3d. Type heading icons

When multiple device types are shown together (no type filter active), the group headings ("Hosts", "VMs", "Network"…) gain a small topo icon (16px) to the left of the label.

```html
<div class="inv-type-heading">
  <svg width="16" height="16" viewBox="0 0 32 32" aria-hidden="true">
    <use href="#topo-icon-{type}"/>
  </svg>
  <span class="inv-type-heading-label">Hosts</span>
  <span class="inv-type-heading-count">4</span>
</div>
```

### 3e. Status pill polish

The Status column currently shows raw all-caps strings ("UP", "DOWN") in `.inv-link-pill`. Update `formatInvCell` for the `linked` key to use styled colored pills:

- **Up** (`is_up && status !== 'DEGRADED' && status !== 'WAIT'`): green bg/text (`--green-bg` / `--green-text`), label "Up"
- **Degraded** (`status === 'DEGRADED'`): amber bg/text (`--amber-bg` / `--amber-text`), label "Degraded"
- **Wait** (`status === 'WAIT'`): amber bg/text (`--amber-bg` / `--amber-text`), label "Wait"
- **Down** (`!is_up && status === 'DOWN'`): red bg/text (`--red-bg` / `--red-text`), label "Down"
- **Idle** (`status === 'IDLE'`): subtle bg, muted text, label "Idle"
- **No link:** no pill, muted italic "no link" text

Labels are title-case (not all-caps).

---

## Non-goals

- No backend/Python changes
- No new API endpoints
- No changes to the topology web view
- No new dependencies

---

## Implementation order

1. Icon replacement (`deviceIcon` function + call sites) — isolated, affects two call sites
2. Status pill polish — isolated CSS/JS in `formatInvCell`
3. Type heading icons — small HTML change in `renderTypeTable`
4. Empty state smarter messaging — JS logic change in `renderInventoryTablesByType`
5. Designed truly-empty state — HTML + CSS
6. Status filter chips — new filter row HTML + `_inventoryFilter.status` JS + filter logic
7. Chip counts respect search — JS fix in `renderInventoryChips`
8. FAB CSS + HTML
9. Add Host modal HTML
10. Add Host modal JS (`openAddHostModal`, `saveAddHost`)
