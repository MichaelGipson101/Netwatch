// briefs.js — extracted from core.js (Netwatch 4.0 page split). Code moved verbatim.

let _briefsFetched = false;

function fetchBriefs() {
  if (_briefsFetched) return;
  fetch('/api/brief', {credentials: 'same-origin'})
    .then(r => r.ok ? r.json() : Promise.reject(r.status))
    .then(d => { _briefsFetched = true; renderBriefs(d.briefs || []); })
    .catch(() => {});
}

function renderBriefs(briefs) {
  const empty  = document.getElementById('briefs-empty');
  const list   = document.getElementById('briefs-list');
  const badge  = document.getElementById('briefs-count');

  const dayAgo = Date.now() / 1000 - 86400;
  const recent = briefs.filter(b => b.created_ts > dayAgo).length;
  badge.textContent = recent;
  badge.style.display = recent > 0 ? '' : 'none';

  if (!briefs.length) {
    empty.style.display = '';
    list.style.display  = 'none';
    return;
  }
  empty.style.display = 'none';
  list.style.display  = '';

  const dateKey    = ts => new Date(ts * 1000).toDateString();
  const dateLabel  = ts => {
    const d = new Date(ts * 1000), now = new Date();
    if (d.toDateString() === now.toDateString()) return 'Today';
    const y = new Date(now); y.setDate(now.getDate() - 1);
    if (d.toDateString() === y.toDateString()) return 'Yesterday';
    return d.toLocaleDateString(undefined, {weekday:'long', month:'short', day:'numeric'});
  };
  const timeLabel  = ts => new Date(ts * 1000).toLocaleTimeString(undefined, {hour:'numeric', minute:'2-digit'});
  const briefLabel = ts => { const h = new Date(ts * 1000).getHours(); return h < 12 ? 'Morning Brief' : h < 17 ? 'Afternoon Brief' : 'Evening Brief'; };

  // Group by calendar date (briefs already sorted newest-first)
  const groups = [];
  let curKey = null;
  for (const b of briefs) {
    const k = dateKey(b.created_ts);
    if (k !== curKey) { groups.push({label: dateLabel(b.created_ts), items: []}); curKey = k; }
    groups[groups.length - 1].items.push(b);
  }

  let firstCard = true;
  const html = groups.map(g => {
    const cards = g.items.map(b => {
      const s      = b.stats || {};
      const dn     = s.down  ?? 0;
      const idle   = s.idle  ?? 0;
      const up     = s.up    ?? 0;
      const accent = dn > 0 ? 'var(--red)' : idle > 0 ? 'var(--amber)' : 'var(--green)';
      const label  = briefLabel(b.created_ts);
      const paragraphs = (b.narrative || '').split(/\n\n+/)
        .filter(p => p.trim())
        .map(p => `<p class="brief-para">${escapeHtml(p.trim())}</p>`)
        .join('');
      const dnStyle   = dn   > 0 ? ' style="color:var(--red-text)"'   : '';
      const idleStyle = idle > 0 ? ' style="color:var(--amber-text)"' : '';
      const open = firstCard;
      firstCard  = false;
      return `<div class="brief-card${open ? ' open' : ''}" style="--brief-accent:${accent}">
        <div class="brief-header">
          <div class="brief-header-left">
            <span class="brief-label">${escapeHtml(label)}</span>
            <span class="brief-time">${escapeHtml(timeLabel(b.created_ts))}</span>
          </div>
          <span class="brief-chevron">▾</span>
        </div>
        <div class="brief-body">
          ${paragraphs}
          <div class="brief-footer">
            <span style="color:var(--green-text)">${up} up</span>
            <span${dnStyle}>${dn} dn</span>
            <span${idleStyle}>${idle} idle</span>
          </div>
        </div>
      </div>`;
    }).join('');
    return `<div class="briefs-date-label">${escapeHtml(g.label)}</div>${cards}`;
  }).join('');

  list.innerHTML = html;
  list.querySelectorAll('.brief-header').forEach(hdr => {
    hdr.addEventListener('click', () => hdr.closest('.brief-card').classList.toggle('open'));
  });
}
