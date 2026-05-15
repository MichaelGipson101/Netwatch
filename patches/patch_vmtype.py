#!/usr/bin/env python3
"""
netwatch patch: add 'vm' as a first-class inventory device type.

VMs become a real type alongside host/network/ups/disk/peripheral, with
their own type-specific properties: hypervisor, vCPU count, allocated
RAM, allocated disk, and autostart-with-host. They keep all the host
fields too (CPU description, OS, MAC, IP, etc.) since VMs ARE hosts in
every functional sense.

WHAT THIS CHANGES:

  - Backend: 'vm' added to INVENTORY_TYPES; new properties registered
  - Inventory editor: when type=VM, both the host fields AND the new
    VM-specific fields appear (since VMs are hosts that ALSO have
    hypervisor/vCPU info)
  - Type filter chip row: 'VMs' appears between 'Hosts' and 'Network'
  - Per-type table: VMs get a tailored column set focused on their
    properties (Hypervisor, vCPU, Allocated RAM, Status)
  - Topology web view: VMs render as smaller circles (radius 16 vs 22)
    with a slight purple-tinted fill, visually conveying "smaller, runs
    inside something"
  - VM badge logic: badge now shows for nodes with EITHER device_type=vm
    OR being the source of a virtual edge. This keeps backward
    compatibility with VMs you previously created as host-type with
    virtual connections.

WHAT YOU CAN DO AFTER:

  - Create new VM records cleanly with the proper type
  - Optionally re-edit existing VMs (TrueNAS, EmailArchive, etc.) and
    change their type from host to vm. Then they'll appear under the
    VMs filter chip and have proper VM properties available.
  - Filter the inventory by the 'VMs' chip to see only your guests
  - Export your inventory and the VM-specific fields go to the XLSX too

NOT REQUIRED: existing host-type VMs with virtual connections still get
their VM badge in the web view. Migration is optional, not mandatory.

Must be applied AFTER patch_bugfix_drawer_wol.py.

Run once from ~/netwatch/:
    python3 patch_vm_type.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_vmtype.
Idempotent - safe to re-run.
"""

import os, shutil, sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_vmtype"
SENTINEL = '"vm",'  # presence in INVENTORY_TYPES means already patched


PATCHES = [
    # ──────────────────────────────────────────────────────────────────────
    # 1. Backend: add 'vm' to INVENTORY_TYPES (between host and network)
    # ──────────────────────────────────────────────────────────────────────
    (
        '''INVENTORY_TYPES = ("host", "network", "ups", "disk", "peripheral")''',
        '''INVENTORY_TYPES = ("host", "vm", "network", "ups", "disk", "peripheral")'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 2. Backend: register VM-specific properties.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''INVENTORY_TYPE_PROPERTIES = {
    "host": [],  # all fields are top-level (cpu, ram, os, etc.)
    "network": [''',
        '''INVENTORY_TYPE_PROPERTIES = {
    "host": [],  # all fields are top-level (cpu, ram, os, etc.)
    "vm": [
        # VMs use ALL the host fields (cpu, ram, os, etc. all top-level)
        # AND these VM-specific fields stored in the properties JSON.
        ("hypervisor",     "string", "Hypervisor (Proxmox/KVM/ESXi/etc.)"),
        ("vcpu_count",     "int",    "vCPU count"),
        ("ram_alloc_gb",   "int",    "Allocated RAM (GB)"),
        ("disk_alloc_gb",  "int",    "Allocated disk (GB)"),
        ("autostart",      "bool",   "Auto-starts with host"),
    ],
    "network": ['''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 3. Frontend: mirror the VM-specific properties in the JS dictionary
    # used by the inventory editor form.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''const INVENTORY_TYPE_PROPERTIES = {
  host: [],
  network: [''',
        '''const INVENTORY_TYPE_PROPERTIES = {
  host: [],
  vm: [
    {key:"hypervisor",    type:"string", label:"Hypervisor (Proxmox/KVM/ESXi/etc.)"},
    {key:"vcpu_count",    type:"int",    label:"vCPU count"},
    {key:"ram_alloc_gb",  type:"int",    label:"Allocated RAM (GB)"},
    {key:"disk_alloc_gb", type:"int",    label:"Allocated disk (GB)"},
    {key:"autostart",     type:"bool",   label:"Auto-starts with host"},
  ],
  network: ['''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 4. Editor form: when type=VM, the host fields (CPU, RAM, OS, etc.)
    # should ALSO be visible since VMs are hosts. Currently the code
    # hides them for any type that isn't "host". We make it match
    # "host" or "vm".
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  // Show host-specific fields only for host type
  hostFields.style.display = (newType === "host") ? "contents" : "none";''',
        '''  // Show host-specific fields for host AND vm types - VMs are hosts
  // that happen to run on a hypervisor, so they use all host fields PLUS
  // the VM-specific properties.
  const hostLikeTypes = (newType === "host" || newType === "vm");
  hostFields.style.display = hostLikeTypes ? "contents" : "none";'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 5. Per-type table columns: define a sensible column set for VMs
    # focusing on what makes a VM unique - hypervisor, vCPU, RAM,
    # plus its monitored status.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''const INV_TYPE_COLUMNS = {
  host: [
    {key:'system',     label:'System',   sort:'system'},
    {key:'category',   label:'Category', sort:'category'},
    {key:'cpu',        label:'CPU',      sort:'cpu'},
    {key:'ram_gb',     label:'RAM',      sort:'ram_gb'},
    {key:'os',         label:'OS',       sort:'os'},
    {key:'linked',     label:'Status',   sort:'linked'},
  ],
  network: [''',
        '''const INV_TYPE_COLUMNS = {
  host: [
    {key:'system',     label:'System',   sort:'system'},
    {key:'category',   label:'Category', sort:'category'},
    {key:'cpu',        label:'CPU',      sort:'cpu'},
    {key:'ram_gb',     label:'RAM',      sort:'ram_gb'},
    {key:'os',         label:'OS',       sort:'os'},
    {key:'linked',     label:'Status',   sort:'linked'},
  ],
  vm: [
    {key:'system',           label:'System',     sort:'system'},
    {key:'p:hypervisor',     label:'Hypervisor', sort:'p:hypervisor'},
    {key:'p:vcpu_count',     label:'vCPU',       sort:'p:vcpu_count'},
    {key:'p:ram_alloc_gb',   label:'RAM (GB)',   sort:'p:ram_alloc_gb'},
    {key:'p:disk_alloc_gb',  label:'Disk (GB)',  sort:'p:disk_alloc_gb'},
    {key:'os',               label:'OS',         sort:'os'},
    {key:'linked',           label:'Status',     sort:'linked'},
  ],
  network: ['''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 6. Type label + ordering for the chip row
    # ──────────────────────────────────────────────────────────────────────
    (
        '''const INV_TYPE_LABELS = {
  host: 'Hosts', network: 'Network', ups: 'UPS',
  disk: 'Disks', peripheral: 'Peripherals'
};
const INV_TYPE_ORDER = ['host', 'network', 'ups', 'disk', 'peripheral'];''',
        '''const INV_TYPE_LABELS = {
  host: 'Hosts', vm: 'VMs', network: 'Network', ups: 'UPS',
  disk: 'Disks', peripheral: 'Peripherals'
};
const INV_TYPE_ORDER = ['host', 'vm', 'network', 'ups', 'disk', 'peripheral'];'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 7. Topology web view: render VMs as smaller circles (radius 16),
    # distinct from regular hosts (radius 22). Also tweak the radius
    # used by force-collide so VMs cluster more tightly.
    # We anchor on the existing host (else) branch in the .each() block.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''    } else {
      // host (default)
      sel.append('circle').attr('class', 'topo-node-shape').attr('r', 22);
      sel.append('text').attr('class', 'topo-node-label-below')
        .attr('y', 22 + 14).text(truncateLabel(d.name, 18));
    }''',
        '''    } else if(d.device_type === 'vm'){
      // VM - smaller circle than a host, slight purple tint, conveys
      // "runs inside something else" via reduced size
      sel.append('circle').attr('class', 'topo-node-shape').attr('r', 16);
      sel.append('text').attr('class', 'topo-node-label-below')
        .attr('y', 16 + 14).text(truncateLabel(d.name, 18));
    } else {
      // host (default)
      sel.append('circle').attr('class', 'topo-node-shape').attr('r', 22);
      sel.append('text').attr('class', 'topo-node-label-below')
        .attr('y', 22 + 14).text(truncateLabel(d.name, 18));
    }'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 8. nodeRadiusFor (used by force-collide and seedPosition): give vm
    # a radius between peripheral (16) and host (24).
    # ──────────────────────────────────────────────────────────────────────
    (
        '''function nodeRadiusFor(d){
  if(d.device_type === 'network' || d.device_type === 'ups') return 60;
  if(d.device_type === 'disk') return 22;
  if(d.device_type === 'peripheral') return 16;
  return 24; // host
}''',
        '''function nodeRadiusFor(d){
  if(d.device_type === 'network' || d.device_type === 'ups') return 60;
  if(d.device_type === 'disk') return 22;
  if(d.device_type === 'peripheral') return 16;
  if(d.device_type === 'vm') return 18;
  return 24; // host
}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 9. seedPosition: VMs should seed near the host cluster (close to
    # center). The force layout with short virtual-edge link distance
    # will pull them tight to their actual host.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  if(t === 'peripheral') return { x: cx + 200 + (Math.random() - 0.5) * 80, y: cy - 100 + (Math.random() - 0.5) * 50 };
  // host: ring around the center
  const angle = Math.random() * Math.PI * 2;
  const r = 180 + Math.random() * 40;
  return { x: cx + Math.cos(angle) * r, y: cy + Math.sin(angle) * r };
}''',
        '''  if(t === 'peripheral') return { x: cx + 200 + (Math.random() - 0.5) * 80, y: cy - 100 + (Math.random() - 0.5) * 50 };
  if(t === 'vm'){
    // VMs seed near the center so they\'re close to their host once
    // the virtual edge force pulls them in.
    return { x: cx + (Math.random() - 0.5) * 80, y: cy + (Math.random() - 0.5) * 80 };
  }
  // host: ring around the center
  const angle = Math.random() * Math.PI * 2;
  const r = 180 + Math.random() * 40;
  return { x: cx + Math.cos(angle) * r, y: cy + Math.sin(angle) * r };
}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 10. VM badge logic: extend so it triggers for device_type='vm' too,
    # not only for sources of virtual edges. Backward compatible: existing
    # data still gets badges via the edge-based detection.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''  // Compute the set of VM node IDs: any node that is the SOURCE of a
  // 'virtual' edge is treated as a VM. This is implicit detection - no
  // schema changes needed - and updates automatically when virtual
  // connections are added or removed.
  const vmIds = new Set();
  renderEdges.forEach(e => {
    if(e.connection_type === 'virtual'){
      vmIds.add(typeof e.source === 'object' ? e.source.id : e.source);
    }
  });''',
        '''  // Compute the set of VM node IDs. A node is treated as a VM if EITHER:
  //   1. Its device_type is 'vm' (the explicit, modern way), OR
  //   2. It\'s the source of a virtual edge (legacy implicit detection,
  //      kept for backward compatibility with VMs you created before the
  //      vm device_type existed)
  const vmIds = new Set();
  renderNodes.forEach(n => {
    if(n.device_type === 'vm') vmIds.add(n.id);
  });
  renderEdges.forEach(e => {
    if(e.connection_type === 'virtual'){
      vmIds.add(typeof e.source === 'object' ? e.source.id : e.source);
    }
  });'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 11. CSS: VM-specific node fill (slight purple tint to match badge)
    # ──────────────────────────────────────────────────────────────────────
    (
        '''.topo-node-network .topo-node-shape{fill:#1a2540}
.topo-node-ups     .topo-node-shape{fill:#3a2e15}
.topo-node-disk    .topo-node-shape{fill:#1f2a25}
.topo-node-peripheral .topo-node-shape{fill:var(--subtle)}''',
        '''.topo-node-network .topo-node-shape{fill:#1a2540}
.topo-node-ups     .topo-node-shape{fill:#3a2e15}
.topo-node-disk    .topo-node-shape{fill:#1f2a25}
.topo-node-vm      .topo-node-shape{fill:#1f1a26}
.topo-node-peripheral .topo-node-shape{fill:var(--subtle)}'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 12. Bump version
    # ──────────────────────────────────────────────────────────────────────
    (
        'netwatch v3.25 - raspberry pi',
        'netwatch v3.26 - raspberry pi'
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

    if "navigateToHostDrawerSafe" not in content:
        print("ERROR: This patch requires patch_bugfix_drawer_wol first.")
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

    if 'VERSION = "3.25"' in content:
        content = content.replace('VERSION = "3.25"', 'VERSION = "3.26"', 1)

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
    print("  2. Open the Inventory tab. The chip row now includes a VMs")
    print("     chip between Hosts and Network.")
    print("  3. To create a new VM record: + Add, set Type to 'VM'. Both")
    print("     the standard host fields (CPU, RAM, OS, MAC) AND the new")
    print("     VM-specific fields (hypervisor, vCPU, allocated RAM/disk,")
    print("     autostart) appear in the editor.")
    print("  4. Optional: open your existing VM records (TrueNAS, etc.)")
    print("     and change their Type from 'host' to 'vm' to migrate them")
    print("     into the proper category. Their existing virtual edges")
    print("     still keep them clustered around their host in the web view.")
    print()
    print("In the topology web view, VMs render as smaller circles with a")
    print("subtle purple-tinted fill, visually distinct from physical hosts.")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
