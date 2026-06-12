# Premium UI Uplift — Design Spec

**Date:** 2026-06-11
**Status:** Approved
**Source:** Full UI/design audit performed 2026-06-11 (screenshots of every tab, both themes, desktop + 390px mobile, via sandboxed instance on :8089). Goal: take netwatch from "very good amateur project" to high-end, premium quality.

## User decisions (design forks)

1. **Topology canvas in light mode → light glass variants.** Light mode is a first-class citizen. Build proper light-mode styling for all canvas furniture rather than forcing the canvas dark.
2. **Small-size device icons → bigger + tuned dimensional.** Keep the dimensional icon language everywhere; bump table icons 18px → 22px and add a light-mode legibility lift. Do NOT swap to thin-stroke outline icons.
3. **Inventory category chips → top 8 + "+N more" expander,** with tiny STATUS / TYPE / CATEGORY row labels.

## Constraints

- Single-file-deployment philosophy stays: plain HTML/CSS/JS served by monitor.py, no build step, no framework.
- Zero WAN dependency after this work: all assets self-hosted.
- The Deep Frost dark aesthetic is the identity anchor; light mode rises to meet it, not the reverse.
- `localStorage` keys (`nw-theme`, `nw-tab`, `nw-topo-view`, positions) keep their current values — no migration.
- Python changes are test-driven against `tests/test_netwatch.py`.

## 1. Availability — self-hosted assets

- Vendor D3 v7 minified → `static/d3.v7.min.js`. `topology.js ensureD3()` loads `/static/d3.v7.min.js?v={{VERSION}}` instead of cdnjs.
- Self-host fonts: DM Sans woff2 (300/400/500/600), DM Mono woff2 (400/500), latin subset. New `static/fonts.css` with `@font-face` + `font-display:swap`. Replace the Google Fonts `<link>`/preconnect in `dashboard.html`.
- `monitor.py _STATIC_FILES` whitelist gains: `d3.v7.min.js` (application/javascript), `fonts.css` (text/css), `*.woff2` (font/woff2, flat filenames in `static/`, no subdirectory), plus Section 7 assets.

## 2. Theme architecture — JS resolution, single-source CSS

- Inline `<head>` script (before the stylesheet link) reads `nw-theme`, resolves `auto` → `light`/`dark` via `matchMedia('(prefers-color-scheme: dark)')`, sets `data-theme` on `<html>`. Listens for OS scheme changes and re-resolves when pref is `auto`.
- `setTheme(mode)` stores the pref (`light|auto|dark`) but applies only resolved values to `data-theme`. Toggle buttons' active state reflects the stored pref, unchanged UX.
- Delete every `@media(prefers-color-scheme:dark){[data-theme="auto"]…}` duplicate block in `main.css` (~45 rules), including the `[data-theme="auto"]` dark-variable block at the top. `[data-theme="dark"]` selectors become the single source of dark truth.
- Add `color-scheme: light` / `color-scheme: dark` per theme so native widgets (checkboxes, selects, scrollbars) match. Thin styled scrollbars on inner scroll panes (drawer body, modal body, AI messages).

## 3. Light mode first-class

- **Topo canvas light furniture** (scoped vars on `.topo-web` switched by theme):
  - Overlays/legend/legend-btn/fs buttons: light frost `rgba(255,255,255,≈.8)` + `backdrop-filter`, dark text, `rgba(0,0,0,.08)` borders, soft shadow.
  - Vignette: theme-aware — two SVG `radialGradient` defs (`#topo-vignette-dark` at `rgba(0,0,0,.4)`, `#topo-vignette-light` at `rgba(0,0,0,.07)`); the vignette rect's `fill` is switched between them by a `[data-theme]` CSS rule.
  - Grid dots, dead-edge, `other`-edge colors via scoped variables; edge palette slightly deepened in light mode for contrast on white (e.g. ethernet `#5b8eff` → `#3f6fe0`-range).
- **Elevation:** light cards/tables get layered soft shadows (`--shadow-1/2/3` tokens); faint green gradient wash at page top mirroring dark's ambient blobs.
- **Contrast:** light `--hint` → ≈`#757a84` (≥4.5:1 on `--bg`); dark `--hint` lifted modestly (≈`#76736b`).
- Light parity frost already exists for menus/modals; verify and align.

## 4. Mobile rebuild (≤768px, with ≤480px refinements)

Replace all dead selectors (`.topbar`, `.row-hdr`, `.events-hdr`, `.col-started/.col-ended`, `.group-card`, `.theme-label`, `.col-group`) with rules targeting real markup:

- Nav: one compact row; `#clock` hidden ≤480px; `#pip` hidden ≤768px; logo scales down.
- Hosts: `.row.hdr` hidden; data rows get an intentional two-line grid (dot+name+badge / uptime spanning under), not accidental wrap. Toolbar stacks: full-width filter input, chips row below, Compact toggle right-aligned.
- Events: grid → `6px 1fr auto auto` (bar, host, duration, badge); `.event-time` hidden on phones.
- Topology: toolbar wraps without clipping; canvas overlays scale down (smaller padding/font) and clamp inside the canvas; auto-fit handles initial zoom.
- Inventory: `.inv-export-split` participates in toolbar flex (no clipping); chips get `max-width` + ellipsis; mobile card grid rewritten around `td:first-child` (title area) and `td:last-child` (status pill) so 6- and 7-column type tables both map correctly; remove the stray `•` separator artifact (spacing via margins, no `::after`).

## 5. Per-view fixes

- **Hosts:** icons 22px + light-mode filter lift (`brightness/contrast/saturate` tuned by screenshot); IP once per breakpoint (desktop: column only, hide `.host-ip-sub`; mobile: subtitle only — compact mode already hides the sub); "No hosts match your filter" empty state when filter/chips produce zero rows; `fmtLatency` thresholds 20/100 → 50/150.
- **Drawer:** stat 1 becomes STATUS (UP/DOWN/DEGRADED/IDLE/WAIT, status-colored), stat 2 stays LATENCY; focus moves to drawer close button on open and returns to the invoking element on close.
- **Events:** `monitor.py` adds `started_ts` (epoch) to event payloads; `started_str` becomes date-aware ("21:39:26" today, "Jun 10 17:39" otherwise) as a no-JS fallback. Client groups the list under Today / Yesterday / "Jun 9" headers. TDD: payload test first.
- **Inventory:** category chips top-8-by-count + "+N more" expander chip (expands inline, collapses on re-click); STATUS/TYPE/CATEGORY mono row labels; chips become `<button type="button">`; singular type labels ("1 printer"); delete `inventory.js` tab-restore + click-hook — `setTab('inventory')` triggers `fetchInventory()`.
- **Topology:** auto `fitTopologyToView()` once after sim settle (~4s) unless the user has panned/zoomed/dragged (track via `zoom` events with non-null `sourceEvent` and `dragStart`); legend `?` gets a visible border + lighter glyph in both themes.

## 6. Performance & boot hygiene

- Simulation cools to a stop: settle phase ends with `alphaTarget(0)`; drag/resize warm it temporarily then return to 0.
- Flow dots: dedicated `requestAnimationFrame` loop, per-edge path lengths cached on tick (recomputed only while the sim is hot or after drag), time-based progress; loop runs only when web view visible; pauses on `document.hidden`; disabled entirely under `prefers-reduced-motion`.
- `prefers-reduced-motion` additionally stops: grid drift, live-dot pulse, AI pulse, load stagger.
- Boot block (`fetchAuthState` + 3 `setInterval`s + initial inventory fetch) moves from `inventory.js` into `core.js` DOMContentLoaded.
- Dead code removed: outline icon sprite set (`dashboard.html:12-53`), `.topo-legend-shape/.topo-legend-line` CSS, `@keyframes topo-hover-ring`.

## 7. Premium finish layer

- **Identity assets:** `static/favicon.svg` (green polyline on dark rounded square) + `favicon-alert.svg` (red status dot variant); `refresh()` swaps the link href when `down > 0`. `<meta name="theme-color">` synced to resolved theme. `static/manifest.json` (standalone display, theme/background colors) + PIL-generated `icon-192.png`, `icon-512.png`, `apple-touch-icon.png` (180). No service worker.
- **Motion:** one staggered load reveal (summary cards → group cards, ≈250ms total, `animation-delay` stagger, first render only); hover lift (+shadow) on cards; reduced-motion-safe.
- **Consistency:** toast component (top-right stack, success/error/info, auto-dismiss, ARIA live region) replaces all `alert()` calls (`detectMac`, `openEditor` failure, backup errors); "Reset positions" `confirm()` → two-step inline confirm button; `->` → `→`; AI bubble/panel z-index below drawer/modals (≈36-38); Escape closes discover, inventory editor, import modal, AI usage modal, then AI panel; `tabular-nums` on clock, scard values, drawer stats, latency/uptime cells, overlay numbers; footer reads `netwatch v{{VERSION}} · raspberry pi` and the refresh cadence is set from `REFRESH` by JS; `.err-banner` border uses a theme token; FAB glow uses a `--green-glow` token; login/setup/add-host modal inline styles extracted to shared `.form-field` classes with dark-frost variants.
- **Keyboard & ARIA:** delegated Enter/Space activation + `tabindex="0"` + `role="button"` for host rows, topo card nodes, event rows, problem pills, inventory rows; tabs maintain `aria-selected`; theme buttons `aria-pressed`; global `:focus-visible` ring (2px `var(--blue)`, 2px offset); lightweight focus trap helper for drawer + open modals (Tab wraps; Escape unchanged); `aria-label` on the hosts filter input.

## 8. Verification

1. `pytest` green on every monitor.py change (new tests for `started_ts`, static whitelist serving).
2. Sandbox screenshot matrix (same harness as audit): {dark, light} × {topology web, topology cards, hosts, events, inventory} desktop + {hosts, topo web, inventory} at 390px + landing. Compare against audit baselines in `/tmp/nw-audit/shots/`.
3. JS console error sweep on every screenshot run.
4. Reduced-motion spot check (`--force-prefers-reduced-motion`).
5. `verification-before-completion` before claiming done.

## Out of scope

Service worker / offline caching, hash routing, sticky table headers, AI chat redesign, server-side rendering changes beyond the events payload and static whitelist.
