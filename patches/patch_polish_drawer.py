#!/usr/bin/env python3
"""
netwatch patch: UI polish.

Two small fixes:

1. System health section no longer flashes on every refresh.
   Previously, the entire drawer body was rebuilt on each 5-second refresh
   tick, which destroyed and recreated the pi-health DOM, causing the
   "Reading metrics..." placeholder to flicker through. Now the pi-health
   container is built once when the drawer opens, and updated in place
   afterwards - values change, bars animate to new widths, no flash.

2. Editor rows no longer auto-expand when they have extra data.
   The previous behaviour expanded any row with specs/notes/links already
   set, which made the editor very long once most hosts were configured.
   Rows now always start collapsed. The "..." button gets a subtle dot
   indicator when there is hidden data, so you can see at a glance which
   hosts have extras without having to click each one.

Must be applied AFTER patch_pi_health.py.

Run once from ~/netwatch/:
    python3 patch_polish_drawer.py
    sudo systemctl restart netwatch

Backup saved to monitor.py.bak_polish.
Idempotent - safe to re-run.
"""

import os
import shutil
import sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_polish"
SENTINEL = "updatePiHealth"  # presence means already patched


PATCHES = [
    # ──── 1. CSS: add a subtle dot indicator to the more-btn when there's hidden data
    (
        '''.edit-row .more-btn:hover{background:var(--subtle);color:var(--text)}
.edit-row .more-btn.open{color:var(--text);background:var(--subtle)}''',
        '''.edit-row .more-btn:hover{background:var(--subtle);color:var(--text)}
.edit-row .more-btn.open{color:var(--text);background:var(--subtle)}
.edit-row .more-btn.has-data{position:relative}
.edit-row .more-btn.has-data::after{content:'';position:absolute;top:3px;right:3px;width:6px;height:6px;border-radius:50%;background:var(--blue)}'''
    ),

    # ──── 2. Editor: rows always start collapsed, but show the dot when extras exist
    # First sub-patch: add hasLinks/hasData calculation
    (
        '''  const hasExtra = !!(specs.cpu || specs.ram || specs.storage || specs.os || specs.mac || notes);''',
        '''  const hasLinks = !!(h && h.links && (h.links.primary || (h.links.extras && h.links.extras.length)));
  const hasData = !!(specs.cpu || specs.ram || specs.storage || specs.os || specs.mac || notes || hasLinks);'''
    ),

    # Second sub-patch: stop auto-opening more-btn (but mark it has-data)
    (
        """    + '<button class="more-btn' + (hasExtra ? ' open' : '') + '" type="button" title="More fields (specs, notes)">...</button>'""",
        """    + '<button class="more-btn' + (hasData ? ' has-data' : '') + '" type="button" title="' + (hasData ? 'More fields (this host has saved extras)' : 'More fields (specs, notes, links)') + '">...</button>'"""
    ),

    # Third sub-patch: stop auto-opening row-extra
    (
        """    + '<div class="row-extra' + (hasExtra ? ' open' : '') + '">'""",
        """    + '<div class="row-extra">'"""
    ),

    # ──── 3. Editor: when toggling, also toggle the more-btn open state
    # The current toggle code is fine, just verifies it works without the
    # auto-open initial state.
    # No change needed here - existing toggle preserves has-data class.

    # ──── 4. Replace the pi-health placeholder generation in renderDrawer.
    # Now we render the structure once and only refresh values in-place.
    (
        '''  let piHtml = '';
  if(h.is_pi){
    piHtml = '<div class="d-section"><div class="d-section-hdr"><span>System health</span><span style="color:var(--muted)" id="d-pi-meta">loading...</span></div>'
      + '<div class="d-pihealth" id="d-pihealth"><div class="d-pi-empty">Reading metrics...</div></div></div>';
  }

  document.getElementById('drawer-body').innerHTML = statsHtml + linksHtml + piHtml + sparkHtml + specsHtml + notesHtml + incHtml + actionsHtml;

  const wakeBtn = document.getElementById('d-wake-btn');
  if(wakeBtn) wakeBtn.addEventListener('click', () => sendWake(wakeBtn.dataset.ip));

  if(h.is_pi){
    fetchPiHealth();
  }
}''',
        '''  let piHtml = '';
  if(h.is_pi){
    piHtml = '<div class="d-section"><div class="d-section-hdr"><span>System health</span><span style="color:var(--muted)" id="d-pi-meta"></span></div>'
      + '<div class="d-pihealth" id="d-pihealth">'
      + '<div class="d-pi-row" data-metric="temp" style="display:none"><div class="d-pi-key">CPU TEMP</div><div class="d-pi-bar"><div class="d-pi-bar-fill"></div></div><span class="d-pi-val mono"></span></div>'
      + '<div class="d-pi-row" data-metric="load" style="display:none"><div class="d-pi-key">LOAD AVG</div><div class="d-pi-bar"><div class="d-pi-bar-fill"></div></div><span class="d-pi-val mono"></span></div>'
      + '<div class="d-pi-row" data-metric="mem" style="display:none"><div class="d-pi-key">MEMORY</div><div class="d-pi-bar"><div class="d-pi-bar-fill"></div></div><div class="d-pi-val mono"></div></div>'
      + '<div class="d-pi-row" data-metric="disk" style="display:none"><div class="d-pi-key">DISK</div><div class="d-pi-bar"><div class="d-pi-bar-fill"></div></div><div class="d-pi-val mono"></div></div>'
      + '<div class="d-pi-row" data-metric="uptime" style="display:none"><div class="d-pi-key">UPTIME</div><div></div><span class="d-pi-val mono"></span></div>'
      + '<div class="d-pi-empty" id="d-pi-empty">Reading metrics...</div>'
      + '</div></div>';
  }

  // Only rebuild the drawer body if it's a different host than what's currently shown.
  // Otherwise just update the stat values in place to avoid flashing.
  const drawerBody = document.getElementById('drawer-body');
  if(drawerBody.dataset.hostIp !== h.ip){
    drawerBody.dataset.hostIp = h.ip;
    drawerBody.innerHTML = statsHtml + linksHtml + piHtml + sparkHtml + specsHtml + notesHtml + incHtml + actionsHtml;
    const wakeBtn = document.getElementById('d-wake-btn');
    if(wakeBtn) wakeBtn.addEventListener('click', () => sendWake(wakeBtn.dataset.ip));
  } else {
    // Same host - just update the stat values without rebuilding everything
    updateDrawerStats(h, data);
  }

  if(h.is_pi){
    updatePiHealth();
  }
}

function updateDrawerStats(h, data){
  // Update the stats grid in place. We just replace the four stat values
  // with new innerHTML. The structure stays put so there's no flicker.
  const isIdle = h.status === 'IDLE';
  const labelLat = h.is_up && h.latency_ms !== null ? h.latency_ms.toFixed(1) + ' <sup>ms</sup>' : (h.is_up ? 'up' : 'offline');
  const availLabel = h.uptime_pct !== null ? h.uptime_pct.toFixed(1) + ' <sup>%</sup>' : '-';
  const uColor = isIdle ? 'var(--hint)' : uptimeColor(h.uptime_pct);

  const stats = document.querySelectorAll('#drawer-body .d-statgrid .d-stat-val');
  if(stats.length >= 4){
    stats[0].className = 'd-stat-val ' + (h.is_up ? 'green' : (isIdle ? '' : 'red'));
    stats[0].innerHTML = labelLat;
    stats[1].innerHTML = (h.latency_ms !== null ? h.latency_ms.toFixed(1) + ' <sup>ms</sup>' : '-');
    stats[2].style.color = uColor;
    stats[2].innerHTML = availLabel;
    stats[3].textContent = lastSeenStr(h.last_seen_up_seconds);
  }

  // Refresh the header status badge since the host might have changed state
  const meta = document.getElementById('d-meta');
  if(meta){
    const badgeCls = h.status === 'WAIT' ? 'badge-wt' : h.is_up ? 'badge-up' : (h.status === 'IDLE' ? 'badge-idle' : 'badge-dn');
    meta.innerHTML =
      '<span>' + escapeHtml(h.ip) + '</span><span>·</span><span>' + escapeHtml(h.group) + '</span>'
      + '<span class="badge ' + badgeCls + '">' + h.status + '</span>';
  }
  const dotEl = document.getElementById('d-dot');
  if(dotEl){
    dotEl.className = 'drawer-name-dot ' + (h.status === 'WAIT' ? 'wait' : h.is_up ? 'up' : (h.status === 'IDLE' ? 'idle' : 'down'));
  }
}'''
    ),

    # ──── 5. Replace fetchPiHealth with one that updates in place
    (
        '''async function fetchPiHealth(){
  const target = document.getElementById('d-pihealth');
  if(!target) return;
  try {
    const res = await fetch('/api/pi-health');
    if(!res.ok) throw new Error('bad response');
    const h = await res.json();
    const meta = document.getElementById('d-pi-meta');
    if(meta) meta.textContent = h.cpu_count ? h.cpu_count + ' CPU cores' : '';

    const rows = [];

    if(h.cpu_temp_c !== undefined){
      rows.push({
        key: 'CPU TEMP',
        bar_pct: Math.min(100, (h.cpu_temp_c / 90) * 100),
        bar_color: _tempColor(h.cpu_temp_c),
        val: h.cpu_temp_c.toFixed(1) + ' &deg;C',
        val_color: _tempColor(h.cpu_temp_c),
      });
    }

    if(h.load_1m !== undefined){
      const cls = _loadClass(h.load_1m, h.cpu_count);
      rows.push({
        key: 'LOAD AVG',
        bar_pct: Math.min(100, (h.load_1m / (h.cpu_count || 1)) * 100),
        bar_color: _loadColor(h.load_1m, h.cpu_count),
        val: h.load_1m.toFixed(2) + ' &middot; ' + h.load_5m.toFixed(2) + ' &middot; ' + h.load_15m.toFixed(2),
        val_class: cls,
      });
    }

    if(h.mem_pct !== undefined){
      rows.push({
        key: 'MEMORY',
        bar_pct: h.mem_pct,
        bar_color: _pctColor(h.mem_pct),
        val: _bytesHuman(h.mem_used_bytes) + ' / ' + _bytesHuman(h.mem_total_bytes),
        val_class: _pctClass(h.mem_pct),
        sub: h.mem_pct.toFixed(1) + '%',
      });
    }

    if(h.disk_pct !== undefined){
      rows.push({
        key: 'DISK',
        bar_pct: h.disk_pct,
        bar_color: _pctColor(h.disk_pct),
        val: _bytesHuman(h.disk_used_bytes) + ' / ' + _bytesHuman(h.disk_total_bytes),
        val_class: _pctClass(h.disk_pct),
        sub: h.disk_pct.toFixed(1) + '%',
      });
    }

    if(h.uptime_seconds !== undefined){
      rows.push({
        key: 'UPTIME',
        bar_pct: null,
        val: _uptimeHuman(h.uptime_seconds),
      });
    }

    if(rows.length === 0){
      target.innerHTML = '<div class="d-pi-empty">No system metrics available.</div>';
      return;
    }

    target.innerHTML = rows.map(r => {
      const barCell = r.bar_pct !== null && r.bar_pct !== undefined
        ? '<div class="d-pi-bar"><div class="d-pi-bar-fill" style="width:' + Math.max(2, r.bar_pct) + '%;background:' + r.bar_color + '"></div></div>'
        : '<div></div>';
      const valHtml = r.sub
        ? '<div><span class="d-pi-val ' + (r.val_class || '') + '">' + r.sub + '</span><div style="font-family:DM Mono,monospace;font-size:10px;color:var(--hint);text-align:right">' + r.val + '</div></div>'
        : '<span class="d-pi-val ' + (r.val_class || '') + '" style="color:' + (r.val_color || '') + '">' + r.val + '</span>';
      return '<div class="d-pi-row"><div class="d-pi-key">' + r.key + '</div>' + barCell + valHtml + '</div>';
    }).join('');
  } catch(e){
    target.innerHTML = '<div class="d-pi-empty">Could not read metrics.</div>';
  }
}''',
        '''async function updatePiHealth(){
  // In-place update: doesn't re-render the section, just changes values.
  // Each metric row already exists in the DOM; we just set its content
  // and visibility based on what came back.
  const target = document.getElementById('d-pihealth');
  if(!target) return;
  try {
    const res = await fetch('/api/pi-health');
    if(!res.ok) throw new Error('bad response');
    const h = await res.json();

    const meta = document.getElementById('d-pi-meta');
    if(meta) meta.textContent = h.cpu_count ? h.cpu_count + ' CPU cores' : '';

    const empty = target.querySelector('#d-pi-empty');
    if(empty) empty.style.display = 'none';

    const setRow = (metric, show, barPct, barColor, valHtml, valColor, valClass) => {
      const row = target.querySelector('[data-metric="' + metric + '"]');
      if(!row) return;
      if(!show){ row.style.display = 'none'; return; }
      row.style.display = '';
      const bar = row.querySelector('.d-pi-bar-fill');
      if(bar){
        if(barPct === null || barPct === undefined){
          bar.parentElement.style.visibility = 'hidden';
        } else {
          bar.parentElement.style.visibility = '';
          bar.style.width = Math.max(2, Math.min(100, barPct)) + '%';
          bar.style.background = barColor;
        }
      }
      const val = row.querySelector('.d-pi-val');
      if(val){
        val.innerHTML = valHtml;
        val.style.color = valColor || '';
        val.className = 'd-pi-val mono ' + (valClass || '');
      }
    };

    setRow('temp',
      h.cpu_temp_c !== undefined,
      h.cpu_temp_c !== undefined ? (h.cpu_temp_c / 90) * 100 : null,
      _tempColor(h.cpu_temp_c),
      h.cpu_temp_c !== undefined ? h.cpu_temp_c.toFixed(1) + ' &deg;C' : '-',
      _tempColor(h.cpu_temp_c)
    );

    setRow('load',
      h.load_1m !== undefined,
      h.load_1m !== undefined ? (h.load_1m / (h.cpu_count || 1)) * 100 : null,
      _loadColor(h.load_1m, h.cpu_count),
      h.load_1m !== undefined ? h.load_1m.toFixed(2) + ' &middot; ' + h.load_5m.toFixed(2) + ' &middot; ' + h.load_15m.toFixed(2) : '-',
      null,
      _loadClass(h.load_1m, h.cpu_count)
    );

    setRow('mem',
      h.mem_pct !== undefined,
      h.mem_pct,
      _pctColor(h.mem_pct),
      h.mem_pct !== undefined
        ? '<div>' + h.mem_pct.toFixed(1) + '%</div><div style="font-size:10px;color:var(--hint);text-align:right">' + _bytesHuman(h.mem_used_bytes) + ' / ' + _bytesHuman(h.mem_total_bytes) + '</div>'
        : '-',
      null,
      _pctClass(h.mem_pct)
    );

    setRow('disk',
      h.disk_pct !== undefined,
      h.disk_pct,
      _pctColor(h.disk_pct),
      h.disk_pct !== undefined
        ? '<div>' + h.disk_pct.toFixed(1) + '%</div><div style="font-size:10px;color:var(--hint);text-align:right">' + _bytesHuman(h.disk_used_bytes) + ' / ' + _bytesHuman(h.disk_total_bytes) + '</div>'
        : '-',
      null,
      _pctClass(h.disk_pct)
    );

    setRow('uptime',
      h.uptime_seconds !== undefined,
      null, null,
      _uptimeHuman(h.uptime_seconds)
    );

    // If literally nothing came back, show the empty state
    const anyVisible = target.querySelectorAll('.d-pi-row[style=""], .d-pi-row:not([style])').length;
    if(anyVisible === 0 && empty){
      empty.textContent = 'No system metrics available.';
      empty.style.display = '';
    }
  } catch(e){
    const empty = target.querySelector('#d-pi-empty');
    if(empty){
      empty.textContent = 'Could not read metrics.';
      empty.style.display = '';
    }
  }
}'''
    ),

    # ──── 6. Bump version
    (
        '<span>netwatch v3.4 - raspberry pi</span>',
        '<span>netwatch v3.5 - raspberry pi</span>'
    ),

    # ──── 7. CSS: add .mono helper used by the new pi-health rows
    (
        '''.d-pi-val{font-family:'DM Mono',monospace;font-size:12px;text-align:right;white-space:nowrap}''',
        '''.d-pi-val{font-family:'DM Mono',monospace;font-size:12px;text-align:right;white-space:nowrap;color:var(--text)}
.d-pi-val.mono{font-family:'DM Mono',monospace}'''
    ),

    # ──── 8. Reset drawer-body's hostIp tracking when drawer is closed
    # so that re-opening the same host triggers a clean rebuild
    (
        '''function closeDrawer(){
  openDrawerIp = null;
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('drawer-backdrop').classList.remove('open');
}''',
        '''function closeDrawer(){
  openDrawerIp = null;
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('drawer-backdrop').classList.remove('open');
  // Clear the cached host so next open does a fresh render
  const body = document.getElementById('drawer-body');
  if(body) body.dataset.hostIp = '';
}'''
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

    if "fetchPiHealth" not in content:
        print("ERROR: This patch requires patch_pi_health first.")
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

    if 'VERSION = "3.4"' in content:
        content = content.replace('VERSION = "3.4"', 'VERSION = "3.5"', 1)

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
    print("Next steps:")
    print("  1. sudo systemctl restart netwatch")
    print("  2. Click your Pi - System health updates smoothly without flashing")
    print("  3. Open 'Edit hosts' - rows now start collapsed.")
    print("     Hosts with hidden specs/notes/links show a small blue dot on the ... button.")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
