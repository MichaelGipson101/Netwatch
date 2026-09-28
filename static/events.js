// events.js — extracted from core.js (Netwatch 4.0 page split). Code moved verbatim.

function renderEvents(data){
  const events = data.events || [];
  const ongoing = events.filter(e => e.ongoing).length;
  const countBadge = document.getElementById('events-count');
  if(ongoing > 0){
    countBadge.textContent = ongoing;
    countBadge.style.display = '';
  } else {
    countBadge.style.display = 'none';
  }
  const empty = document.getElementById('events-empty');
  const list = document.getElementById('events-list');
  if(!events.length){
    empty.style.display = '';
    list.style.display = 'none';
    return;
  }
  empty.style.display = 'none';
  list.style.display = '';
  const dayLabel = ts => {
    const d = new Date(ts * 1000), now = new Date();
    const sameDay = (a,b) => a.toDateString() === b.toDateString();
    if(sameDay(d, now)) return 'Today';
    const y = new Date(now); y.setDate(now.getDate() - 1);
    if(sameDay(d, y)) return 'Yesterday';
    return d.toLocaleDateString(undefined, {month:'short', day:'numeric'});
  };
  let lastDay = null, html = '';
  events.forEach(e => {
    const day = e.started_ts ? dayLabel(e.started_ts) : '';
    if(day && day !== lastDay){
      html += '<div class="events-day-hdr">' + day + '</div>';
      lastDay = day;
    }
    const cls = e.ongoing ? 'ongoing' : 'resolved';
    const badgeCls = e.ongoing ? 'badge-dn' : 'badge-up';
    const badgeTxt = e.ongoing ? 'ONGOING' : 'RESOLVED';
    const dur = durationStr(e.duration_seconds || 0);
    const timeLabel = e.started_ts
      ? new Date(e.started_ts * 1000).toLocaleTimeString(undefined, {hourCycle:'h23'})
      : (e.started_str || '');
    html += '<div class="event ' + cls + '" data-ip="' + escapeHtml(e.host_ip) + '" tabindex="0" role="button" onclick="openDrawer(this.dataset.ip)">'
      + '<div class="event-bar"></div>'
      + '<div class="event-host">' + escapeHtml(e.host_name) + ' <span class="ip">' + escapeHtml(e.host_ip) + '</span></div>'
      + '<div class="event-time">' + escapeHtml(timeLabel) + '</div>'
      + '<div class="event-dur">' + dur + '</div>'
      + '<div class="event-status"><span class="badge ' + badgeCls + '">' + badgeTxt + '</span></div>'
      + '</div>';
  });
  list.innerHTML = html;
}

nwStatus.subscribe(renderEvents);
