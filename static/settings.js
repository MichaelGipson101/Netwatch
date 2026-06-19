// ── Settings panel ──────────────────────────────────────────────────────────

let _settingsCache = {};

async function openSettings() {
  try {
    const res = await fetch('/api/settings');
    if (res.status === 401) { openLogin(() => openSettings()); return; }
    if (res.status === 403) { toast('Admin access required.', 'error'); return; }
    if (!res.ok) { toast('Could not load settings.', 'error'); return; }
    _settingsCache = await res.json();
    _fillSettingsForm(_settingsCache);
    document.getElementById('settings-error').textContent = '';
    switchSettingsTab('general');
    document.getElementById('settings-overlay').classList.add('open');
  } catch (e) { toast('Could not load settings: ' + e.message, 'error'); }
}

function closeSettings() {
  document.getElementById('settings-overlay').classList.remove('open');
}

function _settingsField(k) { return document.getElementById('s-' + k); }

function _fillSettingsForm(s) {
  const keys = [
    'default_interval', 'ping_timeout', 'history_window', 'refresh_rate', 'history_days',
    'ntfy_topic', 'ntfy_server',
    'truenas_url', 'truenas_api_key',
    'proxmox_url', 'proxmox_user', 'proxmox_password',
    'proxmox_token_id', 'proxmox_token_secret', 'proxmox_node',
    'openrouter_api_key', 'ai_model',
  ];
  for (const k of keys) {
    const el = _settingsField(k);
    if (el) el.value = (s[k] != null) ? s[k] : '';
  }
}

function _collectSettingsForm() {
  const intKeys = ['default_interval', 'ping_timeout', 'history_window', 'refresh_rate', 'history_days'];
  const strKeys = [
    'ntfy_topic', 'ntfy_server',
    'truenas_url', 'truenas_api_key',
    'proxmox_url', 'proxmox_user', 'proxmox_password',
    'proxmox_token_id', 'proxmox_token_secret', 'proxmox_node',
    'openrouter_api_key', 'ai_model',
  ];
  const out = {};
  for (const k of intKeys) {
    const el = _settingsField(k);
    if (el) out[k] = el.value.trim() === '' ? '' : Number(el.value);
  }
  for (const k of strKeys) {
    const el = _settingsField(k);
    if (el) out[k] = el.value.trim();
  }
  return out;
}

async function saveSettings() {
  const data = _collectSettingsForm();
  const errEl = document.getElementById('settings-error');
  errEl.textContent = '';
  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    const json = await res.json();
    if (!res.ok) { errEl.textContent = json.error || 'Save failed'; return; }
    _settingsCache = json.settings || _settingsCache;
    toast('Settings saved.', 'success');
    closeSettings();
  } catch (e) { errEl.textContent = 'Network error'; }
}

function switchSettingsTab(tab) {
  document.querySelectorAll('.stab-content').forEach(el => el.style.display = 'none');
  document.querySelectorAll('.stab-btn').forEach(el => el.classList.remove('active'));
  const content = document.getElementById('stab-' + tab);
  if (content) content.style.display = '';
  const btn = document.querySelector('.stab-btn[data-tab="' + tab + '"]');
  if (btn) btn.classList.add('active');
  if (tab === 'backups') { loadBackupStatus(); loadInventoryBackupStatus(); }
}

// ── Backup status ────────────────────────────────────────────────────────────

const BACKUP_STALE_SECONDS = 8 * 86400; // weekly job; flag if older
const INVENTORY_BACKUP_STALE_SECONDS = 2 * 86400; // daily job; flag if older

function _fmtBytes(bytes) {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = bytes;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return v.toFixed(i > 0 ? 1 : 0) + ' ' + units[i];
}

function _timeAgo(epochSeconds) {
  const diff = Math.floor(Date.now() / 1000 - epochSeconds);
  if (diff < 60) return diff + 's ago';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  return Math.floor(diff / 86400) + 'd ago';
}

async function loadBackupStatus() {
  await _loadBackupStatusInto('backup-status-card', '/api/backup-status', BACKUP_STALE_SECONDS);
}

async function loadInventoryBackupStatus() {
  await _loadBackupStatusInto('inventory-backup-status-card', '/api/inventory-backup-status', INVENTORY_BACKUP_STALE_SECONDS);
}

async function _loadBackupStatusInto(elId, endpoint, staleSeconds) {
  const card = document.getElementById(elId);
  if (!card) return;
  card.textContent = 'Loading…';
  try {
    const res = await fetch(endpoint);
    if (!res.ok) { card.textContent = 'Could not load backup status.'; return; }
    const data = await res.json();
    card.innerHTML = _renderBackupStatus(data, staleSeconds);
  } catch (e) {
    card.textContent = 'Network error loading backup status.';
  }
}

function _renderBackupStatus(data, staleSeconds) {
  if (!data.configured) {
    return '<div class="backup-status-row backup-status-none">No automated NAS backup detected.</div>';
  }
  const age = _timeAgo(data.checked_at);
  const stale = (Date.now() / 1000 - data.checked_at) > staleSeconds;
  if (!data.ok) {
    return '<div class="backup-status-row backup-status-error">✗ Last backup run FAILED — ' + age + '</div>'
      + (data.error ? '<div class="backup-status-detail">' + escapeHtml(data.error) + '</div>' : '');
  }
  const sizeStr = _fmtBytes(data.size_bytes);
  const fileCount = data.files ? Object.keys(data.files).length : 0;
  let html = '<div class="backup-status-row ' + (stale ? 'backup-status-warn' : 'backup-status-ok') + '">'
    + (stale ? '⚠ Last successful backup is overdue — ' : '✓ Last backup succeeded — ') + age + '</div>'
    + '<div class="backup-status-detail">' + escapeHtml(data.filename || '') + ' · ' + sizeStr
    + (data.files ? ' · ' + fileCount + ' file(s)' : '') + '</div>';
  return html;
}

function toggleFieldVis(inputId) {
  const el = document.getElementById(inputId);
  if (!el) return;
  el.type = el.type === 'password' ? 'text' : 'password';
  const btn = el.closest('.field-with-toggle');
  if (btn) btn.querySelector('.toggle-vis-btn').textContent = el.type === 'password' ? 'Show' : 'Hide';
}

// ── Config wizard ────────────────────────────────────────────────────────────

let _wizardStep = 1;
const _WIZARD_STEPS = 4;
let _wizardData = {};

const _WIZARD_TITLES  = ['Monitoring', 'Push Alerts', 'Integrations', 'AI Assistant'];
const _WIZARD_INTKEYS = { 1: ['default_interval','ping_timeout','history_window','refresh_rate','history_days'] };
const _WIZARD_STRKEYS = {
  2: ['ntfy_topic','ntfy_server'],
  3: ['truenas_url','truenas_api_key','proxmox_url','proxmox_user','proxmox_password','proxmox_token_id','proxmox_token_secret','proxmox_node'],
  4: ['openrouter_api_key','ai_model'],
};

async function openWizard() {
  closeSettings();
  try {
    const res = await fetch('/api/settings');
    if (!res.ok) { toast('Could not load settings.', 'error'); return; }
    _settingsCache = await res.json();
  } catch (e) { toast('Network error', 'error'); return; }
  _wizardStep = 1;
  _wizardData = {};
  _wizardFillStep(1);
  _wizardRender();
  document.getElementById('wizard-overlay').classList.add('open');
}

function closeWizard() {
  document.getElementById('wizard-overlay').classList.remove('open');
}

function _wizardRender() {
  for (let i = 1; i <= _WIZARD_STEPS; i++) {
    const el = document.getElementById('wstep-' + i);
    if (el) el.style.display = i === _wizardStep ? '' : 'none';
  }

  document.getElementById('wizard-title').textContent = 'Setup — ' + _WIZARD_TITLES[_wizardStep - 1];

  const ind = document.getElementById('wizard-step-indicator');
  if (ind) {
    ind.innerHTML = Array.from({ length: _WIZARD_STEPS }, (_, i) => {
      const cls = (i + 1 === _wizardStep) ? 'active' : (i + 1 < _wizardStep) ? 'done' : '';
      return '<span class="wstep-dot ' + cls + '"></span>';
    }).join('');
  }

  document.getElementById('wizard-back-btn').style.display = _wizardStep > 1 ? '' : 'none';
  document.getElementById('wizard-next-btn').textContent = _wizardStep === _WIZARD_STEPS ? 'Finish' : 'Next';
  document.getElementById('wizard-skip-btn').style.display = _wizardStep === 1 ? 'none' : '';
  document.getElementById('wizard-error').textContent = '';
}

function _wizardFillStep(step) {
  const src = Object.assign({}, _settingsCache, _wizardData);
  for (const k of (_WIZARD_INTKEYS[step] || [])) {
    const el = document.getElementById('w-' + k);
    if (el && src[k] != null) el.value = src[k];
  }
  for (const k of (_WIZARD_STRKEYS[step] || [])) {
    const el = document.getElementById('w-' + k);
    if (el) el.value = (src[k] != null) ? src[k] : '';
  }
}

function _wizardCollectStep(step) {
  const out = {};
  for (const k of (_WIZARD_INTKEYS[step] || [])) {
    const el = document.getElementById('w-' + k);
    if (el) out[k] = el.value.trim() === '' ? '' : Number(el.value);
  }
  for (const k of (_WIZARD_STRKEYS[step] || [])) {
    const el = document.getElementById('w-' + k);
    if (el) out[k] = el.value.trim();
  }
  return out;
}

async function wizardNext() {
  Object.assign(_wizardData, _wizardCollectStep(_wizardStep));
  if (_wizardStep === _WIZARD_STEPS) { await _wizardFinish(); return; }
  _wizardStep++;
  _wizardFillStep(_wizardStep);
  _wizardRender();
}

function wizardBack() {
  if (_wizardStep > 1) { _wizardStep--; _wizardRender(); }
}

function wizardSkip() {
  if (_wizardStep === _WIZARD_STEPS) { _wizardFinish(); return; }
  _wizardStep++;
  _wizardFillStep(_wizardStep);
  _wizardRender();
}

async function _wizardFinish() {
  _wizardData.setup_wizard_complete = true;
  const errEl = document.getElementById('wizard-error');
  errEl.textContent = '';
  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(_wizardData),
    });
    const json = await res.json();
    if (!res.ok) { errEl.textContent = json.error || 'Save failed'; return; }
    _settingsCache = json.settings || _settingsCache;
    toast('Setup complete!', 'success');
    closeWizard();
  } catch (e) { errEl.textContent = 'Network error'; }
}

// Called after first admin account creation to auto-open wizard if unconfigured.
async function checkAndOpenWizard() {
  try {
    const res = await fetch('/api/settings');
    if (!res.ok) return;
    const s = await res.json();
    if (!s.setup_wizard_complete) openWizard();
  } catch (e) { /* ignore */ }
}

document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  if (document.getElementById('wizard-overlay').classList.contains('open')) closeWizard();
  else if (document.getElementById('settings-overlay').classList.contains('open')) closeSettings();
});
