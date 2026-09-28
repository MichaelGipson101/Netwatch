document.addEventListener('DOMContentLoaded', () => {
  const current = localStorage.getItem('nw-theme') || 'auto';
  document.querySelectorAll('.theme-toggle button').forEach(b => {
    b.classList.toggle('active', b.dataset.themeBtn === current);
    b.setAttribute('aria-pressed', b.dataset.themeBtn === current ? 'true' : 'false');
    b.addEventListener('click', () => setTheme(b.dataset.themeBtn));
  });

  let initialTab = localStorage.getItem('nw-tab') || 'overview';
  if (initialTab === 'storage') initialTab = 'servers';  // renamed in v3.41
  setTab(initialTab);
  // Restore Cards/Web view preference for the topology tab
  if(typeof setTopoView === 'function') setTopoView(_topoView);
  document.querySelectorAll('.tab').forEach(t => {
    t.addEventListener('click', () => setTab(t.dataset.tab));
  });

  const compactSaved = localStorage.getItem('nw-compact') === 'true';
  document.getElementById('compact-mode').checked = compactSaved;
  document.body.classList.toggle('compact', compactSaved);
  document.getElementById('compact-mode').addEventListener('change', e => {
    document.body.classList.toggle('compact', e.target.checked);
    localStorage.setItem('nw-compact', e.target.checked);
  });

  // App boot: auth gate, polling loops. Lives here (not inventory.js) so a
  // failure in any later-loaded file can't kill the heartbeat.
  fetchAuthState();
  setInterval(fetchAuthState, 60000);
  setInterval(refresh, REFRESH);
  setInterval(clockTick, 1000);
  clockTick();

  const footerRefresh = document.getElementById('footer-refresh');
  if(footerRefresh) footerRefresh.textContent = 'refreshes every ' + (REFRESH/1000) + ' s';
});

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
    if(window.updateMiraStatus) window.updateMiraStatus(data);
    window.nwLastData = data;
    if(document.getElementById('view-overview').classList.contains('active')
       && typeof renderOverviewLive === 'function') renderOverviewLive(data);
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
    renderSummary(data);
    renderTopology(data);
    updateTopologyWebStatus(data);
    renderGroups(data);
    if(_hostStatusChip !== 'all' || (document.getElementById('hosts-filter') && document.getElementById('hosts-filter').value)){
      applyHostFilter();
    }
    renderEvents(data);
    if(typeof updateConnectionsBadge === 'function') updateConnectionsBadge(data.suggestions_pending);
    if(openDrawerIp){
      const h = data.hosts.find(x => x.ip === openDrawerIp);
      if(h) renderDrawer(h, data);
    }
    // Note: pi-health auto-refresh happens inside renderDrawer when h.is_pi
    refreshPowerCard();
    refreshUpsIcon();
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

document.addEventListener('keydown', e => {
  if(e.key !== 'Escape') return;
  const open = id => { const el = document.getElementById(id); return el && el.classList.contains('open'); };
  const aiUsage = document.getElementById('ai-usage-modal');
  const aiPanel = document.getElementById('ai-panel');
  // Order follows the z-index ladder: modals (50) > drawer (41) > AI panel (37)
  if(open('discover-overlay')) closeDiscover();
  else if(open('import-overlay')) closeImportModal();
  else if(open('inv-edit-overlay')) closeInventoryEditor();
  else if(open('add-host-overlay')) closeAddHostModal();
  else if(open('ups-modal-overlay')) closeUpsModal();
  else if(open('modal-overlay')) closeEditor();
  else if(openDrawerIp) closeDrawer();
  else if(aiUsage && !aiUsage.classList.contains('hidden')) aiUsage.classList.add('hidden');
  else if(aiPanel && !aiPanel.classList.contains('hidden')) aiPanel.classList.add('hidden');
});

// ── Editor ──
