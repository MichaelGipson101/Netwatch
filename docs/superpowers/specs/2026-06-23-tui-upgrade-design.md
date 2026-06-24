# TUI Bugfix & Polish — Design Spec

**Date:** 2026-06-23
**Status:** Approved

---

## Overview

Netwatch's curses-based TUI (`draw_tui`, monitor.py:4636+, shown when running without `--no-tui`) predates several web-dashboard additions and has drifted out of sync. The user rarely uses it day-to-day (the web dashboard and `--no-tui` systemd mode are the primary interfaces), so this is **not** a feature-parity rebuild — it's a focused bugfix-and-polish pass on the existing host-table view. A thorough code review (not just a feature-list comparison) surfaced three real defects, addressed here; feature parity with Proxmox/TrueNAS/inventory/AI panels is explicitly out of scope.

---

## Defects found and fixed

1. **`DEGRADED` status renders as healthy green.** `Host.status_str` (monitor.py:71-89) returns `"DEGRADED"` for a host that's pinging fine but failing a strict service check — this state has `is_up == True`. `draw_tui`'s color branch only checks `pend`/`is_up`/`status == "IDLE"`/else, so a degraded host gets the same green coloring as a fully healthy one, even though the text says "DEGRADED". The color is actively misleading.

2. **No crash protection around color initialization.** `draw_tui` calls `curses.start_color()` and `curses.init_pair()` with 256-color codes (e.g. `208`, `214`) unconditionally. On any terminal that isn't 256-color-capable — a serial console, a plain `TERM=linux` virtual console, some minimal SSH clients — `curses.error` is raised, uncaught anywhere above `draw_tui`. `main()`'s `except KeyboardInterrupt` around `curses.wrapper(...)` doesn't catch it, so the whole process crashes with a raw traceback.

3. **Status column too narrow for "DEGRADED".** The status field is rendered at a fixed width of 7 characters (`f"{status:<7}"`); `"DEGRADED"` is 8 characters, eating into the gap before the next column. Compounds defect #1 once that's fixed and the text starts actually appearing in practice.

---

## Architecture

**1. Extracted pure decision function.** A new `_tui_status_role(pend, is_up, status) -> str` returns one of `"wait"`, `"degraded"`, `"up"`, `"idle"`, `"down"` — the actual bug fix (adds the missing `degraded` branch, checked before the generic `up` branch) pulled out as a plain function with no curses dependency, so it's unit-testable without faking a screen. `draw_tui` becomes a thin caller: it gets the role string, then looks up the corresponding `(ind_attr, st_attr, nm_attr)` curses attributes from a dict built once per draw cycle.

**2. Widened status column.** The status field width changes from 7 to 9 characters (`f"{status:<9}"`), giving `"DEGRADED"` a clean trailing gap before the latency column, consistent with the spacing pattern used by the other columns.

**3. Graceful color fallback + top-level safety net.**
- A new `_init_tui_colors()` function tries the existing 256-color palette (the current `curses.init_pair(1, 82, -1)` etc. calls); on `curses.error`, falls back to an 8-color-safe palette using `curses.COLOR_GREEN`/`COLOR_RED`/`COLOR_YELLOW`/`COLOR_CYAN`/`COLOR_WHITE`; if that *also* raises (a genuinely monochrome terminal), falls back to an all-`0` (default, no color) attribute set rather than raising further. Returns a dict of named attributes (`C_UP`, `C_DOWN`, `C_WAIT`, `C_HDR`, `C_HDRB`, `C_MUTED`, `C_TEXT`, `C_LATC`, `C_SPARK`) that `draw_tui` uses instead of its current inline `curses.init_pair`/`curses.color_pair` calls.
- In `main()`, the existing `try: curses.wrapper(draw_tui, ...) except KeyboardInterrupt: pass` gains a sibling `except curses.error:` clause that prints `"[netwatch] Terminal doesn't support the TUI's required features. Try --no-tui."` and exits with a non-zero code, instead of an unhandled traceback. This is a last-resort net for failures `_init_tui_colors()`'s own fallback chain doesn't anticipate (e.g. `curses.curs_set(0)` failing on a terminal with no cursor-visibility control).

No other structural changes. The host-table layout, summary cards, sparkline history, and keybindings (`q`/`r`) are unchanged.

---

## Testing

- `_tui_status_role()` gets full pytest coverage: all 5 roles, including the specific regression case (`is_up=True, status="DEGRADED"` → must return `"degraded"`, not `"up"`) and confirming role priority (`pend=True` wins over everything else; `DEGRADED` is checked before the generic `up` branch).
- `_init_tui_colors()`'s actual curses calls are not practically unit-testable (would require faking curses's color subsystem) — verified manually: run the TUI in a normal 256-color terminal and in a terminal forced to a lower color mode (e.g. `TERM=linux`), confirming neither crashes.
- The column-width fix and the `DEGRADED` color fix together are verified visually: configure a host with `strict: true` and a failing service check, run the TUI, and confirm the row shows amber/warning coloring with `"DEGRADED"` rendered without crowding the latency column.

---

## Out of scope

- Feature parity with the web dashboard (Proxmox/TrueNAS panels, inventory, incident log, Mira AI assistant) — the TUI stays a lightweight host-status fallback view, not a second full UI.
- Immediate redraw on terminal resize (`KEY_RESIZE` handling) — current behavior (picked up at the next scheduled redraw) is slow but not broken; not worth the added complexity for a rarely-used view.
- Any new keybindings or interactive features beyond the existing `q`/`r`.
