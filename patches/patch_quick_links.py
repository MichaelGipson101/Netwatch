#!/usr/bin/env python3
"""
netwatch patch: per-host quick links.

Adds a "primary URL" plus optional named "extra links" per host. Surfaces
them in the host detail drawer with a prominent "Open" button for the
primary URL and a clean list for any extras.

Behavior:
  - Primary URL defaults to http://<ip> if blank
  - Extra links are name + url pairs (e.g. "Admin" -> "https://nas:5001")
  - Editable via the host editor (in the existing "more fields" expansion)
  - Renders in the detail drawer just below the stat grid
  - URLs validated as http(s) only - no javascript:/file:/etc.
  - Links open in a new tab so they don't replace the dashboard

Must be applied AFTER patch_wol_subnet.py.

Run once from ~/netwatch/:
    python3 patch_quick_links.py
    sudo systemctl restart netwatch

Backup saved to monitor.py.bak_links.
Idempotent - safe to re-run.
"""

import os
import shutil
import sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_links"
SENTINEL = "f-primary-url"  # presence means already patched


PATCHES = [
    # ──── 1. Add 'links' field to HostState dataclass
    (
        '''@dataclass
class HostState:
    name: str
    ip: str
    group: str
    interval: int
    always_on: bool = True
    specs: dict = field(default_factory=dict)
    notes: str = ""
    history: deque = field(default_factory=lambda: deque(maxlen=100))''',
        '''@dataclass
class HostState:
    name: str
    ip: str
    group: str
    interval: int
    always_on: bool = True
    specs: dict = field(default_factory=dict)
    notes: str = ""
    links: dict = field(default_factory=dict)
    history: deque = field(default_factory=lambda: deque(maxlen=100))'''
    ),

    # ──── 2. Include links in to_dict (with auto-default for primary)
    (
        '''            return {
                "name":         self.name,
                "ip":           self.ip,
                "group":        self.group,
                "interval":     self.interval,
                "always_on":    self.always_on,
                "specs":        dict(self.specs) if self.specs else {},
                "notes":        self.notes,
                "status":       self.status_str,''',
        '''            primary_url = (self.links or {}).get("primary", "").strip()
            if not primary_url:
                primary_url = f"http://{self.ip}"
            extra_links = []
            for entry in (self.links or {}).get("extras", []) or []:
                if isinstance(entry, dict) and entry.get("url") and entry.get("name"):
                    extra_links.append({"name": str(entry["name"]), "url": str(entry["url"])})
            return {
                "name":         self.name,
                "ip":           self.ip,
                "group":        self.group,
                "interval":     self.interval,
                "always_on":    self.always_on,
                "specs":        dict(self.specs) if self.specs else {},
                "notes":        self.notes,
                "links":        {"primary": primary_url, "extras": extra_links},
                "status":       self.status_str,'''
    ),

    # ──── 3. _spawn: accept links
    (
        '''    def _spawn(self, name, ip, group, interval, always_on=True, specs=None, notes=""):
        host = HostState(
            name=name, ip=ip, group=group, interval=interval,
            always_on=always_on,
            specs=specs or {},
            notes=notes or "",
            history=deque(maxlen=self.history_window),
            stop_event=threading.Event(),
        )''',
        '''    def _spawn(self, name, ip, group, interval, always_on=True, specs=None, notes="", links=None):
        host = HostState(
            name=name, ip=ip, group=group, interval=interval,
            always_on=always_on,
            specs=specs or {},
            notes=notes or "",
            links=links or {},
            history=deque(maxlen=self.history_window),
            stop_event=threading.Event(),
        )'''
    ),

    # ──── 4. load_initial: pass links
    (
        '''    def load_initial(self, raw_hosts, default_interval):
        with self.lock:
            for h in raw_hosts:
                self.hosts.append(self._spawn(
                    h["name"], h["ip"], h.get("group", "General"),
                    h.get("interval", default_interval),
                    bool(h.get("always_on", True)),
                    h.get("specs") if isinstance(h.get("specs"), dict) else None,
                    h.get("notes", "") if isinstance(h.get("notes"), str) else ""
                ))''',
        '''    def load_initial(self, raw_hosts, default_interval):
        with self.lock:
            for h in raw_hosts:
                self.hosts.append(self._spawn(
                    h["name"], h["ip"], h.get("group", "General"),
                    h.get("interval", default_interval),
                    bool(h.get("always_on", True)),
                    h.get("specs") if isinstance(h.get("specs"), dict) else None,
                    h.get("notes", "") if isinstance(h.get("notes"), str) else "",
                    h.get("links") if isinstance(h.get("links"), dict) else None
                ))'''
    ),

    # ──── 5. reload_from_config: handle links
    (
        '''                always_on = bool(h.get("always_on", True))
                specs = h.get("specs") if isinstance(h.get("specs"), dict) else {}
                notes = h.get("notes", "") if isinstance(h.get("notes"), str) else ""
                if ip in current_by_ip:
                    existing = current_by_ip[ip]
                    existing.name = name
                    existing.group = group
                    existing.interval = interval
                    existing.always_on = always_on
                    existing.specs = specs
                    existing.notes = notes
                    rebuilt.append(existing)
                else:
                    rebuilt.append(self._spawn(name, ip, group, interval, always_on, specs, notes))''',
        '''                always_on = bool(h.get("always_on", True))
                specs = h.get("specs") if isinstance(h.get("specs"), dict) else {}
                notes = h.get("notes", "") if isinstance(h.get("notes"), str) else ""
                links = h.get("links") if isinstance(h.get("links"), dict) else {}
                if ip in current_by_ip:
                    existing = current_by_ip[ip]
                    existing.name = name
                    existing.group = group
                    existing.interval = interval
                    existing.always_on = always_on
                    existing.specs = specs
                    existing.notes = notes
                    existing.links = links
                    rebuilt.append(existing)
                else:
                    rebuilt.append(self._spawn(name, ip, group, interval, always_on, specs, notes, links))'''
    ),

    # ──── 6. validate_hosts_config: validate links
    (
        '''        if "notes" in h and not isinstance(h["notes"], str):
            return False, f"Host '{h['name']}': notes must be a string."
    return True, None''',
        '''        if "notes" in h and not isinstance(h["notes"], str):
            return False, f"Host '{h['name']}': notes must be a string."
        if "links" in h:
            if not isinstance(h["links"], dict):
                return False, f"Host '{h['name']}': links must be a mapping."
            primary = h["links"].get("primary", "")
            if primary and not _validate_url(primary):
                return False, f"Host '{h['name']}': primary URL must start with http:// or https://"
            extras = h["links"].get("extras", []) or []
            if extras and not isinstance(extras, list):
                return False, f"Host '{h['name']}': links.extras must be a list."
            for i, x in enumerate(extras):
                if not isinstance(x, dict):
                    return False, f"Host '{h['name']}': link #{i+1} must be a mapping."
                if not x.get("name") or not isinstance(x["name"], str):
                    return False, f"Host '{h['name']}': link #{i+1} is missing a name."
                if not x.get("url") or not _validate_url(x["url"]):
                    return False, f"Host '{h['name']}': link '{x.get('name')}' has an invalid URL."
    return True, None'''
    ),

    # ──── 7. Add _validate_url helper just before validate_hosts_config
    (
        '''def validate_hosts_config(config):''',
        '''def _validate_url(s):
    """Allow only http/https URLs. Rejects javascript:, file:, etc."""
    if not isinstance(s, str):
        return False
    s = s.strip()
    return s.startswith("http://") or s.startswith("https://")


def validate_hosts_config(config):'''
    ),
]


# ─── Frontend changes are surgical: add fields to editor, links section to drawer ──

FRONTEND_PATCHES = [
    # ──── F1. Update editor row HTML (addRow) to include link fields in the
    # row-extra section. We add them right after the MAC field.
    (
        '''    + '<label class="full">MAC address<input type="text" class="f-mac" placeholder="aa:bb:cc:dd:ee:ff (required for Wake-on-LAN)" value="' + escapeHtml(specs.mac || '') + '"></label>'
    + '<label class="full">Notes<textarea class="f-notes" placeholder="Anything else worth remembering about this device.">' + escapeHtml(notes) + '</textarea></label>'
    + '</div>';''',
        '''    + '<label class="full">MAC address<input type="text" class="f-mac" placeholder="aa:bb:cc:dd:ee:ff (required for Wake-on-LAN)" value="' + escapeHtml(specs.mac || '') + '"></label>'
    + '<label class="full">Primary URL<input type="text" class="f-primary-url" placeholder="http://' + escapeHtml(h ? h.ip : 'host') + ' (defaults to http://<ip> if blank)" value="' + escapeHtml((h && h.links && h.links.primary && !h.links.primary.endsWith("/" + h.ip) ? h.links.primary : "")) + '"></label>'
    + '<label class="full">Extra links<div class="extras-wrap" data-ip="' + escapeHtml(h ? h.ip : "") + '"></div><button type="button" class="add-extra-btn">+ Add link</button></label>'
    + '<label class="full">Notes<textarea class="f-notes" placeholder="Anything else worth remembering about this device.">' + escapeHtml(notes) + '</textarea></label>'
    + '</div>';'''
    ),

    # ──── F2. After document.getElementById('edit-rows').appendChild(row);
    # we need to populate the extras and wire up the "+ Add link" button.
    (
        '''  document.getElementById('edit-rows').appendChild(row);

  // Wire up the more/delete buttons
  row.querySelector('.more-btn').addEventListener('click', () => {
    const extra = row.querySelector('.row-extra');
    const btn = row.querySelector('.more-btn');
    extra.classList.toggle('open');
    btn.classList.toggle('open', extra.classList.contains('open'));
  });
  row.querySelector('.del-btn').addEventListener('click', () => row.remove());
}''',
        '''  document.getElementById('edit-rows').appendChild(row);

  // Wire up the more/delete buttons
  row.querySelector('.more-btn').addEventListener('click', () => {
    const extra = row.querySelector('.row-extra');
    const btn = row.querySelector('.more-btn');
    extra.classList.toggle('open');
    btn.classList.toggle('open', extra.classList.contains('open'));
  });
  row.querySelector('.del-btn').addEventListener('click', () => row.remove());

  // Populate extras + wire up "+ Add link"
  const extrasWrap = row.querySelector('.extras-wrap');
  const initialExtras = (h && h.links && Array.isArray(h.links.extras)) ? h.links.extras : [];
  initialExtras.forEach(e => addExtraLinkRow(extrasWrap, e.name, e.url));
  row.querySelector('.add-extra-btn').addEventListener('click', () => addExtraLinkRow(extrasWrap, '', ''));
}

function addExtraLinkRow(container, name, url){
  const div = document.createElement('div');
  div.className = 'extra-link-row';
  div.innerHTML =
    '<input type="text" class="f-extra-name" placeholder="Label (e.g. Admin)" value="' + escapeHtml(name || '') + '">'
    + '<input type="text" class="f-extra-url" placeholder="https://..." value="' + escapeHtml(url || '') + '">'
    + '<button type="button" class="del-btn" title="Remove">X</button>';
  container.appendChild(div);
  div.querySelector('.del-btn').addEventListener('click', () => div.remove());
}'''
    ),

    # ──── F3. saveHosts: collect links from the form
    (
        '''    const specs = {};
    ['cpu','ram','storage','os','mac'].forEach(k => {
      const el = row.querySelector('.f-' + k);
      if(el && el.value.trim()) specs[k] = el.value.trim();
    });
    if(Object.keys(specs).length) entry.specs = specs;
    const notesEl = row.querySelector('.f-notes');
    if(notesEl && notesEl.value.trim()) entry.notes = notesEl.value.trim();''',
        '''    const specs = {};
    ['cpu','ram','storage','os','mac'].forEach(k => {
      const el = row.querySelector('.f-' + k);
      if(el && el.value.trim()) specs[k] = el.value.trim();
    });
    if(Object.keys(specs).length) entry.specs = specs;
    const notesEl = row.querySelector('.f-notes');
    if(notesEl && notesEl.value.trim()) entry.notes = notesEl.value.trim();

    // Links
    const links = {};
    const primaryEl = row.querySelector('.f-primary-url');
    const primaryVal = primaryEl ? primaryEl.value.trim() : '';
    if(primaryVal){
      if(!/^https?:\\/\\//.test(primaryVal)){ primaryEl.classList.add('invalid'); hasError = true; }
      else links.primary = primaryVal;
    }
    const extras = [];
    row.querySelectorAll('.extra-link-row').forEach(extraRow => {
      const en = extraRow.querySelector('.f-extra-name').value.trim();
      const eu = extraRow.querySelector('.f-extra-url').value.trim();
      if(!en && !eu) return;
      if(!en || !eu || !/^https?:\\/\\//.test(eu)){
        extraRow.querySelector('.f-extra-url').classList.add('invalid');
        if(!en) extraRow.querySelector('.f-extra-name').classList.add('invalid');
        hasError = true;
        return;
      }
      extras.push({ name: en, url: eu });
    });
    if(extras.length) links.extras = extras;
    if(Object.keys(links).length) entry.links = links;'''
    ),

    # ──── F4. Detail drawer renderDrawer: insert links section right after the stats grid
    (
        '''  let statsHtml = '<div class="d-statgrid">'
    + '<div class="d-stat"><div class="d-stat-label">CURRENT</div><div class="d-stat-val ' + (h.is_up ? 'green' : (isIdle ? '' : 'red')) + '">' + labelLat + '</div></div>'
    + '<div class="d-stat"><div class="d-stat-label">CURRENT LATENCY</div><div class="d-stat-val blue">' + (h.latency_ms !== null ? h.latency_ms.toFixed(1) + ' <sup>ms</sup>' : '-') + '</div></div>'
    + '<div class="d-stat"><div class="d-stat-label">' + (isIdle ? 'AVAILABILITY' : 'UPTIME') + '</div><div class="d-stat-val" style="color:' + uColor + '">' + availLabel + '</div></div>'
    + '<div class="d-stat"><div class="d-stat-label">LAST SEEN</div><div class="d-stat-val" style="font-size:14px">' + lastSeenStr(h.last_seen_up_seconds) + '</div></div>'
    + '</div>';''',
        '''  let statsHtml = '<div class="d-statgrid">'
    + '<div class="d-stat"><div class="d-stat-label">CURRENT</div><div class="d-stat-val ' + (h.is_up ? 'green' : (isIdle ? '' : 'red')) + '">' + labelLat + '</div></div>'
    + '<div class="d-stat"><div class="d-stat-label">CURRENT LATENCY</div><div class="d-stat-val blue">' + (h.latency_ms !== null ? h.latency_ms.toFixed(1) + ' <sup>ms</sup>' : '-') + '</div></div>'
    + '<div class="d-stat"><div class="d-stat-label">' + (isIdle ? 'AVAILABILITY' : 'UPTIME') + '</div><div class="d-stat-val" style="color:' + uColor + '">' + availLabel + '</div></div>'
    + '<div class="d-stat"><div class="d-stat-label">LAST SEEN</div><div class="d-stat-val" style="font-size:14px">' + lastSeenStr(h.last_seen_up_seconds) + '</div></div>'
    + '</div>';

  // Links section (always visible - primary defaults to http://<ip>)
  let linksHtml = '';
  const primaryUrl = (h.links && h.links.primary) || ('http://' + h.ip);
  const extras = (h.links && h.links.extras) || [];
  linksHtml = '<div class="d-section"><div class="d-section-hdr"><span>Quick links</span></div>'
    + '<a class="d-link-primary" href="' + escapeHtml(primaryUrl) + '" target="_blank" rel="noopener">'
    + '<span><span class="d-link-name">Open</span> <span class="d-link-url">' + escapeHtml(primaryUrl) + '</span></span>'
    + '<span class="d-link-arrow">->></span></a>';
  if(extras.length){
    linksHtml += '<div class="d-link-extras">' + extras.map(e =>
      '<a class="d-link-extra" href="' + escapeHtml(e.url) + '" target="_blank" rel="noopener">'
      + '<span class="d-link-name">' + escapeHtml(e.name) + '</span>'
      + '<span class="d-link-url">' + escapeHtml(e.url) + '</span></a>'
    ).join('') + '</div>';
  }
  linksHtml += '</div>';''',
    ),

    # ──── F5. Insert linksHtml into drawerBody output (right after stats)
    (
        '''  document.getElementById('drawer-body').innerHTML = statsHtml + sparkHtml + specsHtml + notesHtml + incHtml + actionsHtml;''',
        '''  document.getElementById('drawer-body').innerHTML = statsHtml + linksHtml + sparkHtml + specsHtml + notesHtml + incHtml + actionsHtml;'''
    ),

    # ──── F6. CSS additions for links section + extra rows in editor
    (
        '''.d-action-status.error{color:var(--red-text)}''',
        '''.d-action-status.error{color:var(--red-text)}

/* Links */
.d-link-primary{display:flex;align-items:center;justify-content:space-between;background:var(--text);color:var(--surface);border-radius:8px;padding:11px 14px;text-decoration:none;font-family:'DM Sans',sans-serif;font-size:13px;font-weight:500;transition:all .15s;border:1px solid var(--text)}
.d-link-primary:hover{background:#000;border-color:#000}
.d-link-primary .d-link-name{font-weight:600}
.d-link-primary .d-link-url{font-family:'DM Mono',monospace;font-size:11px;font-weight:400;color:rgba(255,255,255,.7);margin-left:8px}
.d-link-primary .d-link-arrow{color:rgba(255,255,255,.6);font-size:14px;font-family:'DM Mono',monospace}
.d-link-extras{display:flex;flex-direction:column;gap:5px;margin-top:7px}
.d-link-extra{display:flex;align-items:center;justify-content:space-between;background:var(--subtle);border:1px solid var(--border-light);border-radius:7px;padding:8px 12px;text-decoration:none;color:var(--text);transition:all .15s;font-size:12px}
.d-link-extra:hover{background:var(--surface);border-color:var(--hint)}
.d-link-extra .d-link-name{font-weight:500}
.d-link-extra .d-link-url{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted);margin-left:8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:60%}

/* Extra-link editor rows */
.extras-wrap{display:flex;flex-direction:column;gap:6px;margin-bottom:6px}
.extra-link-row{display:grid;grid-template-columns:1fr 2fr 30px;gap:6px;align-items:center}
.extra-link-row input{width:100%;padding:6px 9px;border:1px solid var(--border);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:12px;background:var(--surface);color:var(--text)}
.extra-link-row input:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px var(--blue-bg)}
.extra-link-row input.invalid{border-color:var(--red);background:var(--red-bg)}
.extra-link-row .del-btn{background:transparent;border:none;color:var(--hint);cursor:pointer;padding:4px;border-radius:4px;font-size:12px;line-height:1}
.extra-link-row .del-btn:hover{background:var(--red-bg);color:var(--red)}
.row-extra .add-extra-btn{margin-top:0;font-size:12px;padding:6px 10px;border:1px dashed var(--border);background:transparent;border-radius:6px;color:var(--muted);cursor:pointer;width:auto}
.row-extra .add-extra-btn:hover{border-color:var(--hint);background:var(--surface);color:var(--text)}'''
    ),

    # ──── F7. Bump version label in footer
    (
        '<span>netwatch v3.2 - raspberry pi</span>',
        '<span>netwatch v3.3 - raspberry pi</span>'
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

    if "send_wol_packet" not in content:
        print("ERROR: This patch requires patch_detail_drawer first.")
        sys.exit(1)

    shutil.copy2(TARGET, BACKUP)
    print(f"[OK] Backed up {TARGET} -> {BACKUP}")

    applied = 0
    failed = 0

    # Apply Python patches
    for i, (old, new) in enumerate(PATCHES, 1):
        count = content.count(old)
        if count == 0:
            print(f"[WARN] Backend patch #{i}: target not found")
            failed += 1
            continue
        if count > 1:
            print(f"[FAIL] Backend patch #{i}: matches {count}x")
            shutil.copy2(BACKUP, TARGET); sys.exit(1)
        content = content.replace(old, new, 1)
        applied += 1

    # Apply frontend patches
    for i, (old, new) in enumerate(FRONTEND_PATCHES, 1):
        count = content.count(old)
        if count == 0:
            print(f"[WARN] Frontend patch #{i}: target not found")
            failed += 1
            continue
        if count > 1:
            print(f"[FAIL] Frontend patch #{i}: matches {count}x")
            shutil.copy2(BACKUP, TARGET); sys.exit(1)
        content = content.replace(old, new, 1)
        applied += 1

    # Bump VERSION
    if 'VERSION = "3.2"' in content:
        content = content.replace('VERSION = "3.2"', 'VERSION = "3.3"', 1)

    open(TARGET, "w").write(content)

    import ast
    try:
        ast.parse(open(TARGET).read())
        print("[OK] Resulting Python is valid")
    except SyntaxError as e:
        print(f"[FAIL] Syntax error: {e}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)

    print(f"[OK] Applied {applied} patches" + (f" ({failed} skipped)" if failed else ""))
    print()
    print("Next steps:")
    print("  1. sudo systemctl restart netwatch")
    print("  2. Refresh dashboard. Click any host - the drawer now shows a")
    print("     'Quick links' section with an 'Open' button defaulting to http://<ip>")
    print("  3. Edit hosts -> click ... on a row to expand:")
    print("     - 'Primary URL' field (e.g. http://192.168.1.50:8123 for HA)")
    print("     - 'Extra links' for additional URLs (Admin, SSH, Logs, etc.)")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
