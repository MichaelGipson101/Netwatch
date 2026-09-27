// Quick add: the shared "connect two devices" control (Netwatch 4.0 plan 3).
// Used by the Connections workspace, the inventory drawer and the port map.
// The server decides which end is the child (GET /api/connections/preview);
// this component never re-implements orientation. The swap control only
// appears when the server says the pair is ambiguous.

const QA_CONNECTION_TYPES = ['ethernet', 'fiber', 'wifi', 'virtual', 'power', 'usb', 'console', 'other'];
let _qaSeq = 0;
let _qaInvCache = null;     // {at, promise}
let _qaChildPorts = null;   // promise of distinct child-port strings

// ── Pure helpers (unit-tested in node; keep brackets balanced in literals) ──

function qaMatchDevices(items, query, excludeIds, limit){
  const q = String(query || '').trim().toLowerCase();
  const skip = new Set(excludeIds || []);
  const scored = [];
  (items || []).forEach(it => {
    if(skip.has(it.id)) return;
    const name = String(it.system || '').toLowerCase();
    const hay = name + ' ' + String(it.ip || '').toLowerCase() + ' ' + String(it.device_type || '').toLowerCase();
    let score;
    if(!q) score = 2;
    else if(name.startsWith(q)) score = 0;
    else if(hay.indexOf(q) !== -1) score = 1;
    else return;
    scored.push([score, name, it]);
  });
  scored.sort((a, b) => a[0] - b[0] || (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0));
  return scored.slice(0, limit || 8).map(s => s[2]);
}

function qaPortOptions(ports){
  if(!ports) return null;
  return ports.map(p => {
    const occ = p.occupants || [];
    let label = p.name;
    if(occ.length) label += ' · ' + occ.map(o => o.name).join(', ');
    else if(p.up === true) label += ' · link up';
    return {value: p.name, label: label, taken: occ.length > 0, idx: typeof p.idx === 'number' ? p.idx : null};
  });
}

// "8" or "port 8" -> "Port 8" when the device has live port names.
function qaMatchPortOption(opts, stored){
  const s = String(stored === null || stored === undefined ? '' : stored).trim();
  if(!opts || !s) return null;
  const low = s.toLowerCase();
  for(const o of opts){
    if(String(o.value).toLowerCase() === low) return o.value;
  }
  if(/^\d+$/.test(s)){
    const n = parseInt(s, 10);
    for(const o of opts){
      if(o.idx === n) return o.value;
    }
  }
  return null;
}

function qaOrient(preview, swapped){
  if(!preview) return null;
  const o = {child_id: preview.child_id, child_name: preview.child_name,
             parent_id: preview.parent_id, parent_name: preview.parent_name};
  if(!swapped) return o;
  return {child_id: o.parent_id, child_name: o.parent_name, parent_id: o.child_id, parent_name: o.child_name};
}

function qaSentence(orient, port, type){
  if(!orient) return '';
  let s = orient.child_name + ' → ' + orient.parent_name;
  if(port) s += ' · ' + port;
  if(type) s += ' · ' + type;
  return s;
}

// ── Data ─────────────────────────────────────────────────────────────────────

function qaLoadInventory(){
  const now = Date.now();
  if(_qaInvCache && now - _qaInvCache.at < 30000) return _qaInvCache.promise;
  const entry = {at: now, promise: null};
  const promise = fetch('/api/inventory')
    .then(r => {
      if(!r.ok){ if(_qaInvCache === entry) _qaInvCache = null; return {items: []}; }
      return r.json();
    })
    .then(j => (j.items || []).slice().sort((a, b) => String(a.system).localeCompare(String(b.system))))
    .catch(() => { if(_qaInvCache === entry) _qaInvCache = null; return []; });
  entry.promise = promise;
  _qaInvCache = entry;
  return promise;
}

function qaInvalidateInventory(){ _qaInvCache = null; }

function qaLoadChildPorts(){
  if(!_qaChildPorts){
    _qaChildPorts = fetch('/api/connections')
      .then(r => r.ok ? r.json() : {items: []})
      .then(j => Array.from(new Set((j.items || []).map(c => c.child_port).filter(Boolean))).sort())
      .catch(() => []);
  }
  return _qaChildPorts;
}

// ── Component ────────────────────────────────────────────────────────────────

function qaPickerHtml(uid, which, label){
  return '<div class="qa-picker" data-which="' + which + '">'
    + '<label class="qa-field"><span>' + label + '</span>'
    + '<input type="search" class="qa-input" role="combobox" aria-autocomplete="list" aria-expanded="false"'
    + ' aria-controls="' + uid + '-' + which + '-list" placeholder="Type to search…" autocomplete="off" spellcheck="false"></label>'
    + '<ul class="qa-list" id="' + uid + '-' + which + '-list" role="listbox" hidden></ul>'
    + '</div>';
}

function renderQuickAdd(container, opts){
  if(!container) return null;
  opts = opts || {};
  const uid = 'qa' + (++_qaSeq);
  const st = {a: null, b: null, preview: null, ports: null, swapped: false,
              typeChosen: null, port: opts.parent_port || '',
              portParent: opts.parent_port && opts.b_id != null ? Number(opts.b_id) : null,
              busy: false, seq: 0};
  container.innerHTML =
    '<div class="qa' + (opts.compact ? ' qa-compact' : '') + '" id="' + uid + '">'
    + '<div class="qa-row">'
      + qaPickerHtml(uid, 'a', 'Device')
      + '<span class="qa-link" aria-hidden="true">⇄</span>'
      + qaPickerHtml(uid, 'b', 'Connect to')
    + '</div>'
    + '<div class="qa-row qa-row-opts">'
      + '<label class="qa-field qa-port-field"><span>Port</span><span class="qa-port-slot"></span></label>'
      + '<label class="qa-field"><span>Type</span><select class="qa-type">'
        + QA_CONNECTION_TYPES.map(t => '<option value="' + t + '">' + t + '</option>').join('')
      + '</select></label>'
    + '</div>'
    + '<details class="qa-more"><summary>More</summary>'
      + '<label class="qa-field"><span>This end\'s port (optional)</span>'
      + '<input type="text" class="qa-child-port" list="' + uid + '-cp" placeholder="e.g. eth0" autocomplete="off" spellcheck="false">'
      + '<datalist id="' + uid + '-cp"></datalist></label>'
    + '</details>'
    + '<div class="qa-foot">'
      + '<div class="qa-sentence" aria-live="polite"></div>'
      + '<button type="button" class="btn qa-swap" hidden title="Swap which end is upstream">⇅ Swap</button>'
      + '<button type="button" class="btn btn-primary qa-add" disabled>Add</button>'
    + '</div>'
    + '<div class="qa-error" role="alert"></div>'
    + '</div>';
  const root = document.getElementById(uid);
  const typeSel = root.querySelector('.qa-type');
  const portSlot = root.querySelector('.qa-port-slot');
  const sentenceEl = root.querySelector('.qa-sentence');
  const swapBtn = root.querySelector('.qa-swap');
  const addBtn = root.querySelector('.qa-add');
  const errEl = root.querySelector('.qa-error');
  const childPortEl = root.querySelector('.qa-child-port');

  function showError(msg){ errEl.textContent = msg || ''; }

  function currentParentId(){
    const o = qaOrient(st.preview, st.swapped);
    return o ? o.parent_id : null;
  }

  function paintSentence(){
    const o = qaOrient(st.preview, st.swapped);
    const wifi = typeSel.value === 'wifi';
    if(o) sentenceEl.textContent = qaSentence(o, wifi ? '' : st.port, typeSel.value);
    else if(st.a && st.b && st.a.id === st.b.id) sentenceEl.textContent = 'Pick two different devices.';
    else sentenceEl.textContent = 'Pick two devices; Netwatch works out which end is upstream.';
    sentenceEl.classList.toggle('qa-sentence-hint', !o);
  }

  function paint(){
    if(st.preview && !st.typeChosen) typeSel.value = st.preview.default_type || 'ethernet';
    const wifi = typeSel.value === 'wifi';
    const opts2 = qaPortOptions(st.ports);
    if(opts2){
      const cur = qaMatchPortOption(opts2, st.port);
      let extraOption = '';
      if(cur) st.port = cur;
      else if(st.port) extraOption = '<option value="' + escapeHtml(st.port) + '" selected>'
        + escapeHtml(st.port) + ' (not a port on this device)</option>';
      portSlot.innerHTML = '<select class="qa-port"' + (wifi ? ' disabled' : '') + '>'
        + '<option value="">— no port —</option>'
        + opts2.map(p => '<option value="' + escapeHtml(p.value) + '"' + (p.value === cur ? ' selected' : '') + '>'
          + escapeHtml(p.label) + (p.taken ? ' (in use)' : '') + '</option>').join('')
        + extraOption
        + '</select>';
      portSlot.querySelector('.qa-port').addEventListener('change', e => {
        st.port = e.target.value;
        st.portParent = currentParentId();
        paintSentence();
      });
    } else {
      portSlot.innerHTML = '<input type="text" class="qa-port" autocomplete="off" spellcheck="false"'
        + ' placeholder="' + (wifi ? 'n/a for wifi' : 'optional') + '"' + (wifi ? ' disabled' : '') + '>';
      const inp = portSlot.querySelector('.qa-port');
      inp.value = st.port;
      inp.addEventListener('input', e => {
        st.port = e.target.value.trim();
        st.portParent = currentParentId();
        paintSentence();
      });
    }
    swapBtn.hidden = !(st.preview && st.preview.ambiguous);
    addBtn.disabled = !st.preview || st.busy;
    addBtn.textContent = st.busy ? 'Adding…' : 'Add';
    paintSentence();
  }

  async function onPairChanged(){
    st.preview = null; st.swapped = false; st.ports = null;
    const seq = ++st.seq;
    if(!st.a || !st.b || st.a.id === st.b.id){
      showError('');
      paint();
      return;
    }
    paint();
    const typeQ = st.typeChosen ? '&type=' + encodeURIComponent(st.typeChosen) : '';
    try {
      const res = await fetch('/api/connections/preview?a=' + st.a.id + '&b=' + st.b.id + typeQ);
      const body = await res.json().catch(() => ({}));
      if(seq !== st.seq) return;
      if(!res.ok){ showError(body.error || ('Preview failed (HTTP ' + res.status + ')')); return; }
      st.preview = body;
      st.ports = body.ports;
      if(currentParentId() !== st.portParent) st.port = '';
      showError('');
      paint();
    } catch(e){ if(seq === st.seq) showError('Network error'); }
  }

  function wirePicker(which){
    const box = root.querySelector('.qa-picker[data-which="' + which + '"]');
    const input = box.querySelector('.qa-input');
    const list = box.querySelector('.qa-list');
    let matches = [];
    let active = -1;
    async function show(){
      const items = await qaLoadInventory();
      if(document.activeElement !== input) return;
      const other = which === 'a' ? st.b : st.a;
      matches = qaMatchDevices(items, input.value, other ? [other.id] : [], 8);
      active = matches.length ? 0 : -1;
      list.innerHTML = matches.length
        ? matches.map((it, i) => '<li role="option" data-i="' + i + '" aria-selected="' + (i === active) + '">'
            + deviceIcon(it.device_type || 'host', 16) + '<span class="qa-list-name">' + escapeHtml(it.system) + '</span>'
            + '<span class="qa-list-type">' + escapeHtml(it.device_type || 'host') + '</span></li>').join('')
        : '<li class="qa-list-empty">No matching device</li>';
      list.hidden = false;
      input.setAttribute('aria-expanded', 'true');
    }
    function hide(){ list.hidden = true; input.setAttribute('aria-expanded', 'false'); }
    function choose(it){
      st[which] = it;
      input.value = it.system;
      hide();
      onPairChanged();
    }
    input.addEventListener('focus', show);
    input.addEventListener('input', () => { st[which] = null; onPairChanged(); show(); });
    input.addEventListener('blur', () => setTimeout(hide, 150));
    input.addEventListener('keydown', e => {
      if(list.hidden) return;
      if(e.key === 'ArrowDown' || e.key === 'ArrowUp'){
        e.preventDefault();
        if(!matches.length) return;
        active = (active + (e.key === 'ArrowDown' ? 1 : matches.length - 1)) % matches.length;
        list.querySelectorAll('[role="option"]').forEach((li, i) => li.setAttribute('aria-selected', i === active ? 'true' : 'false'));
      } else if(e.key === 'Enter'){
        if(active >= 0){ e.preventDefault(); choose(matches[active]); }
      } else if(e.key === 'Escape'){
        hide();
      }
    });
    list.addEventListener('mousedown', e => {
      const li = e.target.closest('[role="option"]');
      if(!li) return;
      e.preventDefault();  // keep focus in the input so blur doesn't race the click
      choose(matches[Number(li.dataset.i)]);
    });
    return {
      input: input,
      set: it => { st[which] = it; input.value = it ? it.system : ''; },
      lock: () => { input.disabled = true; hide(); },
    };
  }

  const pickA = wirePicker('a');
  const pickB = wirePicker('b');

  typeSel.addEventListener('change', () => { st.typeChosen = typeSel.value; onPairChanged(); });

  swapBtn.addEventListener('click', async () => {
    if(!st.preview || !st.preview.ambiguous) return;
    st.swapped = !st.swapped;
    const o = qaOrient(st.preview, st.swapped);
    if(o.parent_id !== st.portParent) st.port = '';
    st.ports = null;
    const seq = ++st.seq;
    paint();
    try {
      const res = await fetch('/api/ports/' + o.parent_id);
      const body = res.ok ? await res.json() : null;
      if(seq !== st.seq) return;
      st.ports = body ? body.ports : null;
    } catch(e){ /* no port list: free text */ }
    paint();
  });

  root.querySelector('.qa-more').addEventListener('toggle', async e => {
    if(!e.target.open) return;
    const ports = await qaLoadChildPorts();
    root.querySelector('#' + uid + '-cp').innerHTML = ports.map(p => '<option value="' + escapeHtml(p) + '">').join('');
  });

  addBtn.addEventListener('click', async () => {
    if(!st.preview || st.busy) return;
    st.busy = true;
    paint();
    const wifi = typeSel.value === 'wifi';
    const body = {a_id: st.a.id, b_id: st.b.id, connection_type: typeSel.value,
                  parent_port: wifi ? '' : st.port, child_port: childPortEl.value.trim(),
                  swap: st.swapped};
    let added = null;
    try {
      const res = await apiFetch('/api/connections', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
      const out = await res.json().catch(() => ({}));
      if(!res.ok){ showError(out.error || ('Could not add (HTTP ' + res.status + ')')); return; }
      showError('');
      const inUse = (out.warnings || []).indexOf('port_in_use') !== -1;
      toast(inUse ? 'Connection added. Heads up: that port already had a connection.' : 'Connection added',
            inUse ? 'info' : 'success');
      _qaChildPorts = null;
      // Keep A (adding several links to one device is the common case); clear the rest.
      pickB.set(null);
      st.port = ''; st.portParent = null; st.typeChosen = null; typeSel.value = 'ethernet'; childPortEl.value = '';
      st.preview = null; st.ports = null; st.swapped = false;
      added = out;
    } catch(e){
      showError('Network error');
    } finally {
      st.busy = false;
      paint();
    }
    if(added){
      pickB.input.focus();
      if(opts.onAdded) opts.onAdded(added);
    }
  });

  paint();
  let prefillDone = null;
  if(opts.a_id || opts.b_id){
    prefillDone = qaLoadInventory().then(items => {
      const find = id => items.find(i => i.id === Number(id)) || null;
      // Don't clobber text the user already typed while the prefill was in flight.
      if(opts.a_id && !pickA.input.value) pickA.set(find(opts.a_id));
      if(opts.b_id && !pickB.input.value) pickB.set(find(opts.b_id));
      if(opts.lockA && st.a) pickA.lock();
      onPairChanged();
    });
  }
  return {focus: () => Promise.resolve(prefillDone).then(() => (st.a ? pickB : pickA).input.focus())};
}
