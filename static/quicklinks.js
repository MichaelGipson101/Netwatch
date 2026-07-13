/* Quick Links — Overview card. Fetches /api/quicklinks on mount and renders
   pill buttons; admin edit-modal wiring lives in the second half of this
   file (openQuickLinksEditor and friends), added once the modal markup
   exists in dashboard.html. */
(function () {
  'use strict';

  var _links = [];

  window.mountQuickLinksCard = function () {
    fetch('/api/quicklinks').then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) { _links = d.links || []; _renderPills(); } })
      .catch(function () {});
  };

  function _renderPills () {
    var el = document.getElementById('ov-ql-pills');
    if (!el) return;
    if (!_links.length) {
      var isAdmin = (typeof _authState !== 'undefined' && _authState.logged_in && _authState.admin);
      el.innerHTML = isAdmin
        ? '<div class="ov-empty">No quick links yet — click ✎ above to add your first one.</div>'
        : '<div class="ov-empty">No quick links yet</div>';
      return;
    }
    el.innerHTML = _links.map(function (l) {
      return '<a class="ov-ql-pill" href="' + escapeHtml(l.url) + '" target="_blank" rel="noopener noreferrer">'
        + '<span class="ov-ql-icon">' + escapeHtml(l.icon || '\u{1F517}') + '</span>'
        + '<span class="ov-ql-label">' + escapeHtml(l.label) + '</span></a>';
    }).join('');
  }

  // ── Admin edit modal ──────────────────────────────────────────────────
  window.openQuickLinksEditor = function () {
    document.getElementById('ql-edit-overlay').classList.add('open');
    _renderEditRows();
  };

  window.closeQuickLinksEditor = function () {
    document.getElementById('ql-edit-overlay').classList.remove('open');
  };

  function _renderEditRows () {
    var el = document.getElementById('ql-edit-rows');
    if (!el) return;
    el.innerHTML = _links.map(function (l) {
      return '<div class="ql-edit-row" data-id="' + l.id + '">'
        + '<input type="text" class="ql-row-icon" value="' + escapeHtml(l.icon || '') + '" maxlength="8"'
        + ' onblur="saveQuickLinkField(' + l.id + ',\'icon\',this.value)">'
        + '<input type="text" class="ql-row-label" value="' + escapeHtml(l.label) + '"'
        + ' onblur="saveQuickLinkField(' + l.id + ',\'label\',this.value)">'
        + '<input type="text" class="ql-row-url" value="' + escapeHtml(l.url) + '"'
        + ' onblur="saveQuickLinkField(' + l.id + ',\'url\',this.value)">'
        + '<button type="button" class="ql-row-btn" onclick="moveQuickLink(' + l.id + ',\'up\')" aria-label="Move up">▲</button>'
        + '<button type="button" class="ql-row-btn" onclick="moveQuickLink(' + l.id + ',\'down\')" aria-label="Move down">▼</button>'
        + '<button type="button" class="ql-row-btn ql-row-del" onclick="deleteQuickLink(' + l.id + ')" aria-label="Delete">×</button>'
        + '</div>';
    }).join('') || '<div class="ov-empty">No quick links yet — add one below.</div>';
  }

  window.saveQuickLinkField = function (id, field, value) {
    var body = {};
    body[field] = value;
    apiFetch('/api/quicklinks/' + id, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
    }).then(function (r) { return r.json().then(function (j) { return {ok: r.ok, body: j}; }); })
      .then(function (res) {
        var err = document.getElementById('ql-edit-error');
        if (!res.ok) { if (err) err.textContent = res.body.error || 'Save failed'; return; }
        if (err) err.textContent = '';
        return _refreshAndRender();
      }).catch(function () {});
  };

  window.moveQuickLink = function (id, direction) {
    apiFetch('/api/quicklinks/' + id + '/move', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({direction: direction})
    }).then(function () { return _refreshAndRender(); }).catch(function () {});
  };

  window.deleteQuickLink = function (id) {
    if (!confirm('Delete this quick link?')) return;
    apiFetch('/api/quicklinks/' + id + '/delete', { method: 'POST' })
      .then(function () { return _refreshAndRender(); }).catch(function () {});
  };

  window.addQuickLink = function () {
    var icon = document.querySelector('.ql-add-icon').value.trim();
    var label = document.querySelector('.ql-add-label').value.trim();
    var url = document.querySelector('.ql-add-url').value.trim();
    var err = document.getElementById('ql-edit-error');
    apiFetch('/api/quicklinks', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({icon: icon, label: label, url: url})
    }).then(function (r) { return r.json().then(function (j) { return {ok: r.ok, body: j}; }); })
      .then(function (res) {
        if (!res.ok) { if (err) err.textContent = res.body.error || 'Add failed'; return; }
        if (err) err.textContent = '';
        document.querySelector('.ql-add-icon').value = '';
        document.querySelector('.ql-add-label').value = '';
        document.querySelector('.ql-add-url').value = '';
        return _refreshAndRender();
      }).catch(function () {});
  };

  function _refreshAndRender () {
    return fetch('/api/quicklinks').then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        _links = d.links || [];
        _renderPills();
        _renderEditRows();
      });
  }
})();
