# Deep Frost Polish Pass — Design Spec

**Date:** 2026-05-19
**Status:** Approved

## Overview

A global visual polish pass on Netwatch's dark mode, leaning into the frosted glass aesthetic already present on the nav and drawer. Every floating/overlapping surface gets consistent glass treatment. Light mode receives minor parity fixes only. A full bug sweep runs alongside the visual changes.

---

## Glass System

### Philosophy

Glass works by making surfaces semi-transparent so the background bleeds through the `backdrop-filter: blur()`. The existing warm dark palette (`#0f0e0d`, `#1a1917`) stays unchanged. Two subtle radial green glow blobs on `body::before` give the glass something to diffract — making the `#5dbb8d` accent feel ambient rather than just point-source.

### Dark Mode Surface Changes

All changes live inside `[data-theme="dark"]` blocks (and matching `prefers-color-scheme: dark` block for the `auto` theme). Light mode values are only touched where noted.

| Surface | Current (dark) | After (dark) |
|---|---|---|
| `nav` | blur(14px) sat(1.6) alpha .88 | blur(24px) sat(1.8) alpha .80 |
| `.drawer` | blur(20px) sat(1.5) alpha .84 | blur(28px) sat(1.7) alpha .78 |
| `.drawer-backdrop` | blur(4px) | blur(8px) |
| `.modal-overlay` backdrop | blur(5px) | blur(16px) |
| `.modal` content panel | solid `--surface` | `rgba(18,17,15,.82)` + blur(28px) sat(1.8) + inset top highlight |
| `.user-dropdown` | solid `--surface` | `rgba(18,17,15,.82)` + blur(20px) sat(1.7) |
| `.inv-export-menu` | solid `--surface` | `rgba(18,17,15,.82)` + blur(20px) sat(1.7) |
| Tab bar `.tabs` | solid `--surface` | `rgba(255,255,255,.06)` + blur(12px) |
| Summary cards `.scard` | solid `--surface` | `rgba(26,25,23,.65)` + blur(20px) sat(1.6) + inset top edge |
| Host rows `.row` (non-semantic) | solid `--surface` | `rgba(255,255,255,.03)` — very subtle; down/degraded rows keep semantic bg colors unchanged |
| Form inputs inside overlays (`.inv-edit-form`, `.modal-body`) | solid `--surface` | `rgba(255,255,255,.05)` tinted background |
| `.topo-web-overlay` | blur(8px), no sat | blur(16px) sat(1.6) |
| `.topo-legend` | blur(10px) | blur(16px) sat(1.6) |
| `.topo-tip` | blur(12px) sat(1.3) | blur(20px) sat(1.7) |

### Inset Top Edge Highlight

Glass panels feel more dimensional with a 1px inner top border of `rgba(255,255,255,.08)`. Apply via `box-shadow: inset 0 1px 0 rgba(255,255,255,.08)` on cards and modal panels.

### Light Mode Parity Fixes

The dropdown, export menu, and modal content panel are currently flat white in light mode. They'll receive a subtle glass treatment to match the nav/drawer treatment already in place:
- `rgba(255,255,255,.90)` + blur(16px) + existing border
- No background blobs, no glow — just backdrop-filter parity with the nav.

---

## Background Treatment (Dark Mode Only)

Add `[data-theme="dark"] body::before` (and matching auto/dark media block) with two radial green blobs:

```css
[data-theme="dark"] body::before {
  content: '';
  position: fixed;
  inset: 0;
  pointer-events: none;
  z-index: 0;
  background:
    radial-gradient(ellipse 600px 400px at 30% -10%, rgba(93,187,141,.055) 0%, transparent 70%),
    radial-gradient(ellipse 400px 300px at 85% 90%,  rgba(93,187,141,.035) 0%, transparent 70%);
}
```

All page content must have `position: relative; z-index: 1` or higher so it renders above the blob layer. The nav already has `z-index: 30` so no change needed there.

---

## Status Glow Polish

Status indicators currently use solid color dots and bars. Add a soft bloom matching each status color:

**Live dots (`.live-dot` and card status indicators):**
```css
box-shadow: 0 0 7px currentColor;
```

**Uptime bars (`.uptime-fill`):**
```css
box-shadow: 0 0 5px var(--fill-color);  /* use the actual status color variable */
```

**Topology node halos** already bloom — no change needed. This brings card-view indicators into parity.

---

## Bug Fix Sweep

During implementation, run a full sweep covering:

1. **Z-index audit** — `.topo-tip` is z-index 50, same as `.modal-overlay`. If a tooltip fires while a modal is open the tip renders over the modal. Fix: raise `.modal-overlay` to z-index 55 or drop `.topo-tip` to z-index 45.

2. **JS event handling** — check for any listener leaks beyond the `outsideClick` fix already shipped. Audit `setInterval`/`setTimeout` calls for missing `clearTimeout` on component teardown.

3. **Python backend** — scan API handlers for unhandled exception paths, missing `return` after `_send_json`, and any routes that don't call `_require_auth`.

4. **CSS layout** — check for overflow/scroll issues on mobile breakpoints, particularly the inventory editor and modal panels.

5. **Form inputs** — verify all `type="number"` inputs have appropriate `min` to prevent accidental negative values (e.g., `cpu_score`, `tdp_watts`).

---

## Scope / Non-Goals

- Light mode gets parity glass on floating surfaces only — no blobs, no glow.
- No layout or information architecture changes.
- No new features.
- Topology edge/node rendering is untouched beyond the overlay/tip blur bump.
