/* Proxmox VE panel — Servers tab */
(function () {
  'use strict';

  /* ── State ──────────────────────────────────────────────────────────────── */
  var _hostsVmidMap = {};  // proxmox_vmid (int) -> {name, is_up}

  /* ── Public API ─────────────────────────────────────────────────────────── */

  window.fetchProxmox = function () {
    fetch('/api/proxmox')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        _buildHostsMap(function () { _renderProxmox(data); });
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

  function _renderProxmox (data) {
    var el = document.getElementById('proxmox-content');
    if (!el) return;
    if (!data.reachable && data.error === 'Proxmox not configured') {
      el.innerHTML = '<div class="pve-unavailable">Proxmox is not configured.'
        + ' Add credentials in <strong>Settings → Integrations</strong>.</div>';
      return;
    }
    el.innerHTML = _renderActionBar(data)
      + _renderNodeCards(data.nodes || [])
      + _renderGuestTable(data.nodes || []);
  }

  function _renderActionBar (data) {
    var ago  = data.last_updated ? _timeAgo(new Date(data.last_updated)) : 'never';
    var info = data.reachable
      ? '<span class="pve-meta">Last updated ' + ago + ' \xB7 polls every 60s</span>'
      : '<span class="pve-warn">Proxmox unreachable \xB7 last data ' + ago + '</span>';
    return '<div class="pve-action-bar">'
      + '<button class="btn pve-refresh-btn" onclick="fetchProxmox()">↻ Refresh now</button>'
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
      var memUsed  = _fmtBytes(n.mem_used_bytes);
      var memTotal = _fmtBytes(n.mem_total_bytes);
      var uptime   = _fmtUptime(n.uptime_seconds);
      return '<div class="pve-node-card">'
        + '<div class="pve-node-name">' + escapeHtml(n.name) + '</div>'
        + '<div class="pve-node-badge ' + okCls + '">' + label + '</div>'
        + '<div class="pve-node-stat"><span class="pve-stat-lbl">CPU</span>'
        +   '<div class="pve-bar"><div class="pve-bar-fill" style="width:' + cpuPct + '%"></div></div>'
        +   '<span class="pve-stat-val">' + cpuPct + '%</span></div>'
        + '<div class="pve-node-stat"><span class="pve-stat-lbl">RAM</span>'
        +   '<div class="pve-bar"><div class="pve-bar-fill" style="width:' + memPct + '%"></div></div>'
        +   '<span class="pve-stat-val">' + memUsed + ' / ' + memTotal + '</span></div>'
        + '<div class="pve-node-uptime">Up ' + uptime + '</div>'
        + '</div>';
    }).join('');
    return '<div class="pve-node-cards">' + cards + '</div>';
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

    return '<table class="pve-guest-table">' + head + body + '</table>';
  }

  function _statusBadge (status) {
    var map = {
      running: ['pve-badge-running', 'Running'],
      stopped: ['pve-badge-stopped', 'Stopped'],
      paused:  ['pve-badge-paused',  'Paused'],
    };
    var pair = map[status] || ['pve-badge-stopped', status];
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

    fetch('/api/proxmox/action', {
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
