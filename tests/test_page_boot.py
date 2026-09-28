import re

import pytest

from boot_fixtures import default_fixtures
from boot_smoke import needs_chromium, render
from test_pages import PAGES
from netwatch.pages import PAGES as PAGE_TABLE

THEMES = ["dark", "light"]


def _cases():
    """(page, sub-view) for every page; a page without sub-views yields one case with ''."""
    for p in PAGES:
        subs = [s for s, _ in PAGE_TABLE[p.name].subviews] or [""]
        for s in subs:
            yield pytest.param(p, s, id=f"{p.name}/{s}" if s else p.name)


def _pathname(page, sub):
    return PAGE_TABLE[page.name].path + ("/" + sub if sub else "")


@needs_chromium
@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("page,sub", list(_cases()))
def test_page_boots_without_errors(page, sub, theme, tmp_path):
    r = render(page.html, default_fixtures(tmp_path), theme=theme, pathname=_pathname(page, sub))
    assert r.errors == [], f"{page.name}/{sub} ({theme}) boot errors: {r.errors}"


@needs_chromium
@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("page,sub", list(_cases()))
@pytest.mark.parametrize("width", [320, 390])
def test_page_has_no_horizontal_overflow(page, sub, theme, width, tmp_path):
    r = render(page.html, default_fixtures(tmp_path), width=width, height=800, theme=theme,
               pathname=_pathname(page, sub))
    assert r.inner_width == width, (
        f"{page.name}/{sub} ({theme}): viewport was {r.inner_width}px, not {width}px; "
        "the overflow check would be vacuous")
    assert r.overflow <= 0, f"{page.name}/{sub} ({theme}) overflows by {r.overflow}px at {width}px"


# ---------------------------------------------------------------------------
# Hardening: hot-spot regressions (unauthenticated, fresh install, deep links)
# ---------------------------------------------------------------------------

def _page(name):
    return next(p for p in PAGES if p.name == name)


@needs_chromium
@pytest.mark.parametrize("name", list(PAGE_TABLE))
def test_logged_out_visit_shows_landing_without_errors(name, tmp_path):
    """Every page, unauthenticated: the login landing is visible and nothing throws."""
    fx = default_fixtures(tmp_path, logged_in=False)
    fx["__status401"] = True
    r = render(_page(name).html, fx, pathname=PAGE_TABLE[name].path)
    assert r.errors == [], f"{name}: {r.errors}"
    assert re.search(r'<div id="landing-page"(?![^>]*\bhidden\b)', r.dom), f"{name}: landing page is hidden for a logged-out visitor"
    assert re.search(r'id="landing-login-form"(?![^>]*display:\s*none)', r.dom), f"{name}: login form not shown"


@needs_chromium
@pytest.mark.parametrize("name", list(PAGE_TABLE))
def test_logged_in_visit_keeps_the_landing_hidden(name, tmp_path):
    """Control for the logged-out test: the landing starts hidden in the static HTML (no login
    flash on page navigation) and stays hidden for a logged-in user. The username in the nav
    proves auth.js actually ran against the logged-in fixture."""
    r = render(_page(name).html, default_fixtures(tmp_path), pathname=PAGE_TABLE[name].path)
    assert r.errors == [], f"{name}: {r.errors}"
    assert re.search(r'<div id="landing-page"[^>]*\bhidden\b', r.dom), name
    assert "admin" in r.dom[r.dom.index('id="nav-auth"'):], f"{name}: auth.js did not render the user"


@pytest.mark.parametrize("name", list(PAGE_TABLE))
def test_landing_is_hidden_in_the_served_html(name):
    """Every page load is a fresh document, so the landing must not be visible before
    auth.js has answered; otherwise a logged-in user sees a login flash on every navigation."""
    assert re.search(r'<div id="landing-page"[^>]*\bclass="hidden"', _page(name).html), name


@needs_chromium
@pytest.mark.parametrize("name", list(PAGE_TABLE))
def test_fresh_install_payloads_do_not_throw(name, tmp_path):
    """No hosts, no events, nothing configured."""
    fx = default_fixtures(tmp_path)
    fx["/api/status"]["hosts"] = []
    fx["/api/status"]["events"] = []
    fx["/api/status"]["suggestions_pending"] = 0
    fx["/api/status"]["summary"] = {"total": 0, "up": 0, "down": 0, "idle": 0, "pending": 0}
    r = render(_page(name).html, fx, pathname=PAGE_TABLE[name].path)
    assert r.errors == [], f"{name}: {r.errors}"


@needs_chromium
def test_unknown_host_deep_link_opens_no_drawer_and_no_error(tmp_path):
    r = render(_page("monitor").html, default_fixtures(tmp_path), url_path="host=9.9.9.9",
               pathname="/monitor/hosts")
    assert r.errors == []
    assert 'class="drawer open"' not in r.dom


@needs_chromium
def test_known_host_deep_link_opens_the_drawer(tmp_path):
    r = render(_page("monitor").html, default_fixtures(tmp_path), url_path="host=10.0.0.2",
               pathname="/monitor/hosts")
    assert r.errors == []
    assert 'class="drawer open"' in r.dom


@needs_chromium
def test_hostile_host_param_is_not_rendered_as_markup(tmp_path):
    """An injected element would serialize as <img src="x" onerror="alert(1)"> (attributes
    quoted), and its failed load of `x` would land in r.errors as a 'resource failed'
    entry, so check both the DOM (by regex on the real serialization) and the error log."""
    r = render(_page("monitor").html, default_fixtures(tmp_path),
               url_path="host=%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E", pathname="/monitor/hosts")
    assert r.errors == []
    assert not re.search(r"<img\b[^>]*\bonerror\b", r.dom, re.I)
    assert not re.search(r'<img\b[^>]*\bsrc="?x"?[\s>]', r.dom, re.I)


# ---------------------------------------------------------------------------
# Content renders: a page that boots "cleanly" but renders blank must fail CI.
# The fixtures (boot_fixtures.status_payload) hold 4 hosts in 3 groups, 2 down,
# one ongoing incident (jellyfin) and 2 pending suggestions. Assertions target
# JS-produced markup, not the fixture JSON that the stub also embeds in the DOM.
# ---------------------------------------------------------------------------

def _boot(name, sub, tmp_path, **kw):
    r = render(_page(name).html, default_fixtures(tmp_path),
               pathname=PAGE_TABLE[name].path + ("/" + sub if sub else ""), **kw)
    assert r.errors == [], f"{name}/{sub}: {r.errors}"
    return r.dom


@needs_chromium
def test_monitor_hosts_renders_groups_and_fixture_hosts(tmp_path):
    dom = _boot("monitor", "hosts", tmp_path)
    assert '<div class="view active" id="view-hosts">' in dom
    for group in ("Homelab", "Virtual Machines", "Computers"):
        assert f'<div class="group-label">{group}' in dom, group   # label extras (counts) may follow
    for name, ip in (("pve", "10.0.0.2"), ("nas", "10.0.0.3"), ("jellyfin", "10.0.0.4"),
                     ("laptop", "10.0.0.5")):
        assert f'<span>{name}</span></div><div class="host-ip-sub">{ip}</div>' in dom, name
    assert "2/4 up" in dom  # the toolbar summary line, computed client-side by nwComputeSummary


@needs_chromium
def test_monitor_events_renders_the_ongoing_incident(tmp_path):
    dom = _boot("monitor", "events", tmp_path)
    assert '<div class="view active" id="view-events">' in dom
    assert '<div class="event ongoing" data-ip="10.0.0.4"' in dom
    assert 'class="badge badge-dn">ONGOING</span>' in dom


@needs_chromium
def test_home_renders_the_hosts_up_count_and_the_down_host(tmp_path):
    dom = _boot("home", "", tmp_path)
    assert 'id="ov-hosts-num">2<span class="ov-num-dim">/4</span>' in dom
    assert '<span class="ov-row-name">jellyfin</span>' in dom


@needs_chromium
def test_lab_topology_renders_its_shell_and_problem_banner(tmp_path):
    dom = _boot("lab", "topology", tmp_path)
    assert '<div class="view active" id="view-topology">' in dom
    assert '<div class="problem-pill" data-ip="10.0.0.4"' in dom


@needs_chromium
def test_lab_connections_renders_its_panels(tmp_path):
    dom = _boot("lab", "connections", tmp_path)
    assert '<div class="view active" id="view-connections">' in dom
    assert "No discovery source is set up yet" in dom      # renderCxStatus
    assert 'id="cx-quick"><div class="qa"' in dom          # mountConnectionsTab built the quick-add form


@needs_chromium
def test_lab_inventory_renders_its_metrics(tmp_path):
    dom = _boot("lab", "inventory", tmp_path)
    assert '<div class="view active" id="view-inventory">' in dom
    assert '<div class="inv-metric-label">Total devices</div>' in dom


@needs_chromium
@pytest.mark.parametrize("panel,marker", [
    ("proxmox", 'id="proxmox-content"><div class="pve-unavailable">Proxmox is not configured'),
    ("truenas", 'id="nas-content"><div class="nas-unavailable">'),
])
def test_infra_renders_the_panel_named_by_the_url(panel, marker, tmp_path):
    dom = _boot("infra", panel, tmp_path)
    assert marker in dom


@needs_chromium
def test_links_renders_its_cards_from_the_quicklinks_fixture(tmp_path):
    dom = _boot("links", "", tmp_path)
    grid = dom[dom.index('id="ql-page-grid"'):]
    for label, domain in (("Proxmox VE", "pve.lan"), ("Grafana", "grafana.lan")):
        assert f'<span class="ql-card-label">{label}</span>' in grid, label
        assert f'<span class="ql-card-domain">{domain}</span>' in grid, domain
