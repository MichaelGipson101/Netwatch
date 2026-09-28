import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from netwatch.pages import PAGES, SHELL_SCRIPTS, render_all, resolve
from netwatch.server import _STATIC_FILES, make_handler

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.parametrize("path,expected", [
    ("/", ("home", "")), ("/index.html", ("home", "")),
    ("/monitor", ("monitor", "hosts")), ("/monitor/", ("monitor", "hosts")),
    ("/monitor/hosts", ("monitor", "hosts")), ("/monitor/events", ("monitor", "events")),
    ("/monitor/briefs", ("monitor", "briefs")),
    ("/lab", ("lab", "topology")), ("/lab/topology", ("lab", "topology")),
    ("/lab/connections", ("lab", "connections")), ("/lab/inventory", ("lab", "inventory")),
    ("/infra", ("infra", "proxmox")), ("/infra/proxmox", ("infra", "proxmox")),
    ("/infra/truenas", ("infra", "truenas")),
    ("/links", ("links", "")),
    ("/lab/topology/", ("lab", "topology")),
    ("/monitor/hosts?host=10.0.0.2", ("monitor", "hosts")),
    ("/?x=1", ("home", "")),
])
def test_resolve_known_paths(path, expected):
    assert resolve(path) == expected


@pytest.mark.parametrize("path", [
    "/lab/nope", "/monitor/hosts/extra", "//", "/LAB", "/monitor/events/x", "/static/x.js",
    "/api/status", "/infra/proxmox/x", "/links/x", "/lab//topology", "/index.htm", "",
])
def test_resolve_rejects_unknown_paths(path):
    assert resolve(path) is None


def test_render_all_assembles_every_page():
    pages = render_all(REPO, "9.9.9")
    assert set(pages) == set(PAGES)
    for name, html in pages.items():
        assert "{{" not in html, f"{name}: unreplaced placeholder"
        assert html.count("<title>") == 1
        assert "?v=9.9.9" in html
        assert html.count('aria-current="page"') == (0 if name == "links" else 1)
        # shell scripts first, in order, then the page's own
        srcs = __import__("re").findall(r'<script src="/static/([\w.-]+\.js)\?', html)
        assert srcs[:len(SHELL_SCRIPTS)] == list(SHELL_SCRIPTS)
        for s in PAGES[name].scripts:
            assert s in srcs and s in _STATIC_FILES


def test_shared_partials_are_included_not_copied():
    pages = render_all(REPO, "1")
    assert pages["monitor"].count('id="drawer"') == 1
    assert pages["lab"].count('id="drawer"') == 1
    assert pages["home"].count('id="ql-edit-overlay"') == 1
    assert pages["links"].count('id="ql-edit-overlay"') == 1
    assert 'id="drawer"' not in pages["home"]


def test_multi_view_pages_get_a_subnav_and_body_attrs():
    pages = render_all(REPO, "1")
    lab = pages["lab"]
    assert 'data-page="lab"' in lab and 'data-subviews="topology,connections,inventory"' in lab
    for sub in ("topology", "connections", "inventory"):
        assert f'href="/lab/{sub}"' in lab and f'data-subview="{sub}"' in lab
    assert 'data-subviews=""' in pages["home"] or "data-subviews" not in pages["home"]


def _serve(handler_pages, dashboard_html=""):
    handler = make_handler(None, {}, "/dev/null", dashboard_html=dashboard_html, pages=handler_pages)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    return server


def _get(server, path):
    t = threading.Thread(target=server.handle_request)
    t.start()
    try:
        return urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}{path}")
    finally:
        t.join()


def test_http_serves_each_page_and_404s_unknown():
    pages = {name: f"<html>{name}</html>" for name in PAGES}
    server = _serve(pages)
    try:
        for path, name in [("/", "home"), ("/monitor/events", "monitor"), ("/lab", "lab"),
                           ("/infra/truenas", "infra"), ("/links", "links"), ("/lab/topology/?x=1", "lab")]:
            with _get(server, path) as r:
                assert r.status == 200
                assert r.read() == f"<html>{name}</html>".encode()
                assert r.headers["Cache-Control"] == "no-cache"
        with pytest.raises(urllib.error.HTTPError) as e:
            _get(server, "/lab/nope")
        assert e.value.code in (404,)
    finally:
        server.server_close()


def test_legacy_dashboard_html_still_serves_root_when_no_pages_given():
    server = _serve(None, dashboard_html="<html>legacy</html>")
    try:
        with _get(server, "/") as r:
            assert r.read() == b"<html>legacy</html>"
    finally:
        server.server_close()
