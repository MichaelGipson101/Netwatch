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

// shell.js — extracted from core.js (Netwatch 4.0 page split). Code moved verbatim.

function setTab(tab){
  document.querySelectorAll('.tab').forEach(t => {
    t.classList.toggle('active', t.dataset.tab === tab);
    t.setAttribute('aria-selected', t.dataset.tab === tab ? 'true' : 'false');
  });
  // Web-overlay metrics only apply when topology tab is active in web mode
  document.body.classList.toggle('nw-topo-web',
    tab === 'topology' && _topoView === 'web');
  // Overview hides the summary row + tab bar for a clean landing screen;
  // its hamburger icon (toggleOverviewMenu) brings the tab bar back.
  document.body.classList.toggle('nw-overview', tab === 'overview');
  if(tab !== 'overview') document.body.classList.remove('nw-overview-menu-open');
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + tab));
  localStorage.setItem('nw-tab', tab);
  if(tab === 'overview'  && typeof initOverviewTab === 'function') initOverviewTab();
  if(tab === 'inventory' && typeof fetchInventory === 'function') fetchInventory();
  if(tab === 'servers'   && typeof initServersTab === 'function') initServersTab();
  if(tab === 'briefs') fetchBriefs();
  if(tab === 'quicklinks' && typeof mountQuickLinksPage === 'function') mountQuickLinksPage();
  if(tab === 'connections' && typeof mountConnectionsTab === 'function') mountConnectionsTab();
  // Re-fetch topology when switching to the tab (but only after D3 has loaded
  // at least once — initial load is handled by setTopoView on page boot).
  if(tab === 'topology' && _topoD3Loaded && typeof fetchAndRenderTopologyWeb === 'function') fetchAndRenderTopologyWeb();
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
    if(typeof updateConnectionsBadge === 'function') updateConnectionsBadge(data.suggestions_pending);
    else nwSetConnBadge(data.suggestions_pending);
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

  // Legacy single-page tab mode (replaced by pages + sub-views in Task 6)
  let initialTab = localStorage.getItem('nw-tab') || 'overview';
  if (initialTab === 'storage') initialTab = 'servers';  // renamed in v3.41
  setTab(initialTab);
  document.querySelectorAll('.tab').forEach(t => {
    t.addEventListener('click', () => setTab(t.dataset.tab));
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
