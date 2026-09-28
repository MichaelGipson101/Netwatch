"""Static split-breakage tests: what a page split actually breaks is a missing function,
a missing element, an unlisted script, or a dead API path on some page."""
import os
import re

import pytest

from page_analysis import (
    REPO, STATIC, PageSource, api_literals, declared_names, handler_calls, html_ids,
    js_created_ids, lit_ids, read, script_srcs,
)
from netwatch.server import _STATIC_FILES

# ids looked up by literal that are created by JS at runtime, not present in the HTML
DYNAMIC_IDS = {"nw-toasts"}
DYNAMIC_ID_PREFIXES = ("ov-card-",)
# ids intentionally looked up, per script, on pages that may not have them; every use site is
# null-guarded (add an entry here only together with the guard, and only for the script that
# actually contains the guarded lookup, so a stray unguarded lookup elsewhere still fails)
GUARDED_IDS = {
    "utils.js": {
        "save-status",          # setStatus(): `if(!el) return`
        "drawer",               # focus trap: `drawer && drawer.classList...`
    },
    "quicklinks.js": {
        "ov-ql-count",          # _renderCount(): `if (!el) return` (Links page has no Home card)
        "ql-page-grid",         # _renderCards() `if (!el) return`, and the boot hook checks it first
    },
    "auth.js": {
        "ql-page-edit-btn",     # updateAuthUI(): `if (qlEdit)`
    },
    # connections.js is loaded by Home for the port-map helpers only; its Connections-tab
    # renderers all bail out when their container is absent:
    "connections.js": {
        "cx-table",             # cxApplyTableOpen / renderCxTable / cxRenderTableRows: `if(el)` / `if(!el) return`
        "cx-table-toggle",      # cxApplyTableOpen: `if(btn)`
        "cx-table-count",       # renderCxTable: `if(countEl)`
        "cx-status",            # renderCxStatus: `if(!el) return`
        "cx-suggestions",       # renderCxSuggestions: `if(!el) return`
        "cx-sugg-count",        # renderCxSuggestions: `if(countEl)`
        "cx-ports",             # renderCxPortMaps: `if(!panel || !el) return`
        "cx-ports-panel",       # renderCxPortMaps: `if(!panel || !el) return`
        "cx-quick",             # mountConnectionsTab (only runs on the Lab subview hook, and guarded) / cxQuickAddAt: `if(!box) return`
        "view-connections",     # cxHighlight*/cxQuickAddAt: `view && ...`
        "drawer",               # openInventoryDrawer hand-off: `typeof ... && document.getElementById('drawer')`
    },
}

# Names that existed in the original static/core.js and were deliberately removed
# (each added by the task that removed it)
INTENTIONALLY_REMOVED = {"renderSummary", "_latHistory", "LAT_SPARK_SAMPLES", "renderPowerSparkline"}

# Every top-level declaration that existed in static/core.js at v3.80 (snapshot).
CORE_JS_ORIGINAL_DECLARATIONS = """
setTheme setTab REFRESH LAT_SPARK_SAMPLES _latHistory _firstRender lastOk lastData openDrawerIp
drawerHistRange _drawerOpener HIST_RANGES renderHost _hostStatusChip setHostChip applyHostFilter
renderGroups renderTopologyNode renderTopology renderEvents _briefsFetched fetchBriefs renderBriefs
renderSummary refresh clockTick openDrawer closeDrawer _maintenanceSectionHtml _maintenanceLabel
renderDrawer loadDrawerHistory setHistRange renderDrawerHistory fmtChartTime latencyChartSvg
dayStripHtml updateDrawerStats updatePiHealth sendWake startMaintenanceFromDrawer
clearMaintenanceFromDrawer closeEditor openAddHostModal closeAddHostModal saveAddHost addRow
addServiceRow detectMac addExtraLinkRow _discoverPollTimer openDiscover closeDiscover startDiscover
pollDiscover refreshDiscoverState renderDiscoverResults addDiscovered saveHosts renderPowerSparkline
refreshPowerCard _upsFillClass refreshUpsIcon _UPS_STATUS_LABELS _upsStatusLabel openUpsModal
closeUpsModal
""".split()


def load_pages():
    """The pages the dashboard serves: the assembled output of netwatch.pages.render_all()."""
    from netwatch.pages import PAGES as _P, SHELL_SCRIPTS, render_all
    html = render_all(REPO, "test")
    out = []
    for name, page in _P.items():
        scripts = list(SHELL_SCRIPTS) + [s for s in page.scripts if s not in SHELL_SCRIPTS]
        out.append(PageSource(name, html[name], scripts))
    return out


PAGES = load_pages()
IDS = [p.name for p in PAGES]


@pytest.mark.parametrize("page", PAGES, ids=IDS)
def test_inline_handlers_resolve(page):
    """Every function called from an inline handler (in the HTML or in HTML built by the
    page's scripts) is defined by one of the page's scripts."""
    defined = declared_names(list(page.js.values()) + [page.inline_js])
    calls = handler_calls(page.html)
    for src in page.js.values():
        calls |= handler_calls(src)
    missing = sorted(c for c in calls if c not in defined)
    assert not missing, f"page {page.name}: handlers call undefined functions: {missing}"


@pytest.mark.parametrize("page", PAGES, ids=IDS)
def test_literal_element_ids_exist(page):
    """Every getElementById('literal') in the page's scripts resolves to an element in the
    assembled HTML or one the scripts create themselves."""
    present = html_ids(page.html)
    for src in page.js.values():
        present |= js_created_ids(src)
    missing = {}
    for script, src in page.js.items():
        for i in lit_ids(src):
            if (i in present or i in DYNAMIC_IDS or i in GUARDED_IDS.get(script, ())
                    or i.startswith(DYNAMIC_ID_PREFIXES)):
                continue
            missing.setdefault(script, []).append(i)
    assert not missing, f"page {page.name}: scripts look up ids not on the page: {missing}"


def test_guarded_id_entries_are_all_live():
    """A GUARDED_IDS entry that no longer matches a lookup in its script is stale: delete it."""
    stale = []
    for script, ids in GUARDED_IDS.items():
        used = lit_ids(read(os.path.join(STATIC, script)))
        stale += [f"{script}:{i}" for i in sorted(ids) if i not in used]
    assert not stale, f"stale GUARDED_IDS entries: {stale}"


@pytest.mark.parametrize("page", PAGES, ids=IDS)
def test_scripts_exist_and_are_allowlisted(page):
    for s in page.scripts:
        assert s in _STATIC_FILES, f"{s} missing from _STATIC_FILES"
        assert os.path.exists(os.path.join(STATIC, s)), f"{s} not on disk"


@pytest.mark.parametrize("page", PAGES, ids=IDS)
def test_no_unreplaced_placeholders(page):
    html = page.html.replace("{{VERSION}}", "")  # substituted at startup by the loader
    assert "{{" not in html, f"page {page.name} still contains template placeholders"


def test_api_paths_used_by_scripts_have_server_routes():
    server = read(os.path.join(REPO, "netwatch", "server.py"))
    used = set()
    for name in os.listdir(STATIC):
        if name.endswith(".js") and not name.startswith("d3."):
            used |= api_literals(read(os.path.join(STATIC, name)))
    missing = sorted(a for a in used if a not in server)
    assert not missing, f"JS calls API paths the server does not route: {missing}"


def test_every_static_js_is_allowlisted():
    on_disk = {n for n in os.listdir(STATIC) if n.endswith(".js")}
    assert on_disk <= set(_STATIC_FILES), sorted(on_disk - set(_STATIC_FILES))


def test_core_js_declarations_are_preserved():
    """Every top-level declaration that lived in core.js still exists somewhere in static/."""
    all_js = [read(os.path.join(STATIC, n)) for n in os.listdir(STATIC)
              if n.endswith(".js") and not n.startswith("d3.")]
    now = declared_names(all_js)
    lost = sorted(n for n in CORE_JS_ORIGINAL_DECLARATIONS
                  if n not in now and n not in INTENTIONALLY_REMOVED)
    assert not lost, f"declarations from the original core.js disappeared: {lost}"


def test_no_reference_to_the_never_defined_showtab():
    assert "showTab" not in read(os.path.join(STATIC, "proxmox.js"))


def test_infra_panel_choice_comes_from_the_url_not_localstorage():
    src = read(os.path.join(STATIC, "proxmox.js"))
    assert "nw-servers-panel" not in src
    assert "popstate" in src
