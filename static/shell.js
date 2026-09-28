// shell.js — extracted from core.js (Netwatch 4.0 page split). Code moved verbatim.

// Theme: the inline <head> script resolves auto -> light|dark before first
// paint and exposes window.nwApplyTheme. setTheme() handles button wiring
// and delegates actual theme application to that head script.
function setTheme(mode){
  localStorage.setItem('nw-theme', mode);
  if(window.nwApplyTheme) window.nwApplyTheme();
  document.querySelectorAll('.theme-toggle button').forEach(b => {
    const active = b.dataset.themeBtn === mode;
    b.classList.toggle('active', active);
    b.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}

const REFRESH = 5000;

let _firstRender = true;

let lastOk = true;

let lastData = null;

function clockTick(){
  const d = new Date();
  const p = n => String(n).padStart(2,'0');
  document.getElementById('clock').textContent =
    d.getFullYear() + '-' + p(d.getMonth()+1) + '-' + p(d.getDate()) + '  ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

// ── Pages and sub-views (replaces the single-page tab bar) ────────────────
const NW_PAGE_PATH = {home:'/', monitor:'/monitor', lab:'/lab', infra:'/infra', links:'/links'};
// legacy tab name -> [page, sub-view]; keeps every existing setTab('x') call site working
const NW_TABS = {
  overview:['home',''], hosts:['monitor','hosts'], events:['monitor','events'], briefs:['monitor','briefs'],
  topology:['lab','topology'], connections:['lab','connections'], inventory:['lab','inventory'],
  servers:['infra','proxmox'], quicklinks:['links',''],
};
const _subviewHooks = {};
let _currentSubview = '';

function nwPageUrl(page, sub){
  const p = NW_PAGE_PATH[page];
  return sub ? p + '/' + sub : p;
}
function nwOnSubview(name, fn){ (_subviewHooks[name] = _subviewHooks[name] || []).push(fn); }
function nwCurrentSubview(){ return _currentSubview; }
// The context key Mira (and anything else page-aware) uses; same values the old tab names had.
function nwPageKey(){
  const p = document.body.dataset.page;
  if(p === 'home') return 'overview';
  if(p === 'infra') return 'servers';
  if(p === 'links') return 'quicklinks';
  return _currentSubview || '';
}
function _subviewNames(){
  const s = document.body.dataset.subviews;
  return s ? s.split(',') : [];
}
function _subviewFromPath(){
  const names = _subviewNames();
  // window.__nwPath lets the file:// boot smoke tests simulate a URL path
  const seg = (window.__nwPath || location.pathname).split('/')[2] || '';
  return names.includes(seg) ? seg : (names[0] || '');
}

function nwShowSubview(name, opts){
  if(!_subviewNames().includes(name)) return false;
  const same = (_currentSubview === name);   // before updating: is this a click on the active one?
  _currentSubview = name;
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + name));
  document.querySelectorAll('.subnav [data-subview]').forEach(a => {
    const on = a.dataset.subview === name;
    a.classList.toggle('active', on);
    a.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  if(opts && opts.push){
    const url = nwPageUrl(document.body.dataset.page, name);
    // Re-selecting the active sub-view must not stack a duplicate history entry; if the URL is
    // not canonical yet (bare /lab showing topology) fix it in place.
    if(location.pathname !== url){
      if(same) history.replaceState({sub: name}, '', url);
      else history.pushState({sub: name}, '', url);
    }
  }
  (_subviewHooks[name] || []).forEach(fn => { try { fn(); } catch(e){ console.error(e); } });
  // Only a real change notifies listeners; re-showing the active sub-view is not a switch.
  if(!same) window.dispatchEvent(new CustomEvent('nw:subview', {detail: {name: name}}));
  return true;
}

// Legacy entry point kept as a shim: in-page sub-views switch in place, other pages navigate.
function setTab(tab){
  if(tab === 'storage') tab = 'servers';  // renamed in v3.41
  const t = NW_TABS[tab];
  if(!t) return;
  const page = t[0], sub = t[1];
  if(page === document.body.dataset.page){
    if(sub && _subviewNames().includes(sub)) nwShowSubview(sub, {push: true});
    else if(page === 'infra' && typeof switchServersPanel === 'function') switchServersPanel(sub || 'proxmox');
    return;
  }
  location.href = nwPageUrl(page, sub === 'proxmox' ? '' : sub);
}

// ── Status store ─────────────────────────────────────────────────────────
// One poller per page. Page scripts subscribe a renderer instead of being called
// from refresh(). A throwing renderer is logged and never blocks the others, and never
// flips the connection banner to "stale" (a renderer bug is not a lost connection).
const _statusSubs = [];
const nwStatus = {
  subscribe(fn){
    _statusSubs.push(fn);
    if(lastData){ try { fn(lastData); } catch(e){ console.error(e); } }
  },
  subscribeOnce(fn){
    const wrap = d => {
      const i = _statusSubs.indexOf(wrap);
      if(i >= 0) _statusSubs.splice(i, 1);
      fn(d);
    };
    nwStatus.subscribe(wrap);
  },
  refreshNow(){ return refresh(); },
};

// Pure aggregate over a /api/status payload; the Monitor summary line and the Lab canvas
// overlay both render from it. avgLat/avgUpt are null when there is nothing to average.
function nwComputeSummary(data){
  const hosts = (data && data.hosts) || [];
  const mean = a => a.length ? a.reduce((x, y) => x + y, 0) / a.length : null;
  return {
    up: hosts.filter(h => h.is_up).length,
    total: hosts.length,
    down: hosts.filter(h => !h.is_up && h.status === 'DOWN').length,
    degraded: hosts.filter(h => h.status === 'DEGRADED').length,
    maintenance: hosts.filter(h => h.status === 'MAINTENANCE').length,
    avgLat: mean(hosts.filter(h => h.latency_ms != null).map(h => h.latency_ms)),
    avgUpt: mean(hosts.filter(h => h.always_on !== false && h.uptime_pct != null).map(h => h.uptime_pct)),
  };
}

function nwSetConnBadge(n){
  const el = document.getElementById('conn-count');
  if(!el) return;
  n = n || 0;
  el.style.display = n > 0 ? '' : 'none';
  el.textContent = n;
}

async function refresh(){
  try {
    const res = await fetch('/api/status');
    if(res.status === 401){
      if(_authState.logged_in){ _authState.logged_in = false; updateAuthUI(); openLogin(refresh); }
      else { showLanding(_authState.setup_required ? 'setup' : 'login'); }
      return;
    }
    if(!res.ok) throw new Error('bad');
    const data = await res.json();
    lastData = data;
    window.nwLastData = data;
    if(window.updateMiraStatus) window.updateMiraStatus(data);
    const down = data.hosts.filter(h => !h.is_up && h.status === 'DOWN').length;
    const fav = document.getElementById('favicon-link');
    if(fav){
      const want = down > 0 ? '/static/favicon-alert.svg' : '/static/favicon.svg';
      if(!fav.href.endsWith(want)) fav.href = want;
    }
    if(_firstRender){
      _firstRender = false;
      if(!window.matchMedia('(prefers-reduced-motion: reduce)').matches){
        document.body.classList.add('nw-anim');
        // Safe only while 900ms < REFRESH: no re-render lands mid-animation.
        setTimeout(() => document.body.classList.remove('nw-anim'), 900);
      }
    }
    _statusSubs.slice().forEach(fn => { try { fn(data); } catch(e){ console.error(e); } });
    // Its own guard: a throw in the badge code is not a lost connection, so it must not
    // fall through to the stale-banner catch below.
    try {
      if(typeof updateConnectionsBadge === 'function') updateConnectionsBadge(data.suggestions_pending);
      else nwSetConnBadge(data.suggestions_pending);
    } catch(e){ console.error(e); }
    if(!lastOk){
      document.getElementById('err-banner').style.display = 'none';
      const pipEl = document.getElementById('pip');
      pipEl.classList.remove('stale');
      pipEl.querySelector('span:last-child').textContent = 'live';
      lastOk = true;
    }
  } catch(e) {
    document.getElementById('err-banner').style.display = 'block';
    const pipEl = document.getElementById('pip');
    pipEl.classList.add('stale');
    pipEl.querySelector('span:last-child').textContent = 'stale';
    lastOk = false;
    if(window.updateMiraStatus) window.updateMiraStatus(null);
  }
}

// ── Boot ──────────────────────────────────────────────────────────────────
// Page scripts register init work with nwOnReady(); it runs after the shell's own setup.
const _readyHooks = [];
function nwOnReady(fn){ _readyHooks.push(fn); }

document.addEventListener('DOMContentLoaded', () => {
  const current = localStorage.getItem('nw-theme') || 'auto';
  document.querySelectorAll('.theme-toggle button').forEach(b => {
    b.classList.toggle('active', b.dataset.themeBtn === current);
    b.setAttribute('aria-pressed', b.dataset.themeBtn === current ? 'true' : 'false');
    b.addEventListener('click', () => setTheme(b.dataset.themeBtn));
  });

  const names = _subviewNames();
  if(names.length) nwShowSubview(_subviewFromPath(), {push: false});
  else { const v = document.querySelector('.view'); if(v) v.classList.add('active'); }
  document.addEventListener('click', e => {
    const a = e.target.closest && e.target.closest('.subnav [data-subview]');
    if(!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;
    e.preventDefault();
    nwShowSubview(a.dataset.subview, {push: true});
  });
  window.addEventListener('popstate', () => {
    if(_subviewNames().length) nwShowSubview(_subviewFromPath(), {push: false});
  });

  _readyHooks.forEach(fn => { try { fn(); } catch(e){ console.error(e); } });

  // App boot: auth gate, polling loops. Lives in the shell (not a page script) so a
  // failure in any page script can't kill the heartbeat.
  fetchAuthState();
  setInterval(fetchAuthState, 60000);
  setInterval(refresh, REFRESH);
  setInterval(clockTick, 1000);
  clockTick();

  const footerRefresh = document.getElementById('footer-refresh');
  if(footerRefresh) footerRefresh.textContent = 'refreshes every ' + (REFRESH/1000) + ' s';
});

// Escape closes the topmost open layer. Each page owns only some of these modals, so every
// close call is guarded. Order follows the z-index ladder: modals (50) > drawer (41) > AI panel (37).
document.addEventListener('keydown', e => {
  if(e.key !== 'Escape') return;
  const open = id => { const el = document.getElementById(id); return el && el.classList.contains('open'); };
  const call = name => { if(typeof window[name] === 'function') window[name](); };
  const aiUsage = document.getElementById('ai-usage-modal');
  const aiPanel = document.getElementById('ai-panel');
  if(open('discover-overlay')) call('closeDiscover');
  else if(open('import-overlay')) call('closeImportModal');
  else if(open('inv-edit-overlay')) call('closeInventoryEditor');
  else if(open('add-host-overlay')) call('closeAddHostModal');
  else if(open('ups-modal-overlay')) call('closeUpsModal');
  else if(open('modal-overlay')) call('closeEditor');
  else if(typeof openDrawerIp !== 'undefined' && openDrawerIp) call('closeDrawer');
  else if(aiUsage && !aiUsage.classList.contains('hidden')) aiUsage.classList.add('hidden');
  else if(aiPanel && !aiPanel.classList.contains('hidden')) aiPanel.classList.add('hidden');
});
