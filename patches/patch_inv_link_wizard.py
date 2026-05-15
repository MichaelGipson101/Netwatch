#!/usr/bin/env python3
"""
netwatch patch: inventory linking wizard.

Adds a "Link to monitored host" dropdown to the inventory editor that lets
you pick from your list of monitored hosts to auto-fill the MAC address,
making inventory <-> host cross-linking a one-click operation instead of
a manual MAC-copying chore.

Behaviour:
  - Dropdown shows all monitored hosts. Hosts with no MAC are grayed out
    and unselectable, with a hint to add a MAC to the host first.
  - Hosts already linked to a *different* inventory record are flagged
    "(already linked to X)" but still selectable - you can move the link.
  - Picking a host copies its MAC into the inventory record. The MAC
    field stays editable so you can still hand-edit if needed.
  - Picking "(none)" clears the MAC, breaking the link.
  - Selection auto-suggests based on:
      a) Existing MAC match (the obvious case - already linked)
      b) Fuzzy name match between inventory.system and host.name

Pure frontend feature - no backend changes needed because:
  - GET /api/status already returns all hosts with their MACs
  - GET /api/inventory already returns all inventory records (for the
    'already linked' annotation)

Must be applied AFTER patch_backup.py.

Run once from ~/netwatch/:
    python3 patch_inv_link_wizard.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_invlink.
Idempotent - safe to re-run.
"""

import os
import shutil
import sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_invlink"
SENTINEL = "renderInvLinkPicker"  # presence means already patched


PATCHES = [
    # ──── 1. Add the picker UI to the inventory editor modal.
    # We insert a new label-and-select row right BEFORE the MAC address field.
    (
        '''        <label>MAC address<input type="text" class="inv-f-mac" placeholder="aa:bb:cc:dd:ee:ff (used for cross-linking)"></label>''',
        '''        <label class="full" id="inv-link-picker-wrap">Link to monitored host
          <select class="inv-f-link" onchange="handleInvLinkChange(this)">
            <option value="">(none — not linked to a monitored host)</option>
          </select>
          <span style="font-size:10px;color:var(--hint);margin-top:2px;text-transform:none;letter-spacing:0;font-family:'DM Sans',sans-serif" id="inv-link-hint">Pick a host to auto-fill the MAC below.</span>
        </label>
        <label>MAC address<input type="text" class="inv-f-mac" placeholder="aa:bb:cc:dd:ee:ff (used for cross-linking)"></label>'''
    ),

    # ──── 2. Add CSS for the select element styling so it matches the rest of
    # the form. We append to the existing inv-edit-form rules.
    (
        '''.inv-edit-form input::placeholder,.inv-edit-form textarea::placeholder{color:var(--hint);text-transform:none;letter-spacing:0;font-family:'DM Sans',sans-serif}''',
        '''.inv-edit-form input::placeholder,.inv-edit-form textarea::placeholder{color:var(--hint);text-transform:none;letter-spacing:0;font-family:'DM Sans',sans-serif}
.inv-edit-form select{width:100%;padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:13px;background:var(--surface);color:var(--text);text-transform:none;letter-spacing:0;cursor:pointer}
.inv-edit-form select:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px var(--blue-bg)}
.inv-edit-form select option:disabled{color:var(--hint)}'''
    ),

    # ──── 3. JS: add the picker logic. We anchor on openInventoryEditor since
    # we need to extend it to populate the picker, and add helpers nearby.
    # Strategy: wrap openInventoryEditor without breaking it, by adding the
    # picker-population call after its existing body completes.
    # The cleanest way is to inject helpers near the existing inventory JS.
    #
    # We add the new functions just AFTER the existing closeInventoryEditor.
    (
        '''function closeInventoryEditor(){
  document.getElementById('inv-edit-overlay').classList.remove('open');
}''',
        '''function closeInventoryEditor(){
  document.getElementById('inv-edit-overlay').classList.remove('open');
}

// Normalise a MAC string for comparison. Returns lowercase 12-hex-char form
// or '' if the input doesn't parse.
function normMac(s){
  if(!s) return '';
  const clean = String(s).toLowerCase().replace(/[^0-9a-f]/g, '');
  return clean.length === 12 ? clean : '';
}

// Cheap fuzzy similarity between two strings (0-1 range).
// Used to auto-suggest a host match based on system name.
function fuzzyMatch(a, b){
  if(!a || !b) return 0;
  const x = String(a).toLowerCase();
  const y = String(b).toLowerCase();
  if(x === y) return 1;
  if(x.includes(y) || y.includes(x)) return 0.8;
  // Token overlap
  const tokA = new Set(x.split(/[^a-z0-9]+/).filter(t => t.length > 2));
  const tokB = new Set(y.split(/[^a-z0-9]+/).filter(t => t.length > 2));
  if(!tokA.size || !tokB.size) return 0;
  let shared = 0;
  tokA.forEach(t => { if(tokB.has(t)) shared++; });
  return shared / Math.max(tokA.size, tokB.size);
}

// Populate the "Link to monitored host" dropdown based on current data.
// Called after the editor opens (and after _editingInvId / fields are set).
async function renderInvLinkPicker(currentRec){
  const sel = document.querySelector('.inv-f-link');
  const hint = document.getElementById('inv-link-hint');
  if(!sel) return;
  // Reset to placeholder
  sel.innerHTML = '<option value="">(none — not linked to a monitored host)</option>';

  // Gather monitored hosts from cached lastData (set by refresh()).
  const hosts = (typeof lastData !== 'undefined' && lastData && lastData.hosts) ? lastData.hosts : [];
  if(!hosts.length){
    if(hint) hint.textContent = 'No monitored hosts found yet. Add a host first to enable linking.';
    return;
  }

  // Find which hosts are already linked to other inventory records,
  // so we can warn when picking would steal a link.
  const otherLinks = {};  // mac_normalised -> system_name
  _inventoryData.forEach(i => {
    const m = normMac(i.mac);
    if(!m) return;
    if(_editingInvId && i.id === _editingInvId) return;  // skip self
    otherLinks[m] = i.system;
  });

  const currentMacNorm = normMac(currentRec && currentRec.mac);

  // Auto-suggest: if no MAC is set yet, find best fuzzy match by system name
  let suggestedIp = null;
  if(currentRec && currentRec.system && !currentMacNorm){
    let best = { score: 0, ip: null };
    hosts.forEach(h => {
      const score = fuzzyMatch(currentRec.system, h.name);
      if(score > best.score){ best = { score, ip: h.ip }; }
    });
    if(best.score >= 0.5) suggestedIp = best.ip;
  }

  // Sort hosts by group then name for a stable, browseable dropdown
  const sorted = [...hosts].sort((a, b) => {
    const ga = (a.group || '').toLowerCase();
    const gb = (b.group || '').toLowerCase();
    if(ga !== gb) return ga.localeCompare(gb);
    return (a.name || '').toLowerCase().localeCompare((b.name || '').toLowerCase());
  });

  let selectedFound = false;
  sorted.forEach(h => {
    const hostMac = (h.specs && h.specs.mac) ? h.specs.mac : '';
    const macNorm = normMac(hostMac);
    const opt = document.createElement('option');
    opt.value = h.ip;
    let label = h.name + ' (' + h.ip + ')';
    if(!macNorm){
      label += ' — no MAC set on host';
      opt.disabled = true;
    } else if(otherLinks[macNorm]){
      label += ' — already linked to ' + otherLinks[macNorm];
    } else if(h.ip === suggestedIp){
      label += ' — likely match';
    }
    opt.textContent = label;
    opt.dataset.mac = macNorm;
    // Pre-select if MAC matches current record
    if(macNorm && macNorm === currentMacNorm){
      opt.selected = true;
      selectedFound = true;
    }
    sel.appendChild(opt);
  });

  // If we couldn't find a host matching the current MAC but there's a fuzzy
  // suggestion, surface it in the hint instead of preselecting (less invasive)
  if(!selectedFound && suggestedIp){
    const suggested = sorted.find(h => h.ip === suggestedIp);
    if(suggested && hint){
      hint.textContent = 'Suggested match: ' + suggested.name + ' (' + suggested.ip + ') — pick from dropdown to confirm';
    }
  } else if(selectedFound && hint){
    hint.textContent = 'Linked. Picking another host will replace the MAC below.';
  } else if(currentMacNorm && !selectedFound && hint){
    hint.textContent = 'MAC is set but does not match any monitored host.';
  } else if(hint){
    hint.textContent = 'Pick a host to auto-fill the MAC below.';
  }
}

// Called when the user picks a host from the dropdown.
function handleInvLinkChange(sel){
  const macField = document.querySelector('.inv-f-mac');
  if(!macField) return;
  const opt = sel.options[sel.selectedIndex];
  if(!opt || !opt.value){
    // "(none)" picked - clear the MAC
    macField.value = '';
    const hint = document.getElementById('inv-link-hint');
    if(hint) hint.textContent = 'Link cleared. Pick a host to re-link.';
    return;
  }
  const mac = opt.dataset.mac;
  if(mac){
    // Format for display: aa:bb:cc:dd:ee:ff
    macField.value = mac.match(/.{2}/g).join(':');
    const hint = document.getElementById('inv-link-hint');
    if(hint) hint.textContent = 'Linked to ' + opt.textContent.split(' — ')[0] + '. MAC has been auto-filled.';
  }
}'''
    ),

    # ──── 4. Hook into openInventoryEditor so the picker gets populated.
    # We need to call renderInvLinkPicker() after the existing fetch+populate.
    # Both code paths (existing record vs new record) need it.
    (
        '''  if(existingId){
    try {
      const res = await fetch('/api/inventory/' + existingId);
      if(res.ok){
        const rec = await res.json();
        ['system','category','role','cpu','ram_gb','gpu','architecture','os','cpu_score','tdp_watts','tpm','mac','ip','serial','notes'].forEach(f => {
          const el = document.querySelector('.inv-f-' + f);
          if(el && rec[f] !== null && rec[f] !== undefined) el.value = rec[f];
        });
      }
    } catch(e){}
  }
  document.getElementById('inv-edit-overlay').classList.add('open');
}''',
        '''  let loadedRec = null;
  if(existingId){
    try {
      const res = await fetch('/api/inventory/' + existingId);
      if(res.ok){
        loadedRec = await res.json();
        ['system','category','role','cpu','ram_gb','gpu','architecture','os','cpu_score','tdp_watts','tpm','mac','ip','serial','notes'].forEach(f => {
          const el = document.querySelector('.inv-f-' + f);
          if(el && loadedRec[f] !== null && loadedRec[f] !== undefined) el.value = loadedRec[f];
        });
      }
    } catch(e){}
  }
  // Populate the link picker. For new records loadedRec is null; we still
  // pass current form values so the auto-suggest works on the system name.
  if(!loadedRec){
    const sysEl = document.querySelector('.inv-f-system');
    loadedRec = { system: sysEl ? sysEl.value : '', mac: '' };
  }
  renderInvLinkPicker(loadedRec);
  document.getElementById('inv-edit-overlay').classList.add('open');
}'''
    ),

    # ──── 5. Bonus: re-run picker logic when system name changes for new records,
    # so the auto-suggest updates as the user types. We do this via a delegated
    # input listener.
    (
        '''document.addEventListener('input', e => {
  if(e.target.id === 'inv-search'){
    _inventoryFilter.search = e.target.value;
    renderInventoryRows();
  }
});''',
        '''document.addEventListener('input', e => {
  if(e.target.id === 'inv-search'){
    _inventoryFilter.search = e.target.value;
    renderInventoryRows();
  }
  // Refresh the link picker's auto-suggest when the system name changes
  // (only for new records - existing records have their MAC already set)
  if(e.target.classList.contains('inv-f-system') && !_editingInvId){
    const sel = document.querySelector('.inv-f-link');
    if(sel && !sel.value){
      const macField = document.querySelector('.inv-f-mac');
      renderInvLinkPicker({
        system: e.target.value,
        mac: macField ? macField.value : '',
      });
    }
  }
});'''
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

    if "create_backup_tarball" not in content:
        print("ERROR: This patch requires patch_backup first.")
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
    print("  2. Open Inventory tab -> click any record -> click 'Edit record'")
    print("  3. New 'Link to monitored host' dropdown above the MAC field.")
    print("  4. Pick a host - the MAC field auto-fills.")
    print("  5. For new records, the picker auto-suggests a likely match")
    print("     based on the system name as you type.")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
