/* Proxmox VE panel — Servers tab */
(function () {
  'use strict';

  /* ── State ──────────────────────────────────────────────────────────────── */
  var _hostsVmidMap = {};  // proxmox_vmid (int) -> {name, is_up}

  /* ── Public API ─────────────────────────────────────────────────────────── */

  window.fetchProxmox = function (force) {
    Promise.all([
      fetch(force ? '/api/proxmox?refresh=1' : '/api/proxmox').then(function (r) { return r.json(); }),
      fetch(force ? '/api/pbs?refresh=1' : '/api/pbs').then(function (r) { return r.json(); }),
    ])
      .then(function (results) {
        var data = results[0];
        var pbs  = results[1];
        window.nwLastProxmox = data;
        window.nwLastPbs = pbs;
        _buildHostsMap(function () { _renderProxmox(data, pbs); });
      })
      .catch(function () {
        var el = document.getElementById('proxmox-content');
        if (el) el.innerHTML = '<div class="pve-unavailable">Could not reach Netwatch server.</div>';
      });
  };

  window.initServersTab = function () {
    var saved = localStorage.getItem('nw-servers-panel') || 'proxmox';
    window.switchServersPanel(saved, false);
  };

  window.switchServersPanel = function (panel, save) {
    document.querySelectorAll('.servers-pill').forEach(function (btn) {
      btn.classList.toggle('active', btn.dataset.panel === panel);
    });
    document.querySelectorAll('[data-servers-panel]').forEach(function (div) {
      div.style.display = div.dataset.serversPanel === panel ? '' : 'none';
    });
    if (save !== false) localStorage.setItem('nw-servers-panel', panel);
    if (panel === 'proxmox') window.fetchProxmox();
    if (panel === 'truenas' && typeof fetchNas === 'function') fetchNas();
  };

  /* ── Hosts map (for Netwatch link dot) ─────────────────────────────────── */

  function _buildHostsMap (cb) {
    var status = window.nwLastData;
    var hosts  = (status && status.hosts) ? status.hosts : [];
    fetch('/api/inventory')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (inv) {
        _hostsVmidMap = {};
        if (inv && inv.items) {
          inv.items.forEach(function (rec) {
            var vmid = rec.properties && rec.properties.proxmox_vmid;
            if (!vmid) return;
            vmid = parseInt(vmid, 10);
            if (isNaN(vmid)) return;
            var ip   = rec.ip;
            var host = hosts.find(function (h) { return h.ip === ip; });
            _hostsVmidMap[vmid] = {
              name:  rec.system,
              is_up: host ? !!host.is_up : null,
            };
          });
        }
        if (cb) cb();
      })
      .catch(function () { if (cb) cb(); });  // dots are non-critical
  }

  /* ── Render ─────────────────────────────────────────────────────────────── */

  function _renderProxmox (data, pbs) {
    var el = document.getElementById('proxmox-content');
    if (!el) return;
    if (data.error === 'Proxmox not configured' || (!data.reachable && !data.last_updated && !data.error)) {
      el.innerHTML = '<div class="pve-unavailable">Proxmox is not configured.'
        + ' Add credentials in <strong>Settings → Integrations</strong>.</div>';
      return;
    }
    el.innerHTML = _renderActionBar(data)
      + _renderNodeCards(data.nodes || [])
      + _renderBackupCard(pbs, data.nodes || [])
      + _renderGuestTable(data.nodes || []);
  }

  function _renderActionBar (data) {
    var ago  = data.last_updated ? _timeAgo(new Date(data.last_updated)) : 'never';
    var info = data.reachable
      ? '<span class="pve-meta">Last updated ' + ago + ' \xB7 polls every 60s</span>'
      : '<span class="pve-warn">Proxmox unreachable \xB7 last data ' + ago + '</span>';
    return '<div class="pve-action-bar">'
      + '<button class="btn pve-refresh-btn" onclick="fetchProxmox(true)">↻ Refresh now</button>'
      + info + '</div>';
  }

  /* ── Node cards ─────────────────────────────────────────────────────────── */

  function _renderNodeCards (nodes) {
    if (!nodes.length) return '';
    var cards = nodes.map(function (n) {
      var okCls    = n.status === 'online' ? 'pve-node-badge-ok' : 'pve-node-badge-err';
      var label    = n.status === 'online' ? 'ONLINE' : 'OFFLINE';
      var cpuPct   = (n.cpu_percent || 0).toFixed(1);
      var memPct   = n.mem_total_bytes
        ? Math.round(n.mem_used_bytes / n.mem_total_bytes * 100) : 0;
      var uptime   = _fmtUptime(n.uptime_seconds);
      return '<div class="pve-node-card">'
        + '<div class="pve-node-name">' + escapeHtml(n.name) + '</div>'
        + '<div class="pve-node-badge ' + okCls + '">' + label + '</div>'
        + '<div class="pve-node-sparks">'
        +   '<div class="pve-spark">'
        +     '<div class="pve-spark-hdr"><span>CPU</span><span>' + cpuPct + '%</span></div>'
        +     '<svg width="100%" height="24" viewBox="0 0 100 24" preserveAspectRatio="none"><polyline points="'
        +       nwSparkPoints(n.cpu_history || [], 100, 24)
        +     '" fill="none" stroke="var(--green)" stroke-width="1.6"/></svg>'
        +   '</div>'
        +   '<div class="pve-spark">'
        +     '<div class="pve-spark-hdr"><span>RAM</span><span>' + memPct + '%</span></div>'
        +     '<svg width="100%" height="24" viewBox="0 0 100 24" preserveAspectRatio="none"><polyline points="'
        +       nwSparkPoints(n.mem_history || [], 100, 24)
        +     '" fill="none" stroke="var(--blue)" stroke-width="1.6"/></svg>'
        +   '</div>'
        + '</div>'
        + '<div class="pve-node-uptime">Up ' + uptime + '</div>'
        + '</div>';
    }).join('');
    return '<div class="pve-node-cards">' + cards + '</div>';
  }

  /* ── Backup status card (Proxmox Backup Server) ────────────────────────── */

  function _renderBackupCard (pbs, nodes) {
    if (!pbs || pbs.error === 'PBS not configured') return '';

    var guestNames = {};
    nodes.forEach(function (n) {
      (n.guests || []).forEach(function (g) { guestNames[g.vmid] = g.name; });
    });

    var warn = pbs.reachable ? ''
      : '<div class="pbs-warn">PBS unreachable'
        + (pbs.last_updated ? ' \xB7 last data ' + _timeAgo(new Date(pbs.last_updated)) : '') + '</div>';

    var dsRows = (pbs.datastores || []).map(function (ds) {
      return '<div class="pbs-ds-row">'
        + '<span class="pbs-ds-name">' + escapeHtml(ds.name) + '</span>'
        + '<div class="pbs-bar"><div class="pbs-bar-fill" style="width:' + ds.percent + '%"></div></div>'
        + '<span class="pbs-ds-val">' + _fmtBytes(ds.used_bytes) + ' / ' + _fmtBytes(ds.total_bytes) + '</span>'
        + '</div>';
    }).join('');

    var bodyRows = (pbs.backups || []).map(function (b) {
      var name = guestNames[b.vmid] || ('VMID ' + b.vmid);
      var ago  = b.last_backup_time ? _timeAgo(new Date(b.last_backup_time)) : 'never';
      var typePill = b.type === 'vm'
        ? '<span class="pve-type-vm">VM</span>'
        : '<span class="pve-type-lxc">LXC</span>';
      return '<tr>'
        + '<td class="pve-td-mono">' + b.vmid + '</td>'
        + '<td>' + escapeHtml(name) + '</td>'
        + '<td>' + typePill + '</td>'
        + '<td>' + ago + '</td>'
        + '<td class="pve-td-num">' + (b.size_bytes ? _fmtBytes(b.size_bytes) : '—') + '</td>'
        + '<td>' + _backupBadge(b.status) + '</td>'
        + '</tr>';
    }).join('');

    return '<div class="pbs-card">'
      + '<div class="pbs-hdr" onclick="this.closest(\'.pbs-card\').classList.toggle(\'open\')">'
        + 'Backups<span class="pbs-chevron">▾</span></div>'
      + warn
      + '<div class="pbs-datastores">' + (dsRows || '<div class="pve-unavailable">No datastores found.</div>') + '</div>'
      + '<div class="pbs-body">'
        + (bodyRows
            ? '<div class="pve-table-scroll"><table class="pbs-backup-table">'
              + '<thead><tr><th>VMID</th><th>Name</th><th>Type</th><th>Last Backup</th><th>Size</th><th>Status</th></tr></thead>'
              + '<tbody>' + bodyRows + '</tbody></table></div>'
            : '<div class="pve-unavailable">No backup history found.</div>')
      + '</div>'
      + '</div>';
  }

  function _backupBadge (status) {
    var map = {
      ok:     ['pbs-badge-ok',     'OK'],
      stale:  ['pbs-badge-stale',  'Stale'],
      failed: ['pbs-badge-failed', 'Failed'],
      none:   ['pbs-badge-none',   'None'],
    };
    var pair = map[status] || ['pbs-badge-none', escapeHtml(status)];
    return '<span class="pve-badge ' + pair[0] + '">' + pair[1] + '</span>';
  }

  /* ── Guest table ─────────────────────────────────────────────────────────── */

  function _renderGuestTable (nodes) {
    var rows = [];
    nodes.forEach(function (node) {
      (node.guests || []).forEach(function (g) {
        rows.push({ nodeName: node.name, guest: g });
      });
    });
    if (!rows.length) {
      return '<div class="pve-unavailable">No guests found.</div>';
    }

    var head = '<thead><tr>'
      + '<th>Node</th><th>VMID</th><th>Name</th><th>Type</th>'
      + '<th>Status</th><th>CPU%</th><th>RAM</th>'
      + '<th class="pve-col-nw" title="Netwatch link">NW</th>'
      + '<th>Actions</th></tr></thead>';

    var body = '<tbody>' + rows.map(function (r) {
      var g       = r.guest;
      var running = g.status === 'running';
      var cpu     = running ? (g.cpu_percent || 0).toFixed(1) + '%' : '—';
      var ram     = running
        ? _fmtBytes(g.mem_used_bytes) + ' / ' + _fmtBytes(g.mem_total_bytes)
        : '—';
      return '<tr id="pve-row-' + g.vmid + '">'
        + '<td class="pve-td-mono">' + escapeHtml(r.nodeName) + '</td>'
        + '<td class="pve-td-mono">' + g.vmid + '</td>'
        + '<td>' + escapeHtml(g.name) + '</td>'
        + '<td>' + _typePill(g.type) + '</td>'
        + '<td>' + _statusBadge(g.status) + '</td>'
        + '<td class="pve-td-num">' + cpu + '</td>'
        + '<td class="pve-td-num">' + ram + '</td>'
        + '<td class="pve-td-nw">' + _nwLink(g.vmid) + '</td>'
        + '<td class="pve-td-actions">' + _actionButtons(r.nodeName, g.vmid, g.type, g.status) + '</td>'
        + '</tr>';
    }).join('') + '</tbody>';

    return '<div class="pve-table-scroll"><table class="pve-guest-table">' + head + body + '</table></div>';
  }

  function _statusBadge (status) {
    var map = {
      running: ['pve-badge-running', 'Running'],
      stopped: ['pve-badge-stopped', 'Stopped'],
      paused:  ['pve-badge-paused',  'Paused'],
    };
    var pair = map[status] || ['pve-badge-stopped', escapeHtml(status)];
    return '<span class="pve-badge ' + pair[0] + '">' + pair[1] + '</span>';
  }

  function _typePill (type) {
    return type === 'qemu'
      ? '<span class="pve-type-vm">VM</span>'
      : '<span class="pve-type-lxc">LXC</span>';
  }

  /* ── Netwatch host link dot ─────────────────────────────────────────────── */

  function _nwLink (vmid) {
    var entry = _hostsVmidMap[vmid];
    if (!entry) return '';
    var upCls = entry.is_up === true  ? 'pve-nw-up'
              : entry.is_up === false ? 'pve-nw-down'
              : 'pve-nw-unknown';
    var title = escapeHtml(entry.name)
      + (entry.is_up === true ? ' • UP' : entry.is_up === false ? ' • DOWN' : '');
    return '<span class="pve-nw-dot ' + upCls + '" title="' + title + '"'
      + ' onclick="if(typeof showTab===\'function\')setTab(\'inventory\')"'
      + ' style="cursor:pointer" tabindex="0" role="link"'
      + ' onkeydown="if(event.key===\'Enter\'&&typeof showTab===\'function\')setTab(\'inventory\')"></span>';
  }

  /* ── Action buttons ─────────────────────────────────────────────────────── */

  function _actionButtons (node, vmid, type, status) {
    var n = escapeHtml(node);
    var t = escapeHtml(type);
    if (status === 'running') {
      return '<button class="pve-btn pve-btn-stop" title="Stop"'
        + ' onclick="proxmoxAction(\'' + n + '\',' + vmid + ',\'' + t + '\',\'stop\')">&#9632;</button>'
        + '<button class="pve-btn pve-btn-reboot" title="Reboot"'
        + ' onclick="proxmoxAction(\'' + n + '\',' + vmid + ',\'' + t + '\',\'reboot\')">&#8634;</button>';
    }
    if (status === 'stopped') {
      return '<button class="pve-btn pve-btn-start" title="Start"'
        + ' onclick="proxmoxAction(\'' + n + '\',' + vmid + ',\'' + t + '\',\'start\')">&#9654;</button>';
    }
    return '';
  }

  window.proxmoxAction = function (node, vmid, type, action) {
    var row  = document.getElementById('pve-row-' + vmid);
    var btns = row ? Array.from(row.querySelectorAll('.pve-btn')) : [];
    btns.forEach(function (b) { b.disabled = true; b.classList.add('pve-btn-loading'); });

    apiFetch('/api/proxmox/action', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ node: node, vmid: vmid, type: type, action: action }),
    })
      .then(function (r) {
        return r.json().then(function (d) { return { ok: r.ok, data: d }; });
      })
      .then(function (res) {
        if (res.ok) {
          window.fetchProxmox();
        } else {
          _flashBtns(btns, 'pve-btn-err');
        }
      })
      .catch(function () { _flashBtns(btns, 'pve-btn-err'); });
  };

  function _flashBtns (btns, cls) {
    btns.forEach(function (b) {
      b.disabled = false;
      b.classList.remove('pve-btn-loading');
      b.classList.add(cls);
    });
    setTimeout(function () {
      btns.forEach(function (b) { b.classList.remove(cls); });
    }, 2000);
  }

  /* ── Utilities ──────────────────────────────────────────────────────────── */

  function _fmtBytes (bytes) {
    if (!bytes) return '0 B';
    var units = ['B', 'KB', 'MB', 'GB', 'TB'];
    var i = 0; var v = bytes;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return v.toFixed(i > 0 ? 1 : 0) + ' ' + units[i];
  }

  function _fmtUptime (s) {
    if (!s) return '0s';
    var d = Math.floor(s / 86400);
    var h = Math.floor((s % 86400) / 3600);
    var m = Math.floor((s % 3600) / 60);
    if (d > 0) return d + 'd ' + h + 'h';
    return h + 'h ' + m + 'm';
  }

  function _timeAgo (date) {
    var diff = Math.floor((Date.now() - date.getTime()) / 1000);
    if (diff < 60)   return diff + 's ago';
    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
    return Math.floor(diff / 3600) + 'h ago';
  }

})();
