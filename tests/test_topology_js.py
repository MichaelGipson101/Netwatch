"""Pure topology.js helpers (plan 5), run through node."""
import os

from js_harness import STATIC, needs_node, run_js

TOPO_JS = os.path.join(STATIC, "topology.js")
D3 = os.path.join(STATIC, "d3.v7.min.js")
HELPERS = [(TOPO_JS, "const TOPO_TREE_RULES"), (TOPO_JS, "function topoPortLabel"),
           (TOPO_JS, "function topoTreeOrientation"), (TOPO_JS, "function topoParseCollapsed"),
           (TOPO_JS, "function topoIsCollapsed"), (TOPO_JS, "function topoScene"),
           (TOPO_JS, "function topoBuildForest")]


def js(expr, parts=HELPERS, prelude=""):
    return run_js(parts, expr, prelude)


def n(id_, name, parent=None, role=None):
    return {"id": id_, "name": name, "primary_parent_id": parent, "network_role": role}


@needs_node
def test_port_label_and_orientation():
    assert js("[topoPortLabel('Port 11'), topoPortLabel('11'), topoPortLabel(' 08 '), "
              "topoPortLabel('SFP+ 1'), topoPortLabel(null), topoPortLabel('')]") == [
        ":11", ":11", ":8", ":SFP+ 1", "", ""]
    assert js("[topoTreeOrientation(1200, 700), topoTreeOrientation(390, 700), "
              "topoTreeOrientation(500, 500)]") == ["down", "right", "down"]


@needs_node
def test_collapsed_prefs_parse_and_default():
    assert js("[topoParseCollapsed('{\"5\": false, \"7\": true}'), topoParseCollapsed('[3, 4]'), "
              "topoParseCollapsed('nope'), topoParseCollapsed(null)]") == [
        {"5": False, "7": True}, {"3": True, "4": True}, {}, {}]
    assert js("[topoIsCollapsed(5, 12, {}), topoIsCollapsed(5, 6, {}), "
              "topoIsCollapsed(5, 12, {'5': false}), topoIsCollapsed(5, 0, {'5': true})]") == [
        True, False, False, True]


@needs_node
def test_scene_filters_unconnected_and_ghost_endpoints():
    data = {"nodes": [n(1, "a"), n(2, "b"), n(3, "c"), n(4, "d")],
            "edges": [{"id": 9, "source": 1, "target": 2}],
            "suggested_edges": [{"suggestion_id": 5, "source": 3, "target": 2},
                                {"suggestion_id": 6, "source": 4, "target": 77}]}
    import json as _json
    out = js(f"(() => {{ const d = {_json.dumps(data)}; return ["
             "topoScene(d, {includeUnconnected: false, showGhosts: true}),"
             "topoScene(d, {includeUnconnected: false, showGhosts: false}) ]; })()")
    on, off = out
    assert [x["id"] for x in on["nodes"]] == [1, 2, 3]
    assert [g["suggestion_id"] for g in on["ghosts"]] == [5]
    assert on["unconnected"] == 1
    assert [x["id"] for x in off["nodes"]] == [1, 2] and off["ghosts"] == []
    assert off["unconnected"] == 2


@needs_node
def test_forest_puts_the_gateway_tree_first_and_collapses_big_guest_parents():
    import json as _json
    nodes = [n(1, "Eero", role="gateway"), n(2, "USW", 1), n(3, "Prodesk", 2),
             n(20, "TP-Link"), n(21, "Behind TP", 20), n(22, "Also behind", 20),
             n(23, "Third", 20)]
    edges = []
    for i in range(7):                             # 7 guests -> starts collapsed
        nodes.append(n(100 + i, f"vm{i}", 3))
        edges.append({"source": 100 + i, "target": 3, "connection_type": "virtual",
                      "is_primary": True})
    expr = (f"(() => {{ const N = {_json.dumps(nodes)}; const E = {_json.dumps(edges)};"
            " return [topoBuildForest(N, E, {}), topoBuildForest(N, E, {'3': false})]; })()")
    auto, expanded = js(expr)
    assert [t["id"] for t in auto["trees"]] == [1, 20]            # gateway first, then bigger
    assert auto["info"]["3"] == {"collapsible": True, "collapsed": True, "hidden": 7,
                                 "guestPill": True}
    assert 100 not in auto["visible"] and 3 in auto["visible"]
    prodesk = auto["trees"][0]["children"][0]["children"][0]
    assert prodesk["id"] == 3 and prodesk["children"] == []
    assert expanded["info"]["3"]["collapsed"] is False and 106 in expanded["visible"]
    assert auto["info"]["20"]["collapsed"] is False              # 3 plain kids, not guests
    assert auto["info"]["21"]["collapsible"] is False


@needs_node
def test_forest_survives_a_cycle_and_missing_parents():
    import json as _json
    nodes = [n(1, "a", 2), n(2, "b", 1), n(3, "c", 99)]
    out = js(f"topoBuildForest({_json.dumps(nodes)}, [], {{}})")
    assert sorted(out["visible"]) == [1, 2, 3]                   # nobody lost


@needs_node
def test_tree_positions_down_and_right():
    import json as _json
    trees = [{"id": 1, "children": [{"id": 2, "children": []}, {"id": 3, "children": []}]},
             {"id": 9, "children": []}]
    parts = [(TOPO_JS, "const TOPO_TREE_RULES"), (TOPO_JS, "function topoTreePositions")]
    prelude = f"const d3 = require({_json.dumps(D3)});"
    down, right = run_js(parts, f"[topoTreePositions({_json.dumps(trees)}, 'down'),"
                                f" topoTreePositions({_json.dumps(trees)}, 'right')]", prelude)
    assert down["2"]["y"] > down["1"]["y"] and down["2"]["y"] == down["3"]["y"]
    assert down["2"]["x"] < down["3"]["x"] and down["9"]["x"] > down["3"]["x"]  # tree 2 to the right
    assert right["2"]["x"] > right["1"]["x"] and right["9"]["y"] > right["3"]["y"]  # tree 2 below


# ── Task 4: renderer split ───────────────────────────────────────────────────

def _src():
    with open(TOPO_JS, encoding="utf-8") as f:
        return f.read()


def test_renderer_is_split_into_scene_and_layouts():
    src = _src()
    for name in ("function _topoBuildScene(", "function _topoPositionAll(",
                 "function _layoutForce(", "function _topoArcPath(",
                 "function _topoObserveResize(", "function _topoStartFlow("):
        assert name in src, name
    body = src[src.index("function renderTopologyWeb("):src.index("function _topoBuildScene(")]
    assert "d3.forceSimulation" not in body          # the simulation lives in _layoutForce
    assert "topoScene(_topoData" in body
    assert "_topoSimulation.stop()" in body          # no leaked simulation on re-render
    force = src[src.index("function _layoutForce("):src.index("function _topoArcPath(")]
    for needle in ("d3.forceSimulation", "saveTopoLastLayout", "saveTopoPosition",
                   "d3.drag()", "fitTopologyToView", "spreadOverlappingLabels"):
        assert needle in force, needle


def test_empty_state_links_to_connections_and_reset_button_has_an_id():
    assert "Open Connections</a>" in _src()
    with open(os.path.join(STATIC, "..", "dashboard.html"), encoding="utf-8") as f:
        html = f.read()
    assert 'id="topo-reset-btn"' in html
    assert "getElementById('topo-reset-btn')" in _src()


# ── Task 5: tree layout wiring ───────────────────────────────────────────────

def test_tree_layout_is_wired():
    src = _src()
    for name in ("function _layoutTree(", "function _topoTreePath(",
                 "function _topoAddCollapseControls(", "function setTopoLayout(",
                 "function syncTopoLayoutControls(", "function topologyToggleCollapse(",
                 "function topoLoadCollapsed("):
        assert name in src, name
    render = src[src.index("function renderTopologyWeb("):src.index("function _topoBuildScene(")]
    assert "topoBuildForest(" in render and "_layoutTree(ctx)" in render
    tree = src[src.index("function _layoutTree("):src.index("function _topoTreePath(")]
    for needle in ("topoTreePositions(", "topoTreeOrientation(", "_topoObserveResize(",
                   "fitTopologyToView"):
        assert needle in tree, needle
    for forbidden in ("d3.drag", "saveTopoPosition", "saveTopoLastLayout", "forceSimulation"):
        assert forbidden not in tree, forbidden          # tree never pins or overwrites Force state
    init = src[src.index("async function initTopologyWeb("):src.index("async function fetchAndRenderTopologyWeb(")]
    assert "syncTopoLayoutControls()" in init


def test_toolbar_has_the_segmented_layout_control():
    with open(os.path.join(STATIC, "..", "dashboard.html"), encoding="utf-8") as f:
        html = f.read()
    assert 'id="topo-layout-force"' in html and 'id="topo-layout-tree"' in html
    assert "setTopoLayout('tree')" in html and "setTopoLayout('force')" in html
