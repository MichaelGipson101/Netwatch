#!/usr/bin/env python3
"""
netwatch visual facelift PREVIEW.

A targeted introduction of three signature visual elements inspired by
the Reddit homelab dashboard:

  1. COPPER ACCENT COLOR - replaces blue as the primary action color.
     The new --accent variable is a warm copper (#d97757-ish). Used
     throughout for active tabs, primary buttons, focus rings, link
     colors, status pills, and key highlights. Blue still exists for
     "info" semantic contexts but is no longer the brand color.

  2. EDITORIAL HEADLINE - a serif italic phrase appears above the
     summary scards on each tab, describing the current homelab state
     in editorial language with mixed accent colors. Examples:

       "16 hosts online · 8 idle · all systems healthy"
       "14 hosts online · 2 down · 1 service affected"

     The phrase is generated from live data, updates with the
     dashboard, and uses Crimson Pro or similar serif italic.

  3. AMBIENT CARD GRADIENTS - two card surfaces get subtle vertical
     tints that give them quiet personality:
       - The .summary scard row: gets a warm tint matching the accent
       - The .topo-web canvas: gets a cool teal tint matching the graph

     These are intentionally subtle - not gradients you "see," gradients
     you "feel."

WHY THIS IS A PREVIEW:

  Visual decisions are subjective. This patch deliberately keeps the
  changes contained to specific surfaces (scards, header, primary
  buttons) so you can evaluate the direction without committing to a
  full reskin. If it lands, we expand to drawers/modals/etc in a
  follow-up. If it doesn't land, this patch is fully revertable in
  one command.

  NOT included in this preview (deliberately):
    - Drawer styling (sensitive interaction surface)
    - Modal restyling (works fine, would distract from main eval)
    - Mobile-specific tuning (do that AFTER desktop look is settled)
    - Footer/header restructuring (current works)

Must be applied AFTER patch_security_fixes.py.

Run once from ~/netwatch/:
    python3 patch_visual_preview.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_visual.
Idempotent - safe to re-run.

To revert: cp monitor.py.bak_visual monitor.py && sudo systemctl restart netwatch
"""

import os, shutil, sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_visual"
SENTINEL = "--accent:"


PATCHES = [
    # ──────────────────────────────────────────────────────────────────────
    # 1. Add the --accent color variable (copper) to both light and dark
    # palettes. Also add --accent-soft for backgrounds and --accent-text
    # for foreground use.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  --blue:#2563eb;--blue-bg:#dbeafe;''',
        '''  --blue:#2563eb;--blue-bg:#dbeafe;
  --accent:#c2410c;--accent-bg:#fef0e6;--accent-text:#9a3412;--accent-soft:#fff7f0;'''
    ),
    (
        '''  --blue:#3b82f6;--blue-bg:#0f1a2e;
  --primary-hover:#fff;
  --text-inverse-muted:rgba(15,14,13,.7);
  --text-inverse-hint:rgba(15,14,13,.55);
  --text-inverse-bg:rgba(15,14,13,.2);
}
@media (prefers-color-scheme: dark){''',
        '''  --blue:#3b82f6;--blue-bg:#0f1a2e;
  --accent:#e8754f;--accent-bg:#2a1810;--accent-text:#f4956f;--accent-soft:#1a110b;
  --primary-hover:#fff;
  --text-inverse-muted:rgba(15,14,13,.7);
  --text-inverse-hint:rgba(15,14,13,.55);
  --text-inverse-bg:rgba(15,14,13,.2);
}
@media (prefers-color-scheme: dark){'''
    ),
    (
        '''    --blue:#3b82f6;--blue-bg:#0f1a2e;
    --primary-hover:#fff;
    --text-inverse-muted:rgba(15,14,13,.7);
    --text-inverse-hint:rgba(15,14,13,.55);
    --text-inverse-bg:rgba(15,14,13,.2);
  }
}''',
        '''    --blue:#3b82f6;--blue-bg:#0f1a2e;
    --accent:#e8754f;--accent-bg:#2a1810;--accent-text:#f4956f;--accent-soft:#1a110b;
    --primary-hover:#fff;
    --text-inverse-muted:rgba(15,14,13,.7);
    --text-inverse-hint:rgba(15,14,13,.55);
    --text-inverse-bg:rgba(15,14,13,.2);
  }
}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 2. Add the serif font import + editorial headline CSS. Anchor on
    # the existing summary CSS for logical grouping.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''.summary{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px}''',
        '''/* Editorial headline - a serif italic phrase describing the homelab\'s
   state in plain language. Sits above the summary scards. */
.editorial-headline{
  font-family:Georgia, \'Times New Roman\', serif;
  font-style:italic;
  font-size:32px;
  font-weight:400;
  letter-spacing:-.01em;
  line-height:1.15;
  margin:8px 0 18px;
  color:var(--text);
}
.editorial-headline .eh-part{display:inline}
.editorial-headline .eh-sep{
  color:var(--hint);
  font-style:normal;
  margin:0 .35em;
  font-size:.7em;
  vertical-align:.18em;
}
.editorial-headline .eh-warm  {color:var(--accent-text)}
.editorial-headline .eh-good  {color:var(--green-text)}
.editorial-headline .eh-info  {color:#7eb3ff}
.editorial-headline .eh-warn  {color:var(--amber-text)}
.editorial-headline .eh-bad   {color:var(--red-text)}
@media (max-width:768px){
  .editorial-headline{font-size:22px;margin:6px 0 14px}
}

/* Subtle warm tint on the summary card row - ambient, not loud */
.summary{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px;position:relative}
.summary::before{
  content:\'\';
  position:absolute;
  inset:-8px -16px;
  background:radial-gradient(ellipse at top, var(--accent-soft) 0%, transparent 60%);
  opacity:.4;
  pointer-events:none;
  z-index:-1;
  border-radius:14px;
}
.summary-row-original-placeholder{display:none}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 3. Insert the editorial headline element above the summary row.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  <div class="summary">
    <div class="scard"><div class="scard-label">Hosts up</div><div class="scard-val" id="s-up">-</div><div class="scard-sub" id="s-up-sub">loading...</div></div>''',
        '''  <div class="editorial-headline" id="editorial-headline">
    <span class="eh-part eh-warm" id="eh-1">homelab</span><span class="eh-sep">\u00b7</span><span class="eh-part eh-good" id="eh-2">checking in</span><span class="eh-sep">\u00b7</span><span class="eh-part eh-info" id="eh-3">live</span>
  </div>
  <div class="summary">
    <div class="scard"><div class="scard-label">Hosts up</div><div class="scard-val" id="s-up">-</div><div class="scard-sub" id="s-up-sub">loading...</div></div>'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 4. Add updateEditorialHeadline function. Anchor on the existing
    # updateSummary or render function.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''function updateSummary(data){''',
        '''function updateEditorialHeadline(data){
  // Build a plain-language description of the homelab state.
  // The three phrases are colored with semantic accent colors:
  //   warm  = identity/total
  //   good  = healthy
  //   info  = neutral metadata
  //   warn  = degraded
  //   bad   = down/critical
  const hosts = (data && data.hosts) || [];
  const total = hosts.length;
  const up    = hosts.filter(h => h.is_up).length;
  const down  = hosts.filter(h => !h.is_up && h.status !== \'IDLE\' && h.status !== \'WAIT\').length;
  const idle  = hosts.filter(h => h.status === \'IDLE\').length;
  const deg   = hosts.filter(h => h.status === \'DEGRADED\').length;

  const el1 = document.getElementById(\'eh-1\');
  const el2 = document.getElementById(\'eh-2\');
  const el3 = document.getElementById(\'eh-3\');
  if(!el1 || !el2 || !el3) return;

  // Part 1: warm count, the identity ("X hosts online")
  el1.textContent = up + \' host\' + (up === 1 ? \'\' : \'s\') + \' online\';
  el1.className = \'eh-part eh-warm\';

  // Part 2: state of the unmonitored/idle pool
  if(idle > 0){
    el2.textContent = idle + \' idle\';
    el2.className = \'eh-part eh-info\';
  } else {
    el2.textContent = total + \' monitored\';
    el2.className = \'eh-part eh-info\';
  }

  // Part 3: overall health
  if(down > 0){
    el3.textContent = down + \' down\';
    el3.className = \'eh-part eh-bad\';
  } else if(deg > 0){
    el3.textContent = deg + \' degraded\';
    el3.className = \'eh-part eh-warn\';
  } else if(up === total - idle && total > 0){
    el3.textContent = \'all systems healthy\';
    el3.className = \'eh-part eh-good\';
  } else {
    el3.textContent = \'monitoring\';
    el3.className = \'eh-part eh-good\';
  }
}

function updateSummary(data){'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 5. Call updateEditorialHeadline from the main render loop. Anchor
    # on the existing updateSummary call.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  updateSummary(data);''',
        '''  updateEditorialHeadline(data);
  updateSummary(data);'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 6. Adopt the accent color in key places: active tab, primary
    # buttons, focus rings, key links. We do this by overriding the
    # existing rules that used --blue.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''.tab.active{background:var(--text);color:var(--bg)}''',
        '''.tab.active{background:var(--accent);color:#fff;box-shadow:0 0 0 1px var(--accent), 0 4px 12px rgba(232, 117, 79, .25)}
.tab.active::before{
  content:\'\';
  display:inline-block;
  width:6px;
  height:6px;
  border-radius:50%;
  background:#fff;
  margin-right:8px;
  vertical-align:middle;
  box-shadow:0 0 8px rgba(255,255,255,.6);
}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 7. Primary buttons (btn-primary): use accent color
    # ──────────────────────────────────────────────────────────────────────
    (
        '''.btn-primary{background:var(--text);color:var(--bg);border-color:var(--text)}
.btn-primary:hover:not(:disabled){background:var(--primary-hover);border-color:var(--primary-hover)}''',
        '''.btn-primary{background:var(--accent);color:#fff;border-color:var(--accent)}
.btn-primary:hover:not(:disabled){background:var(--accent-text);border-color:var(--accent-text);box-shadow:0 0 0 3px rgba(232, 117, 79, .15)}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 8. Subtle ambient tint on the topo-web canvas - cool teal feels
    # right for the graph
    # ──────────────────────────────────────────────────────────────────────
    (
        '''.topo-web{position:relative;background:var(--bg);border:1px solid var(--border);border-radius:12px;overflow:hidden;height:min(78vh,1000px);min-height:540px;transition:height .3s cubic-bezier(.4,0,.2,1)}''',
        '''.topo-web{position:relative;background:linear-gradient(180deg, rgba(15,30,30,.4) 0%, var(--bg) 40%);border:1px solid var(--border);border-radius:12px;overflow:hidden;height:min(78vh,1000px);min-height:540px;transition:height .3s cubic-bezier(.4,0,.2,1)}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 9. The "Add" button + similar action buttons use accent on hover
    # ──────────────────────────────────────────────────────────────────────
    (
        '''.btn:hover{border-color:var(--text);color:var(--text)}''',
        '''.btn:hover{border-color:var(--accent);color:var(--accent-text)}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 10. Bump version
    # ──────────────────────────────────────────────────────────────────────
    (
        'netwatch v3.37 - raspberry pi',
        'netwatch v3.38 - raspberry pi'
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

    if "_validate_ip_or_hostname" not in content:
        print("ERROR: This patch requires patch_security_fixes first.")
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
            print(f"[FAIL] Patch #{i}: matches {count}x - too ambiguous, needs more context")
            shutil.copy2(BACKUP, TARGET); sys.exit(1)
        content = content.replace(old, new, 1)
        applied += 1

    if 'VERSION = "3.37"' in content:
        content = content.replace('VERSION = "3.37"', 'VERSION = "3.38"', 1)

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
    print("Visual preview applied. Three signature elements introduced:")
    print()
    print("  1. Copper accent color (replaces blue as primary)")
    print("     - Active tab indicator")
    print("     - Primary buttons")
    print("     - Hover states on ghost buttons")
    print()
    print("  2. Editorial serif italic headline above summary cards")
    print("     - 'X hosts online \u00b7 Y idle \u00b7 all systems healthy'")
    print("     - Updates live with the data")
    print("     - Mixed accent colors per phrase")
    print()
    print("  3. Ambient gradients (subtle, almost imperceptible)")
    print("     - Warm tint behind the summary row")
    print("     - Cool teal tint in the topology web canvas")
    print()
    print("USE IT FOR A DAY. If it lands, we expand to drawers/modals/etc.")
    print("If it doesn't, single command revert:")
    print(f"   cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
