// ── Auth state ──
let _authState = { logged_in: false, username: null, admin: false, setup_required: false, csrf_token: null };

async function fetchAuthState(){
  try {
    const res = await fetch('/api/auth/status');
    if(res.ok){
      const wasLoggedIn = _authState.logged_in;
      _authState = await res.json();
      updateAuthUI();
      if(_authState.logged_in){
        hideLanding();
        if(!wasLoggedIn) refresh();
      } else if(_authState.setup_required){
        showLanding('setup');
      } else {
        showLanding('login');
      }
    }
  } catch(e){ /* ignore */ }
}

function updateAuthUI(){
  const navAuth = document.getElementById('nav-auth');
  if(!navAuth) return;
  if(_authState.logged_in){
    const label = escapeHtml(_authState.username) + (_authState.admin ? ' <span style="opacity:.5">(admin)</span>' : '');
    const backupItem = _authState.admin
      ? '<button class="user-dropdown-item" onclick="toggleUserMenu();openSettings()">Settings</button>'
      + '<button class="user-dropdown-item" onclick="toggleUserMenu();downloadBackup()">Download backup</button>'
      : '';
    navAuth.innerHTML =
      '<div class="user-menu" id="user-menu">'
      + '<button class="user-menu-trigger" onclick="toggleUserMenu()" aria-haspopup="true" aria-expanded="false" id="user-menu-trigger">'
      + label
      + '<span class="user-menu-chevron">▾</span>'
      + '</button>'
      + '<div class="user-dropdown" id="user-dropdown">'
      + backupItem
      + '<button class="user-dropdown-item" onclick="toggleUserMenu();logout()">Log out</button>'
      + '</div>'
      + '</div>';
  } else {
    navAuth.innerHTML = '<button class="btn" onclick="openLogin()" style="font-size:12px">Log in</button>';
  }
}

let _userMenuOutsideClick = null;

function toggleUserMenu(){
  const dropdown = document.getElementById('user-dropdown');
  const trigger  = document.getElementById('user-menu-trigger');
  if(!dropdown) return;
  const opening = !dropdown.classList.contains('open');
  if(_userMenuOutsideClick){
    document.removeEventListener('click', _userMenuOutsideClick, true);
    _userMenuOutsideClick = null;
  }
  dropdown.classList.toggle('open', opening);
  if(trigger) trigger.setAttribute('aria-expanded', opening ? 'true' : 'false');
  if(opening){
    _userMenuOutsideClick = function(e){
      const menu = document.getElementById('user-menu');
      if(menu && !menu.contains(e.target)){
        dropdown.classList.remove('open');
        if(trigger) trigger.setAttribute('aria-expanded','false');
        document.removeEventListener('click', _userMenuOutsideClick, true);
        _userMenuOutsideClick = null;
      }
    };
    document.addEventListener('click', _userMenuOutsideClick, true);
  }
}

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

// ── Landing page ──────────────────────────────────────────────────────────
function showLanding(mode){
  const lp = document.getElementById('landing-page');
  const alreadyVisible = !lp.classList.contains('hidden');
  lp.classList.remove('hidden');
  if(alreadyVisible) return;
  const loginForm = document.getElementById('landing-login-form');
  const setupForm = document.getElementById('landing-setup-form');
  if(mode === 'setup'){
    loginForm.style.display = 'none';
    setupForm.style.display = '';
    document.getElementById('landing-setup-error').textContent = '';
    setTimeout(() => document.getElementById('landing-setup-username').focus(), 50);
  } else {
    loginForm.style.display = '';
    setupForm.style.display = 'none';
    document.getElementById('landing-error').textContent = '';
    document.getElementById('landing-username').value = '';
    document.getElementById('landing-password').value = '';
    setTimeout(() => document.getElementById('landing-username').focus(), 50);
  }
}
function hideLanding(){
  document.getElementById('landing-page').classList.add('hidden');
}
async function submitLandingLogin(ev){
  if(ev) ev.preventDefault();
  const username = document.getElementById('landing-username').value.trim();
  const password = document.getElementById('landing-password').value;
  const err = document.getElementById('landing-error');
  err.textContent = '';
  if(!username || !password){ err.textContent = 'Username and password required'; return; }
  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ username, password })
    });
    const data = await res.json();
    if(!res.ok){ err.textContent = data.error || 'Login failed'; return; }
    _authState = { logged_in: true, username: data.username, admin: data.admin, setup_required: false, csrf_token: data.csrf_token };
    updateAuthUI();
    hideLanding();
    refresh();
  } catch(e){ err.textContent = 'Network error'; }
}
async function submitLandingSetup(ev){
  if(ev) ev.preventDefault();
  const username = document.getElementById('landing-setup-username').value.trim();
  const password = document.getElementById('landing-setup-password').value;
  const password2 = document.getElementById('landing-setup-password2').value;
  const err = document.getElementById('landing-setup-error');
  err.textContent = '';
  if(password !== password2){ err.textContent = 'Passwords do not match'; return; }
  if(password.length < 8){ err.textContent = 'Password must be at least 8 characters'; return; }
  try {
    const res = await fetch('/api/auth/setup', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ username, password })
    });
    const data = await res.json();
    if(!res.ok){ err.textContent = data.error || 'Setup failed'; return; }
    _authState = { logged_in: true, username: data.username, admin: true, setup_required: false, csrf_token: data.csrf_token };
    updateAuthUI();
    hideLanding();
    refresh();
    if(typeof checkAndOpenWizard === 'function') checkAndOpenWizard();
  } catch(e){ err.textContent = 'Network error'; }
}

let _afterLogin = null;

function openLogin(thenCallback){
  _afterLogin = thenCallback || null;
  document.getElementById('login-overlay').classList.add('open');
  document.getElementById('login-error').textContent = '';
  document.getElementById('login-username').value = '';
  document.getElementById('login-password').value = '';
  setTimeout(() => document.getElementById('login-username').focus(), 50);
}
function closeLogin(){
  document.getElementById('login-overlay').classList.remove('open');
}
async function submitLogin(ev){
  if(ev) ev.preventDefault();
  const username = document.getElementById('login-username').value.trim();
  const password = document.getElementById('login-password').value;
  const err = document.getElementById('login-error');
  err.textContent = '';
  if(!username || !password){ err.textContent = 'Username and password required'; return; }
  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ username, password })
    });
    const data = await res.json();
    if(!res.ok){ err.textContent = data.error || 'Login failed'; return; }
    _authState = { logged_in: true, username: data.username, admin: data.admin, setup_required: false, csrf_token: data.csrf_token };
    updateAuthUI();
    closeLogin();
    if(_afterLogin){ _afterLogin(); _afterLogin = null; }
  } catch(e){ err.textContent = 'Network error'; }
}
async function logout(){
  await fetch('/api/auth/logout', { method: 'POST' });
  _authState = { logged_in: false, username: null, admin: false, setup_required: false };
  updateAuthUI();
  showLanding('login');
}

async function downloadBackup(){
  // Trigger the backup endpoint and let the browser handle the download.
  // We use fetch instead of a plain link so we can show progress + errors.
  const btn = event && event.target;
  const origText = btn ? btn.textContent : null;
  if(btn){ btn.disabled = true; btn.textContent = 'Building...'; }
  try {
    const res = await fetch('/api/backup', { method: 'POST' });
    if(!res.ok){
      let msg = 'Backup failed (HTTP ' + res.status + ')';
      try { const j = await res.json(); if(j.error) msg = j.error; } catch(e){}
      toast(msg, 'error');
      return;
    }
    const blob = await res.blob();
    // Get filename from Content-Disposition header, or fall back
    const dispo = res.headers.get('Content-Disposition') || '';
    const m = dispo.match(/filename="?([^";]+)"?/);
    const filename = m ? m[1] : 'netwatch-backup.tar.gz';
    // Trigger a download
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch(e){
    toast('Backup failed: ' + e.message, 'error');
  } finally {
    if(btn){ btn.disabled = false; btn.textContent = origText || 'Download backup'; }
  }
}

function openSetup(){
  document.getElementById('setup-overlay').classList.add('open');
  document.getElementById('setup-error').textContent = '';
  setTimeout(() => document.getElementById('setup-username').focus(), 50);
}
function closeSetup(){
  document.getElementById('setup-overlay').classList.remove('open');
}
async function submitSetup(ev){
  if(ev) ev.preventDefault();
  const username = document.getElementById('setup-username').value.trim();
  const password = document.getElementById('setup-password').value;
  const password2 = document.getElementById('setup-password2').value;
  const err = document.getElementById('setup-error');
  err.textContent = '';
  if(password !== password2){ err.textContent = 'Passwords do not match'; return; }
  if(password.length < 8){ err.textContent = 'Password must be at least 8 characters'; return; }
  try {
    const res = await fetch('/api/auth/setup', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ username, password })
    });
    const data = await res.json();
    if(!res.ok){ err.textContent = data.error || 'Setup failed'; return; }
    _authState = { logged_in: true, username: data.username, admin: true, setup_required: false, csrf_token: data.csrf_token };
    updateAuthUI();
    closeSetup();
    if(typeof checkAndOpenWizard === 'function') checkAndOpenWizard();
  } catch(e){ err.textContent = 'Network error'; }
}

document.addEventListener('keydown', e => {
  if(e.key === 'Escape'){
    if(document.getElementById('login-overlay').classList.contains('open')) closeLogin();
    else if(document.getElementById('setup-overlay').classList.contains('open')) closeSetup();
  }
});
