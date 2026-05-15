#!/usr/bin/env python3
"""
netwatch patch: drawer + topology polish.

A focused round of small UX wins covering the surfaces I've watched
get rough around the edges over many sessions. Nothing here is a new
feature - everything is making existing things feel better.

WHAT'S INCLUDED:

  1. Drawer specs section: switch from rigid 110px label column to
     a flexible layout. Long property keys ('TPM VERSION', 'CPU SCORE',
     'IP ADDRESS', etc.) no longer clip on the right.

  2. Smarter truncation for topology node labels. Today we cut at 14
     chars mid-word, producing 'TP Link 24-po...' and 'Lenovo ThinkPad
     T...'. New behavior: prefer space boundaries, expand to 20 chars
     when there's room, use proper ellipsis (one Unicode char rather
     than three dots).

  3. Topology tooltip clips off the right/bottom edges of the canvas.
     Now flips to the opposite side of the cursor when it would clip,
     keeping the tooltip always fully visible.

  4. Drawer close button: bigger touch target, proper multiplication-
     sign character (×), brighter on hover. Easier to hit on phones,
     more obvious affordance.

  5. Footer leaks into fullscreen via the topo-fullscreen-active hide
     list - I added '<footer>' to the list but the actual page uses
     '<div class="footer">'. Fixed by also hiding .footer.

  6. Topology loading state: replace italic gray text with a softly
     pulsing dot. Feels alive while D3 loads from CDN. Same on the
     'still no data' state.

  7. Status pill in the linked-host card: IDLE pill currently uses
     muted gray that can blend with surrounding text. Brightened
     slightly so non-UP statuses pop more.

WHAT I CONSIDERED BUT DEFERRED:

  - Theme toggle redesign (works, change is risky)
  - Color contrast WCAG audit (deserves a dedicated session)
  - Form button styling consistency (mostly cosmetic)
  - Mobile drawer animation timing (no real complaint observed)
  - Keyboard shortcuts (worth a separate dedicated patch)

Must be applied AFTER patch_fullscreen_height_fix.py.

Run once from ~/netwatch/:
    python3 patch_polish_drawer_topo.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_polish.
Idempotent - safe to re-run.
"""

import os, shutil, sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_polish"
SENTINEL = "topo-loading-pulse"


PATCHES = [
    # ──────────────────────────────────────────────────────────────────────
    # 1. inv-drawer-row: drop the rigid 110px column, switch to a flexible
    # layout where labels can shrink-to-fit but values get the rest.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''.inv-drawer-row{display:grid;grid-template-columns:110px 1fr;gap:10px;padding:9px 12px;border-bottom:1px solid var(--border-light);font-size:13px;align-items:start}''',
        '''.inv-drawer-row{display:grid;grid-template-columns:minmax(90px,max-content) 1fr;gap:14px;padding:9px 12px;border-bottom:1px solid var(--border-light);font-size:13px;align-items:start}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 2. Mobile breakpoint: same fix on smaller screens
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  .inv-drawer-row{grid-template-columns:90px 1fr;font-size:12px}''',
        '''  .inv-drawer-row{grid-template-columns:minmax(80px,max-content) 1fr;font-size:12px;gap:10px}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 3. Smarter truncation for node labels. Replace the existing simple
    # truncateLabel with a version that:
    #   - Prefers cutting at a space boundary if one exists in the cut zone
    #   - Uses single-char ellipsis (\u2026) instead of three dots
    #   - Expands the budget to 20 characters since we have more space now
    # ──────────────────────────────────────────────────────────────────────
    (
        '''function truncateLabel(s, max){
  if(!s) return '';
  return s.length > max ? s.substring(0, max - 1) + '\\u2026' : s;
}''',
        '''function truncateLabel(s, max){
  if(!s) return '';
  if(s.length <= max) return s;
  // Prefer breaking at a space boundary if one exists in the back half
  // of the cut. This avoids ugly mid-word cuts like "TP Link 24-po..."
  // which become "TP Link 24-port..." or just "TP Link..." instead.
  const halfBack = Math.floor(max * 0.6);
  const lastSpace = s.lastIndexOf(\' \', max - 1);
  if(lastSpace >= halfBack){
    return s.substring(0, lastSpace) + \'\\u2026\';
  }
  return s.substring(0, max - 1) + \'\\u2026\';
}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 4. Bump the truncation budget where it's called - 20 chars for
    # "below the node" labels (was 18), 16 for "inside the node" labels
    # (was 14).
    # ──────────────────────────────────────────────────────────────────────
    (
        '''      sel.append('text').attr('class', 'topo-node-label-inside')
        .attr('y', 5).text(truncateLabel(d.name, 14));''',
        '''      sel.append('text').attr('class', 'topo-node-label-inside')
        .attr('y', 5).text(truncateLabel(d.name, 16));'''
    ),

    (
        '''        .attr('y', s/2 + 14).text(truncateLabel(d.name, 18));
    } else if(d.device_type === 'peripheral'){
      sel.append('circle').attr('class', 'topo-node-shape').attr('r', 14);
      sel.append('text').attr('class', 'topo-node-label-below')
        .attr('y', 14 + 14).text(truncateLabel(d.name, 18));''',
        '''        .attr('y', s/2 + 14).text(truncateLabel(d.name, 20));
    } else if(d.device_type === 'peripheral'){
      sel.append('circle').attr('class', 'topo-node-shape').attr('r', 14);
      sel.append('text').attr('class', 'topo-node-label-below')
        .attr('y', 14 + 14).text(truncateLabel(d.name, 20));'''
    ),

    (
        '''      sel.append('circle').attr('class', 'topo-node-shape').attr('r', 16);
      sel.append('text').attr('class', 'topo-node-label-below')
        .attr('y', 16 + 14).text(truncateLabel(d.name, 18));
    } else {
      // host (default)
      sel.append('circle').attr('class', 'topo-node-shape').attr('r', 22);
      sel.append('text').attr('class', 'topo-node-label-below')
        .attr('y', 22 + 14).text(truncateLabel(d.name, 18));
    }''',
        '''      sel.append('circle').attr('class', 'topo-node-shape').attr('r', 16);
      sel.append('text').attr('class', 'topo-node-label-below')
        .attr('y', 16 + 14).text(truncateLabel(d.name, 20));
    } else {
      // host (default)
      sel.append('circle').attr('class', 'topo-node-shape').attr('r', 22);
      sel.append('text').attr('class', 'topo-node-label-below')
        .attr('y', 22 + 14).text(truncateLabel(d.name, 20));
    }'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 5. Tooltip: smart positioning to avoid clipping at edges. Currently
    # always offsets +12,+12 from cursor. New behavior: measure tooltip
    # size after building, flip to the other side of cursor if it would
    # clip the right or bottom edge of the container.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  nodeSel.on('mousemove', function(ev, d){
    const rect = container.getBoundingClientRect();
    tip.style('left', (ev.clientX - rect.left + 12) + 'px')
       .style('top',  (ev.clientY - rect.top  + 12) + 'px')
       .style('display', 'block')
       .html(buildNodeTip(d));
  }).on('mouseleave.tip', () => tip.style('display', 'none'));''',
        '''  nodeSel.on('mousemove', function(ev, d){
    const rect = container.getBoundingClientRect();
    // First render so we can measure
    tip.style('display', 'block').html(buildNodeTip(d));
    const tipNode = tip.node();
    const tipW = tipNode ? tipNode.offsetWidth : 240;
    const tipH = tipNode ? tipNode.offsetHeight : 60;
    const cx = ev.clientX - rect.left;
    const cy = ev.clientY - rect.top;
    const margin = 14;
    // Default: down-and-right of cursor
    let x = cx + 12;
    let y = cy + 12;
    // Flip to LEFT if would clip right edge
    if(x + tipW + margin > rect.width){
      x = cx - tipW - 12;
    }
    // Flip ABOVE if would clip bottom edge
    if(y + tipH + margin > rect.height){
      y = cy - tipH - 12;
    }
    // Final clamp so we never go off the left/top either
    x = Math.max(8, x);
    y = Math.max(8, y);
    tip.style('left', x + 'px').style('top', y + 'px');
  }).on('mouseleave.tip', () => tip.style('display', 'none'));'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 6. Drawer close button: bigger touch target, proper × character.
    # Was: small bare "x" letter.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''.drawer-close{background:transparent;border:none;color:var(--hint);cursor:pointer;font-size:16px;padding:4px 10px;border-radius:6px;line-height:1}
.drawer-close:hover{background:var(--subtle);color:var(--text)}''',
        '''.drawer-close{background:transparent;border:none;color:var(--hint);cursor:pointer;font-size:20px;padding:6px 12px;border-radius:6px;line-height:1;font-family:\'DM Sans\',sans-serif;min-width:34px;min-height:34px;display:flex;align-items:center;justify-content:center;transition:all .15s}
.drawer-close:hover{background:var(--subtle);color:var(--text)}
.drawer-close:active{transform:scale(.94)}'''
    ),

    (
        '''    <button class="drawer-close" onclick="closeDrawer()" aria-label="Close">x</button>''',
        '''    <button class="drawer-close" onclick="closeDrawer()" aria-label="Close">\u00D7</button>'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 7. Fullscreen hide list: also hide .footer (class). Currently only
    # hides <footer> element, but the page uses <div class="footer">.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''body.topo-fullscreen-active footer,
body.topo-fullscreen-active .nav-strip,''',
        '''body.topo-fullscreen-active footer,
body.topo-fullscreen-active .footer,
body.topo-fullscreen-active .nav-strip,'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 8. Topology loading state: replace italic text with a soft pulse.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''.topo-web-loading{display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:13px;font-style:italic;padding:40px;text-align:center}''',
        '''.topo-web-loading{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:13px;padding:40px;text-align:center;gap:14px}
.topo-web-loading::before{content:\'\';display:block;width:8px;height:8px;border-radius:50%;background:var(--blue);animation:topo-loading-pulse 1.4s ease-in-out infinite}
.topo-web-loading.topo-web-error::before{display:none}
@keyframes topo-loading-pulse{
  0%,100% {opacity:.3;transform:scale(.9)}
  50%     {opacity:1;transform:scale(1.1);box-shadow:0 0 16px var(--blue)}
}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 9. Brighter IDLE pill in the linked-host card. Currently uses
    # muted gray that blends with surrounding text. Make it visibly a
    # status indicator.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''.inv-link-pill.idle{background:var(--subtle);color:var(--muted);border:1px solid var(--border)}''',
        '''.inv-link-pill.idle{background:var(--subtle);color:var(--text);border:1px solid var(--border-light);font-weight:500}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 10. Bump version
    # ──────────────────────────────────────────────────────────────────────
    (
        'netwatch v3.31 - raspberry pi',
        'netwatch v3.32 - raspberry pi'
    ),
]


def main():
    if not os.path.isfile(TARGET):
        print(f"ERROR: {TARGET} not found.")
        sys.exit(1)

    content = open(TARGET).read()

    if SENTINEL in content:
        print(f"NOTE: '{SENTINEL}' found - patch already applied.")
        sys.exit(0)

    if "_topoResizeObserver" not in content:
        print("ERROR: This patch requires patch_fullscreen_resize first.")
        sys.exit(1)

    shutil.copy2(TARGET, BACKUP)
    print(f"[OK] Backed up {TARGET} -> {BACKUP}")

    applied = 0
    for i, (old, new) in enumerate(PATCHES, 1):
        count = content.count(old)
        if count == 0:
            print(f"[FAIL] Patch #{i}: target not found")
            shutil.copy2(BACKUP, TARGET); sys.exit(1)
        if count > 1:
            print(f"[FAIL] Patch #{i}: matches {count}x")
            shutil.copy2(BACKUP, TARGET); sys.exit(1)
        content = content.replace(old, new, 1)
        applied += 1

    if 'VERSION = "3.31"' in content:
        content = content.replace('VERSION = "3.31"', 'VERSION = "3.32"', 1)

    open(TARGET, "w").write(content)

    import ast
    try:
        ast.parse(open(TARGET).read())
        print("[OK] Resulting Python is valid")
    except SyntaxError as e:
        print(f"[FAIL] Syntax error: {e}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)

    print(f"[OK] Applied {applied} patches")
    print()
    print("Polished surfaces:")
    print("  - Inventory drawer specs: long property keys no longer clip")
    print("  - Topology labels: smarter truncation at word boundaries")
    print("  - Tooltip in graph: flips to opposite side at canvas edges")
    print("  - Drawer X button: bigger, more obvious, mobile-friendly")
    print("  - Footer no longer leaks under the fullscreen overlay")
    print("  - Topology 'Loading...' replaced with a soft pulsing dot")
    print("  - IDLE status pill is now actually visible")
    print()
    print("None of these change behavior - they just make existing things")
    print("feel better. Open the dashboard, use it normally, see if the")
    print("rough edges feel rounder.")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
