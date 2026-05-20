# Deep Frost Polish Pass — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply consistent deep frosted glass to all dark-mode surfaces in Netwatch, add ambient green glow blobs, polish status indicators, fix the topo-tip z-index bug, and tighten form input guards.

**Architecture:** All changes live in `dashboard.html`. Dark-mode overrides follow the existing dual-selector pattern (`[data-theme="dark"] .selector` + matching `@media(prefers-color-scheme:dark){[data-theme="auto"] .selector}`). New CSS goes as a single block inserted just before the first `</style>` tag (line 1075). No Python changes — backend swept and clean.

**Tech Stack:** Vanilla CSS (backdrop-filter, rgba, box-shadow, background-image radial-gradient), HTML attributes.

---

## Files

- Modify: `dashboard.html`
  - Lines 391, 583 — bug fixes (z-index, form min attrs)
  - Lines 1585–1588 — form input min attrs
  - Before line 1075 (`</style>`) — all new dark frost CSS block

---

## Task 1: Bug Fixes — Z-Index and Form Input Guards

**Files:** `dashboard.html:391`, `dashboard.html:1585-1588`

The `.topo-tip` (z-index 50) shares the same stacking level as `.modal-overlay` (z-index 50). A topology tooltip triggered just before a modal opens can bleed through. Fix: drop `.topo-tip` to z-index 45.

Number inputs `cpu_score`, `tdp_watts`, and `ram_gb` have no `min` attribute, allowing negative values via keyboard or scroll wheel.

- [ ] **Step 1: Fix topo-tip z-index**

  In `dashboard.html` at line 391, change `z-index:50` to `z-index:45`:

  ```
  OLD: .topo-tip{...z-index:50;...}
  NEW: .topo-tip{...z-index:45;...}
  ```

  The full line (391) currently reads:
  ```
  .topo-tip{position:absolute;pointer-events:none;background:rgba(255,255,255,.80);backdrop-filter:blur(12px) saturate(1.3);-webkit-backdrop-filter:blur(12px) saturate(1.3);border:1px solid rgba(0,0,0,.08);border-radius:8px;padding:8px 11px;box-shadow:0 4px 16px rgba(0,0,0,.25);font-size:12px;z-index:50;max-width:280px}
  ```
  Replace `z-index:50` with `z-index:45` in that line.

- [ ] **Step 2: Add min="0" to number inputs**

  In `dashboard.html` at lines 1585–1588, update three inputs:

  ```html
  <!-- line 1585 — was: <input type="number" step="0.001" class="inv-f-ram_gb" placeholder="16"> -->
  <input type="number" step="0.001" min="0" class="inv-f-ram_gb" placeholder="16">

  <!-- line 1587 — was: <input type="number" class="inv-f-cpu_score" placeholder="PassMark or similar"> -->
  <input type="number" min="0" class="inv-f-cpu_score" placeholder="PassMark or similar">

  <!-- line 1588 — was: <input type="number" class="inv-f-tdp_watts" placeholder="65"> -->
  <input type="number" min="0" class="inv-f-tdp_watts" placeholder="65">
  ```

- [ ] **Step 3: Verify**

  Restart the service and open the inventory editor. Try typing `-5` in the CPU score field — the browser should reject negative values. Open the topology view and hover a node; confirm the tooltip disappears correctly when a modal opens over it.

  ```bash
  sudo systemctl restart netwatch
  ```

- [ ] **Step 4: Commit**

  ```bash
  git add dashboard.html
  git commit -m "fix: topo-tip z-index below modal; add min=0 to numeric inventory inputs"
  ```

---

## Task 2: Ambient Green Glow Blobs

**Files:** `dashboard.html` — add to dark-mode CSS block before line 1075

Two subtle radial gradients on `body` give the glass surfaces something to refract. `background-attachment: fixed` keeps the blobs viewport-anchored while content scrolls. The dark `body` background-color (`var(--bg)` = `#0f0e0d`) remains as-is; the background-image layers on top.

- [ ] **Step 1: Insert CSS**

  Just before the `</style>` tag at line 1075, insert:

  ```css
  /* ── Deep Frost: ambient background blobs (dark mode only) ── */
  [data-theme="dark"] body{background-image:radial-gradient(ellipse 640px 420px at 28% -8%,rgba(93,187,141,.052) 0%,transparent 68%),radial-gradient(ellipse 420px 320px at 84% 92%,rgba(93,187,141,.032) 0%,transparent 68%);background-attachment:fixed}
  @media(prefers-color-scheme:dark){[data-theme="auto"] body{background-image:radial-gradient(ellipse 640px 420px at 28% -8%,rgba(93,187,141,.052) 0%,transparent 68%),radial-gradient(ellipse 420px 320px at 84% 92%,rgba(93,187,141,.032) 0%,transparent 68%);background-attachment:fixed}}
  ```

- [ ] **Step 2: Verify**

  Restart and open in dark mode. The background should have a very faint warm green tinge in the top-left corner and a subtler one bottom-right. It should not be garish — more of a "is that there?" quality.

  ```bash
  sudo systemctl restart netwatch
  ```

- [ ] **Step 3: Commit**

  ```bash
  git add dashboard.html
  git commit -m "feat: add ambient green glow blobs to dark mode background"
  ```

---

## Task 3: Strengthen Nav + Drawer Glass

**Files:** `dashboard.html:36-37`, `dashboard.html:51-52`, and new CSS block before line 1075

Update the `--nav-bg` and `--drawer-bg` token values in both dark blocks to lower alpha. Then add backdrop-filter overrides for dark mode (the filter values are hardcoded in the base rules, not tokens).

- [ ] **Step 1: Update dark token values**

  In the `[data-theme="dark"]` block (lines 36–37), change:
  ```css
  --nav-bg:rgba(15,14,13,.88);
  --drawer-bg:rgba(26,25,23,.84);
  ```
  to:
  ```css
  --nav-bg:rgba(15,14,13,.80);
  --drawer-bg:rgba(20,19,17,.78);
  ```

  In the `@media(prefers-color-scheme:dark)` block (lines 51–52), make the same change:
  ```css
  --nav-bg:rgba(15,14,13,.80);
  --drawer-bg:rgba(20,19,17,.78);
  ```

- [ ] **Step 2: Add backdrop-filter overrides**

  Append to the dark frost CSS block (before line 1075):

  ```css
  /* ── Deep Frost: nav + drawer ── */
  [data-theme="dark"] nav{backdrop-filter:blur(24px) saturate(1.8);-webkit-backdrop-filter:blur(24px) saturate(1.8);box-shadow:0 1px 0 rgba(255,255,255,.05),0 2px 20px rgba(0,0,0,.4)}
  @media(prefers-color-scheme:dark){[data-theme="auto"] nav{backdrop-filter:blur(24px) saturate(1.8);-webkit-backdrop-filter:blur(24px) saturate(1.8);box-shadow:0 1px 0 rgba(255,255,255,.05),0 2px 20px rgba(0,0,0,.4)}}
  [data-theme="dark"] .drawer{backdrop-filter:blur(28px) saturate(1.7);-webkit-backdrop-filter:blur(28px) saturate(1.7);box-shadow:-6px 0 32px rgba(0,0,0,.5),inset 1px 0 0 rgba(255,255,255,.07)}
  @media(prefers-color-scheme:dark){[data-theme="auto"] .drawer{backdrop-filter:blur(28px) saturate(1.7);-webkit-backdrop-filter:blur(28px) saturate(1.7);box-shadow:-6px 0 32px rgba(0,0,0,.5),inset 1px 0 0 rgba(255,255,255,.07)}}
  [data-theme="dark"] .drawer-backdrop{backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px)}
  @media(prefers-color-scheme:dark){[data-theme="auto"] .drawer-backdrop{backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px)}}
  ```

- [ ] **Step 3: Verify**

  Restart. In dark mode, the nav should feel more intensely frosted — content below it should look blurrier when scrolling. The drawer should have a stronger glass depth. The blobs from Task 2 should show faintly through the nav.

  ```bash
  sudo systemctl restart netwatch
  ```

- [ ] **Step 4: Commit**

  ```bash
  git add dashboard.html
  git commit -m "feat: strengthen nav and drawer frosted glass in dark mode"
  ```

---

## Task 4: Glass on Floating Surfaces — Dropdown + Export Menu

**Files:** `dashboard.html` — new CSS block before line 1075

The user dropdown and inventory export menu are currently solid `var(--surface)`. They get dark glass treatment. Light mode gets a subtle backdrop-filter addition for parity.

- [ ] **Step 1: Add dark + light overrides**

  Append to the dark frost CSS block:

  ```css
  /* ── Deep Frost: floating menus ── */
  [data-theme="dark"] .user-dropdown{background:rgba(18,17,15,.88);backdrop-filter:blur(20px) saturate(1.7);-webkit-backdrop-filter:blur(20px) saturate(1.7);border-color:rgba(255,255,255,.09);box-shadow:0 8px 28px rgba(0,0,0,.5),inset 0 1px 0 rgba(255,255,255,.07)}
  @media(prefers-color-scheme:dark){[data-theme="auto"] .user-dropdown{background:rgba(18,17,15,.88);backdrop-filter:blur(20px) saturate(1.7);-webkit-backdrop-filter:blur(20px) saturate(1.7);border-color:rgba(255,255,255,.09);box-shadow:0 8px 28px rgba(0,0,0,.5),inset 0 1px 0 rgba(255,255,255,.07)}}
  [data-theme="dark"] .inv-export-menu{background:rgba(18,17,15,.88);backdrop-filter:blur(20px) saturate(1.7);-webkit-backdrop-filter:blur(20px) saturate(1.7);border-color:rgba(255,255,255,.09);box-shadow:0 8px 28px rgba(0,0,0,.5),inset 0 1px 0 rgba(255,255,255,.07)}
  @media(prefers-color-scheme:dark){[data-theme="auto"] .inv-export-menu{background:rgba(18,17,15,.88);backdrop-filter:blur(20px) saturate(1.7);-webkit-backdrop-filter:blur(20px) saturate(1.7);border-color:rgba(255,255,255,.09);box-shadow:0 8px 28px rgba(0,0,0,.5),inset 0 1px 0 rgba(255,255,255,.07)}}
  /* Light mode parity — subtle blur on floating menus */
  .user-dropdown{backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px)}
  .inv-export-menu{backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px)}
  ```

- [ ] **Step 2: Verify**

  Restart. In dark mode, open the username dropdown and the export chevron dropdown — both should look glassy, distinct from the solid surface behind them. In light mode, open both — they should look nearly the same as before but with slight glass depth.

  ```bash
  sudo systemctl restart netwatch
  ```

- [ ] **Step 3: Commit**

  ```bash
  git add dashboard.html
  git commit -m "feat: glass treatment on user dropdown and export menu"
  ```

---

## Task 5: Glass on Tab Bar + Modal

**Files:** `dashboard.html` — new CSS block before line 1075

The tab bar (`.tabs`) is a solid surface pill. The modal content panel (`.modal`) is solid white/dark. The backdrop blur on `.modal-overlay` is only 5px — bumping it to 16px makes the background feel properly frosted when a modal is open.

- [ ] **Step 1: Add dark overrides for tabs and modal**

  Append to the dark frost CSS block:

  ```css
  /* ── Deep Frost: tabs ── */
  [data-theme="dark"] .tabs{background:rgba(255,255,255,.055);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border-color:rgba(255,255,255,.08)}
  @media(prefers-color-scheme:dark){[data-theme="auto"] .tabs{background:rgba(255,255,255,.055);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border-color:rgba(255,255,255,.08)}}
  [data-theme="dark"] .tab.active{background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.12)}
  @media(prefers-color-scheme:dark){[data-theme="auto"] .tab.active{background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.12)}}

  /* ── Deep Frost: modal overlay + panel ── */
  [data-theme="dark"] .modal-overlay{backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);background:rgba(0,0,0,.55)}
  @media(prefers-color-scheme:dark){[data-theme="auto"] .modal-overlay{backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);background:rgba(0,0,0,.55)}}
  [data-theme="dark"] .modal{background:rgba(18,17,15,.88);backdrop-filter:blur(28px) saturate(1.8);-webkit-backdrop-filter:blur(28px) saturate(1.8);border-color:rgba(255,255,255,.09);box-shadow:0 24px 64px rgba(0,0,0,.6),inset 0 1px 0 rgba(255,255,255,.08)}
  @media(prefers-color-scheme:dark){[data-theme="auto"] .modal{background:rgba(18,17,15,.88);backdrop-filter:blur(28px) saturate(1.8);-webkit-backdrop-filter:blur(28px) saturate(1.8);border-color:rgba(255,255,255,.09);box-shadow:0 24px 64px rgba(0,0,0,.6),inset 0 1px 0 rgba(255,255,255,.08)}}
  [data-theme="dark"] .modal-foot{background:rgba(255,255,255,.04)}
  @media(prefers-color-scheme:dark){[data-theme="auto"] .modal-foot{background:rgba(255,255,255,.04)}}
  /* Light mode: subtle blur on modal panel */
  .modal{backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px)}
  ```

- [ ] **Step 2: Verify**

  Restart. In dark mode: switch tabs — the active tab pill should look glassy. Open any modal (Import XLSX, Add Host) — the background behind it should be strongly blurred, and the modal panel itself should read as glass rather than solid dark. Confirm modal footer is subtly tinted, not a harsh line.

  ```bash
  sudo systemctl restart netwatch
  ```

- [ ] **Step 3: Commit**

  ```bash
  git add dashboard.html
  git commit -m "feat: glass tab bar, modal overlay and panel in dark mode"
  ```

---

## Task 6: Summary Cards + Host Row Subtle Translucency

**Files:** `dashboard.html` — new CSS block before line 1075

`.scard` are the four stat cards (Hosts up, Avg latency, etc.). They get full glass treatment. The `.scard-health-ok` and `.scard-health-warn` semantic states must not be overridden by the glass rule — keep their colored backgrounds. Host rows (`.row`) get only the faintest translucency; `.down-row` and `.degraded-row` keep their semantic colors.

- [ ] **Step 1: Add overrides**

  Append to the dark frost CSS block:

  ```css
  /* ── Deep Frost: summary cards ── */
  [data-theme="dark"] .scard{background:rgba(26,25,23,.65);backdrop-filter:blur(20px) saturate(1.6);-webkit-backdrop-filter:blur(20px) saturate(1.6);border-color:rgba(255,255,255,.08);box-shadow:0 4px 20px rgba(0,0,0,.3),inset 0 1px 0 rgba(255,255,255,.06)}
  @media(prefers-color-scheme:dark){[data-theme="auto"] .scard{background:rgba(26,25,23,.65);backdrop-filter:blur(20px) saturate(1.6);-webkit-backdrop-filter:blur(20px) saturate(1.6);border-color:rgba(255,255,255,.08);box-shadow:0 4px 20px rgba(0,0,0,.3),inset 0 1px 0 rgba(255,255,255,.06)}}
  /* Preserve semantic health colors — they must override the glass rule above */
  [data-theme="dark"] .scard-health-ok{background:var(--green-soft);backdrop-filter:none;-webkit-backdrop-filter:none;box-shadow:none}
  @media(prefers-color-scheme:dark){[data-theme="auto"] .scard-health-ok{background:var(--green-soft);backdrop-filter:none;-webkit-backdrop-filter:none;box-shadow:none}}
  [data-theme="dark"] .scard-health-warn{background:var(--red-soft);backdrop-filter:none;-webkit-backdrop-filter:none;box-shadow:none}
  @media(prefers-color-scheme:dark){[data-theme="auto"] .scard-health-warn{background:var(--red-soft);backdrop-filter:none;-webkit-backdrop-filter:none;box-shadow:none}}

  /* ── Deep Frost: host rows (very subtle — semantic row colours untouched) ── */
  [data-theme="dark"] .table{background:rgba(26,25,23,.60);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border-color:rgba(255,255,255,.07)}
  @media(prefers-color-scheme:dark){[data-theme="auto"] .table{background:rgba(26,25,23,.60);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border-color:rgba(255,255,255,.07)}}
  ```

  Note: applying glass to the `.table` container (which wraps all `.row` elements) rather than individual rows avoids conflicting with `.down-row` and `.degraded-row` semantic backgrounds.

- [ ] **Step 2: Verify**

  Restart. In dark mode: the four summary cards should look like floating glass panels. If any host is down, trigger that state — confirm the "Hosts up" card still turns red-tinted (`scard-health-warn`) and the `.down-row` stays red. The host list container should feel subtly elevated from the background.

  ```bash
  sudo systemctl restart netwatch
  ```

- [ ] **Step 3: Commit**

  ```bash
  git add dashboard.html
  git commit -m "feat: glass summary cards and host table container in dark mode"
  ```

---

## Task 7: Topology Overlays Enhancement

**Files:** `dashboard.html` — new CSS block before line 1075

The topology overlays, legend, and tooltip are already glass but at lower intensities. Bump them to match the rest of the UI.

- [ ] **Step 1: Add overrides**

  Append to the dark frost CSS block:

  ```css
  /* ── Deep Frost: topology overlays ── */
  [data-theme="dark"] .topo-web-overlay{background:rgba(15,14,13,.78);backdrop-filter:blur(18px) saturate(1.7);-webkit-backdrop-filter:blur(18px) saturate(1.7);border-color:rgba(255,255,255,.09)}
  @media(prefers-color-scheme:dark){[data-theme="auto"] .topo-web-overlay{background:rgba(15,14,13,.78);backdrop-filter:blur(18px) saturate(1.7);-webkit-backdrop-filter:blur(18px) saturate(1.7);border-color:rgba(255,255,255,.09)}}
  [data-theme="dark"] .topo-legend{background:rgba(15,14,13,.88);backdrop-filter:blur(18px) saturate(1.6);-webkit-backdrop-filter:blur(18px) saturate(1.6);border-color:rgba(255,255,255,.09)}
  @media(prefers-color-scheme:dark){[data-theme="auto"] .topo-legend{background:rgba(15,14,13,.88);backdrop-filter:blur(18px) saturate(1.6);-webkit-backdrop-filter:blur(18px) saturate(1.6);border-color:rgba(255,255,255,.09)}}
  [data-theme="dark"] .topo-tip{backdrop-filter:blur(20px) saturate(1.7);-webkit-backdrop-filter:blur(20px) saturate(1.7);box-shadow:0 6px 20px rgba(0,0,0,.45)}
  @media(prefers-color-scheme:dark){[data-theme="auto"] .topo-tip{backdrop-filter:blur(20px) saturate(1.7);-webkit-backdrop-filter:blur(20px) saturate(1.7);box-shadow:0 6px 20px rgba(0,0,0,.45)}}
  ```

- [ ] **Step 2: Verify**

  Restart and open the Topology tab. Hover over a node — tooltip should appear crisp and glassy. Click the legend `?` button — legend panel should feel like a deeper glass pane than before. The overlay stat chips (top-left counters) should look denser.

  ```bash
  sudo systemctl restart netwatch
  ```

- [ ] **Step 3: Commit**

  ```bash
  git add dashboard.html
  git commit -m "feat: enhance topology overlay, legend, and tooltip glass in dark mode"
  ```

---

## Task 8: Status Glow Polish

**Files:** `dashboard.html` — new CSS block before line 1075

Status dots and the live pip get a soft bloom. `uptime-fill` gets a fixed green glow (acceptable since nearly all bars are green when hosts are up; down/degraded hosts show minimal bar anyway). Drawer name dot halos get a tighter outer glow.

- [ ] **Step 1: Add glow overrides**

  Append to the dark frost CSS block:

  ```css
  /* ── Deep Frost: status glow ── */
  [data-theme="dark"] .live-dot{box-shadow:0 0 7px var(--green),0 0 14px rgba(34,197,94,.3)}
  @media(prefers-color-scheme:dark){[data-theme="auto"] .live-dot{box-shadow:0 0 7px var(--green),0 0 14px rgba(34,197,94,.3)}}
  [data-theme="dark"] .dot-up{box-shadow:0 0 0 3px var(--green-bg),0 0 8px rgba(34,197,94,.5)}
  @media(prefers-color-scheme:dark){[data-theme="auto"] .dot-up{box-shadow:0 0 0 3px var(--green-bg),0 0 8px rgba(34,197,94,.5)}}
  [data-theme="dark"] .dot-dn{box-shadow:0 0 0 3px var(--red-bg),0 0 8px rgba(239,68,68,.5)}
  @media(prefers-color-scheme:dark){[data-theme="auto"] .dot-dn{box-shadow:0 0 0 3px var(--red-bg),0 0 8px rgba(239,68,68,.5)}}
  [data-theme="dark"] .dot-wt{box-shadow:0 0 0 3px var(--amber-bg),0 0 8px rgba(245,158,11,.5)}
  @media(prefers-color-scheme:dark){[data-theme="auto"] .dot-wt{box-shadow:0 0 0 3px var(--amber-bg),0 0 8px rgba(245,158,11,.5)}}
  [data-theme="dark"] .uptime-fill{box-shadow:0 0 5px rgba(34,197,94,.45)}
  @media(prefers-color-scheme:dark){[data-theme="auto"] .uptime-fill{box-shadow:0 0 5px rgba(34,197,94,.45)}}
  [data-theme="dark"] .drawer-name-dot.up{box-shadow:0 0 0 3px var(--green-bg),0 0 10px rgba(34,197,94,.45)}
  @media(prefers-color-scheme:dark){[data-theme="auto"] .drawer-name-dot.up{box-shadow:0 0 0 3px var(--green-bg),0 0 10px rgba(34,197,94,.45)}}
  [data-theme="dark"] .drawer-name-dot.down{box-shadow:0 0 0 3px var(--red-bg),0 0 10px rgba(239,68,68,.45)}
  @media(prefers-color-scheme:dark){[data-theme="auto"] .drawer-name-dot.down{box-shadow:0 0 0 3px var(--red-bg),0 0 10px rgba(239,68,68,.45)}}
  [data-theme="dark"] .drawer-name-dot.wait{box-shadow:0 0 0 3px var(--amber-bg),0 0 10px rgba(245,158,11,.45)}
  @media(prefers-color-scheme:dark){[data-theme="auto"] .drawer-name-dot.wait{box-shadow:0 0 0 3px var(--amber-bg),0 0 10px rgba(245,158,11,.45)}}
  ```

- [ ] **Step 2: Verify**

  Restart. In dark mode, the nav "live" dot should have a visible green bloom. Status dots in the host list should glow to match their color. Open a host drawer — the name dot should glow. Uptime bars should have a faint green shimmer. Check that a down host's red dot glows correctly.

  ```bash
  sudo systemctl restart netwatch
  ```

- [ ] **Step 3: Commit**

  ```bash
  git add dashboard.html
  git commit -m "feat: status indicator glow bloom in dark mode"
  ```

---

## Task 9: Form Input Tint + Inventory Editor Glass

**Files:** `dashboard.html` — new CSS block before line 1075

Form inputs inside the inventory editor and add-host modal are solid `var(--surface)`. In dark mode they get a tinted glass look. The inventory editor panel itself (`.inv-edit-overlay` which reuses `.modal-overlay`/`.modal`) is already covered by Task 5, but the form inputs inside need their own treatment.

- [ ] **Step 1: Add overrides**

  Append to the dark frost CSS block:

  ```css
  /* ── Deep Frost: form inputs inside overlays ── */
  [data-theme="dark"] .inv-edit-form input,
  [data-theme="dark"] .inv-edit-form textarea,
  [data-theme="dark"] .inv-edit-form select{background:rgba(255,255,255,.05);border-color:rgba(255,255,255,.10)}
  @media(prefers-color-scheme:dark){
    [data-theme="auto"] .inv-edit-form input,
    [data-theme="auto"] .inv-edit-form textarea,
    [data-theme="auto"] .inv-edit-form select{background:rgba(255,255,255,.05);border-color:rgba(255,255,255,.10)}
  }
  [data-theme="dark"] .row-extra{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.07);border-radius:8px}
  @media(prefers-color-scheme:dark){[data-theme="auto"] .row-extra{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.07);border-radius:8px}}
  ```

- [ ] **Step 2: Verify**

  Restart. Open the inventory editor (click any inventory item → Edit, or click + Add). The input fields should look tinted/glassy against the glass modal panel behind them rather than solid blocks. Open the host editor (pencil icon on a host row) and expand the extras — the extra fields row should look like a soft inset glass panel.

  ```bash
  sudo systemctl restart netwatch
  ```

- [ ] **Step 3: Commit**

  ```bash
  git add dashboard.html
  git commit -m "feat: tinted glass form inputs and row-extra panel in dark mode"
  ```

---

## Self-Review Checklist

**Spec coverage:**
- [x] Nav + drawer glass enhancement → Task 3
- [x] Drawer-backdrop blur bump → Task 3
- [x] Modal overlay backdrop blur bump → Task 5
- [x] Modal content panel glass → Task 5
- [x] User dropdown glass → Task 4
- [x] Export menu glass → Task 4
- [x] Tab bar glass → Task 5
- [x] Summary cards glass → Task 6
- [x] Host rows subtle → Task 6 (via `.table` container)
- [x] Form inputs tint → Task 9
- [x] Topology overlays → Task 7
- [x] Topo legend + tip → Task 7
- [x] Background glow blobs → Task 2
- [x] Status glow (dots, live-dot, uptime, drawer dots) → Task 8
- [x] Z-index bug fix → Task 1
- [x] Form input min attrs → Task 1
- [x] Light mode parity (dropdown, modal, export menu) → Tasks 4 + 5

**Placeholder scan:** None found.

**Type consistency:** All CSS class names verified against live `dashboard.html` — `.scard`, `.row`, `.table`, `.topo-tip`, `.modal`, `.tabs`, `.user-dropdown`, `.inv-export-menu`, `.drawer`, `.inv-edit-form`, `.row-extra`, `.live-dot`, `.dot-up`, `.dot-dn`, `.dot-wt`, `.uptime-fill`, `.drawer-name-dot`.
