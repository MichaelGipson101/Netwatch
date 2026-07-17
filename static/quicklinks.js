/* Quick Links — Overview count summary, dedicated page card grid, and admin
   edit modal. Fetches /api/quicklinks on mount; edit mutations patch the
   local _links array and re-render in place (no refetch, no full edit-row
   rebuild on field save — rebuilding those rows would blow away focus and
   in-progress keystrokes in a sibling field on the same onblur). */
(function () {
  'use strict';

  var _links = [];

  window.mountQuickLinksCard = function () {
    fetch('/api/quicklinks').then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) { _links = d.links || []; _renderCount(); } })
      .catch(function () {});
  };

  function _renderCount () {
    var el = document.getElementById('ov-ql-count');
    if (!el) return;
    el.textContent = String(_links.length);
  }

  window.mountQuickLinksPage = function () {
    fetch('/api/quicklinks').then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) { _links = d.links || []; _renderCount(); _renderCards(); } })
      .catch(function () {});
  };

  function _renderCards () {
    var el = document.getElementById('ql-page-grid');
    if (!el) return;
    if (!_links.length) {
      var isAdmin = (typeof _authState !== 'undefined' && _authState.logged_in && _authState.admin);
      el.innerHTML = isAdmin
        ? '<div class="ov-empty">No quick links yet — click "Edit links" above to add your first one.</div>'
        : '<div class="ov-empty">No quick links yet</div>';
      return;
    }
    el.innerHTML = _links.map(function (l) {
      return '<a class="ql-card" href="' + escapeHtml(l.url) + '" target="_blank" rel="noopener noreferrer">'
        + '<span class="ql-card-icon">' + escapeHtml(l.icon || '\u{1F517}') + '</span>'
        + '<span class="ql-card-label">' + escapeHtml(l.label) + '</span>'
        + '<span class="ql-card-domain">' + escapeHtml(_domainOf(l.url)) + '</span></a>';
    }).join('');
  }

  function _domainOf (url) {
    try { return new URL(url).hostname; }
    catch (e) { return url; }
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
        + '<div class="ql-edit-row-fields">'
        + '<input type="text" class="ql-row-label" value="' + escapeHtml(l.label) + '"'
        + ' onblur="saveQuickLinkField(' + l.id + ',\'label\',this.value)">'
        + '<input type="text" class="ql-row-url" value="' + escapeHtml(l.url) + '"'
        + ' onblur="saveQuickLinkField(' + l.id + ',\'url\',this.value)">'
        + '</div>'
        + '<div class="ql-row-actions">'
        + '<button type="button" class="ql-row-btn" onclick="moveQuickLink(' + l.id + ',\'up\')" aria-label="Move up">▲</button>'
        + '<button type="button" class="ql-row-btn" onclick="moveQuickLink(' + l.id + ',\'down\')" aria-label="Move down">▼</button>'
        + '<button type="button" class="ql-row-btn ql-row-del" onclick="deleteQuickLink(' + l.id + ')" aria-label="Delete">×</button>'
        + '</div></div>';
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
        var link = _links.filter(function (l) { return l.id === id; })[0];
        if (link) link[field] = value;
        // Don't rebuild #ql-edit-rows here: it's still focused (this just
        // fired from its own onblur) and a sibling field may be mid-edit.
        _renderCards();
      }).catch(function () {});
  };

  window.moveQuickLink = function (id, direction) {
    apiFetch('/api/quicklinks/' + id + '/move', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({direction: direction})
    }).then(function (r) { return r.ok ? r.json() : null; })
      .then(function (res) {
        if (!res || !res.ok) return;
        var idx = _links.findIndex(function (l) { return l.id === id; });
        if (idx === -1) return;
        var otherIdx = direction === 'up' ? idx - 1 : idx + 1;
        if (otherIdx < 0 || otherIdx >= _links.length) return; // matches server no-op at either end
        var tmp = _links[idx];
        _links[idx] = _links[otherIdx];
        _links[otherIdx] = tmp;
        _renderCards();
        _renderEditRows();
      }).catch(function () {});
  };

  window.deleteQuickLink = function (id) {
    if (!confirm('Delete this quick link?')) return;
    apiFetch('/api/quicklinks/' + id + '/delete', { method: 'POST' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (res) {
        if (!res || !res.ok) return;
        _links = _links.filter(function (l) { return l.id !== id; });
        _renderCount();
        _renderCards();
        _renderEditRows();
      }).catch(function () {});
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
        _links.push({id: res.body.id, label: label, url: url, icon: icon});
        _renderCount();
        _renderCards();
        _renderEditRows();
      }).catch(function () {});
  };
})();
