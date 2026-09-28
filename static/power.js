// power.js — extracted from core.js (Netwatch 4.0 page split). Code moved verbatim.

function renderPowerSparkline(history) {
  const el = document.getElementById('power-sparkline');
  if (!el) return;
  el.innerHTML = '';
  const valid = history.filter(d => d.watts !== null);
  if (!valid.length) return;
  const W = el.clientWidth || 300, H = 40;
  const m = {t: 4, r: 4, b: 4, l: 4};
  const w = W - m.l - m.r, h = H - m.t - m.b;
  const x = d3.scaleTime()
    .domain(d3.extent(valid, d => new Date(d.timestamp * 1000)))
    .range([0, w]);
  const maxW = d3.max(valid, d => d.watts);
  const y = d3.scaleLinear()
    .domain([0, maxW ? maxW * 1.15 : 1])
    .range([h, 0]);
  const line = d3.line()
    .x(d => x(new Date(d.timestamp * 1000)))
    .y(d => y(d.watts))
    .curve(d3.curveMonotoneX);
  const area = d3.area()
    .x(d => x(new Date(d.timestamp * 1000)))
    .y0(h).y1(d => y(d.watts))
    .curve(d3.curveMonotoneX);
  const svg = d3.select(el).append('svg')
    .attr('width', W).attr('height', H);
  const g = svg.append('g').attr('transform', `translate(${m.l},${m.t})`);
  g.append('path').datum(valid)
    .attr('fill', 'var(--blue-bg)').attr('d', area);
  g.append('path').datum(valid)
    .attr('fill', 'none')
    .attr('stroke', 'var(--blue)')
    .attr('stroke-width', 1.5)
    .attr('d', line);

  // Tooltip
  const tooltip = d3.select(el).append('div')
    .style('position', 'absolute').style('pointer-events', 'none')
    .style('background', 'var(--surface)').style('border', '1px solid var(--border)')
    .style('border-radius', '5px').style('padding', '4px 8px')
    .style('font-size', '11px').style('color', 'var(--text)')
    .style('display', 'none');
  svg.on('mousemove', function(event) {
    const [mx] = d3.pointer(event);
    const date = x.invert(mx - m.l);
    const bisect = d3.bisector(d => new Date(d.timestamp * 1000)).left;
    const i = bisect(valid, date, 1);
    const d = valid[Math.min(i, valid.length - 1)];
    if (!d) return;
    tooltip.style('display', 'block')
      .style('left', (mx + 8) + 'px').style('top', '4px')
      .text(d.watts.toFixed(1) + ' W');
  }).on('mouseleave', () => tooltip.style('display', 'none'));
  el.style.position = 'relative';
}

async function refreshPowerCard() {
  try {
    const res = await fetch('/api/power');
    if (!res.ok) return;
    const data = await res.json();
    window.nwLastPower = data;
    const card = document.getElementById('power-card');
    if (!card) return;
    if (!data.configured) { card.style.display = 'none'; return; }
    card.style.display = '';
    const live = data.live || {};
    const dot = document.getElementById('power-dot');
    if (dot) {
      dot.className = 'power-dot ' + (live.reachable ? 'power-dot-ok' : 'power-dot-err');
    }
    const wattsEl = document.getElementById('power-watts');
    if (wattsEl) wattsEl.textContent = live.watts !== null && live.watts !== undefined
      ? live.watts.toFixed(1) + ' W' : '-';
    const volEl = document.getElementById('power-voltage');
    if (volEl) volEl.textContent = live.voltage !== null && live.voltage !== undefined
      ? live.voltage.toFixed(1) + ' V' : '';
    const curEl = document.getElementById('power-current');
    if (curEl) curEl.textContent = live.current_a !== null && live.current_a !== undefined
      ? live.current_a.toFixed(2) + ' A' : '';
    const engEl = document.getElementById('power-energy');
    if (engEl) engEl.textContent = live.energy_kwh !== null && live.energy_kwh !== undefined
      ? live.energy_kwh.toFixed(3) + ' kWh today' : '';
    const updEl = document.getElementById('power-updated');
    if (updEl && live.last_updated) {
      const ago = Math.round((Date.now() / 1000 - live.last_updated) / 60);
      updEl.textContent = ago < 2 ? 'just now' : ago + 'm ago';
    }
    // D3 is lazy-loaded by the topology web view, which may never initialize
    // (e.g. saved cards view) — load it on demand so the sparkline still draws.
    if (typeof d3 === 'undefined') await ensureD3();
    renderPowerSparkline(data.history || []);
  } catch (_) { /* non-critical */ }
}

nwStatus.subscribe(function(){ refreshPowerCard(); });
