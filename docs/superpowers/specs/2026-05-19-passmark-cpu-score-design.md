# PassMark CPU Score Auto-Lookup — Design Spec

**Date:** 2026-05-19  
**Status:** Approved

## Overview

When adding or editing an inventory item of `device_type: host`, the `cpu_score` field is
automatically populated by looking up the CPU name against the PassMark CPU benchmark chart
(cpubenchmark.net). CPUs not found on PassMark receive an estimated score computed from the
CPU string. Existing entries can be bulk-filled via a toolbar action.

---

## Backend

### New endpoint: `GET /api/passmark-lookup?q=<cpu_name>`

- Auth-required.
- On first call: fetches `https://www.cpubenchmark.net/cpu_list.php` using `urllib.request`
  (already used in monitor.py; no new dependencies).
- Parses the HTML with a regex identical to siliconboard's `PassMarkClient`:
  ```
  <a href="/cpu_lookup\.php[^"]*">([^<]+)</a></td><td>([\d,]+)</td>
  ```
- Stores the resulting `dict[str, int]` in a module-level `_passmark_chart` variable.
  Subsequent calls skip the fetch (in-memory cache, lost on restart).
- Search logic (same as siliconboard):
  1. Collect all entries where `needle in name.lower()`, pick the one with the highest
     `difflib.SequenceMatcher` ratio.
  2. Fuzzy fallback via `difflib.get_close_matches` at cutoff 0.6 if no substring match.

**Response shapes:**

```json
{ "matched": "Intel Core i9-12900K", "score": 39500, "source": "passmark" }

{ "score": 28000, "source": "estimate", "basis": "16 cores × ~1750" }

{ "score": null, "source": "none" }
```

### Estimation (when not found on PassMark)

Parse the CPU string for:
- Core count: explicit hints ("12-core", "8-Core"), Intel tier (i3/i5/i7/i9 → 2/6/8/16),
  AMD tier (Ryzen 3/5/7/9 → 4/6/8/12), Apple M-series, ARM, etc.
- Architecture multiplier: ~1800/core for x86 desktop, ~1200/core for laptop/ARM.

Formula: `estimated_score = cores × per_core_multiplier`  
Result always includes `"source": "estimate"` so the UI can flag it visually.  
If nothing useful can be parsed, returns `{ "score": null, "source": "none" }`.

---

## Frontend — Inventory Editor UI

### CPU name field blur (auto-trigger)

- When focus leaves `inv-f-cpu`, if the field is non-empty **and** `inv-f-cpu_score` is currently
  empty, fire a lookup silently.
- If cpu_score already has a value, do not auto-lookup (respect user-set values).

### Manual lookup button

- A small `↗` button rendered adjacent to the `inv-f-cpu_score` input.
- Always clickable; re-runs the lookup even if cpu_score already has a value.
- Uses the current value of `inv-f-cpu`.

### Feedback states

| State | cpu_score field | Hint text (below field) |
|---|---|---|
| Fetching | unchanged | spinner `⟳` |
| `source: passmark` | filled | `matched: <name>` (muted) |
| `source: estimate` | filled | `~estimated (<basis>)` (amber) |
| `source: none` | unchanged | `not found` (muted) |

- Hint clears when the user manually edits `inv-f-cpu_score`.
- Spinner on button while fetching; button disabled during request.

---

## Frontend — Bulk "Fill missing scores"

A **"Fill missing scores"** button in the inventory toolbar (near the Export button).

Behaviour:
1. Fetches all inventory items via `GET /api/inventory`.
2. Filters to `device_type: host`, non-empty CPU name, no existing `cpu_score`.
3. Calls `/api/passmark-lookup?q=<cpu>` sequentially for each.
4. PATCHes each item via `PATCH /api/inventory/<id>` with the resolved score.
5. Shows a progress toast: `Filling scores… 3 / 7` → `Done — 5 filled, 2 not found`.

---

## Scope / Non-goals

- No disk cache; the in-memory chart is acceptable to re-fetch on restart.
- No PassMark result is written unless the user saves the inventory item (single-item flow) or
  clicks "Fill missing scores" (bulk flow).
- Estimation is a best-effort heuristic; users can always override the filled value.
- The "Fill missing scores" action only targets items with no existing score — it will not
  overwrite manually entered values.
