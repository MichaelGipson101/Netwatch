# UI Polish — Design Spec
_2026-05-18_

## Overview

Eight targeted visual improvements to `dashboard.html`. All changes are contained to that single file (CSS + HTML + minimal JS). No backend changes required.

---

## Changes

### 1. Topology legend — replace CSS shapes with dimensional icon previews

**Current state:** The legend panel ("Node shapes" section) renders device types as plain CSS-drawn shapes: a circle for Host, a wide rectangle for Network, etc. These no longer match the actual topology web view, which switched to dimensional icon sprites (`#topo-icon-*`).

**Change:** Replace each shape swatch in the legend with a small inline SVG that reproduces the actual dimensional icon (same viewBox, scaled to ~18×18). Update the section heading from "Node shapes" to "Node types". The status dot rows below it are fine and stay.

**Scope:** HTML edit to `.topo-legend-section` (the first section only). No CSS changes needed.

---

### 2. Summary cards — status-aware color

**Current state:** All four summary cards (Hosts up, Avg latency, Avg uptime, Monitored) are always the same neutral white/dark surface regardless of network health.

**Change:** The "Hosts up" card gains a green tint when all monitored hosts are up, and a red tint when any are down. The tint uses existing token pairs: `--green-soft`/`--green-text` for all-clear, `--red-soft`/`--red-text` for degraded. Applied via a JS class toggle on the card element (`scard-health-ok` / `scard-health-warn`) updated on each poll.

**Scope:** 2 CSS rules + ~5 lines of JS in the status render path.

---

### 3. Problem banner — count in title + icon refinement

**Current state:** Header reads "Hosts offline" with a `!` in a red circle regardless of count.

**Change:**
- Title becomes "N host(s) offline" (dynamic count, e.g. "2 hosts offline").
- Replace the `!`-circle icon with a `⚠` glyph styled consistently (no circle wrapper needed — the ⚠ character carries enough meaning at the right size/color).

**Scope:** HTML template change in `renderProblemBanner` (or equivalent JS), CSS tweak to `.problem-banner-icon`.

---

### 4. Hosts tab toolbar — filter input + status chips

**Current state:** The hosts toolbar contains only a "Compact" checkbox.

**Change:** Add two elements to the left side of the toolbar:
- **Filter input** (`<input type="text" placeholder="Filter hosts…">`) — filters the rendered host list client-side on `input`, matching against hostname and IP.
- **Status chips** — "All", "Down", "Degraded" pill buttons that filter by status. "All" is default active.

Filter input and status chip combine (AND logic: show hosts matching the text filter AND the selected status).

**Scope:** HTML addition to `.hosts-toolbar`, ~30 lines JS for filter/chip logic hooked into the existing `renderHosts` path.

---

### 5. Latency values — color-coded by threshold

**Current state:** All latency values in the host list render in the same muted mono color (`var(--hint)` or `var(--muted)`).

**Change:** Apply a CSS class based on latency value at render time:
- `< 20ms` → `--green-text`
- `20–100ms` → `--amber-text`
- `> 100ms` → `--red-text`
- No reading (host down) → unchanged muted

**Scope:** Helper function `latClass(ms)` + apply in host row render. ~6 lines JS, 3 CSS rules.

---

### 6. Live pip — "stale" text when feed is stale

**Current state:** When the API feed goes stale, the pip dot turns amber but the label text stays "live".

**Change:** Update the pip label text to "stale" when the stale class is applied, and restore it to "live" when the connection recovers. The existing `.stale` class on `#pip` is the toggle point.

**Scope:** ~4 lines JS alongside existing stale-detection logic.

---

### 7. Host detail drawer — device icon in header

**Current state:** The drawer header shows a small status dot (9px circle) next to the hostname.

**Change:** Replace the dot with the dimensional icon sprite for the host's device type, rendered as an inline `<svg><use>` at ~28×22px. The icon's LED element inherits status color via `currentColor` (same mechanism as the topology web view), so no additional status logic is needed. The dot is removed.

**Scope:** HTML template change in `openDrawer` (or `renderDrawer`), one CSS rule for icon sizing in the drawer context.

---

### 8. Events tab — designed empty state

**Current state:** Empty state is a single grey prose sentence inside `.events-empty`.

**Change:** Replace with a structured empty state:
- A small icon (checkmark or shield, consistent with the "all clear" meaning)
- Bold "All clear" heading
- Shorter subtitle: "No incidents recorded. Down events appear here."

**Scope:** HTML-only change to the `.events-empty` element content + ~3 CSS rules for the icon treatment.

---

## Non-goals

- No changes to the topology web view rendering
- No backend/Python changes
- No new dependencies
- Sparklines, tab transitions, and favicon deferred to a future pass

---

## Implementation order

Suggested order (easiest-to-hardest within the file, minimizes context switching):

1. Events empty state (HTML only)
2. Stale pip text (tiny JS)
3. Latency color coding (helper + CSS)
4. Summary card status coloring (CSS + JS)
5. Problem banner count + icon (JS + CSS)
6. Topology legend icon swatches (HTML)
7. Hosts toolbar filter + chips (HTML + JS)
8. Drawer device icon (JS + CSS)
