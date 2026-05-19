# UI Polish — Design Spec
_2026-05-18_

## Overview

Twelve targeted visual improvements to `dashboard.html`. All changes are contained to that single file (CSS + HTML + minimal JS). No backend changes required.

The overarching aesthetic goal for the blur work (changes 9–12) is a consistent macOS-style frosted glass language — semi-transparent surfaces with `backdrop-filter: blur()` — extending the treatment already present on the topology overlays and legend panel to the rest of the UI.

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

---

### 9. Nav bar — sticky frosted glass

**Current state:** The nav bar scrolls with the page and has a solid opaque background.

**Change:** Make the nav `position: sticky; top: 0; z-index: 30` so it pins to the top during scroll. Add `backdrop-filter: blur(12px) saturate(1.5)` with a semi-transparent background (`rgba` of the current `--surface` value — approximately `rgba(255,255,255,.80)` light / `rgba(26,25,23,.80)` dark). Add a subtle bottom border that uses a semi-transparent border color to avoid a hard line. Both light and dark themes get a matching treatment via the existing theme CSS vars.

**Scope:** ~6 CSS rules on `nav`. No JS.

---

### 10. Detail drawer — frosted glass backdrop + panel

**Current state:** The drawer backdrop is a solid `rgba(0,0,0,.35)` scrim. The drawer itself is a fully opaque `var(--surface)` panel.

**Change:**
- Backdrop: add `backdrop-filter: blur(4px)` to `.drawer-backdrop` with a slightly reduced opacity (`rgba(0,0,0,.25)`) so the blur does the visual work instead of darkness alone.
- Drawer panel: change `.drawer` background to a semi-transparent value (`rgba(255,255,255,.82)` light / `rgba(26,25,23,.82)` dark) and add `backdrop-filter: blur(20px) saturate(1.4)`. The existing border and box-shadow stay.

**Scope:** ~4 CSS rules on `.drawer-backdrop` and `.drawer`. Both light and dark variants.

---

### 11. Topology node tooltip — frosted glass

**Current state:** `.topo-tip` has a solid `var(--surface)` background and a box-shadow.

**Change:** Change the background to semi-transparent (`rgba` of surface at ~75% opacity) and add `backdrop-filter: blur(12px) saturate(1.3)`. Update the border to use a softer `rgba` value matching the existing topology overlay border style (`rgba(255,255,255,.08)` in dark mode). This ties the tooltip visually to the overlays and legend already in the canvas.

**Scope:** ~3 CSS rule changes on `.topo-tip`. Dark-mode variant via `[data-theme="dark"] .topo-tip`.

---

### 12. Modal overlay — blurred scrim

**Current state:** `.modal-overlay` uses `background: rgba(0,0,0,.6)` — a hard dark scrim with no blur.

**Change:** Add `backdrop-filter: blur(5px)` to `.modal-overlay` and reduce the scrim opacity slightly (`rgba(0,0,0,.45)`) so page content remains softly visible behind modals. The modal box itself stays fully opaque — blur is scrim-only.

**Scope:** 2 CSS property changes on `.modal-overlay`.

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
3. Modal overlay blur (2 CSS lines)
4. Nav sticky + blur (CSS only)
5. Latency color coding (helper + CSS)
6. Summary card status coloring (CSS + JS)
7. Problem banner count + icon (JS + CSS)
8. Topology legend icon swatches (HTML)
9. Topology tooltip blur (CSS)
10. Drawer frosted glass — backdrop + panel (CSS)
11. Hosts toolbar filter + chips (HTML + JS)
12. Drawer device icon (JS + CSS)
