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
      el.innerHTML = '<div class="ov-empty">No quick links yet</div>';
      return;
    }
    el.innerHTML = _links.map(function (l) {
      return '<a class="ov-ql-pill" href="' + escapeHtml(l.url) + '" target="_blank" rel="noopener noreferrer">'
        + '<span class="ov-ql-icon">' + escapeHtml(l.icon || '\u{1F517}') + '</span>'
        + '<span class="ov-ql-label">' + escapeHtml(l.label) + '</span></a>';
    }).join('');
  }
})();
