// ups.js — extracted from core.js (Netwatch 4.0 page split). Code moved verbatim.

// Pure (no-DOM) fill-color decision for the nav-bar battery icon, split out
// of refreshUpsIcon() so it's unit-testable on its own. Unreachable takes
// priority over the OL/OB/LB status-flag colors - a lost connection to upsd
// must never render as a healthy green battery with stale numbers. Flag
// membership is checked via split(' ').includes(...) (token membership),
// matching the backend's _parse_status_flags set-membership approach rather
// than a raw substring .includes() check on the whole status string.
function _upsFillClass(live) {
  if (!live.reachable) return 'ups-nav-icon-fill-unknown';
  const flags = live.status ? live.status.split(' ') : [];
  if (flags.includes('LB')) return 'ups-nav-icon-fill-crit';
  if (flags.includes('OB')) return 'ups-nav-icon-fill-warn';
  return 'ups-nav-icon-fill-ok';
}

async function refreshUpsIcon() {
  try {
    const res = await fetch('/api/ups');
    if (!res.ok) return;
    const data = await res.json();
    window.nwLastUps = data;
    const icon = document.getElementById('ups-nav-icon');
    if (!icon) return;
    if (!data.configured) { icon.style.display = 'none'; return; }
    icon.style.display = '';
    const live = data.live || {};
    const fill = document.getElementById('ups-nav-icon-fill');
    if (!fill) return;
    const pct = (live.charge_percent != null) ? live.charge_percent : 0;
    const maxWidth = 16; // matches the outline rect's interior width in the SVG above
    fill.setAttribute('width', Math.max(0, Math.min(maxWidth, maxWidth * pct / 100)));
    fill.setAttribute('class', _upsFillClass(live));
  } catch (e) { /* transient fetch failure - next tick retries, matches refreshPowerCard's silence */ }
}

const _UPS_STATUS_LABELS = {
  OL: 'On mains power', OB: 'Running on battery', LB: 'Low battery',
  CHRG: 'Charging', DISCHRG: 'Discharging', RB: 'Replace battery',
};

function _upsStatusLabel(status) {
  if (!status) return 'Unknown';
  const flags = status.split(' ');
  const labels = flags.map(f => _UPS_STATUS_LABELS[f] || f);
  return labels.join(', ');
}

function openUpsModal() {
  const data = window.nwLastUps || {};
  const live = data.live || {};
  const unreachableEl = document.getElementById('ups-modal-unreachable');
  if (unreachableEl) {
    if (!live.reachable) {
      unreachableEl.style.display = '';
      unreachableEl.textContent = live.error
        ? 'Lost connection to UPS (' + live.error + ') - showing last known data below.'
        : 'Lost connection to UPS - showing last known data below.';
    } else {
      unreachableEl.style.display = 'none';
    }
  }
  document.getElementById('ups-modal-status').textContent = _upsStatusLabel(live.status);
  const rawEl = document.getElementById('ups-modal-status-raw');
  if (rawEl) rawEl.textContent = live.status || '';
  document.getElementById('ups-modal-charge').textContent =
    (live.charge_percent != null) ? live.charge_percent.toFixed(0) + '%' : '-';
  document.getElementById('ups-modal-load').textContent =
    (live.load_percent != null) ? live.load_percent.toFixed(0) + '%' : '-';
  document.getElementById('ups-modal-runtime').textContent =
    (live.runtime_seconds != null) ? Math.round(live.runtime_seconds / 60) + 'm' : '-';
  document.getElementById('ups-modal-input-voltage').textContent =
    (live.input_voltage != null) ? live.input_voltage.toFixed(1) + ' V' : '-';
  document.getElementById('ups-modal-battery-voltage').textContent =
    (live.battery_voltage != null) ? live.battery_voltage.toFixed(1) + ' V' : '-';
  if (live.last_updated) {
    const ago = Math.round((Date.now() - new Date(live.last_updated).getTime()) / 60000);
    document.getElementById('ups-modal-updated').textContent = ago < 2 ? 'just now' : ago + 'm ago';
  } else {
    document.getElementById('ups-modal-updated').textContent = '-';
  }
  document.getElementById('ups-modal-overlay').classList.add('open');
}

function closeUpsModal() {
  document.getElementById('ups-modal-overlay').classList.remove('open');
}
