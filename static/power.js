// power.js — extracted from core.js (Netwatch 4.0 page split). Code moved verbatim.

// Home's power tile (overview.js _renderPower) reads window.nwLastPower; the standalone
// power card (and its D3 sparkline) no longer exists on any page.
async function refreshPowerCard() {
  try {
    const res = await fetch('/api/power');
    if (!res.ok) return;
    window.nwLastPower = await res.json();
  } catch (_) { /* non-critical */ }
}

nwStatus.subscribe(function(){ refreshPowerCard(); });
