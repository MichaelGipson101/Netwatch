#!/usr/bin/env python3
"""
netwatch patch: VM type follow-up - hook VM into UI surfaces I missed.

The previous VM-type patch (patch_vm_type.py) updated the backend types
list, the property registry, the per-type table columns, the topology
node rendering, and the global INV_TYPE_ORDER/INV_TYPE_LABELS constants.

But it missed three places that hardcode the type list LOCALLY rather
than reading the global constants:

  1. The inventory editor's Type <select> dropdown (static HTML)
     - User-visible: Type dropdown was missing 'VM' option entirely
       so you couldn't actually pick VM as a type when creating a record.
       This is the bug you reported.

  2. renderInventoryChips (line ~5615) - uses local typeOrder/typeLabels
     - Effect: VMs chip wouldn't appear in the chip filter row even
       after creating a VM-type record.

  3. The inventory metrics breakdown (line ~5593) - uses local typeOrder
     - Effect: VM counts silently dropped from the "X hosts, Y network..."
       summary line at the top of the inventory tab.

This patch fixes all three by routing through the global constants
(INV_TYPE_ORDER and INV_TYPE_LABELS) rather than keeping local copies.
That way, future type additions only need to update the global constants
and these surfaces follow automatically.

Must be applied AFTER patch_vm_type.py.

Run once from ~/netwatch/:
    python3 patch_vm_type_followup.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_vmfollowup.
Idempotent - safe to re-run.
"""

import os, shutil, sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_vmfollowup"
SENTINEL = '<option value="vm">'


PATCHES = [
    # ──────────────────────────────────────────────────────────────────────
    # 1. Add VM to the static <select> dropdown in the editor form.
    # Inserted right after the Host option so it sits where the type
    # ordering would naturally place it.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''            <option value="host">Host (computer/server)</option>
            <option value="network">Network device (switch/router/AP)</option>''',
        '''            <option value="host">Host (computer/server)</option>
            <option value="vm">VM (virtual machine)</option>
            <option value="network">Network device (switch/router/AP)</option>'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 2. renderInventoryChips: replace the local typeOrder/typeLabels
    # arrays with the global constants. This is more correct AND
    # automatically picks up future type additions.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  const typeOrder = ['host', 'network', 'ups', 'disk', 'peripheral'];
  const typeLabels = {host:'Hosts', network:'Network', ups:'UPS', disk:'Disks', peripheral:'Peripherals'};
  const typeChips = ['<span class="inv-chip inv-chip-type ' + (_inventoryFilter.deviceType === null ? 'active' : '') + '" onclick="setInvTypeFilter(null)">All<span class="inv-chip-count">' + _inventoryData.length + '</span></span>'];
  typeOrder.forEach(t => {''',
        '''  // Use the global INV_TYPE_ORDER / INV_TYPE_LABELS constants so this
  // automatically picks up future type additions.
  const typeChips = ['<span class="inv-chip inv-chip-type ' + (_inventoryFilter.deviceType === null ? 'active' : '') + '" onclick="setInvTypeFilter(null)">All<span class="inv-chip-count">' + _inventoryData.length + '</span></span>'];
  INV_TYPE_ORDER.forEach(t => {'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 3. Update the chip-rendering loop to use INV_TYPE_LABELS lookup
    # instead of the now-removed local typeLabels variable.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''    typeChips.push('<span class="inv-chip inv-chip-type ' + (_inventoryFilter.deviceType === t ? 'active' : '') + '" onclick="setInvTypeFilter(' + safeArg + ')">' + typeLabels[t] + '<span class="inv-chip-count">' + typeCounts[t] + '</span></span>');''',
        '''    typeChips.push('<span class="inv-chip inv-chip-type ' + (_inventoryFilter.deviceType === t ? 'active' : '') + '" onclick="setInvTypeFilter(' + safeArg + ')">' + (INV_TYPE_LABELS[t] || t) + '<span class="inv-chip-count">' + typeCounts[t] + '</span></span>');'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 4. Inventory metrics breakdown: same fix - use global constants.
    # This one we can simplify slightly since typeLabels was using
    # 'Other' for peripheral here (different from chip labels) - so we
    # preserve that distinction by lowercasing the global label.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  const typeOrder = ['host', 'network', 'ups', 'disk', 'peripheral'];
  const typeLabels = {host:'Hosts', network:'Network', ups:'UPS', disk:'Disks', peripheral:'Other'};
  const breakdownParts = typeOrder
    .filter(t => typeCounts[t])
    .map(t => typeCounts[t] + ' ' + typeLabels[t].toLowerCase());''',
        '''  // Use the global INV_TYPE_ORDER constant. For the breakdown line we
  // lowercase the labels and keep "other" for peripheral (was a special
  // case in the original code).
  const breakdownLabels = Object.assign({}, INV_TYPE_LABELS, {peripheral: 'Other'});
  const breakdownParts = INV_TYPE_ORDER
    .filter(t => typeCounts[t])
    .map(t => typeCounts[t] + ' ' + (breakdownLabels[t] || t).toLowerCase());'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 5. Bump version
    # ──────────────────────────────────────────────────────────────────────
    (
        'netwatch v3.26 - raspberry pi',
        'netwatch v3.27 - raspberry pi'
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

    if 'INVENTORY_TYPES = ("host", "vm",' not in content:
        print("ERROR: This patch requires patch_vm_type first.")
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

    if 'VERSION = "3.26"' in content:
        content = content.replace('VERSION = "3.26"', 'VERSION = "3.27"', 1)

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
    print("  2. Refresh the dashboard. Open the inventory editor (+ Add).")
    print("     The Type dropdown now shows 'VM (virtual machine)' between")
    print("     Host and Network device.")
    print("  3. Once you have at least one VM-type record, the VMs chip")
    print("     will appear in the inventory filter row, and the metrics")
    print("     breakdown will include VMs in the count summary.")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
