// hosts-editor.js — extracted from core.js (Netwatch 4.0 page split). Code moved verbatim.

function closeEditor(){ document.getElementById('modal-overlay').classList.remove('open'); }

function openAddHostModal(){
  if(_authState.setup_required){ openSetup(); return; }
  if(!_authState.logged_in){ openLogin(() => openAddHostModal()); return; }
  // Clear all fields
  ['ah-name','ah-ip','ah-group','ah-interval','ah-cpu','ah-ram','ah-storage','ah-os','ah-mac','ah-notes'].forEach(cls => {
    const el = document.querySelector('.' + cls);
    if(el) el.tagName === 'TEXTAREA' ? (el.value = '') : (el.value = cls === 'ah-group' ? 'General' : '');
  });
  ['ah-alwayson','ah-alert'].forEach(cls => {
    const el = document.querySelector('.' + cls);
    if(el) el.checked = true;
  });
  document.getElementById('add-host-error').textContent = '';
  document.getElementById('add-host-status').textContent = '';
  document.getElementById('add-host-overlay').classList.add('open');
  setTimeout(() => { const el = document.querySelector('.ah-name'); if(el) el.focus(); }, 50);
}

function closeAddHostModal(){
  document.getElementById('add-host-overlay').classList.remove('open');
}

async function saveAddHost(){
  const nameEl  = document.querySelector('.ah-name');
  const ipEl    = document.querySelector('.ah-ip');
  const macEl   = document.querySelector('.ah-mac');
  const errEl   = document.getElementById('add-host-error');
  const statEl  = document.getElementById('add-host-status');
  errEl.textContent = '';
  [nameEl, ipEl, macEl].forEach(el => el && el.classList.remove('invalid'));

  const name  = nameEl.value.trim();
  const ip    = ipEl.value.trim();
  const group = (document.querySelector('.ah-group').value.trim()) || 'General';
  const intervalRaw = document.querySelector('.ah-interval').value.trim();
  const mac   = macEl.value.trim();

  let hasError = false;
  if(!name){ nameEl.classList.add('invalid'); hasError = true; }
  if(!ipValid(ip)){ ipEl.classList.add('invalid'); hasError = true; }
  if(mac && !macValid(mac)){ macEl.classList.add('invalid'); hasError = true; }
  if(hasError){ errEl.textContent = 'Fix the highlighted fields.'; return; }

  const entry = { name, ip, group, always_on: document.querySelector('.ah-alwayson').checked };
  if(!document.querySelector('.ah-alert').checked) entry.alert = false;
  if(intervalRaw){ const iv = parseInt(intervalRaw); if(!isNaN(iv) && iv >= 5) entry.interval = iv; }

  const specs = {};
  [['cpu','ah-cpu'],['ram','ah-ram'],['storage','ah-storage'],['os','ah-os'],['mac','ah-mac']].forEach(([k, cls]) => {
    const el = document.querySelector('.' + cls);
    if(el && el.value.trim()) specs[k] = el.value.trim();
  });
  if(Object.keys(specs).length) entry.specs = specs;
  const notes = document.querySelector('.ah-notes').value.trim();
  if(notes) entry.notes = notes;

  statEl.textContent = 'Saving…';
  try {
    const existing = await fetch('/api/hosts');
    if(existing.status === 401){ closeAddHostModal(); openLogin(() => openAddHostModal()); return; }
    const existingData = await existing.json();
    const hosts = [...(existingData.hosts || []), entry];

    if(hosts.some((h, i) => i !== hosts.length - 1 && h.ip === ip)){
      ipEl.classList.add('invalid');
      errEl.textContent = 'A host with this IP already exists.';
      statEl.textContent = '';
      return;
    }

    const res = await apiFetch('/api/hosts', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ hosts })
    });
    const data = await res.json();
    if(!res.ok){ statEl.textContent = ''; errEl.textContent = data.error || 'Save failed.'; return; }
    statEl.textContent = 'Added!';
    setTimeout(() => { closeAddHostModal(); refresh(); }, 600);
  } catch(e){ statEl.textContent = ''; errEl.textContent = 'Network error.'; }
}

function addRow(h){
  const row = document.createElement('div');
  row.className = 'edit-row';
  const alwaysOn = !h || h.always_on !== false;
  const alertOn  = !h || h.alert !== false;
  const specs = (h && h.specs) || {};
  const notes = (h && h.notes) || '';
  const hasLinks = !!(h && h.links && (h.links.primary || (h.links.extras && h.links.extras.length)));
  const hasServices = !!(h && Array.isArray(h.services) && h.services.length);
  const hasData = !!(specs.cpu || specs.ram || specs.storage || specs.os || specs.mac || notes || hasLinks || hasServices);

  row.innerHTML =
    '<div class="row-main">'
    + '<input type="text" placeholder="My device" class="f-name" value="' + (h ? escapeHtml(h.name) : '') + '">'
    + '<input type="text" placeholder="192.168.1.1" class="f-ip" value="' + (h ? escapeHtml(h.ip) : '') + '">'
    + '<input type="text" placeholder="Network" class="f-group" value="' + (h ? escapeHtml(h.group || "General") : "General") + '">'
    + '<input type="number" min="5" placeholder="30" class="f-interval" value="' + (h && h.interval ? h.interval : '') + '">'
    + '<div class="ao-cell"><input type="checkbox" class="f-alwayson" title="Always on? Uncheck for laptops/phones/etc." ' + (alwaysOn ? 'checked' : '') + '></div>'
    + '<div class="ao-cell"><input type="checkbox" class="f-alert" title="Alert on down? Uncheck to silence ntfy notifications for this host." ' + (alertOn ? 'checked' : '') + '></div>'
    + '<button class="more-btn' + (hasData ? ' has-data' : '') + '" type="button" title="' + (hasData ? 'More fields (this host has saved extras)' : 'More fields (specs, notes, links)') + '">...</button>'
    + '<button class="del-btn" title="Remove" type="button">X</button>'
    + '</div>'
    + '<div class="row-extra">'
    + '<label>CPU<input type="text" class="f-cpu" placeholder="e.g. Intel i9-12900K" value="' + escapeHtml(specs.cpu || '') + '"></label>'
    + '<label>RAM<input type="text" class="f-ram" placeholder="e.g. 64 GB DDR5" value="' + escapeHtml(specs.ram || '') + '"></label>'
    + '<label>Storage<input type="text" class="f-storage" placeholder="e.g. 2TB NVMe" value="' + escapeHtml(specs.storage || '') + '"></label>'
    + '<label>OS<input type="text" class="f-os" placeholder="e.g. Windows 11" value="' + escapeHtml(specs.os || '') + '"></label>'
    + '<label class="full">MAC address<div class="mac-row"><input type="text" class="f-mac" placeholder="aa:bb:cc:dd:ee:ff (required for Wake-on-LAN)" value="' + escapeHtml(specs.mac || '') + '" data-auto="' + (specs.mac_auto ? '1' : '') + '"><button type="button" class="mac-detect-btn" onclick="detectMac(this)">Detect</button>' + (specs.mac_auto ? '<span class="mac-auto-tag" title="This MAC was auto-detected from the network">auto</span>' : '') + '</div></label>'
    + '<label class="full">Services (TCP port checks)<div class="svc-wrap"></div><button type="button" class="add-svc-btn">+ Add service</button><label class="svc-strict-toggle"><input type="checkbox" class="f-strict" ' + ((h && h.strict) ? 'checked' : '') + '>Strict mode (mark host DEGRADED if any service fails)</label></label>'
    + '<label class="full">Primary URL<input type="text" class="f-primary-url" placeholder="http://' + escapeHtml(h ? h.ip : 'host') + ' (defaults to http://<ip> if blank)" value="' + escapeHtml((h && h.links && h.links.primary && !h.links.primary.endsWith("/" + h.ip) ? h.links.primary : "")) + '"></label>'
    + '<label class="full">Extra links<div class="extras-wrap" data-ip="' + escapeHtml(h ? h.ip : "") + '"></div><button type="button" class="add-extra-btn">+ Add link</button></label>'
    + '<label class="full">Notes<textarea class="f-notes" placeholder="Anything else worth remembering about this device.">' + escapeHtml(notes) + '</textarea></label>'
    + '</div>';

  document.getElementById('edit-rows').appendChild(row);

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

  // Populate services + wire up "+ Add service"
  const svcWrap = row.querySelector('.svc-wrap');
  const initialServices = (h && Array.isArray(h.services)) ? h.services : [];
  initialServices.forEach(s => addServiceRow(svcWrap, s.port, s.name));
  row.querySelector('.add-svc-btn').addEventListener('click', () => addServiceRow(svcWrap, '', ''));
}

function addServiceRow(container, port, name){
  const div = document.createElement('div');
  div.className = 'svc-row';
  div.innerHTML =
    '<input type="number" class="f-svc-port" placeholder="80" min="1" max="65535" value="' + escapeHtml(port !== undefined && port !== null ? String(port) : '') + '">'
    + '<input type="text" class="f-svc-name" placeholder="e.g. Web UI, SSH" value="' + escapeHtml(name || '') + '">'
    + '<button type="button" class="del-btn" title="Remove">X</button>';
  container.appendChild(div);
  div.querySelector('.del-btn').addEventListener('click', () => div.remove());
}

async function detectMac(btn){
  const row = btn.closest('.edit-row');
  if(!row) return;
  const ipEl = row.querySelector('.f-ip');
  const macEl = row.querySelector('.f-mac');
  if(!ipEl || !macEl) return;
  const ip = ipEl.value.trim();
  if(!ip){ toast('Set the IP first, then try Detect.', 'info'); return; }
  const origText = btn.textContent;
  btn.disabled = true; btn.textContent = '...';
  try {
    const res = await apiFetch('/api/detect-mac', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ip })
    });
    const data = await res.json();
    if(!res.ok){
      toast(data.error || 'Could not detect MAC', 'error');
      return;
    }
    macEl.value = data.mac;
    // Mark as auto-detected (will be saved as mac_auto: true)
    macEl.dataset.auto = '1';
    // Add the visual tag if not already there
    const parent = macEl.parentElement;
    let tag = parent.querySelector('.mac-auto-tag');
    if(!tag){
      tag = document.createElement('span');
      tag.className = 'mac-auto-tag';
      tag.title = 'This MAC was auto-detected from the network';
      tag.textContent = 'auto';
      parent.appendChild(tag);
    }
  } catch(e){
    toast('Network error during MAC detection', 'error');
  } finally {
    btn.disabled = false; btn.textContent = origText;
  }
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
}

let _discoverPollTimer = null;

function openDiscover(){
  document.getElementById('discover-overlay').classList.add('open');
  document.getElementById('discover-status').textContent = 'Click "Scan now" to discover devices on your network.';
  document.getElementById('discover-results').style.display = 'none';
  document.getElementById('discover-list').innerHTML = '';
  document.getElementById('discover-add-btn').disabled = true;
  document.getElementById('discover-scan-btn').disabled = false;
  // Pre-check current state so we don't restart a finished scan
  refreshDiscoverState(false);
}

function closeDiscover(){
  document.getElementById('discover-overlay').classList.remove('open');
  if(_discoverPollTimer){ clearTimeout(_discoverPollTimer); _discoverPollTimer = null; }
}

async function startDiscover(){
  const btn = document.getElementById('discover-scan-btn');
  const statusEl = document.getElementById('discover-status');
  btn.disabled = true;
  statusEl.textContent = 'Starting scan...';
  document.getElementById('discover-list').innerHTML = '';
  document.getElementById('discover-results').style.display = 'none';
  try {
    const res = await apiFetch('/api/discover', { method: 'POST' });
    const data = await res.json();
    if(!res.ok){
      statusEl.textContent = 'Error: ' + (data.error || 'could not start scan');
      btn.disabled = false;
      return;
    }
    statusEl.textContent = data.message || 'Scan started. This may take a few seconds...';
    pollDiscover();
  } catch(e){
    statusEl.textContent = 'Network error starting scan.';
    btn.disabled = false;
  }
}

function pollDiscover(){
  refreshDiscoverState(true);
}

async function refreshDiscoverState(continuePolling){
  try {
    const res = await fetch('/api/discover');
    if(!res.ok) throw new Error('bad');
    const state = await res.json();
    const statusEl = document.getElementById('discover-status');
    const btn = document.getElementById('discover-scan-btn');

    if(state.running){
      statusEl.textContent = 'Scanning ' + (state.subnet || 'network') + '...';
      btn.disabled = true;
      if(continuePolling){
        _discoverPollTimer = setTimeout(pollDiscover, 1500);
      }
      return;
    }
    if(state.error){
      statusEl.textContent = 'Scan failed: ' + state.error;
      btn.disabled = false;
      return;
    }
    if(state.finished && state.results){
      const total = state.results.length;
      const newOnes = state.results.filter(r => !r.already_monitored).length;
      statusEl.textContent = 'Scan of ' + state.subnet + ' complete - ' + total + ' devices found, ' + newOnes + ' new.';
      renderDiscoverResults(state.results);
      btn.disabled = false;
      btn.textContent = 'Scan again';
      return;
    }
    // No previous scan
    statusEl.textContent = 'Click "Scan now" to discover devices on ' + (state.subnet || 'your network') + '.';
    btn.disabled = false;
  } catch(e){
    document.getElementById('discover-status').textContent = 'Could not reach netwatch.';
  }
}

function renderDiscoverResults(results){
  const wrap = document.getElementById('discover-results');
  const list = document.getElementById('discover-list');
  if(!results.length){
    wrap.style.display = 'none';
    return;
  }
  wrap.style.display = '';
  list.innerHTML = results.map(r => {
    const knownTag = r.already_monitored ? '<span class="disc-known-tag">already monitored</span>' : '';
    const hostnameDisplay = r.hostname || (r.vendor ? '(' + r.vendor + ')' : '(unknown)');
    const vendorLine = (r.hostname && r.vendor) ? '<span class="disc-vendor">' + escapeHtml(r.vendor) + '</span>' : '';
    const checkbox = r.already_monitored ? '' : '<input type="checkbox" class="disc-check" data-ip="' + escapeHtml(r.ip) + '" data-name="' + escapeHtml(r.hostname || r.vendor || ('Host ' + r.ip)) + '" data-mac="' + escapeHtml(r.mac || '') + '">';
    return '<div class="disc-row' + (r.already_monitored ? ' known' : '') + '">'
      + '<div>' + checkbox + '</div>'
      + '<div class="disc-ip">' + escapeHtml(r.ip) + '</div>'
      + '<div class="disc-name"><span class="disc-hostname">' + escapeHtml(hostnameDisplay) + knownTag + '</span>' + vendorLine + '</div>'
      + '<div class="disc-mac">' + escapeHtml(r.mac || '') + '</div>'
      + '</div>';
  }).join('');
  // Wire up checkboxes to enable/disable the Add button
  const update = () => {
    const any = list.querySelectorAll('.disc-check:checked').length > 0;
    document.getElementById('discover-add-btn').disabled = !any;
  };
  list.querySelectorAll('.disc-check').forEach(cb => cb.addEventListener('change', update));
  update();
}

async function addDiscovered(){
  const checked = document.querySelectorAll('.disc-check:checked');
  if(!checked.length) return;
  // Fetch the existing host list so we can append to it (rather than replace)
  let existing = [];
  try {
    const res = await fetch('/api/hosts');
    const data = await res.json();
    existing = data.hosts || [];
  } catch(e){
    document.getElementById('discover-status').textContent = 'Could not load existing hosts.';
    return;
  }
  // Build new entries
  const additions = [];
  checked.forEach(cb => {
    const ip = cb.dataset.ip;
    const name = cb.dataset.name || ('Host ' + ip);
    const mac = cb.dataset.mac;
    const entry = { name, ip, group: 'Discovered' };
    if(mac){ entry.specs = { mac }; }
    additions.push(entry);
  });
  const merged = existing.concat(additions);
  const res = await apiFetch('/api/hosts', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ hosts: merged })
  });
  const data = await res.json();
  if(!res.ok){
    document.getElementById('discover-status').textContent = 'Save failed: ' + (data.error || 'unknown');
    return;
  }
  document.getElementById('discover-status').textContent = 'Added ' + additions.length + ' host' + (additions.length===1?'':'s') + '. Closing...';
  setTimeout(() => {
    closeDiscover();
    closeEditor();
    refresh();
  }, 800);
}

async function saveHosts(){
  const rows = document.querySelectorAll('#edit-rows .edit-row');
  const hosts = [];
  let hasError = false;
  const seenIps = new Set();
  rows.forEach(row => {
    const nameEl = row.querySelector('.f-name');
    const ipEl = row.querySelector('.f-ip');
    const groupEl = row.querySelector('.f-group');
    const intervalEl = row.querySelector('.f-interval');
    const macEl = row.querySelector('.f-mac');
    [nameEl, ipEl, macEl].forEach(el => el && el.classList.remove('invalid'));
    const name = nameEl.value.trim();
    const ip = ipEl.value.trim();
    const group = groupEl.value.trim() || 'General';
    const intervalRaw = intervalEl.value.trim();
    if(!name && !ip) return;
    if(!name){ nameEl.classList.add('invalid'); hasError = true; }
    if(!ipValid(ip)){ ipEl.classList.add('invalid'); hasError = true; }
    if(seenIps.has(ip)){ ipEl.classList.add('invalid'); hasError = true; }
    seenIps.add(ip);
    const mac = macEl.value.trim();
    if(!macValid(mac)){ macEl.classList.add('invalid'); hasError = true; }

    const entry = { name, ip, group };
    if(intervalRaw){
      const iv = parseInt(intervalRaw);
      if(!isNaN(iv) && iv >= 5) entry.interval = iv;
    }
    const alwaysOnEl = row.querySelector('.f-alwayson');
    entry.always_on = alwaysOnEl ? alwaysOnEl.checked : true;
    const alertEl = row.querySelector('.f-alert');
    if(alertEl && !alertEl.checked) entry.alert = false;

    const specs = {};
    ['cpu','ram','storage','os','mac'].forEach(k => {
      const el = row.querySelector('.f-' + k);
      if(el && el.value.trim()) specs[k] = el.value.trim();
    });
    // Preserve mac_auto flag if the MAC field still has its auto marker
    // (macEl is the one already grabbed earlier in this function for validation)
    if(macEl && macEl.dataset.auto === '1' && macEl.value.trim()){
      specs.mac_auto = true;
    }
    if(Object.keys(specs).length) entry.specs = specs;
    const notesEl = row.querySelector('.f-notes');
    if(notesEl && notesEl.value.trim()) entry.notes = notesEl.value.trim();

    // Links
    const links = {};
    const primaryEl = row.querySelector('.f-primary-url');
    const primaryVal = primaryEl ? primaryEl.value.trim() : '';
    if(primaryVal){
      if(!/^https?:\/\//.test(primaryVal)){ primaryEl.classList.add('invalid'); hasError = true; }
      else links.primary = primaryVal;
    }
    const extras = [];
    row.querySelectorAll('.extra-link-row').forEach(extraRow => {
      const en = extraRow.querySelector('.f-extra-name').value.trim();
      const eu = extraRow.querySelector('.f-extra-url').value.trim();
      if(!en && !eu) return;
      if(!en || !eu || !/^https?:\/\//.test(eu)){
        extraRow.querySelector('.f-extra-url').classList.add('invalid');
        if(!en) extraRow.querySelector('.f-extra-name').classList.add('invalid');
        hasError = true;
        return;
      }
      extras.push({ name: en, url: eu });
    });
    if(extras.length) links.extras = extras;
    if(Object.keys(links).length) entry.links = links;

    // Services
    const services = [];
    row.querySelectorAll('.svc-row').forEach(svcRow => {
      const portEl = svcRow.querySelector('.f-svc-port');
      const nameEl = svcRow.querySelector('.f-svc-name');
      portEl.classList.remove('invalid');
      const portRaw = portEl.value.trim();
      const svcName = nameEl.value.trim();
      if(!portRaw && !svcName) return;
      const portNum = parseInt(portRaw);
      if(isNaN(portNum) || portNum < 1 || portNum > 65535){
        portEl.classList.add('invalid'); hasError = true; return;
      }
      services.push({ port: portNum, name: svcName || ('port ' + portNum) });
    });
    if(services.length) entry.services = services;
    const strictEl = row.querySelector('.f-strict');
    if(strictEl && strictEl.checked) entry.strict = true;

    hosts.push(entry);
  });
  if(hasError){ setStatus('Fix the highlighted fields and try again', 'error'); return; }
  setStatus('Saving...', '');
  try {
    const res = await apiFetch('/api/hosts', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ hosts })
    });
    const data = await res.json();
    if(!res.ok){ setStatus(data.error || 'Save failed', 'error'); return; }
    setStatus('Saved ' + hosts.length + ' host' + (hosts.length===1?'':'s') + '.', 'success');
    setTimeout(() => { closeEditor(); refresh(); }, 700);
  } catch(e) { setStatus('Network error while saving', 'error'); }
}

// ── Power card ────────────────────────────────────────────────────────────

// hosts-editor.js — extracted from auth.js (Netwatch 4.0 page split). Code moved verbatim.

async function openEditor(){
  // If auth is configured but we are not logged in, offer login first
  if(_authState.setup_required){
    openSetup();
    return;
  }
  if(!_authState.logged_in){
    openLogin(() => openEditor());
    return;
  }
  try {
    const res = await fetch('/api/hosts');
    if(res.status === 401){
      openLogin(() => openEditor());
      return;
    }
    const data = await res.json();
    const container = document.getElementById('edit-rows');
    container.innerHTML = '';
    (data.hosts || []).forEach(h => addRow(h));
    if(!data.hosts || !data.hosts.length) addRow();
    setStatus('Changes apply immediately on save', '');
    document.getElementById('modal-overlay').classList.add('open');
  } catch(e) { toast('Could not load host list.', 'error'); }
}
