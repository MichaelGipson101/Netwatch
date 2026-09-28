import re

import pytest

from boot_fixtures import default_fixtures, home_fixtures
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


def _home(tmp_path, fixtures=None, **kw):
    return render(_page("home").html, fixtures or home_fixtures(tmp_path), pathname="/", **kw)


@needs_chromium
def test_home_renders_every_section_with_real_content(tmp_path):
    r = _home(tmp_path)
    assert r.errors == []
    dom = r.dom
    # Every assertion targets markup that only the JS produces (attribute/tag context): the stub
    # <script> embeds each fixture as JSON, so bare text from a fixture would match a blank section.
    # verdict: the server's headline, LED reflects the level, stats line built from the poll
    assert re.search(r'id="hm-headline"[^>]*>2 problems need attention\. jellyfin is down\.', dom)
    assert 'class="hm-led hm-led-down"' in dom
    assert re.search(r'id="hm-stats">2/4 up[^<]*178 W', dom)
    # needs attention: three rows, badge, deep link, dismiss only on the poller item (admin fixture)
    assert dom.count('<div class="hm-arow">') == 3
    assert "<b>jellyfin is down</b>" in dom and 'class="hm-badge hm-badge-dn">2 affected</span>' in dom
    assert 'class="hm-go" href="/monitor/hosts?host=10.0.0.4">Open' in dom
    assert 'class="hm-go" href="/infra/truenas">Open' in dom
    assert 'class="hm-dismiss" data-dismiss="alert:pool_health_tank"' in dom and dom.count("data-dismiss=") == 1
    assert re.search(r'id="hm-dismissed"[^>]*>1 dismissed', dom) and 'id="hm-restore"' in dom
    assert not re.search(r'id="hm-explain"[^>]*hidden', dom)            # problems exist -> Explain shown
    # hosts: group labels, status-classed tiles, heartbeat strips, problem and idle hosts listed by name
    assert re.search(r'id="hm-hosts-sum">2 of 4 up', dom)
    assert "<span>Homelab</span><em>2/2</em>" in dom and "<span>Virtual Machines</span><em>0/1</em>" in dom
    assert 'class="hm-h3 topo-status-down"' in dom and 'class="hm-hb"' in dom and "linear-gradient(90deg" in dom
    assert 'hm-nu-name">jellyfin</span><span class="hm-nu-meta">down' in dom
    assert 'hm-nu-name">laptop</span><span class="hm-nu-meta">idle' in dom
    assert 'hm-nu-name">pve' not in dom
    # columns
    assert 'class="hm-row-name">pve CPU</span><span class="hm-row-meta">12%</span>' in dom
    assert 'class="hm-row-name">tank pool</span><span class="hm-row-meta">61% used</span>' in dom
    assert 'class="hm-row-name">UPS</span><span class="hm-row-meta">on line · 100%</span>' in dom
    assert not re.search(r'id="hm-servers"[^>]*hidden', dom)
    assert 'id="hm-power-avg">7-day avg 178 W' in dom and 'id="hm-power-big">178<small>W</small>' in dom
    assert re.search(r'id="hm-power-spark" points="[0-9][^"]*"', dom)     # sparkline has real points
    assert not re.search(r'id="hm-power"[^>]*hidden', dom)
    assert not re.search(r'id="hm-network"[^>]*hidden', dom)
    assert 'id="hm-network-sum">1 free' in dom
    assert re.search(r'id="hm-network-body"[^>]*><div class="cx-face">', dom)
    # lower sections
    assert 'class="hm-row-name">jellyfin down</span>' in dom                                # Recent
    assert 'class="hm-brief-title">Quiet night, one slow backup' in dom
    assert 'id="hm-inv-count">3<small> devices</small>' in dom
    assert re.search(r'class="hm-chip"[^>]*>2 vm</span>', dom) and re.search(r'class="hm-chip"[^>]*>1 host</span>', dom)
    assert 'class="hm-ql" href="https://pve.lan:8006"' in dom and 'class="hm-ql" href="http://grafana.lan:3000"' in dom
    assert 'class="hm-ql hm-go"' not in dom                                                  # two links: no "+N"


@needs_chromium
def test_home_survives_empty_and_garbage_endpoints(tmp_path):
    fx = home_fixtures(tmp_path)
    fx["/api/attention"] = {}                  # what the stub returns for unknown paths
    fx["/api/heartbeat"] = {}
    fx["/api/proxmox"] = fx["/api/nas"] = fx["/api/ups"] = fx["/api/brief"] = {}
    fx["/api/inventory"] = fx["/api/quicklinks"] = {}
    r = _home(tmp_path, fx)
    assert r.errors == []
    assert 'class="hm-led hm-led-stale"' in r.dom                            # stale, not blank or broken
    assert re.search(r'id="hm-headline"[^>]*>Checking…', r.dom)              # headline keeps its placeholder
    assert "<span>Homelab</span><em>" in r.dom                               # hosts still render from /api/status
    assert re.search(r'id="hm-servers"[^>]*hidden', r.dom)                  # unconfigured sections hide


@needs_chromium
def test_home_rejects_a_wrong_shaped_attention_response(tmp_path):
    fx = home_fixtures(tmp_path)
    fx["/api/attention"] = {"verdict": {}, "items": [None]}
    r = _home(tmp_path, fx)
    assert r.errors == []
    assert 'class="hm-led hm-led-stale"' in r.dom
    assert re.search(r'id="hm-headline"[^>]*>Checking…', r.dom)
    assert '<div class="hm-arow">' not in r.dom and "Nothing needs attention." not in r.dom
    assert "<span>Homelab</span><em>" in r.dom


@needs_chromium
def test_home_fresh_install_shows_calm_messages_and_hides_the_rest(tmp_path):
    fx = home_fixtures(tmp_path)
    fx["/api/status"] = {"hosts": [], "events": [], "summary": {"total": 0, "up": 0, "down": 0, "idle": 0, "pending": 0},
                         "settings": {}, "suggestions_pending": 0, "generated": ""}
    fx["/api/attention"] = {"generated": "", "items": [], "verdict": {
        "level": "ok", "headline": "No hosts are being monitored yet.",
        "counts": {"hosts_total": 0, "hosts_up": 0, "hosts_down": 0, "affected": 0, "maintenance": 0, "dismissed": 0}}}
    fx["/api/power"] = {"configured": False}
    fx["/api/proxmox"] = {"configured": False, "reachable": False, "nodes": []}
    fx["/api/nas"] = {"configured": False, "reachable": False, "pools": []}
    fx["/api/ups"] = {"configured": False}
    fx["/api/brief"] = {"briefs": []}
    fx["/api/inventory"] = {"items": []}
    fx["/api/quicklinks"] = {"links": []}
    fx["/api/discovery/status"] = {"configured": False, "port_maps": []}
    r = _home(tmp_path, fx)
    assert r.errors == []
    assert re.search(r'id="hm-headline"[^>]*>No hosts are being monitored yet\.', r.dom)
    assert 'class="hm-mut">Add hosts in Monitor → Edit hosts.' in r.dom
    assert 'class="hm-mut">Nothing needs attention.' in r.dom
    for sec in ("hm-servers", "hm-power", "hm-network", "hm-brief", "hm-inventory", "hm-links"):
        assert re.search(rf'id="{sec}"[^>]*hidden', r.dom), sec
    assert re.search(r'id="hm-explain"[^>]*hidden', r.dom)                  # nothing to explain


@needs_chromium
def test_home_renders_hostile_data_as_text(tmp_path):
    fx = home_fixtures(tmp_path)
    evil = "<img src=x onerror=alert(1)>"
    fx["/api/attention"]["items"][0]["title"] = evil
    fx["/api/attention"]["items"][1]["detail"] = "<script>alert(2)</script>"
    fx["/api/status"]["hosts"][0]["name"] = evil
    fx["/api/status"]["events"][0]["host_name"] = evil
    fx["/api/quicklinks"] = {"links": [{"id": 1, "label": evil, "url": "javascript:alert(3)", "icon": "<i>", "sort_order": 0}]}
    fx["/api/brief"]["briefs"][0]["subject"] = evil
    r = _home(tmp_path, fx)
    assert r.errors == []                       # an injected onerror/alert would land here (alert stub records)
    assert not re.search(r"<img[^>]*\bonerror", r.dom) and "<script>alert" not in r.dom
    assert "<b>&lt;img src=x onerror=alert(1)&gt;</b>" in r.dom             # rendered, as text
    assert 'class="hm-brief-title">&lt;img' in r.dom
    assert 'class="hm-ql" href="#"' in r.dom and 'href="javascript:' not in r.dom


def _home_long_strings_fixtures(tmp_path):
    fx = home_fixtures(tmp_path)
    fx["/api/attention"]["items"][0]["title"] = "A very long alert title " + "x" * 60     # unbreakable runs
    fx["/api/status"]["hosts"][2]["name"] = "jellyfin-" + "y" * 60
    fx["/api/status"]["hosts"][0]["group"] = "G" * 70
    fx["/api/quicklinks"]["links"][0]["label"] = "Q" * 70
    fx["/api/brief"]["briefs"][0]["subject"] = "S" * 70
    return fx


@needs_chromium
def test_home_long_strings_fixture_really_renders(tmp_path):
    # the 320/390 boots dump only the iframe wrapper, so prove at full width that the strings the
    # overflow test relies on reach the DOM
    r = _home(tmp_path, _home_long_strings_fixtures(tmp_path))
    assert r.errors == []
    assert "<span>" + "G" * 70 + "</span>" in r.dom
    assert "Q" * 70 + "</a>" in r.dom and 'class="hm-brief-title">' + "S" * 70 in r.dom
    assert "<b>A very long alert title " + "x" * 60 + "</b>" in r.dom
    assert 'hm-nu-name">jellyfin-' + "y" * 60 in r.dom


@needs_chromium
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [320, 390])
def test_home_with_real_content_does_not_overflow(width, theme, tmp_path):
    r = _home(tmp_path, _home_long_strings_fixtures(tmp_path), width=width, theme=theme)
    assert r.errors == []
    assert r.overflow <= 0, f"home overflows at {width}px in {theme}: {r.overflow}"
    assert r.inner_width == width


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
