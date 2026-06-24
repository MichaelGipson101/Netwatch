# TUI Bugfix & Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three real defects in netwatch's curses TUI (`draw_tui`): `DEGRADED` hosts rendering as healthy green, a too-narrow status column, and an unhandled `curses.error` crash on non-256-color terminals — without rebuilding or expanding the TUI's feature set.

**Architecture:** Extract the host-row color decision into a pure, unit-testable function (`_tui_status_role`); extract color-pair setup into a function with a tiered fallback (`_init_tui_colors`); wire both into the existing `draw_tui` loop with minimal structural change; add a `curses.error` safety net around the TUI's entry point in `main()`.

**Tech Stack:** Python stdlib `curses` — no new dependencies.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-23-tui-upgrade-design.md`
- No feature additions (no new panels, no new keybindings) — this is bugfix and polish only.
- `_tui_status_role(pend, is_up, status) -> str` returns exactly one of `"wait"`, `"degraded"`, `"up"`, `"idle"`, `"down"`.
- `_init_tui_colors()` never raises — it must return a usable attribute dict even on a fully monochrome terminal.
- `_init_tui_colors()`'s actual curses calls are not unit-testable (would require faking curses's color subsystem) — verified manually only, per the spec. Only `_tui_status_role` gets automated tests.
- Existing layout, summary cards, sparkline history, and `q`/`r` keybindings are unchanged.

---

### Task 1: `_tui_status_role()` pure function and tests

**Files:**
- Modify: `monitor.py` (new function near `draw_tui`, before line 4636)
- Test: `tests/test_netwatch.py`

**Interfaces:**
- Produces: `_tui_status_role(pend: bool, is_up: bool, status: str) -> str` — returns `"wait"`, `"degraded"`, `"up"`, `"idle"`, or `"down"`. Used by Task 2's `draw_tui` wiring.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_netwatch.py`, in a new section (following the file's `# ── Section Name ──` comment convention):

```python
# ── _tui_status_role ─────────────────────────────────────────────────────────

def test_tui_status_role_wait_when_pending():
    from monitor import _tui_status_role
    assert _tui_status_role(pend=True, is_up=False, status="WAIT") == "wait"


def test_tui_status_role_pending_wins_over_degraded():
    from monitor import _tui_status_role
    # Defensive priority check: pend must win even if status somehow already
    # says DEGRADED before the first check has completed.
    assert _tui_status_role(pend=True, is_up=True, status="DEGRADED") == "wait"


def test_tui_status_role_degraded_when_up_but_degraded():
    from monitor import _tui_status_role
    assert _tui_status_role(pend=False, is_up=True, status="DEGRADED") == "degraded"


def test_tui_status_role_degraded_wins_over_up():
    from monitor import _tui_status_role
    # This is the actual regression this task fixes: a DEGRADED host has
    # is_up == True, so a naive "if is_up: return up" check (the current
    # bug in draw_tui) would misclassify it as healthy.
    assert _tui_status_role(pend=False, is_up=True, status="DEGRADED") != "up"


def test_tui_status_role_up_when_healthy():
    from monitor import _tui_status_role
    assert _tui_status_role(pend=False, is_up=True, status="UP") == "up"


def test_tui_status_role_idle_when_down_and_not_always_on():
    from monitor import _tui_status_role
    assert _tui_status_role(pend=False, is_up=False, status="IDLE") == "idle"


def test_tui_status_role_down_when_down_and_always_on():
    from monitor import _tui_status_role
    assert _tui_status_role(pend=False, is_up=False, status="DOWN") == "down"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_netwatch.py -k tui_status_role -v`
Expected: FAIL — `ImportError: cannot import name '_tui_status_role' from 'monitor'`

- [ ] **Step 3: Implement**

Add directly above `def draw_tui(stdscr, host_manager, refresh_rate, port, stop_event):` (monitor.py:4636):

```python
def _tui_status_role(pend, is_up, status):
    """Pure decision logic for which color role applies to a host's TUI row.

    Decoupled from curses attr objects so this is testable without a real
    screen. status == "DEGRADED" implies is_up is True (see Host.status_str),
    so this must be checked before the generic "up" branch - that ordering
    is the actual bug fix this function exists for: a naive is_up check
    alone misclassifies a degraded host as healthy.
    """
    if pend:
        return "wait"
    if status == "DEGRADED":
        return "degraded"
    if is_up:
        return "up"
    if status == "IDLE":
        return "idle"
    return "down"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_netwatch.py -k tui_status_role -v`
Expected: 7 passed

- [ ] **Step 5: Run the full suite to check for regressions**

Run: `python3 -m pytest tests/test_netwatch.py -v 2>&1 | tail -10`
Expected: 153 passed (146 existing + 7 new)

- [ ] **Step 6: Commit**

```bash
git add monitor.py tests/test_netwatch.py
git commit -m "feat: extract _tui_status_role() pure function, fixing DEGRADED hosts rendering as healthy"
```

---

### Task 2: Wire `_tui_status_role()` into `draw_tui` and widen the status column

**Files:**
- Modify: `monitor.py:4742-4762` (the per-host color branch and status field render, inside `draw_tui`)

**Interfaces:**
- Consumes: `_tui_status_role(pend, is_up, status) -> str` (Task 1).

No automated test applies to this task — it's curses rendering, verified manually per the spec. (Task 1's tests already cover the decision logic this task merely wires up.)

- [ ] **Step 1: Replace the inline if/elif color branch**

In `monitor.py`, replace lines 4742-4749:

```python
                if pend:
                    ind_attr = C_WAIT; st_attr = C_WAIT; nm_attr = C_TEXT
                elif is_up:
                    ind_attr = C_UP;   st_attr = C_UP;   nm_attr = C_TEXT
                elif status == "IDLE":
                    ind_attr = C_MUTED; st_attr = C_MUTED; nm_attr = C_MUTED
                else:
                    ind_attr = C_DOWN; st_attr = C_DOWN; nm_attr = C_DOWN
```

with:

```python
                role = _tui_status_role(pend, is_up, status)
                ind_attr, st_attr, nm_attr = ROLE_ATTRS[role]
```

- [ ] **Step 2: Define `ROLE_ATTRS` once per draw cycle**

`ROLE_ATTRS` maps role strings to `(ind_attr, st_attr, nm_attr)` tuples and depends on the `C_*` color variables, so it must be built after those are set but before the per-host loop. Add it directly after the `C_SPARK = ...` line (monitor.py:4655, immediately before `curses.curs_set(0)`):

```python
    ROLE_ATTRS = {
        "wait":     (C_WAIT,  C_WAIT,  C_TEXT),
        "degraded": (C_WAIT,  C_WAIT,  C_TEXT),
        "up":       (C_UP,    C_UP,    C_TEXT),
        "idle":     (C_MUTED, C_MUTED, C_MUTED),
        "down":     (C_DOWN,  C_DOWN,  C_DOWN),
    }
```

(`degraded` reuses `C_WAIT`'s amber coloring rather than introducing a 9th color pair — matches the spec's stated dict of named attres, which has no separate `C_DEGRADED` entry.)

- [ ] **Step 3: Widen the status column**

Replace line 4762:

```python
                safe_addstr(stdscr, row, COL_STAT, f"{status:<7}",   st_attr)
```

with:

```python
                safe_addstr(stdscr, row, COL_STAT, f"{status:<9}",   st_attr)
```

- [ ] **Step 4: Verify manually**

This requires a host configured with `strict: true` and a service check that's currently failing, so `status_str` actually returns `"DEGRADED"`. Use a throwaway config:

```bash
cd /tmp && mkdir -p tui-verify && cd tui-verify
cat > hosts.yaml <<'EOF'
settings:
  default_interval: 5
  ping_timeout: 2
hosts:
  - name: TestHost
    ip: 127.0.0.1
    group: Test
    always_on: true
    strict: true
    services:
      - name: closed-port
        port: 9
EOF
python3 /home/mgipson/netwatch/monitor.py --config hosts.yaml --port 18080
```

Wait a few seconds for the first ping + service check cycle. Expected: the `TestHost` row shows status `DEGRADED` (since port 9 on `127.0.0.1` should be closed/unreachable in any normal environment) in amber/yellow coloring (not green), with the latency column starting cleanly after it with no visual overlap. Press `q` to quit. Clean up: `cd /tmp && rm -rf tui-verify`.

- [ ] **Step 5: Commit**

```bash
git add monitor.py
git commit -m "feat: wire _tui_status_role into draw_tui, widen status column to fit DEGRADED"
```

---

### Task 3: `_init_tui_colors()` with tiered fallback

**Files:**
- Modify: `monitor.py:4636-4656` (the color-pair setup at the top of `draw_tui`)

**Interfaces:**
- Produces: `_init_tui_colors() -> dict` — keys `"C_UP"`, `"C_DOWN"`, `"C_WAIT"`, `"C_HDR"`, `"C_HDRB"`, `"C_MUTED"`, `"C_TEXT"`, `"C_LATC"`, `"C_SPARK"`, each a curses attribute value. Never raises. Used by this task's own `draw_tui` wiring (Step 2).

No automated test applies — curses color calls aren't practically fakeable in a unit test, per the spec. Verified manually in Step 3.

- [ ] **Step 1: Add `_init_tui_colors()`**

Add directly above `_tui_status_role` (which is itself directly above `draw_tui`, from Task 1):

```python
def _init_tui_colors():
    """Initialize curses color pairs for the TUI, with graceful fallback.

    Tries the full 256-color palette first; falls back to an 8-color-safe
    palette if the terminal can't do 256 colors; falls back further to an
    all-default (monochrome) attribute set if even basic color pairs fail.
    Never raises - draw_tui can always render *something* in this terminal.
    """
    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, 82,  -1)
        curses.init_pair(2, 196, -1)
        curses.init_pair(3, 214, -1)
        curses.init_pair(4, 39,  -1)
        curses.init_pair(5, 243, -1)
        curses.init_pair(6, 255, -1)
        curses.init_pair(7, 208, -1)
        curses.init_pair(8, 51,  -1)
        return {
            "C_UP":    curses.color_pair(1) | curses.A_BOLD,
            "C_DOWN":  curses.color_pair(2) | curses.A_BOLD,
            "C_WAIT":  curses.color_pair(3) | curses.A_BOLD,
            "C_HDR":   curses.color_pair(4),
            "C_HDRB":  curses.color_pair(4) | curses.A_BOLD,
            "C_MUTED": curses.color_pair(5),
            "C_TEXT":  curses.color_pair(6),
            "C_LATC":  curses.color_pair(7),
            "C_SPARK": curses.color_pair(8),
        }
    except curses.error:
        pass

    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN,  -1)
        curses.init_pair(2, curses.COLOR_RED,    -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_CYAN,   -1)
        curses.init_pair(5, curses.COLOR_WHITE,  -1)
        return {
            "C_UP":    curses.color_pair(1) | curses.A_BOLD,
            "C_DOWN":  curses.color_pair(2) | curses.A_BOLD,
            "C_WAIT":  curses.color_pair(3) | curses.A_BOLD,
            "C_HDR":   curses.color_pair(4),
            "C_HDRB":  curses.color_pair(4) | curses.A_BOLD,
            "C_MUTED": curses.color_pair(5),
            "C_TEXT":  0,
            "C_LATC":  curses.color_pair(3),
            "C_SPARK": curses.color_pair(4),
        }
    except curses.error:
        pass

    return {
        "C_UP": 0, "C_DOWN": 0, "C_WAIT": 0, "C_HDR": 0,
        "C_HDRB": curses.A_BOLD, "C_MUTED": 0, "C_TEXT": 0,
        "C_LATC": 0, "C_SPARK": 0,
    }
```

- [ ] **Step 2: Wire it into `draw_tui`**

Replace lines 4637-4655:

```python
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1,  82,  -1)
    curses.init_pair(2,  196, -1)
    curses.init_pair(3,  214, -1)
    curses.init_pair(4,  39,  -1)
    curses.init_pair(5,  243, -1)
    curses.init_pair(6,  255, -1)
    curses.init_pair(7,  208, -1)
    curses.init_pair(8,  51,  -1)
    C_UP    = curses.color_pair(1) | curses.A_BOLD
    C_DOWN  = curses.color_pair(2) | curses.A_BOLD
    C_WAIT  = curses.color_pair(3) | curses.A_BOLD
    C_HDR   = curses.color_pair(4)
    C_HDRB  = curses.color_pair(4) | curses.A_BOLD
    C_MUTED = curses.color_pair(5)
    C_TEXT  = curses.color_pair(6)
    C_LATC  = curses.color_pair(7)
    C_SPARK = curses.color_pair(8)
```

with:

```python
    colors  = _init_tui_colors()
    C_UP    = colors["C_UP"]
    C_DOWN  = colors["C_DOWN"]
    C_WAIT  = colors["C_WAIT"]
    C_HDR   = colors["C_HDR"]
    C_HDRB  = colors["C_HDRB"]
    C_MUTED = colors["C_MUTED"]
    C_TEXT  = colors["C_TEXT"]
    C_LATC  = colors["C_LATC"]
    C_SPARK = colors["C_SPARK"]
```

(Leave the line right after — `curses.curs_set(0)` — exactly as-is; Task 4's `main()` change is the intended safety net for that call failing, not this function.)

- [ ] **Step 3: Verify manually on the normal terminal**

```bash
cd /home/mgipson/netwatch
python3 monitor.py --port 18080
```

Expected: TUI renders with the same colors as before this change (green up, red down, amber wait, etc.) — this change should be visually a no-op on a normal 256-color terminal. Press `q` to quit.

- [ ] **Step 4: Verify the fallback path doesn't crash (best-effort)**

A fully accurate "no 256-color support" test would require a genuinely limited terminal emulator. A reasonable approximation:

```bash
cd /home/mgipson/netwatch
TERM=linux python3 monitor.py --port 18080
```

Expected: either the TUI renders (possibly with different colors) without crashing, or — if your terminal emulator itself still reports 256-color capability regardless of the `TERM` override — no observable difference. Either outcome is fine; what matters is the absence of an unhandled traceback. If you do see a traceback here, stop and tell me what it says before continuing to Task 4 — it would mean `_init_tui_colors()`'s fallback chain has a gap Task 4's outer net wasn't designed to be the only thing standing between the user and a crash.

- [ ] **Step 5: Commit**

```bash
git add monitor.py
git commit -m "feat: add _init_tui_colors() with 256-color/8-color/monochrome fallback chain"
```

---

### Task 4: `curses.error` safety net in `main()`

**Files:**
- Modify: `monitor.py:4905-4912` (the `curses.wrapper(draw_tui, ...)` call site in `main()`)

**Interfaces:** none — this is the outermost layer, nothing depends on it.

No automated test applies — verified manually.

- [ ] **Step 1: Add the except clause**

In `monitor.py`, replace lines 4905-4912:

```python
        else:
            try:
                curses.wrapper(draw_tui, host_manager, refresh_rate, args.port, stop_event)
            except KeyboardInterrupt:
                pass
            finally:
                stop_event.set()
                print("\n[netwatch] Stopped.")
```

with:

```python
        else:
            try:
                curses.wrapper(draw_tui, host_manager, refresh_rate, args.port, stop_event)
            except KeyboardInterrupt:
                pass
            except curses.error:
                print("\n[netwatch] Terminal doesn't support the TUI's required features. Try --no-tui.")
                sys.exit(1)
            finally:
                stop_event.set()
                print("\n[netwatch] Stopped.")
```

`sys` is already imported at the top of `monitor.py` (added in the install/redeploy work for `--restore`'s `sys.exit`) — no new import needed.

- [ ] **Step 2: Verify manually that the normal path still works**

```bash
cd /home/mgipson/netwatch
python3 monitor.py --port 18080
```

Expected: TUI starts normally, `q` quits cleanly with `[netwatch] Stopped.` printed, no `curses.error` message (since nothing is actually broken in a normal terminal).

- [ ] **Step 3: Verify the except clause is reachable (code-level check, not a forced crash)**

There's no safe way to force a genuine `curses.error` from a working terminal without actually breaking that terminal session. Instead, confirm the exception handler is correctly structured by reading it back:

```bash
grep -A 10 "curses.wrapper(draw_tui" monitor.py
```

Expected output shows the `except KeyboardInterrupt:`, `except curses.error:`, and `finally:` clauses in that order, each with their own body, exactly as written in Step 1.

- [ ] **Step 4: Run the full test suite one final time**

Run: `python3 -m pytest tests/test_netwatch.py -v 2>&1 | tail -10`
Expected: 153 passed (no regressions from this purely-defensive, untested-by-design code path)

- [ ] **Step 5: Commit**

```bash
git add monitor.py
git commit -m "feat: catch curses.error around the TUI entry point instead of crashing with a traceback"
```

---

## Self-Review Notes

- **Spec coverage:** defect #1 (DEGRADED-as-green) fixed by Task 1 + Task 2; defect #2 (column width) fixed by Task 2 Step 3; defect #3 (uncaught curses.error on color init) fixed by Task 3 + Task 4 (Task 3 handles the common case gracefully with full functionality retained; Task 4 is the last-resort net for anything Task 3's fallback chain doesn't anticipate, e.g. `curses.curs_set(0)` failing, exactly as the spec describes).
- **Placeholder scan:** none found.
- **Type consistency:** `_tui_status_role(pend, is_up, status) -> str` (Task 1) is consumed identically in Task 2's wiring; `_init_tui_colors() -> dict` (Task 3) and its 9 named keys are consumed identically in Task 3's own `draw_tui` wiring step — no signature drift between definition and use.
