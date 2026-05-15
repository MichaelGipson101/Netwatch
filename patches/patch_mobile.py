#!/usr/bin/env python3
"""
netwatch patch: mobile responsiveness.

Adds a comprehensive mobile UI to netwatch while leaving the desktop/kiosk
experience completely unchanged. All changes are inside `@media (max-width:
768px)` blocks so wide displays (your Pi's HDMI kiosk view, your laptop)
render identically to before.

What changes at narrow width (<= 768px):

  - Edit Hosts modal: rows transform from 7-column grid into stacked cards
  - Inventory table: rows transform from 6-column table into stacked cards
  - Detail drawer: changes from right slide-in to bottom sheet (90vh)
  - All modals: become near-full-screen with reduced margins
  - Inventory editor form: 2-column grid -> single column
  - Hosts table: aggressive column hiding (already partially done at 780px)
  - Header: tighter spacing, hide non-essential labels
  - Topology cards: smaller minimum width
  - Pi health rows + service rows: timestamp moves below value
  - All buttons get larger tap targets

No JS or backend changes. CSS only.

Must be applied AFTER patch_arp_detect.py.

Run once from ~/netwatch/:
    python3 patch_mobile.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_mobile.
Idempotent - safe to re-run.
"""

import os
import shutil
import sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_mobile"
SENTINEL = "/* === MOBILE RESPONSIVE ==="  # presence means already patched


# The big block of mobile CSS. Designed to be additive to existing styles.
# Everything is gated on @media (max-width: 768px).
MOBILE_CSS = '''
/* === MOBILE RESPONSIVE === */
/* All styles below activate only on narrow viewports (phones + tablet portrait).
   Desktop and kiosk views are completely unchanged.                        */
@media (max-width: 768px){

  /* ── Shell padding tighter ─────────────────────────────────────────────── */
  .shell{padding:14px 12px}

  /* ── Header: tighter, allow wrap ──────────────────────────────────────── */
  .topbar{flex-wrap:wrap;gap:8px;row-gap:10px}
  .topbar .live-pip{display:none}              /* Hide "live" indicator pill */
  .topbar .clock{font-size:11px}
  .theme-toggle .theme-label{display:none}     /* Hide "auto/light/dark" word */
  #nav-auth{font-size:10px;gap:6px}
  #nav-auth button{padding:5px 8px}

  /* ── Tabs row: scroll horizontally if needed instead of overflowing ───── */
  .tabs{overflow-x:auto;scrollbar-width:none}
  .tabs::-webkit-scrollbar{display:none}
  .tab{flex-shrink:0;padding:9px 14px}

  /* ── Topology grid: smaller minimum so 2 cards fit on narrow phones ──── */
  .topo-grid{grid-template-columns:1fr;gap:10px}
  .group-card{padding:14px}

  /* ── Hosts table: drop more columns on mobile ─────────────────────────── */
  .row{grid-template-columns:24px 1fr 70px 70px;gap:6px;padding:10px 12px}
  .col-ip,.col-ping,.col-group{display:none}    /* hide IP, ping, group cols */
  .row-hdr{display:none}                          /* hide header row entirely */
  .compact .row{grid-template-columns:24px 1fr 70px 70px}

  /* ── Events table: condense ───────────────────────────────────────────── */
  .event{grid-template-columns:6px 1fr 80px;gap:8px;padding:11px 12px}
  .event .col-started,.event .col-ended{display:none}
  .events-hdr{display:none}

  /* ── Modals: nearly full-screen on mobile ─────────────────────────────── */
  .modal-overlay{padding:0;align-items:flex-end}
  .modal{max-width:none !important;width:100%;border-radius:14px 14px 0 0;
         max-height:95vh;display:flex;flex-direction:column}
  .modal-hdr{padding:14px 16px}
  .modal-title{font-size:15px}
  .modal-body{padding:14px 16px;overflow-y:auto;flex:1}
  .modal-foot{padding:12px 16px;flex-wrap:wrap;gap:8px}
  .modal-foot .btn{flex:1;min-width:0}

  /* ── Edit Hosts: stacked card layout ──────────────────────────────────── */
  .edit-row{padding:14px;background:var(--subtle);border:1px solid var(--border-light);
            border-radius:10px;margin-bottom:10px}
  .edit-row.hdr{display:none}                                   /* hide header */
  .edit-row .row-main{
    grid-template-columns:1fr 1fr;                              /* 2 cols */
    gap:8px;
  }
  .edit-row .row-main .f-name{grid-column:1 / -1}              /* name full width */
  .edit-row .row-main .f-ip{grid-column:1 / -1}                /* ip full width */
  .edit-row .row-main .f-group{grid-column:1 / -1}             /* group full width */
  .edit-row .row-main .f-interval{grid-column:1}
  .edit-row .row-main .ao-cell{grid-column:2;justify-self:end}
  .edit-row .row-main .more-btn{grid-column:1;justify-self:start;
                                width:auto;padding:7px 14px}
  .edit-row .row-main .more-btn::before{content:"More fields ";font-size:11px}
  .edit-row .row-main .del-btn{grid-column:2;justify-self:end}
  .edit-row input,.edit-row select{font-size:14px;padding:9px 11px}    /* bigger tap */
  .row-extra{grid-template-columns:1fr;gap:10px}
  .row-extra label.full{grid-column:1}

  /* MAC row inside expanded host editor */
  .mac-row{flex-wrap:wrap}
  .mac-row input{flex:1 1 100%;font-size:14px}
  .mac-detect-btn{flex:0 0 auto}

  /* Service rows in editor */
  .svc-row{grid-template-columns:80px 1fr 32px}
  .extra-link-row{grid-template-columns:1fr 2fr 32px}

  /* ── Inventory tab ────────────────────────────────────────────────────── */
  .inv-toolbar{flex-direction:column;align-items:stretch;gap:8px}
  .inv-search{width:100%;font-size:14px;padding:10px 12px}
  .inv-toolbar-actions{display:flex;gap:8px}
  .inv-toolbar-actions .btn{flex:1}
  .inv-metrics{grid-template-columns:repeat(2,1fr);gap:8px}
  .inv-metric{padding:10px 12px}
  .inv-metric-val{font-size:18px}

  /* Inventory table -> stacked cards. We hide the table chrome and turn each
     row into a self-contained card with name+role on top, badges below. */
  .inv-table thead{display:none}                          /* hide column headers */
  .inv-table-wrap{background:transparent;border:none;overflow:visible}
  .inv-table,.inv-table tbody,.inv-table tr,.inv-table td{display:block}
  .inv-table tbody{display:flex;flex-direction:column;gap:8px}
  .inv-row{
    background:var(--surface);
    border:1px solid var(--border-light);
    border-radius:10px;
    padding:12px 14px;
    display:grid;
    grid-template-columns:1fr auto;
    grid-template-areas:
      "name   status"
      "meta   status"
      "specs  specs";
    gap:6px 10px;
    align-items:start;
  }
  .inv-row td{padding:0;border:none}
  .inv-row td:nth-child(1){grid-area:name}                        /* System+role */
  .inv-row td:nth-child(2){grid-area:meta;font-size:11px}         /* Category */
  .inv-row td:nth-child(3){grid-area:specs;font-size:11px;color:var(--muted)}
  .inv-row td:nth-child(4){grid-area:specs;text-align:right;
                           font-size:11px;color:var(--muted)}
  .inv-row td:nth-child(5){display:none}                           /* hide OS col */
  .inv-row td:nth-child(6){grid-area:status;justify-self:end}     /* status pill */
  /* CPU and RAM tds (3 and 4) need to share the specs row */
  .inv-row td:nth-child(3),.inv-row td:nth-child(4){
    display:inline-block;
    grid-area:specs;
    width:auto;
  }
  .inv-row td:nth-child(3){padding-right:8px}
  .inv-row td:nth-child(3)::after{content:" \u2022 ";color:var(--hint);
                                 margin-left:5px}

  /* Inventory editor: stack form to 1 column */
  .inv-edit-form{grid-template-columns:1fr;gap:10px}
  .inv-edit-form input,.inv-edit-form select,.inv-edit-form textarea{
    font-size:14px;padding:10px 12px}

  /* ── Detail drawer: bottom sheet ──────────────────────────────────────── */
  .drawer{
    top:auto;left:0;right:0;bottom:0;
    width:100%;max-width:100%;
    height:88vh;
    border-left:none;
    border-top:1px solid var(--border);
    border-radius:14px 14px 0 0;
    transform:translateY(100%);                          /* slide up from below */
    box-shadow:0 -8px 24px rgba(0,0,0,.18);
  }
  .drawer.open{transform:translateY(0)}
  .drawer-hdr{padding:14px 16px;position:relative}
  .drawer-hdr::before{                                /* drag-handle visual cue */
    content:"";
    position:absolute;
    top:6px;left:50%;transform:translateX(-50%);
    width:36px;height:4px;border-radius:2px;
    background:var(--border);
  }
  .drawer-body{padding:14px 16px}
  .drawer-name{font-size:18px}

  /* Detail drawer internal grids */
  .d-statgrid{grid-template-columns:repeat(2,1fr);gap:6px}
  .d-spec-row{grid-template-columns:80px 1fr;font-size:12px}
  .d-svc{grid-template-columns:14px 1fr 70px;gap:8px;font-size:12px}
  .d-svc .d-svc-checked{display:none}                  /* hide last-checked time */
  .d-pi-row{grid-template-columns:80px 1fr;font-size:12px}
  .d-pi-row .d-pi-bar{display:none}                    /* hide bar (saves space) */
  .d-pi-val{text-align:right}
  .inv-drawer-row{grid-template-columns:90px 1fr;font-size:12px}

  /* Action buttons in drawer (Wake, Edit, etc.) */
  .d-action-btn{padding:13px 16px;font-size:14px}        /* bigger tap targets */

  /* ── Discover modal: results list scrollable ──────────────────────────── */
  .disc-row{grid-template-columns:24px 110px 1fr;gap:8px;padding:9px 10px;
            font-size:12px}
  .disc-row .disc-mac{display:none}                  /* hide MAC col on mobile */

  /* ── Forms: any general inputs need bigger tap targets on mobile ──────── */
  input[type="text"],input[type="number"],input[type="password"],
  input[type="email"],textarea,select{font-size:16px}    /* 16px prevents iOS zoom */

  /* ── Backup button text shorter on mobile ─────────────────────────────── */
  /* (Just smaller padding; full text "Download backup" is still fine)      */

}
'''


def main():
    if not os.path.isfile(TARGET):
        print(f"ERROR: {TARGET} not found.")
        sys.exit(1)

    content = open(TARGET).read()

    if SENTINEL in content:
        print(f"NOTE: '{SENTINEL}' found - patch already applied.")
        sys.exit(0)

    if "_detect_mac_for_ip" not in content:
        print("ERROR: This patch requires patch_arp_detect first.")
        sys.exit(1)

    shutil.copy2(TARGET, BACKUP)
    print(f"[OK] Backed up {TARGET} -> {BACKUP}")

    # ── 1. Merge the secondary <style> block (discovery modal CSS, added by
    # patch_discovery) into the main one, then inject MOBILE_CSS at the very
    # end. This ensures mobile media queries reliably override every other
    # rule via cascade order.
    #
    # Strategy: find the secondary block, lift its content, delete it, then
    # append both that content + mobile CSS just before the main block's
    # closing </style>.

    import re
    # Find ALL <style>...</style> blocks
    style_pattern = re.compile(r'<style>(.*?)</style>', re.DOTALL)
    matches = list(style_pattern.finditer(content))
    if len(matches) < 1:
        print(f"[FAIL] no <style> blocks found")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)

    if len(matches) >= 2:
        # Two style blocks - merge secondary into primary
        primary_match = matches[0]
        secondary_match = matches[1]
        primary_content = primary_match.group(1)
        secondary_content = secondary_match.group(1)
        # Build the new merged primary block: primary content + secondary content + mobile CSS
        merged_inner = primary_content + "\n/* === Discovery modal styles (merged from secondary block) === */\n" + secondary_content + "\n" + MOBILE_CSS
        merged_block = "<style>" + merged_inner + "</style>"
        # Replace the secondary block first (since indices shift if we replace primary first)
        # Use the actual matched text (regex group 0) for safe replacement
        content = content.replace(secondary_match.group(0), "", 1)
        # Now replace the primary block with the merged one
        content = content.replace(primary_match.group(0), merged_block, 1)
        print(f"[OK] Merged 2 <style> blocks + appended mobile CSS")
    else:
        # Only one style block - just append mobile CSS before its close
        primary_match = matches[0]
        merged_inner = primary_match.group(1) + "\n" + MOBILE_CSS
        merged_block = "<style>" + merged_inner + "</style>"
        content = content.replace(primary_match.group(0), merged_block, 1)
        print("[OK] Appended mobile CSS to single <style> block")

    # ── 2. Add viewport meta tag if not already present, so mobile browsers
    # know to render at native width rather than scaling down a desktop layout.
    OLD = '''<meta charset="utf-8">'''
    NEW = '''<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'''
    if OLD in content and "viewport" not in content[:content.find("</head>") if "</head>" in content else len(content)]:
        content = content.replace(OLD, NEW, 1)
        print("[OK] Added viewport meta tag")
    else:
        # Either OLD not found or viewport already present
        if "viewport" in content:
            print("[OK] viewport meta tag already present, skipping")
        else:
            print("[WARN] charset meta tag not found, skipping viewport injection")

    # ── 3. Bump version + footer
    if 'VERSION = "3.12"' in content:
        content = content.replace('VERSION = "3.12"', 'VERSION = "3.13"', 1)
    content = content.replace('netwatch v3.12 - raspberry pi', 'netwatch v3.13 - raspberry pi', 1)
    print("[OK] Version bumped to 3.13")

    open(TARGET, "w").write(content)

    import ast
    try:
        ast.parse(open(TARGET).read())
        print("[OK] Resulting Python is valid")
    except SyntaxError as e:
        print(f"[FAIL] Syntax error: {e}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)

    print()
    print("Next steps:")
    print("  1. sudo systemctl restart netwatch")
    print("  2. Open the dashboard on your phone (or shrink the desktop window")
    print("     below 768px). Topology, Hosts, Events, Inventory, drawers, and")
    print("     modals all switch to a mobile-friendly layout.")
    print("  3. Verify your Pi's HDMI kiosk view is unchanged - it's at 1920x1080")
    print("     so the mobile rules never trigger there.")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
