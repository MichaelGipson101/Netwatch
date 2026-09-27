"""Tests for Netwatch 4.0 connections rework, plan 3 (Connections workspace)."""
import json
import os
import shutil
import subprocess
import tempfile
import types

import pytest

from netwatch.connections import migration_drift_key
from netwatch.storage import HistoryDB, InventoryDB

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(REPO, "static")
DASHBOARD = os.path.join(REPO, "dashboard.html")


def make_idb(tmpdir):
    hdb = HistoryDB(os.path.join(tmpdir, "ws_test.db"))
    return hdb, InventoryDB(hdb)


def add_device(idb, system, device_type="host", mac=None, **props):
    data = {"system": system, "device_type": device_type, "properties": props or None}
    if mac:
        data["mac"] = mac
    new_id, err = idb.create(data)
    assert err is None, err
    return new_id


def insert_edge(idb, child, parent, to_port=None, ctype="ethernet", from_port=None):
    with idb.lock:
        return idb.conn.execute(
            "INSERT INTO inventory_connections (from_device_id, to_device_id, from_port, "
            "to_port, connection_type, created_at) VALUES (?, ?, ?, ?, ?, 0)",
            (child, parent, from_port, to_port, ctype)).lastrowid


def pending_keys(idb):
    return {s["subject_key"] for s in idb.suggestions.list()}


# ── Task 1: storage carry-forwards ──────────────────────────────────────────

def test_port_occupancy_ignores_wifi_edges():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        sw = add_device(idb, "Switch", "network", network_role="switch", port_count=8)
        wired = add_device(idb, "Wired")
        radio = add_device(idb, "Radio")
        insert_edge(idb, wired, sw, to_port="2")
        insert_edge(idb, radio, sw, to_port="3", ctype="wifi")  # legacy bad data
        ports, _ = idb.ports_for_device(sw)
        occ = {p["name"]: [o["device_id"] for o in p["occupants"]] for p in ports}
        assert occ["2"] == [wired] and occ["3"] == []
        hdb.close()


def test_changing_type_to_wifi_clears_the_parent_port():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        sw = add_device(idb, "Switch", "network", network_role="switch", port_count=8)
        h = add_device(idb, "Laptop")
        cid = insert_edge(idb, h, sw, to_port="2")
        ok, err, warnings = idb.update_connection(cid, {"connection_type": "wifi"})
        assert ok and err is None and warnings == ["parent_port_cleared"]
        c = idb.get_connection(cid)
        assert c["parent_port"] is None and c["connection_type"] == "wifi"
        ok, err, _ = idb.update_connection(cid, {"parent_port": "4"})
        assert not ok and "wifi" in err
        hdb.close()


def test_explicit_swap_settles_ambiguous_direction_drift():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        a, b = add_device(idb, "Box A"), add_device(idb, "Box B")
        cid = insert_edge(idb, a, b)
        assert idb.migrate_connections_v2()[0]
        assert migration_drift_key(cid) in pending_keys(idb)
        ok, err, _ = idb.update_connection(cid, {"swap": True})
        assert ok, err
        assert migration_drift_key(cid) not in pending_keys(idb)
        hdb.close()


def test_swap_keeps_drift_while_another_issue_remains():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        a = add_device(idb, "Box A", port_count=2)
        b = add_device(idb, "Box B", port_count=2)
        guest = add_device(idb, "Guest", "vm")
        cid = insert_edge(idb, a, b, to_port="1", from_port="2")   # ambiguous (host-host)
        insert_edge(idb, guest, a, to_port="2", ctype="virtual")    # already on Box A port 2
        assert idb.migrate_connections_v2()[0]
        ok, err, warnings = idb.update_connection(cid, {"swap": True})
        assert ok, err
        assert warnings == ["port_in_use"]
        # After the swap Box A is the parent on port "2", which the guest
        # already uses - the direction is settled but the duplicate isn't.
        assert migration_drift_key(cid) in pending_keys(idb)
        hdb.close()


def test_raising_port_count_resolves_bad_port_drift():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        sw = add_device(idb, "Switch", "network", network_role="switch", port_count=4)
        h = add_device(idb, "Host")
        cid = insert_edge(idb, h, sw, to_port="6")
        assert idb.migrate_connections_v2()[0]
        assert migration_drift_key(cid) in pending_keys(idb)
        ok, err = idb.update(sw, {"properties": {"port_count": 8}})
        assert ok, err
        assert migration_drift_key(cid) not in pending_keys(idb)
        assert idb.get(sw)["properties"]["network_role"] == "switch"
        hdb.close()


def test_changing_child_type_resolves_ambiguous_drift():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        guest, host = add_device(idb, "Guest"), add_device(idb, "Hypervisor")
        cid = insert_edge(idb, guest, host)
        assert idb.migrate_connections_v2()[0]
        assert migration_drift_key(cid) in pending_keys(idb)
        ok, err = idb.update(guest, {"device_type": "vm"})
        assert ok, err
        assert migration_drift_key(cid) not in pending_keys(idb)
        hdb.close()


def test_relint_failure_does_not_fail_a_record_edit(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        h = add_device(idb, "Host")

        def boom(*a, **k):
            raise RuntimeError("relint exploded")
        monkeypatch.setattr(idb, "relint_parent", boom)
        assert idb.update(h, {"notes": "hello"}) == (True, None)
        assert idb.get(h)["notes"] == "hello"
        hdb.close()


# ── Task 2: API fields ──────────────────────────────────────────────────────

from netwatch.http_handlers import _h_get_suggestions, _h_get_ports, _h_get_discovery_status
from netwatch.discovery import DiscoveryRunner

USW_MAC = "74:fa:29:1d:a3:dc"


def ready_idb(d):
    hdb, idb = make_idb(d)
    assert idb.migrate_connections_v2()[0]
    return hdb, idb


def test_suggestions_are_counted_by_kind_and_source():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = ready_idb(d)
        idb.suggestions.upsert("edge", "unifi", "edge:unifi:a", {}, "f1")
        idb.suggestions.upsert("edge", "proxmox", "edge:proxmox:b", {}, "f2")
        idb.suggestions.upsert("drift", "unifi", "drift:unifi:c", {}, "f3")
        code, body = _h_get_suggestions(idb)
        assert code == 200
        assert body["counts"] == {"edge": 2, "drift": 1}
        assert body["counts_by_source"] == {"edge": {"unifi": 1, "proxmox": 1},
                                            "drift": {"unifi": 1}}
        hdb.close()


def test_ports_say_whether_they_are_live():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = ready_idb(d)
        sw = add_device(idb, "Switch", "network", network_role="switch", port_count=4)
        code, body = _h_get_ports(f"/api/ports/{sw}", idb)
        assert code == 200 and body["live"] is False and len(body["ports"]) == 4
        idb.live_port_provider = lambda r: ([{"name": "Port 1", "idx": 1, "up": True,
                                              "speed_mbps": 1000, "poe": False}]
                                            if r["id"] == sw else None)
        code, body = _h_get_ports(f"/api/ports/{sw}", idb)
        assert body["live"] is True and [p["name"] for p in body["ports"]] == ["Port 1"]
        hdb.close()


def test_discovery_status_lists_inventory_switches_with_live_port_maps():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = ready_idb(d)
        usw = add_device(idb, "USW Pro Max", "network", mac=USW_MAC, network_role="switch")
        aliased = add_device(idb, "Aliased switch", "network", mac="aa:00:00:00:00:01",
                             network_role="switch", mac_aliases=["aa:00:00:00:00:02"])
        add_device(idb, "Unrelated", "network", mac="aa:00:00:00:00:09")
        runner = types.SimpleNamespace(
            status=lambda: {"sources": {}, "last_scan": 5, "scanning": False},
            switch_macs=lambda: [USW_MAC.upper(), "aa:00:00:00:00:02", "ff:ff:ff:ff:ff:ff"])
        code, body = _h_get_discovery_status(runner, idb)
        assert code == 200 and body["last_scan"] == 5
        assert body["port_maps"] == [{"device_id": aliased, "name": "Aliased switch"},
                                     {"device_id": usw, "name": "USW Pro Max"}]
        assert _h_get_discovery_status(None) == (200, {
            "sources": {}, "last_scan": None, "scanning": False, "port_maps": []})
        hdb.close()


def test_runner_reports_switch_macs_from_the_last_scan():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb = make_idb(d)
        r = DiscoveryRunner(types.SimpleNamespace(data={}), {}, idb)
        assert r.switch_macs() == []
        r._switches = [{"mac": USW_MAC, "ports": []}]
        assert r.switch_macs() == [USW_MAC]
        hdb.close()


# ── JS helper harness (pure functions run in node) ──────────────────────────

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
INV_JS = os.path.join(STATIC, "inventory.js")
UTILS_JS = os.path.join(STATIC, "utils.js")


def js_part(path, marker):
    """Source of the top-level `function name(...)` or `const NAME = ...` that
    starts at `marker`, found by bracket matching. Helpers tested this way
    keep brackets balanced inside string and regex literals."""
    with open(path, encoding="utf-8") as f:
        src = f.read()
    start = src.index(marker)
    opens = [i for i in (src.find("{", start), src.find("[", start)) if i != -1]
    i = min(opens)
    depth = 0
    for j in range(i, len(src)):
        if src[j] in "{[":
            depth += 1
        elif src[j] in "}]":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    if src[end:end + 1] == ";":
        end += 1
    return src[start:end]


def run_js(parts, expr, prelude=""):
    src = prelude + "\n" + "\n".join(js_part(p, m) for p, m in parts)
    script = src + f"\nprocess.stdout.write(JSON.stringify({expr}));"
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# ── Task 3: inventory form ──────────────────────────────────────────────────

INV_DEFS = [(INV_JS, "const INV_NETWORK_ROLES"), (INV_JS, "const INVENTORY_TYPE_PROPERTIES"),
            (INV_JS, "const INVENTORY_COMMON_PROPERTIES"), (INV_JS, "function invPropDefs")]


@needs_node
def test_prop_defs_add_network_role_and_mac_aliases():
    out = run_js(INV_DEFS, "[invPropDefs('network').map(p => p.key), "
                           "invPropDefs('host').map(p => p.key), "
                           "invPropDefs('toaster').map(p => p.key), "
                           "invPropDefs('network')[0]]")
    assert out[0][0] == "network_role" and "port_count" in out[0] and out[0][-1] == "mac_aliases"
    assert out[1] == ["mac_aliases"] and out[2] == ["mac_aliases"]
    role = out[3]
    assert role["type"] == "select" and role["dflt"] == "other"
    assert [o[0] for o in role["options"]] == ["gateway", "switch", "ap", "other"]


@needs_node
def test_parse_mac_list_normalises_dedupes_and_reports_bad_tokens():
    out = run_js([(INV_JS, "function invParseMacList")],
                 "[invParseMacList('D4-3F-32-EB-2A-E0, d43f32eb2ae0 bogus;aa:bb'), "
                 "invParseMacList(''), invParseMacList('  ')]")
    assert out[0] == {"macs": ["d4:3f:32:eb:2a:e0"], "bad": ["bogus", "aa:bb"]}
    assert out[1] == {"macs": [], "bad": []} and out[2] == {"macs": [], "bad": []}


@needs_node
def test_format_prop_value_for_the_drawer():
    out = run_js([(INV_JS, "const INV_NETWORK_ROLES"), (INV_JS, "function invFormatPropValue")],
                 "[invFormatPropValue({type: 'select', options: INV_NETWORK_ROLES}, 'ap'), "
                 "invFormatPropValue({type: 'select', options: INV_NETWORK_ROLES}, 'weird'), "
                 "invFormatPropValue({type: 'maclist'}, ['a', 'b']), "
                 "invFormatPropValue({type: 'maclist'}, []), "
                 "invFormatPropValue({type: 'bool'}, false), "
                 "invFormatPropValue({type: 'int'}, 16), "
                 "invFormatPropValue({type: 'string'}, '')]")
    assert out == ["Access point", "weird", "a, b", None, "No", "16", None]


def test_inventory_form_uses_prop_defs_everywhere():
    src = open(INV_JS, encoding="utf-8").read()
    for fn in ("function onInvTypeChange", "async function submitInventory",
               "function renderInventoryDrawer"):
        assert "invPropDefs(" in js_part(INV_JS, fn), fn
    assert "invParseMacList(" in js_part(INV_JS, "async function submitInventory")
    assert "p.type === \"select\"" in js_part(INV_JS, "function onInvTypeChange")


# ── Task 4: quick add ───────────────────────────────────────────────────────

QA_JS = os.path.join(STATIC, "quickadd.js")
DEVICES = ("[{id: 1, system: 'USW Pro Max 16 PoE', device_type: 'network', ip: '192.168.6.2'},"
           " {id: 2, system: 'Raspberry Pi 5', device_type: 'host', ip: '192.168.6.90'},"
           " {id: 3, system: 'ProDesk1', device_type: 'host', ip: '192.168.6.219'},"
           " {id: 4, system: 'Printer (Office)', device_type: 'printer', ip: null}]")


@needs_node
def test_match_devices_ranks_prefix_first_and_excludes():
    out = run_js([(QA_JS, "function qaMatchDevices")],
                 f"[qaMatchDevices({DEVICES}, 'pr', [], 8).map(d => d.id),"
                 f" qaMatchDevices({DEVICES}, 'pr', [3], 8).map(d => d.id),"
                 f" qaMatchDevices({DEVICES}, '', [], 2).map(d => d.id),"
                 f" qaMatchDevices({DEVICES}, '6.90', [], 8).map(d => d.id),"
                 f" qaMatchDevices({DEVICES}, 'zzz', [], 8)]")
    assert out[0] == [4, 3, 1]       # prefix matches first (alphabetical), then "USW Pro..." contains it
    assert out[1] == [4, 1]
    assert out[2] == [4, 3]          # empty query: alphabetical, limited
    assert out[3] == [2]             # matches on IP too
    assert out[4] == []


@needs_node
def test_port_options_mark_taken_and_live_ports():
    ports = ("[{name: 'Port 1', idx: 1, up: true, occupants: []},"
             " {name: 'Port 2', idx: 2, up: false, occupants: [{name: 'NAS', connection_id: 9}]},"
             " {name: '3', up: null, occupants: []}]")
    out = run_js([(QA_JS, "function qaPortOptions")], f"[qaPortOptions({ports}), qaPortOptions(null)]")
    assert out[0] == [
        {"value": "Port 1", "label": "Port 1 · link up", "taken": False, "idx": 1},
        {"value": "Port 2", "label": "Port 2 · NAS", "taken": True, "idx": 2},
        {"value": "3", "label": "3", "taken": False, "idx": None},
    ]
    assert out[1] is None


@needs_node
def test_match_port_option_maps_hand_typed_ports_onto_live_names():
    opts = ("[{value: 'Port 8', idx: 8}, {value: 'SFP+ 1', idx: 17}, {value: '3', idx: null}]")
    out = run_js([(QA_JS, "function qaMatchPortOption")],
                 f"['8', 'port 8', 'Port 8', '17', 'sfp+ 1', '3', 'eth0', '', null]"
                 f".map(s => qaMatchPortOption({opts}, s))")
    assert out == ["Port 8", "Port 8", "Port 8", "SFP+ 1", "SFP+ 1", "3", None, None, None]


@needs_node
def test_orient_and_sentence():
    preview = "{child_id: 2, child_name: 'Pi', parent_id: 1, parent_name: 'USW', ambiguous: true}"
    out = run_js([(QA_JS, "function qaOrient"), (QA_JS, "function qaSentence")],
                 f"[qaSentence(qaOrient({preview}, false), 'Port 7', 'ethernet'),"
                 f" qaSentence(qaOrient({preview}, true), '', 'ethernet'),"
                 f" qaOrient(null, false), qaSentence(null, 'x', 'y')]")
    assert out == ["Pi → USW · Port 7 · ethernet", "USW → Pi · ethernet", None, ""]


def test_quickadd_is_served_and_loaded():
    from netwatch.server import _STATIC_FILES
    assert _STATIC_FILES["quickadd.js"].startswith("application/javascript")
    html = open(DASHBOARD, encoding="utf-8").read()
    assert '<script src="/static/quickadd.js?v={{VERSION}}"></script>' in html
    assert "qaInvalidateInventory" in js_part(INV_JS, "async function fetchInventory")


@needs_node
@pytest.mark.parametrize("name", sorted(f for f in os.listdir(STATIC)
                                        if f.endswith(".js") and f != "d3.v7.min.js"))
def test_every_static_script_parses(name):
    r = subprocess.run(["node", "--check", os.path.join(STATIC, name)],
                       capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr


# ── Task 5: workspace shell ─────────────────────────────────────────────────

CX_JS = os.path.join(STATIC, "connections.js")
CORE_JS = os.path.join(STATIC, "core.js")
AUTH_JS = os.path.join(STATIC, "auth.js")

# Every workspace panel renderer; cxRender() must call each one. Later
# tasks append to this list as they add panels.
CX_PANELS = ["renderCxStatus"]


@needs_node
def test_counts_text():
    out = run_js([(CX_JS, "const CX_SINGULAR"), (CX_JS, "function cxCountsText")],
                 "[cxCountsText({switches: 1, clients: 35}), cxCountsText({switches: 2}), cxCountsText(null),"
                 " cxCountsText({nodes: 1})]")
    assert out == ["35 clients · 1 switch", "2 switches", "", "1 node"]


@needs_node
def test_source_chips():
    status = ("{sources: {unifi: {configured: true, ok: true, error: null, at: 1000, counts: {switches: 1, clients: 3}},"
              " proxmox: {configured: true, ok: false, error: 'HTTP 401', at: 900, counts: null},"
              " inferred: {configured: false, ok: null},"
              " later: {configured: true, ok: null, at: null}}, apply_error: 'RuntimeError'}")
    out = run_js([(UTILS_JS, "function lastSeenStr"), (CX_JS, "const CX_SOURCE_LABELS"),
                  (CX_JS, "const CX_SINGULAR"),
                  (CX_JS, "function cxCountsText"), (CX_JS, "function cxSourceChips")],
                 f"[cxSourceChips({status}, 1300), cxSourceChips(null, 0)]")
    chips = out[0]
    assert [c["name"] for c in chips] == ["later", "proxmox", "unifi", "apply"]
    assert chips[0] == {"name": "later", "label": "later", "state": "pending", "when": None,
                        "detail": "Waiting for the first scan"}
    assert chips[1]["state"] == "warn" and chips[1]["detail"] == "HTTP 401" and chips[1]["when"] == "6m ago"
    assert chips[2] == {"name": "unifi", "label": "UniFi", "state": "ok", "when": "5m ago",
                        "detail": "3 clients · 1 switch"}
    assert chips[3]["state"] == "warn" and "RuntimeError" in chips[3]["detail"]
    assert out[1] == []


def test_connections_tab_is_wired_in():
    html = open(DASHBOARD, encoding="utf-8").read()
    i_topo = html.index('data-tab="topology"')
    i_conn = html.index('data-tab="connections"')
    i_events = html.index('data-tab="events"')
    assert i_topo < i_conn < i_events
    for needle in ('id="conn-count"', 'id="view-connections"', 'id="cx-status"', 'id="cx-quick"',
                   'id="cx-suggestions"', 'id="cx-ports-panel"', 'id="cx-table"',
                   '<script src="/static/connections.js?v={{VERSION}}"></script>'):
        assert needle in html, needle
    from netwatch.server import _STATIC_FILES
    assert _STATIC_FILES["connections.js"].startswith("application/javascript")
    assert "mountConnectionsTab" in js_part(CORE_JS, "function setTab")
    assert "updateConnectionsBadge(data.suggestions_pending)" in js_part(CORE_JS, "async function refresh")
    auth_body = js_part(AUTH_JS, "function updateAuthUI")
    assert "renderCxStatus" in auth_body
    assert "cxRefreshAll" in auth_body


def test_cx_render_fans_out_to_every_panel():
    body = js_part(CX_JS, "function cxRender(")
    for fn in CX_PANELS:
        assert fn + "()" in body, fn


def test_connections_changed_refreshes_workspace_and_drawer():
    body = js_part(CX_JS, "function connectionsChanged")
    assert "cxRefreshAll()" in body and "loadInventoryConnections(" in body


# ── Task 6: connections table ───────────────────────────────────────────────

CX_PANELS.append("renderCxTable")

CONNS = ("[{id: 1, child_name: 'Pi', parent_name: 'USW', parent_port: 'Port 10', source: 'manual', connection_type: 'ethernet'},"
         " {id: 2, child_name: 'NAS', parent_name: 'USW', parent_port: 'Port 9', source: 'unifi', connection_type: 'ethernet'},"
         " {id: 3, child_name: 'Printer', parent_name: 'Eero', parent_port: null, source: 'manual', connection_type: 'wifi', notes: 'office'},"
         " {id: 4, child_name: 'Box', parent_name: 'USW', parent_port: 'SFP+ 1', source: 'manual', connection_type: 'fiber'}]")
TABLE_PARTS = [(CX_JS, "function cxPortSortKey"), (CX_JS, "function cxCompareConnections"),
               (CX_JS, "function cxFilterConnections"), (CX_JS, "function cxFilterCounts")]


@needs_node
def test_filter_and_sort_connections():
    out = run_js(TABLE_PARTS,
                 f"[cxFilterConnections({CONNS}, 'all', '', []).map(c => c.id),"
                 f" cxFilterConnections({CONNS}, 'drift', '', [3]).map(c => c.id),"
                 f" cxFilterConnections({CONNS}, 'manual', '', []).map(c => c.id),"
                 f" cxFilterConnections({CONNS}, 'discovered', '', []).map(c => c.id),"
                 f" cxFilterConnections({CONNS}, 'all', 'OFFICE', []).map(c => c.id),"
                 f" cxFilterCounts({CONNS}, [3, 99])]")
    assert out[0] == [3, 2, 1, 4]   # by parent, then natural port order (9 before 10), SFP after Port
    assert out[1] == [3] and out[2] == [3, 1, 4] and out[3] == [2] and out[4] == [3]
    assert out[5] == {"all": 4, "drift": 1, "manual": 3, "discovered": 1}


@needs_node
def test_drift_issues_by_connection():
    sugg = ("{items: [{kind: 'drift', payload: {connection_id: 5, issues: ['ambiguous_direction']}},"
            " {kind: 'drift', payload: {connection_id: 6, action: 'replace'}},"
            " {kind: 'edge', payload: {connection_id: 7}}]}")
    out = run_js([(CX_JS, "function cxDriftIssues")], f"[cxDriftIssues({sugg}), cxDriftIssues(null)]")
    assert out == [{"5": ["ambiguous_direction"], "6": []}, {}]


EDIT_PRELUDE = ("let _cxState = {editDraft: {parent_port: 'eth0', connection_type: 'ethernet', notes: 'typed'},"
                " editPorts: [{name: 'Port 8', idx: 8, occupants: []}]};")
EDIT_PARTS = [(UTILS_JS, "function escapeHtml"), (QA_JS, "const QA_CONNECTION_TYPES"),
              (QA_JS, "function qaPortOptions"), (CX_JS, "function cxEditRowHtml")]
EDIT_CONN = "{id: 9, child_name: 'Pi', parent_name: 'USW', parent_id: 1, source: 'unifi'}"


@needs_node
def test_edit_row_renders_from_the_draft_and_keeps_an_unknown_port():
    html = run_js(EDIT_PARTS, f"cxEditRowHtml({EDIT_CONN}, [])", prelude=EDIT_PRELUDE)
    assert 'value="typed"' in html                                   # draft, not the DOM
    assert '<option value="eth0" selected>eth0 (not a port on this device)</option>' in html
    assert "Saving makes this connection manual" in html            # sourced edge
    assert "cxSwapConnection" not in html
    html = run_js(EDIT_PARTS, f"cxEditRowHtml({EDIT_CONN}, ['ambiguous_direction'])", prelude=EDIT_PRELUDE)
    assert "cxSwapConnection(9)" in html


@needs_node
def test_edit_row_waits_for_ports_and_disables_port_for_wifi():
    prelude = ("let _cxState = {editDraft: {parent_port: '', connection_type: 'wifi', notes: ''},"
               " editPorts: undefined};")
    html = run_js(EDIT_PARTS, f"cxEditRowHtml({EDIT_CONN}, [])", prelude=prelude)
    assert "Loading ports" in html
    prelude = ("let _cxState = {editDraft: {parent_port: '', connection_type: 'wifi', notes: ''},"
               " editPorts: null};")
    html = run_js(EDIT_PARTS, f"cxEditRowHtml({EDIT_CONN}, [])", prelude=prelude)
    assert 'data-field="parent_port"' in html and "disabled" in html


# ── Carried ruling (a): cxRefreshAll coalesces instead of dropping ─────────

@needs_node
def test_refresh_all_coalesces_a_call_that_arrives_mid_flight():
    prelude = (
        "global.window = {};\n"
        "let statusCalls = 0, renderCalls = 0;\n"
        "async function cxGetJson(url){\n"
        "  if(url === '/api/discovery/status') statusCalls++;\n"
        "  return {};\n"
        "}\n"
        "async function qaLoadInventory(){ return []; }\n"
        "function cxRender(){ renderCalls++; }\n"
        "function cxStartEdit(id){}\n"
        "let _cxState = {mounted: true, seq: 0, refreshing: false, refreshQueued: false,\n"
        "  status: null, suggestions: null, connections: null, inventory: [], categories: [],\n"
        "  portMaps: [], error: null, pendingEdit: null};\n"
    )
    expr = (
        "(async () => {\n"
        "  cxRefreshAll();\n"
        "  cxRefreshAll();\n"     # arrives while the first is in flight - must coalesce, not drop
        "  for(let i = 0; i < 100 && (renderCalls < 2 || _cxState.refreshing); i++){\n"
        "    await new Promise(r => setTimeout(r, 5));\n"
        "  }\n"
        "  return [statusCalls, renderCalls, _cxState.refreshing, _cxState.refreshQueued];\n"
        "})()"
    )
    src = prelude + "\n" + js_part(CX_JS, "async function cxRefreshAll")
    script = src + f"\n{expr}.then(r => process.stdout.write(JSON.stringify(r)));"
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out == [2, 2, False, False]   # two full fetch rounds ran; the second wasn't swallowed


# ── Carried ruling (b): auth polling only re-fetches connections when the
# login state actually changed, or the workspace never loaded ─────────────

def test_auth_ui_only_refetches_connections_on_login_change_or_missing_status():
    prelude = (
        "global.window = {};\n"
        "function escapeHtml(s){ return String(s); }\n"
        "function makeEl(){\n"
        "  const el = {innerHTML: '', style: {}, textContent: ''};\n"
        "  return new Proxy(el, {get(t, p){ return p in t ? t[p] : function(){}; },\n"
        "                        set(t, p, v){ t[p] = v; return true; }});\n"
        "}\n"
        "global.document = { getElementById: () => makeEl() };\n"
        "let cxRefreshCalls = 0, renderCxStatusCalls = 0;\n"
        "function renderCxStatus(){ renderCxStatusCalls++; }\n"
        "function cxRefreshAll(){ cxRefreshCalls++; }\n"
        "let _cxState = { mounted: true, status: null, lastLoggedIn: null };\n"
        "let _authState = { logged_in: false, username: 'admin', admin: true };\n"
    )
    expr = (
        "(function(){\n"
        "  const seq = [];\n"
        "  updateAuthUI(); seq.push(cxRefreshCalls);\n"                                    # not logged in: no fetch
        "  _authState.logged_in = true; updateAuthUI(); seq.push(cxRefreshCalls);\n"       # login, status null: fetch
        "  _cxState.status = {}; updateAuthUI(); seq.push(cxRefreshCalls);\n"              # loaded, unchanged: no fetch
        "  updateAuthUI(); seq.push(cxRefreshCalls);\n"                                    # 60s poll, unchanged: no fetch
        "  _authState.logged_in = false; updateAuthUI(); seq.push(cxRefreshCalls);\n"      # logout: no fetch
        "  _authState.logged_in = true; updateAuthUI(); seq.push(cxRefreshCalls);\n"       # re-login: fetch again
        "  return [seq, renderCxStatusCalls];\n"
        "})()"
    )
    out = run_js([(AUTH_JS, "function updateAuthUI")], expr, prelude=prelude)
    assert out[0] == [0, 1, 1, 1, 1, 2]
    assert out[1] == 6   # renderCxStatus() stays unconditional on every call
