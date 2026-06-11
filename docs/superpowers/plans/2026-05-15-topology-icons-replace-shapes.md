# Topology Icons: Replace Shapes With Dimensional Illustrations

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the topology view's colored shape backdrops + thin-stroke icons with dimensional, illustration-quality icons (Variant A from the icon-direction mockup) that stand on their own with soft status-color halos and baked-in LEDs.

**Architecture:** Add a new family of dimensional `#topo-icon-{type}` sprite symbols inside `dashboard.html` alongside the existing thin `#icon-{type}` sprites. Rewrite the topology node render loop to drop the rect/circle backdrops, render the dimensional sprite directly, and route status (up / degraded / down / idle) through a soft drop-shadow halo plus a baked-in LED that uses `currentColor`. Leave the thin `#icon-{type}` sprites untouched — they're still used by the host card list and inventory table (`deviceIcon()` helper at `dashboard.html:1569`).

**Tech Stack:** Vanilla JS, D3 v7, inline SVG, CSS — everything lives in the single-file `dashboard.html`.

---

## Background

The current topology renders each node as `<shape><thin-icon><label>`: a colored rect/circle (`.topo-node-shape`) carries the type-fill and the status-stroke; a small `lucide`-style glyph sits inside. The user finds this visually unsatisfying — too many overlapping signals (shape + fill + stroke + thin icon) and not enough character.

The mockup at `topology-icons-mockup.html` (kept at the repo root for reference; deleted at end of this plan) compared three directions. The user picked **Variant A — Hardware Realism**: each device gets a dimensional, two-tone illustration that looks like the thing (server with vent slats and a drive bay, router with antennae and port LEDs, UPS with a lightning bolt and battery bars, etc.). Status reads via:

1. A soft colored drop-shadow halo around the icon (the "subtle green glow").
2. A small LED/indicator baked into the icon, drawn with `currentColor` (the "visible button with status colors").

The colored rect/circle backdrops go away entirely. Labels move below for every type (no more inline labels inside the network/UPS rectangles). The existing pill-shaped VM badge is dropped — the VM icon carries its own "VM" mark.

**Scope:** Topology web view only. The host card list and inventory table keep the existing thin `#icon-{type}` sprites — dimensional illustrations would be muddy at 14-18px in tight rows.

## File Structure

Only `dashboard.html` is modified. Five regions:

| Region | Approx. lines (at plan time) | Change |
|---|---|---|
| Body-top sprite block (`<svg style="display:none">`) | 1051–1090 | **Append** new `#topo-icon-{type}` symbols. Existing `#icon-{type}` sprites untouched. |
| Topology CSS — node/status block | 290–373 | Rewrite. New `.topo-node-icon` rules replace `.topo-node-shape` rules. |
| Topology CSS — VM badge | 266–267 | Delete. |
| `renderTopologyWeb()` node render loop | ~2039–2123 | Rewrite. No more rects/circles for backdrops; render `<use>` of dimensional sprite + invisible hit-target circle. |
| `appendVmBadge()` helper + caller | 2012–2037, 2114 | Delete the function and its invocation. Keep `sel.classed('topo-node-vm', true)` — used elsewhere for VM detection. |
| `nodeRadiusFor()` | 2284–2291 | Adjust radii to match new icon bounding boxes. |

Line numbers will drift as tasks land. **Each task gives string anchors** to search for rather than relying on raw line numbers.

## Conventions

- New sprite IDs: `topo-icon-host`, `topo-icon-vm`, `topo-icon-network`, `topo-icon-ups`, `topo-icon-disk`, `topo-icon-tablet`, `topo-icon-phone`, `topo-icon-printer`, `topo-icon-peripheral`.
- Inside each sprite, status-driven elements get `class="topo-icon-led"` (replaces the mockup's `a-led`) and use `fill="currentColor"`.
- Each rendered node now has exactly one shape-bearing child: `<use class="topo-node-icon" href="#topo-icon-{type}"/>` plus a transparent `<circle class="topo-node-hit">` for reliable hit-testing and dragging.

## Testing approach

There's no JS test harness for `dashboard.html` (Python tests live in `tests/test_netwatch.py` and cover backend logic). Verification for this plan is **manual browser-load** at the end of each code-changing task. Each verification step lists exactly what to look at and what should still work.

If `chromium` or a similar browser is available locally, the engineer can simply run the netwatch server (`python3 netwatch.py`) and open `http://localhost:8080/`. Otherwise, opening `dashboard.html` directly as a `file://` works for layout/styling but won't have data; an alternate route is loading a sample JSON via the file picker on the landing page.

---

## Tasks

### Task 1: Add dimensional sprite definitions

**Files:**
- Modify: `dashboard.html` — body-top sprite block (search anchor: `<symbol id="icon-host"`)

This task is purely additive. The new `#topo-icon-*` symbols are defined but not yet referenced anywhere, so the dashboard renders identically afterward.

- [ ] **Step 1: Locate the existing sprite block.**

Search `dashboard.html` for `<symbol id="icon-host"` to find the opening of the inline SVG sprite sheet. The sprite sheet starts a few lines above with `<svg xmlns="http://www.w3.org/2000/svg" style="display:none" aria-hidden="true">` and ends with `</svg>` after `<symbol id="icon-peripheral">`.

- [ ] **Step 2: Insert the new dimensional sprite block immediately after the existing `</svg>`.**

This adds a second hidden SVG block on the next line, keeping the two families cleanly separated. Find the closing `</svg>` for the existing sprite sheet (the one right before `<div id="landing-page">`) and insert this block immediately before `<div id="landing-page">`:

```html
<!-- Dimensional ("Variant A") icons used inside the topology web view.
     Each icon has fixed chassis colors plus one or more LED elements
     drawn with currentColor — set per-node via the .topo-status-X CSS
     classes so the LED takes on the up/down/degraded/idle color. -->
<svg xmlns="http://www.w3.org/2000/svg" style="display:none" aria-hidden="true">

  <symbol id="topo-icon-host" viewBox="0 0 32 32">
    <rect x="3" y="7" width="26" height="18" rx="2" fill="#2c3242" stroke="#4a5366" stroke-width=".8"/>
    <rect x="3" y="7" width="26" height="2.4" fill="#3a4256"/>
    <g stroke="#15171f" stroke-width=".9" stroke-linecap="round">
      <line x1="7" y1="13" x2="17" y2="13"/>
      <line x1="7" y1="15" x2="17" y2="15"/>
      <line x1="7" y1="17" x2="17" y2="17"/>
      <line x1="7" y1="19" x2="17" y2="19"/>
      <line x1="7" y1="21" x2="17" y2="21"/>
    </g>
    <rect x="20" y="13" width="6.5" height="3.2" rx=".6" fill="#1b1e28" stroke="#3a4256" stroke-width=".5"/>
    <circle class="topo-icon-led" cx="25" cy="21" r="1.6" fill="currentColor"/>
    <circle cx="25" cy="21" r="2.8" fill="currentColor" opacity=".22"/>
    <rect x="5" y="25" width="3" height="1.4" fill="#1b1e28"/>
    <rect x="24" y="25" width="3" height="1.4" fill="#1b1e28"/>
  </symbol>

  <symbol id="topo-icon-vm" viewBox="0 0 32 32">
    <rect x="4" y="6" width="24" height="20" rx="2.2" fill="#2a2336" stroke="#4a3f5e" stroke-width=".8" stroke-dasharray="2.5 1.6"/>
    <rect x="8" y="10" width="16" height="12" rx="1.6" fill="#3a2f4d" stroke="#695582" stroke-width=".7"/>
    <g stroke="#1b1626" stroke-width=".8" stroke-linecap="round">
      <line x1="10.5" y1="13.5" x2="21.5" y2="13.5"/>
      <line x1="10.5" y1="15.5" x2="21.5" y2="15.5"/>
      <line x1="10.5" y1="17.5" x2="18" y2="17.5"/>
    </g>
    <circle class="topo-icon-led" cx="20.5" cy="18.7" r="1.2" fill="currentColor"/>
    <text x="6.5" y="9.2" font-family="DM Mono, monospace" font-size="4.5" font-weight="600" fill="#b07cd6" letter-spacing=".2">VM</text>
  </symbol>

  <symbol id="topo-icon-network" viewBox="0 0 32 32">
    <line x1="9" y1="3" x2="9" y2="9" stroke="#5a6478" stroke-width="1.4" stroke-linecap="round"/>
    <line x1="23" y1="3" x2="23" y2="9" stroke="#5a6478" stroke-width="1.4" stroke-linecap="round"/>
    <circle cx="9" cy="3" r="1.1" fill="#7a8499"/>
    <circle cx="23" cy="3" r="1.1" fill="#7a8499"/>
    <rect x="3" y="10" width="26" height="14" rx="2.4" fill="#2a3a52" stroke="#4c5e7e" stroke-width=".8"/>
    <rect x="3" y="10" width="26" height="2.5" rx="2.4" fill="#3b4d6c"/>
    <g fill="currentColor">
      <rect class="topo-icon-led" x="7" y="17" width="2.5" height="2.5" rx=".4"/>
      <rect x="11.5" y="17" width="2.5" height="2.5" rx=".4" opacity=".35"/>
      <rect x="16" y="17" width="2.5" height="2.5" rx=".4" opacity=".55"/>
      <rect x="20.5" y="17" width="2.5" height="2.5" rx=".4" opacity=".35"/>
    </g>
    <rect x="24.5" y="17" width="2.8" height="2.5" rx=".4" fill="#7a8499"/>
    <rect x="5" y="24" width="3" height="1.6" fill="#1b1e28"/>
    <rect x="24" y="24" width="3" height="1.6" fill="#1b1e28"/>
  </symbol>

  <symbol id="topo-icon-ups" viewBox="0 0 32 32">
    <rect x="7" y="3" width="18" height="26" rx="2.2" fill="#3a2f1f" stroke="#5d4d33" stroke-width=".8"/>
    <rect x="10" y="5.5" width="12" height="2" rx=".6" fill="#1f1810"/>
    <rect x="10" y="9.5" width="12" height="5" rx=".7" fill="#0f1d10" stroke="#3d6b42" stroke-width=".5"/>
    <path d="M17.2 11l-2 2.6h1.7l-1.4 2.4 2.6-2.6h-1.7l1-2.4z" fill="currentColor"/>
    <g stroke="#7a6a4a" stroke-width="1" stroke-linecap="round">
      <line x1="11" y1="17.5" x2="21" y2="17.5"/>
      <line x1="11" y1="19.5" x2="21" y2="19.5"/>
      <line x1="11" y1="21.5" x2="21" y2="21.5"/>
    </g>
    <circle class="topo-icon-led" cx="13" cy="25.5" r="1.2" fill="currentColor"/>
    <text x="16.6" y="26.5" font-family="DM Mono, monospace" font-size="3.4" fill="#a89169" letter-spacing=".4">UPS</text>
  </symbol>

  <symbol id="topo-icon-disk" viewBox="0 0 32 32">
    <rect x="4" y="5" width="24" height="22" rx="1.5" fill="#283a32" stroke="#4a6a5c" stroke-width=".8"/>
    <circle cx="18" cy="16" r="9" fill="#152821" stroke="#2c4a3b" stroke-width=".6"/>
    <circle cx="18" cy="16" r="5.5" fill="#1a2f27" stroke="#2c4a3b" stroke-width=".4"/>
    <circle cx="18" cy="16" r="1.4" fill="#3a5b4c"/>
    <path d="M7 9 L13 14 L11.5 15.5 L6 11 Z" fill="#3a5b4c" stroke="#5a7e6d" stroke-width=".4"/>
    <circle cx="6.5" cy="10" r="1.4" fill="#5a7e6d"/>
    <circle class="topo-icon-led" cx="26" cy="25" r="1.2" fill="currentColor"/>
  </symbol>

  <symbol id="topo-icon-tablet" viewBox="0 0 32 32">
    <rect x="6" y="2" width="20" height="28" rx="2.4" fill="#1f2f30" stroke="#3a5b5c" stroke-width=".8"/>
    <rect x="8" y="4.5" width="16" height="20" rx="1" fill="#0f1d1e" stroke="#2c4444" stroke-width=".4"/>
    <rect x="9.5" y="6" width="13" height=".8" fill="#2c4444"/>
    <rect x="9.5" y="8" width="9" height=".8" fill="#1d3030"/>
    <rect x="9.5" y="10" width="11" height=".8" fill="#1d3030"/>
    <rect x="9.5" y="13" width="6" height="5" fill="#1d3030" rx=".5"/>
    <rect x="17" y="13" width="5.5" height="5" fill="#1d3030" rx=".5"/>
    <circle cx="16" cy="3.2" r=".55" fill="#0a1213"/>
    <circle cx="16" cy="27" r="1.1" fill="#0f1d1e" stroke="#3a5b5c" stroke-width=".4"/>
    <circle class="topo-icon-led" cx="20.5" cy="27" r=".9" fill="currentColor"/>
  </symbol>

  <symbol id="topo-icon-phone" viewBox="0 0 32 32">
    <rect x="9" y="2" width="14" height="28" rx="2.4" fill="#2f1f30" stroke="#5b3a5b" stroke-width=".8"/>
    <rect x="10.5" y="4.5" width="11" height="20" rx="1" fill="#1d0f1e" stroke="#442c44" stroke-width=".4"/>
    <rect x="13" y="4.5" width="6" height="1.4" rx=".5" fill="#0a050a"/>
    <rect x="11.5" y="7.5" width="8" height=".8" fill="#442c44"/>
    <rect x="11.5" y="9.5" width="5" height=".8" fill="#2c1d2c"/>
    <rect x="11.5" y="13" width="9" height="6" fill="#2c1d2c" rx=".5"/>
    <rect x="13" y="27" width="6" height=".9" rx=".4" fill="#5b3a5b"/>
    <circle class="topo-icon-led" cx="20.5" cy="3.5" r=".7" fill="currentColor"/>
  </symbol>

  <symbol id="topo-icon-printer" viewBox="0 0 32 32">
    <rect x="9" y="3" width="14" height="6" fill="#e8e6e0" stroke="#999" stroke-width=".4"/>
    <line x1="11" y1="5" x2="21" y2="5" stroke="#bbb" stroke-width=".4"/>
    <line x1="11" y1="6.5" x2="19" y2="6.5" stroke="#bbb" stroke-width=".4"/>
    <line x1="11" y1="8" x2="20" y2="8" stroke="#bbb" stroke-width=".4"/>
    <rect x="4" y="9" width="24" height="14" rx="1.8" fill="#3a361f" stroke="#5d5733" stroke-width=".8"/>
    <rect x="8" y="10" width="16" height="2" rx=".4" fill="#15140b"/>
    <rect x="20" y="14" width="6" height="4" rx=".5" fill="#0f1d10" stroke="#3d6b42" stroke-width=".4"/>
    <circle cx="22" cy="20.5" r=".7" fill="#5d5733"/>
    <circle cx="24" cy="20.5" r=".7" fill="#5d5733"/>
    <rect x="6" y="23" width="20" height="6" rx="1.4" fill="#2a2614" stroke="#5d5733" stroke-width=".7"/>
    <rect x="9" y="25" width="14" height=".8" fill="#5d5733"/>
    <circle class="topo-icon-led" cx="7" cy="15.5" r="1.1" fill="currentColor"/>
  </symbol>

  <symbol id="topo-icon-peripheral" viewBox="0 0 32 32">
    <rect x="9" y="9" width="14" height="14" rx="2" fill="#2c2c2c" stroke="#4a4a4a" stroke-width=".8"/>
    <rect x="13.5" y="3" width="5" height="6.5" rx=".4" fill="#4a4a4a" stroke="#6a6a6a" stroke-width=".5"/>
    <rect x="14.4" y="4.5" width="3.2" height="3.5" fill="#1a1a1a"/>
    <circle class="topo-icon-led" cx="16" cy="16.5" r="1.4" fill="currentColor"/>
    <circle cx="16" cy="16.5" r="2.4" fill="currentColor" opacity=".25"/>
    <rect x="12" y="19" width="8" height="1.6" rx=".4" fill="#4a4a4a"/>
    <path d="M16 23 Q 16 28 22 28" fill="none" stroke="#4a4a4a" stroke-width="1.2" stroke-linecap="round"/>
  </symbol>

</svg>
```

- [ ] **Step 3: Sanity-check the file parses.**

Run:
```bash
python3 -c "import html.parser as p
class C(p.HTMLParser):
    def error(self, m): print('ERR:', m)
C().feed(open('dashboard.html').read())
print('parse ok')"
```
Expected output: `parse ok`

Open `dashboard.html` in a browser and confirm the dashboard still loads identically (no visual change yet — the new sprites are defined but unused).

- [ ] **Step 4: Commit.**

```bash
git add dashboard.html
git commit -m "Add dimensional topology icon sprites (#topo-icon-*)

Hardware-realism style two-tone illustrations to be used in the
topology view. Existing #icon-* thin sprites are untouched and
still used by the host card list and inventory table."
```

---

### Task 2: Switch topology nodes to render the dimensional icons

**Files:**
- Modify: `dashboard.html` — topology CSS block (search anchor: `.topo-node-shape{fill:var(--surface)`)
- Modify: `dashboard.html` — `renderTopologyWeb()` node render loop (search anchor: `// Render the node body shape based on type`)
- Modify: `dashboard.html` — `nodeRadiusFor` (search anchor: `function nodeRadiusFor(d)`)

After this task the topology renders with the new dimensional icons. Some now-orphaned CSS (`.topo-node-shape`, `.topo-node-label-inside`, VM badge styles) is intentionally left in place; Task 3 cleans it up. This split keeps each commit individually working.

- [ ] **Step 1: Add the new CSS block.**

Find the existing CSS rule starting with `.topo-node-shape{fill:var(--surface);stroke:var(--border);stroke-width:2;...` (search anchor: `.topo-node-shape{fill:var(--surface)`). Immediately **before** that line, insert this new block:

```css
/* ─── Dimensional icon nodes ────────────────────────────────────
   The topology renders each node as a self-contained dimensional
   icon (sprite #topo-icon-{type}) sitting on top of an invisible
   hit-target circle. Status is encoded two ways:
     (1) via `color` on the parent .topo-status-X class, which the
         icon's LED element inherits through `currentColor`
     (2) via a soft drop-shadow halo on .topo-node-icon
*/
.topo-node-icon{transition:transform .25s cubic-bezier(.4,0,.2,1),filter .3s ease}
.topo-node-hit{fill:transparent;stroke:none;pointer-events:all}

.topo-status-up      .topo-node-icon{filter:drop-shadow(0 0 4px rgba(93,187,141,.45)) drop-shadow(0 0 9px rgba(93,187,141,.22))}
.topo-status-degraded .topo-node-icon{filter:drop-shadow(0 0 4px rgba(240,169,59,.50)) drop-shadow(0 0 9px rgba(240,169,59,.22))}
.topo-status-down    .topo-node-icon{filter:drop-shadow(0 0 5px rgba(229,115,115,.55)) drop-shadow(0 0 11px rgba(229,115,115,.28))}
.topo-status-idle    .topo-node-icon{filter:drop-shadow(0 0 3px rgba(122,122,122,.30))}
.topo-status-unknown .topo-node-icon{filter:drop-shadow(0 0 3px rgba(122,122,122,.18))}

/* currentColor drives the .topo-icon-led inside each sprite */
.topo-status-up      {color:#5dbb8d}
.topo-status-degraded{color:#f0a93b}
.topo-status-down    {color:#e57373}
.topo-status-idle    {color:#7a7a7a}
.topo-status-unknown {color:#5a5a5a}

/* Hover / focus: subtle scale-up + a touch more halo */
.topo-node:hover .topo-node-icon,
.topo-node.focus .topo-node-icon{transform:scale(1.06)}
.topo-node-icon{transform-origin:center;transform-box:fill-box}

/* Ambient breathing on UP nodes (stagger applied per-node via JS) */
@keyframes topo-icon-breathe{
  0%,100%{transform:scale(1)}
  50%    {transform:scale(1.03)}
}
.topo-status-up .topo-node-icon{animation:topo-icon-breathe 4.6s ease-in-out infinite}

/* Down-pulse: dim the icon rhythmically */
@keyframes topo-icon-down-pulse{
  0%,100%{opacity:1}
  50%    {opacity:.72}
}
.topo-status-down .topo-node-icon{animation:topo-icon-down-pulse 2.2s ease-in-out infinite}

/* Hover scale wins over the breathing animation */
.topo-node:hover .topo-node-icon,
.topo-node.focus .topo-node-icon{animation:none}

/* Reduced-motion: kill the animations but keep the halo */
@media (prefers-reduced-motion: reduce){
  .topo-status-up .topo-node-icon,
  .topo-status-down .topo-node-icon{animation:none}
}
```

- [ ] **Step 2: Rewrite the node render loop.**

Find the comment `// Render the node body shape based on type` (search anchor: `// Render the node body shape based on type`). The block that follows (from `nodeSel.each(function(d){` through the matching `});` — roughly 80 lines) is what we replace.

Replace the entire `nodeSel.each(function(d){ ... });` block (from `nodeSel.each(function(d){` through the closing `});`, ending after `}`) with this:

```javascript
  // Render the node body as a dimensional icon. There is no longer
  // a backdrop shape; status is conveyed by the parent .topo-status-*
  // class (drives both `color:` for the icon's LED and the drop-
  // shadow halo on .topo-node-icon) plus the breathing/pulse
  // animations defined in CSS.
  nodeSel.each(function(d){
    const sel = d3.select(this);
    // iconSize: rendered px width/height of the sprite. hitR: radius
    // of the invisible hit-target circle (covers the icon + a bit of
    // breathing room so drag/click still feels generous).
    let iconSize, hitR;
    if(d.device_type === 'network'){
      iconSize = 64; hitR = 30;
    } else if(d.device_type === 'ups'){
      iconSize = 56; hitR = 28;
    } else if(d.device_type === 'host'){
      iconSize = 52; hitR = 26;
    } else if(d.device_type === 'disk'){
      iconSize = 48; hitR = 24;
    } else if(d.device_type === 'vm'){
      iconSize = 44; hitR = 22;
    } else if(d.device_type === 'printer'){
      iconSize = 44; hitR = 22;
    } else {
      // tablet, phone, peripheral, and any fallback
      iconSize = 40; hitR = 20;
    }
    const iconHref = '#topo-icon-' + (d.device_type || 'host');

    // Invisible hit target sits first so the icon paints over it
    sel.append('circle')
      .attr('class', 'topo-node-hit')
      .attr('r', hitR);

    // The dimensional sprite
    sel.append('use')
      .attr('class', 'topo-node-icon')
      .attr('href', iconHref)
      .attr('x', -iconSize/2).attr('y', -iconSize/2)
      .attr('width', iconSize).attr('height', iconSize);

    // Label sits below the icon for every type now
    sel.append('text')
      .attr('class', 'topo-node-label-below')
      .attr('y', iconSize/2 + 14)
      .text(truncateLabel(d.name, 20));

    // VM-class marker (still used by tooltip + isVm detection
    // elsewhere). No more pill badge — the VM icon carries its own
    // "VM" mark.
    if(vmIds.has(d.id)){
      sel.classed('topo-node-vm', true);
    }

    // Stagger the ambient breathing so the network doesn't pulse
    // in unison. Hash the node id into a delay between 0–4s.
    const iconEl = sel.select('.topo-node-icon');
    if(!iconEl.empty()){
      const delay = ((d.id * 1.7) % 4).toFixed(2);
      iconEl.style('animation-delay', delay + 's');
    }
  });
```

- [ ] **Step 3: Update `nodeRadiusFor` so the force layout's collision radii match the new icon footprints.**

Find `function nodeRadiusFor(d){` and replace its body. The new values are the hit-radius from Step 2 plus a small padding so labels don't overlap when nodes settle:

```javascript
function nodeRadiusFor(d){
  if(d.device_type === 'network') return 34;
  if(d.device_type === 'ups')     return 32;
  if(d.device_type === 'disk')    return 28;
  if(d.device_type === 'vm')      return 26;
  if(d.device_type === 'printer') return 26;
  if(d.device_type === 'peripheral' || d.device_type === 'tablet'
     || d.device_type === 'phone') return 24;
  return 30; // host
}
```

- [ ] **Step 4: Manual browser verification.**

Run netwatch and open the dashboard. On the Topology tab:

1. Each node shows a dimensional icon (server with vents, router with antennae, etc.) — no colored rect/circle behind it.
2. UP nodes glow softly green; DOWN nodes glow red and gently pulse; DEGRADED nodes glow amber; IDLE/unknown nodes are dim.
3. Each icon's LED (the dot on a server chassis, the bolt on a UPS, the lit port on a router) takes the status color.
4. Labels sit below every icon (including network/UPS — previously they were inline).
5. Hover over a node — it scales up slightly and the halo brightens.
6. Drag a node — drag still works (the invisible hit circle is doing its job).
7. Click a node to focus — the focused-node visual treatment (scale + halo) holds.
8. VM nodes carry a baked "VM" mark in the top-left of the icon; the old pill badge is gone.

Known cosmetic issue at this point: the orphaned `.topo-node-shape` / `.topo-vm-badge` / `.topo-node-label-inside` CSS rules still exist but no longer match anything — they don't break rendering. Task 3 removes them.

- [ ] **Step 5: Commit.**

```bash
git add dashboard.html
git commit -m "Switch topology nodes to dimensional icons; drop shape backdrops

Each node now renders as a self-contained #topo-icon-* sprite with
a soft status-color halo (drop-shadow) and an LED inside the icon
driven by currentColor. The colored rect/circle backdrops are gone,
labels move below every icon, and the pill VM badge is replaced by
the in-icon VM mark.

Hit targets preserved via an invisible <circle class='topo-node-hit'>
behind each icon."
```

---

### Task 3: Remove now-orphaned CSS and the VM badge helper

**Files:**
- Modify: `dashboard.html` — VM badge CSS (search anchor: `.topo-vm-badge rect{fill:#b07cd6`)
- Modify: `dashboard.html` — old node-shape CSS (search anchor: `.topo-node-shape{fill:var(--surface)`)
- Modify: `dashboard.html` — `appendVmBadge()` function (search anchor: `function appendVmBadge(sel, dtype)`)

Pure cleanup. After this task the dashboard renders identically to after Task 2, but the file has no dead rules.

- [ ] **Step 1: Delete the VM badge CSS.**

Find the two lines:

```css
.topo-vm-badge rect{fill:#b07cd6;stroke:#8a5ab2;stroke-width:.5}
.topo-vm-badge text{fill:#fff;font-family:'DM Mono',monospace;font-size:8px;font-weight:600;letter-spacing:.5px;pointer-events:none}
```

Delete both lines.

- [ ] **Step 2: Delete the old node-shape CSS block.**

Find the line starting with `.topo-node-shape{fill:var(--surface);stroke:var(--border);stroke-width:2;` (search anchor: `.topo-node-shape{fill:var(--surface)`) and delete the entire span through the line `.topo-node-printer  .topo-node-shape{fill:#2d2a1e}` (the last `.topo-node-{type} .topo-node-shape` rule). Specifically, delete this entire block of rules:

```css
.topo-node-shape{fill:var(--surface);stroke:var(--border);stroke-width:2;transition:stroke .3s,stroke-width .3s,fill .3s}
.topo-node:hover .topo-node-shape,.topo-node.focus .topo-node-shape{stroke-width:3}

/* Status colors (border) + ambient glows */
.topo-status-up        .topo-node-shape{stroke:#5dbb8d}
.topo-status-up        > .topo-node-shape{filter:url(#topo-glow-up)}
.topo-status-degraded  .topo-node-shape{stroke:#f0a93b}
.topo-status-degraded  > .topo-node-shape{filter:url(#topo-glow-degraded)}
.topo-status-down      .topo-node-shape{stroke:#e57373}
.topo-status-down      > .topo-node-shape{filter:url(#topo-glow-down)}
.topo-status-idle      .topo-node-shape{stroke:#7a7a7a}
.topo-status-unknown   .topo-node-shape{stroke:var(--border-light)}

/* Ambient breathing on UP nodes - very subtle scale animation, staggered
   via animation-delay (set per-node by JS) so the network doesn't pulse
   in unison. */
@keyframes topo-breathe{
  0%,100% {transform:scale(1)}
  50%     {transform:scale(1.02)}
}
.topo-status-up .topo-node-shape{animation:topo-breathe 4s ease-in-out infinite;transform-origin:center;transform-box:fill-box}

/* Status-down nodes get a stronger pulse to attract attention */
@keyframes topo-down-pulse{
  0%,100% {opacity:1}
  50%     {opacity:.75}
}
.topo-status-down .topo-node-shape{animation:topo-down-pulse 2.2s ease-in-out infinite;transform-origin:center;transform-box:fill-box}
```

Keep the unrelated `.topo-grid-bg`, `.topo-edge-line`, and motion-preferences blocks that follow.

- [ ] **Step 3: Update the `.topo-node-shape` reference inside `.topo-node`/`@media (prefers-reduced-motion)` blocks.**

Find:

```css
.topo-node{transition:opacity .3s cubic-bezier(.4,0,.2,1)}
.topo-node-shape{transition:stroke .4s cubic-bezier(.4,0,.2,1),stroke-width .25s cubic-bezier(.4,0,.2,1)}
```

Delete the `.topo-node-shape{transition:...}` line (the `.topo-node` line stays).

Find:

```css
@media (prefers-reduced-motion: reduce){
  .topo-status-up .topo-node-shape,
  .topo-status-down .topo-node-shape,
  .topo-grid-bg{animation:none}
  .topo-edge-line,.topo-edge-flow{filter:none}
}
```

Delete this entire block — the new icon-based version was already added in Task 2.

- [ ] **Step 4: Delete the type-specific fill rules.**

Find and delete the entire block:

```css
/* Type-specific fills */
.topo-node-network  .topo-node-shape{fill:#1a2540}
.topo-node-ups      .topo-node-shape{fill:#3a2e15}
.topo-node-disk     .topo-node-shape{fill:#1f2a25}
.topo-node-vm       .topo-node-shape{fill:#1f1a26}
.topo-node-peripheral .topo-node-shape{fill:var(--subtle)}
.topo-node-tablet   .topo-node-shape{fill:#1a2d2d}
.topo-node-phone    .topo-node-shape{fill:#2a1e2d}
.topo-node-printer  .topo-node-shape{fill:#2d2a1e}
```

- [ ] **Step 5: Delete the inside-label rule.**

Find and delete:

```css
.topo-node-label-inside{text-anchor:middle;fill:var(--text);font-family:'DM Sans',sans-serif;font-size:11px;font-weight:500;pointer-events:none}
```

The `.topo-node-label-below` rule stays — it's the only label class now.

- [ ] **Step 6: Update the transient pulse animation to target the icon, not the shape.**

Find:

```css
.topo-pulsing .topo-node-shape{animation:topo-pulse 1.6s ease-in-out}
.topo-status-down.topo-pulsing .topo-node-shape{animation:topo-pulse 1.6s ease-in-out;filter:drop-shadow(0 0 12px #e57373)}
.topo-status-up.topo-pulsing   .topo-node-shape{animation:topo-pulse 1.6s ease-in-out;filter:drop-shadow(0 0 12px #5dbb8d)}
```

Replace with:

```css
.topo-pulsing .topo-node-icon{animation:topo-pulse 1.6s ease-in-out}
.topo-status-down.topo-pulsing .topo-node-icon{animation:topo-pulse 1.6s ease-in-out;filter:drop-shadow(0 0 12px #e57373)}
.topo-status-up.topo-pulsing   .topo-node-icon{animation:topo-pulse 1.6s ease-in-out;filter:drop-shadow(0 0 12px #5dbb8d)}
```

- [ ] **Step 7: Delete the `appendVmBadge` helper function.**

Find `function appendVmBadge(sel, dtype){` and delete the entire function — from the preceding comment block (`// Helper: append a small "VM" badge to a node's group. Position is`) through the function's closing `}` (the last line is `g.append('text').attr('text-anchor', 'middle').attr('y', 3.5).text('VM');` followed by `}` and `}`).

Specifically delete this block:

```javascript
  // Helper: append a small "VM" badge to a node's group. Position is
  // type-dependent so it sits at the top-right of whatever shape we drew.
  function appendVmBadge(sel, dtype){
    let bx, by;
    if(dtype === 'network' || dtype === 'ups'){
      bx = 110/2 - 8;  by = -38/2 - 4;  // top-right of rectangle
    } else if(dtype === 'disk'){
      bx = 32/2 - 4;   by = -32/2 - 4;  // top-right of square
    } else if(dtype === 'peripheral' || dtype === 'tablet'
              || dtype === 'phone' || dtype === 'printer'){
      bx = 14 - 4;     by = -14 - 4;    // top-right of small circle
    } else {
      bx = 22 - 4;     by = -22 - 4;    // top-right of host circle
    }
    const pillW = 22, pillH = 12;
    const g = sel.append('g').attr('class', 'topo-vm-badge')
      .attr('transform', 'translate(' + bx + ',' + by + ')');
    g.append('rect')
      .attr('x', -pillW/2).attr('y', -pillH/2)
      .attr('width', pillW).attr('height', pillH)
      .attr('rx', 4).attr('ry', 4);
    g.append('text')
      .attr('text-anchor', 'middle')
      .attr('y', 3.5)
      .text('VM');
  }
```

- [ ] **Step 8: Remove the now-orphaned `addGlow` SVG filter defs.**

These three filters were referenced by the old `.topo-status-X > .topo-node-shape{filter:url(...)}` rules removed in Step 2. They're now defined but never used.

Find this block (search anchor: `// Status glow filters - drop-shadow`):

```javascript
  // Status glow filters - drop-shadow with status-appropriate color
  function addGlow(id, color, stddev){
    const f = defs.append('filter')
      .attr('id', id)
      .attr('x', '-50%').attr('y', '-50%')
      .attr('width', '200%').attr('height', '200%');
    f.append('feGaussianBlur')
      .attr('in', 'SourceGraphic')
      .attr('stdDeviation', stddev)
      .attr('result', 'blur');
    f.append('feFlood')
      .attr('flood-color', color)
      .attr('flood-opacity', '0.6')
      .attr('result', 'color');
    f.append('feComposite')
      .attr('in', 'color').attr('in2', 'blur')
      .attr('operator', 'in').attr('result', 'shadow');
    const merge = f.append('feMerge');
    merge.append('feMergeNode').attr('in', 'shadow');
    merge.append('feMergeNode').attr('in', 'SourceGraphic');
  }
  addGlow('topo-glow-up',       '#5dbb8d', 4);
  addGlow('topo-glow-down',     '#e57373', 6);
  addGlow('topo-glow-degraded', '#f0a93b', 5);
```

Delete the entire block. The status halos are now done via CSS `drop-shadow()` filters added in Task 2.

- [ ] **Step 9: Sanity-check the file parses and renders.**

Run:

```bash
python3 -c "import html.parser as p
class C(p.HTMLParser):
    def error(self,m): print('ERR:', m)
C().feed(open('dashboard.html').read())
print('parse ok')"
```

Expected: `parse ok`.

Grep for stale references:

```bash
grep -n 'topo-node-shape\|topo-vm-badge\|appendVmBadge\|topo-node-label-inside\|topo-glow-\|addGlow' dashboard.html
```

Expected: no matches (everything has been removed).

Open the dashboard in a browser. Verify the topology renders the same as after Task 2 (no regression from the cleanup).

- [ ] **Step 10: Commit.**

```bash
git add dashboard.html
git commit -m "Remove now-orphaned topology shape/VM-badge CSS and JS

Cleans up the rules and helper function that were left behind when
nodes switched to dimensional icons. No visual change."
```

---

### Task 4: Tuning pass and reference-file cleanup

**Files:**
- Modify: `dashboard.html` — fine-tune any of: per-type `iconSize` values, halo opacity, breathing/pulse timing
- Delete: `topology-icons-mockup.html` (no longer needed)

This is the "open it with real data and nudge what doesn't read well" pass. Specifics depend on what the user sees, but here are the load-bearing tuning levers:

- [ ] **Step 1: Run netwatch and observe real topology.**

```bash
python3 netwatch.py &
```

Open `http://localhost:8080/` (or the configured port). Wait for the first scan to complete. Click the **Topology** tab.

- [ ] **Step 2: Sanity-check the four key conditions.**

For each, note whether the visual treatment is clearly distinguishable at a glance from across the screen:

1. **Status legibility.** Find an `up`, a `down`, and (if present) a `degraded` node. Is the status obvious at 1m away from the screen?
2. **Density legibility.** When nodes settle and end up clustered, can you still tell types apart (e.g. host vs VM vs disk)? Look for ones that get squashed in the corner.
3. **Hover/focus affordance.** Hover several nodes. Does the scale+halo response feel responsive? Compare to clicking-to-focus.
4. **Label collisions.** With labels now under *every* icon (network/UPS used to be inline), are any labels overlapping that didn't before? `spreadOverlappingLabels` should handle this — verify.

- [ ] **Step 3: Tuning levers (apply only if needed).**

If any icon reads too small, bump its `iconSize` in the render loop and its matching radius in `nodeRadiusFor`:

```javascript
// In the renderTopologyWeb render loop:
if(d.device_type === 'host'){
  iconSize = 56; hitR = 28;  // was 52 / 26
}

// And in nodeRadiusFor:
return 32; // host  (was 30)
```

If halos look too bright in a dense cluster, weaken the second drop-shadow:

```css
.topo-status-up      .topo-node-icon{filter:drop-shadow(0 0 4px rgba(93,187,141,.45)) drop-shadow(0 0 7px rgba(93,187,141,.16))}
```

If the down-pulse opacity dip looks jarring on the rich illustrations, soften it:

```css
@keyframes topo-icon-down-pulse{
  0%,100%{opacity:1}
  50%    {opacity:.82}   /* was .72 */
}
```

If any per-type LED is too small / placed wrong to read at the rendered size, edit the LED `<circle class="topo-icon-led">` inside that sprite — e.g. bump its `r` from `.7` to `1` for the phone:

```html
<circle class="topo-icon-led" cx="20.5" cy="3.5" r="1" fill="currentColor"/>
```

Only apply changes you actually need based on Step 2 observations.

- [ ] **Step 4: Delete the mockup file.**

```bash
rm topology-icons-mockup.html
```

It served its purpose (selecting Variant A) and is no longer referenced anywhere.

- [ ] **Step 5: Verify-before-completion checklist.**

Before claiming done, confirm all of these by running them:

```bash
# 1. dashboard parses
python3 -c "import html.parser as p
class C(p.HTMLParser):
    def error(self,m): print('ERR:', m)
C().feed(open('dashboard.html').read())
print('parse ok')"

# 2. backend tests still pass
python3 -m pytest tests/ -v

# 3. no stale references remain
grep -n 'topo-node-shape\|topo-vm-badge\|appendVmBadge\|topo-node-label-inside\|#icon-host' dashboard.html | grep -v 'deviceIcon\|<symbol id="icon-' || echo 'clean'
```

Expected: parse ok, all pytest tests pass, grep returns only the lines inside `deviceIcon()` and the existing `#icon-host` symbol (used by host card / inventory table — must remain).

Open the topology view once more and confirm:
- [ ] Every device type renders as a dimensional icon
- [ ] Status colors are obvious from across the room
- [ ] Hover, drag, click-to-focus, and tooltips still work
- [ ] No console errors
- [ ] VM nodes show the baked "VM" mark in the icon (no separate pill)

- [ ] **Step 6: Commit and finish.**

If any tuning changes were made:

```bash
git add dashboard.html
git commit -m "Tune topology icon sizes / halo intensity / pulse timing

Adjustments after viewing live data: <describe what was tuned>"
```

Always:

```bash
git add -u  # picks up the mockup deletion
git commit -m "Remove topology-icons-mockup.html

Mockup file used to evaluate three icon directions. The chosen
direction (Variant A) is now implemented in dashboard.html."
```
