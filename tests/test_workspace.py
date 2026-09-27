"""Tests for Netwatch 4.0 connections rework, plan 3 (Connections workspace)."""
import json
import os
import subprocess
import tempfile
import types

import pytest

from js_harness import REPO, STATIC, js_part, needs_node, run_js
from netwatch.connections import migration_drift_key
from netwatch.storage import HistoryDB, InventoryDB

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

INV_JS = os.path.join(STATIC, "inventory.js")
UTILS_JS = os.path.join(STATIC, "utils.js")


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
def test_match_port_option_matches_zero_padded_digits_against_plain_numeric_names():
    # Ports without live idx metadata are just named "1".."8" (port_count
    # devices) - the server's canonical_port treats "08" as "8", so the
    # client match has to as well instead of leaving it "not a port".
    opts = "[{value: '1', idx: null}, {value: '2', idx: null}, {value: '8', idx: null}]"
    out = run_js([(QA_JS, "function qaMatchPortOption")],
                 f"['08', '8', '007'].map(s => qaMatchPortOption({opts}, s))")
    assert out == ["8", "8", None]


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


# ── Task 6 fix round 1 (review rulings) ─────────────────────────────────────
# 1 Important (plan-mandated): the focus guard in cxRenderTableRows swallowed
# user-initiated renders (Cancel/Save/Swap/type-toggle/ports-loaded), because
# clicking a button focuses it and that button lives inside .cx-edit-row.
# Fix: an opts.force escape hatch, passed by every user-initiated call site.
# Folded minors: diff cxSaveEdit against an editOrig snapshot taken at edit
# start (not the live connection, which was already stale for the port-name
# case); stop pendingEdit re-queuing itself forever when the connection never
# shows up; seed _cxState.lastLoggedIn at mount so the first post-mount
# updateAuthUI() isn't misread as a login change.

@needs_node
def test_save_edit_diffs_against_the_edit_start_snapshot_not_the_live_connection():
    src = js_part(CX_JS, "async function cxSaveEdit")
    script = (
        "let postCalls, cancelCalls, _cxState, _conn;\n"
        "async function cxPost(url, body){ postCalls.push([url, body]); return {ok: true, body: {warnings: []}}; }\n"
        "function cxFindConnection(id){ return _conn; }\n"
        "function toast(){}\n"
        "function cxCancelEdit(){ cancelCalls++; }\n"
        "function cxRenderTableRows(opts){}\n"
        "function connectionsChanged(){}\n"
        + src +
        "\n"
        "async function run(draft, orig, conn){\n"
        "  postCalls = []; cancelCalls = 0; _conn = conn;\n"
        "  _cxState = {editingConn: 9, editDraft: draft, editOrig: orig};\n"
        "  await cxSaveEdit(9);\n"
        "  return {posts: postCalls, cancelled: cancelCalls};\n"
        "}\n"
        "(async () => {\n"
        # (a) "8" stored, mapped to "Port 8" in both draft and orig at edit
        # start (per cxStartEdit's qaMatchPortOption fixup) - no real change.
        "  const a = await run(\n"
        "    {parent_port: 'Port 8', connection_type: 'ethernet', notes: ''},\n"
        "    {parent_port: 'Port 8', connection_type: 'ethernet', notes: ''},\n"
        "    {id: 9, parent_port: '8', connection_type: 'ethernet', notes: ''});\n"
        # (b) orig port never matched a live one ("eth0"); draft is untouched
        # (still "eth0"); only notes changed - the live connection's port
        # ('eth0' too here) must not leak a stale parent_port into the body.
        "  const b = await run(\n"
        "    {parent_port: 'eth0', connection_type: 'ethernet', notes: 'new note'},\n"
        "    {parent_port: 'eth0', connection_type: 'ethernet', notes: 'old note'},\n"
        "    {id: 9, parent_port: 'eth0', connection_type: 'ethernet', notes: 'old note'});\n"
        # (c) switched to wifi: connection_type changes, but parent_port must
        # never be sent even though it differs from orig.
        "  const c = await run(\n"
        "    {parent_port: 'Port 8', connection_type: 'wifi', notes: ''},\n"
        "    {parent_port: 'Port 3', connection_type: 'ethernet', notes: ''},\n"
        "    {id: 9, parent_port: 'Port 3', connection_type: 'ethernet', notes: ''});\n"
        # (d) already wifi on both ends of the edit, with a stale stored port
        # ('WAN', migration drift) and no other change - Save must clear it
        # explicitly since the server only auto-clears on a type change.
        "  const dd = await run(\n"
        "    {parent_port: 'WAN', connection_type: 'wifi', notes: ''},\n"
        "    {parent_port: 'WAN', connection_type: 'wifi', notes: ''},\n"
        "    {id: 9, parent_port: 'WAN', connection_type: 'wifi', notes: ''});\n"
        "  process.stdout.write(JSON.stringify([a, b, c, dd]));\n"
        "})();"
    )
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    a, b, c, dd = json.loads(r.stdout)
    assert a == {"posts": [], "cancelled": 1}
    assert b["posts"] == [["/api/connections/9", {"notes": "new note"}]]
    assert c["posts"] == [["/api/connections/9", {"connection_type": "wifi"}]]
    assert dd["posts"] == [["/api/connections/9", {"parent_port": None}]]


@needs_node
def test_refresh_all_replay_does_not_requeue_a_pending_edit_that_never_appears():
    # cxStartEdit(id, {fromReplay: true}) must not re-set pendingEdit when the
    # connection still can't be found - otherwise a pending edit for a
    # deleted/never-existing id would requeue itself on every refresh forever.
    prelude = (
        "global.window = {};\n"
        "async function cxGetJson(url){ return {}; }\n"
        "async function qaLoadInventory(){ return []; }\n"
        "function cxRender(){}\n"
        "let startEditCalls = [];\n"
        "function cxStartEdit(id, opts){ startEditCalls.push([id, opts]); }\n"
        "let _cxState = {mounted: true, seq: 0, refreshing: false, refreshQueued: false,\n"
        "  status: null, suggestions: null, connections: null, inventory: [], categories: [],\n"
        "  portMaps: [], error: null, pendingEdit: 42};\n"
    )
    expr = (
        "(async () => {\n"
        "  await cxRefreshAll();\n"
        "  return [startEditCalls, _cxState.pendingEdit];\n"
        "})()"
    )
    src = prelude + "\n" + js_part(CX_JS, "async function cxRefreshAll")
    script = src + f"\n{expr}.then(r => process.stdout.write(JSON.stringify(r)));"
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    calls, pending = json.loads(r.stdout)
    assert calls == [[42, {"fromReplay": True}]]
    assert pending is None   # consumed before the replay call, not left dangling


# ── Task 7: suggestions ─────────────────────────────────────────────────────

CX_PANELS.append("renderCxSuggestions")

SUGG = ("[{id: 1, kind: 'drift', source: 'migration', payload: {child_name: 'USW', parent_name: 'Eero', message: 'bad port'}},"
        " {id: 2, kind: 'edge', source: 'unifi', payload: {message: 'VF2 is wired to USW · Port 4'}},"
        " {id: 3, kind: 'device', source: 'unifi', payload: {device: {system: 'WORKBENCH-PC'}}},"
        " {id: 4, kind: 'edge', source: 'proxmox', payload: {}},"
        " {id: 5, kind: 'drift', source: 'unifi', payload: {action: 'replace'}},"
        " {id: 6, kind: 'mystery', source: 'unifi', payload: {}}]")
SUGG_PARTS = [(CX_JS, "const CX_SOURCE_LABELS"), (CX_JS, "const CX_KIND_ORDER"),
              (CX_JS, "const CX_KIND_LABELS"), (CX_JS, "const CX_BULK_KINDS"),
              (CX_JS, "function cxGroupSuggestions")]


@needs_node
def test_group_suggestions_by_kind_then_source():
    out = run_js(SUGG_PARTS, f"cxGroupSuggestions({SUGG}).map(g => [g.kind, g.label, g.count, g.bulk,"
                             f" g.sources.map(s => [s.source, s.label, s.items.map(i => i.id)])])")
    assert out == [
        ["device", "New devices", 1, True, [["unifi", "UniFi", [3]]]],
        ["edge", "New connections", 2, True, [["proxmox", "Proxmox", [4]], ["unifi", "UniFi", [2]]]],
        ["drift", "Drift", 2, False, [["migration", "Migration", [1]], ["unifi", "UniFi", [5]]]],
    ]


@needs_node
def test_suggestion_actions_per_kind():
    cases = ("[{kind: 'device'}, {kind: 'edge'}, {kind: 'shared_port'},"
             " {kind: 'identity', payload: {candidate_id: 2}}, {kind: 'identity', payload: {candidate_id: null}},"
             " {kind: 'drift', payload: {action: 'replace'}}, {kind: 'drift', payload: {action: 'remove'}},"
             " {kind: 'drift', payload: {issues: ['bad_parent_port']}}]")
    out = run_js([(CX_JS, "function cxSuggestionActions")], f"{cases}.map(cxSuggestionActions)")
    assert out == [
        [["accept", "Accept", True], ["dismiss", "Dismiss", False]],
        [["accept", "Accept", True], ["dismiss", "Dismiss", False]],
        [["accept", "Create placeholder", True], ["dismiss", "Dismiss", False]],
        [["accept", "Yes", True], ["dismiss", "No", False]],
        [["accept", "That's the one", True], ["dismiss", "No", False]],
        [["accept", "Replace", True], ["dismiss", "Keep mine", False]],
        [["accept", "Remove", True], ["dismiss", "Keep it", False]],
        [["fix", "Fix…", True], ["dismiss", "Mark reviewed", False]],
    ]


@needs_node
def test_suggestion_text_and_done_messages():
    out = run_js([(CX_JS, "function cxSuggestionText"), (CX_JS, "function cxDoneMessage")],
                 f"[{SUGG}.map(cxSuggestionText),"
                 f" cxDoneMessage({{kind: 'drift', payload: {{action: 'remove'}}}}, 'accept'),"
                 f" cxDoneMessage({{kind: 'drift', payload: {{}}}}, 'dismiss'),"
                 f" cxDoneMessage({{kind: 'device', payload: {{}}}}, 'accept')]")
    assert out[0][0] == "USW → Eero: bad port"
    assert out[0][1] == "VF2 is wired to USW · Port 4"
    assert out[0][3] == "edge suggestion"
    assert out[1:] == ["Connection removed", "Kept your version", "Device added"]


@needs_node
def test_bulk_accept_skips_hand_edited_devices():
    out = run_js([(CX_JS, "function cxBulkItems")],
                 "cxBulkItems([{id: 3, kind: 'device', source: 'unifi', fingerprint: 'a'},"
                 " {id: 7, kind: 'device', source: 'unifi', fingerprint: 'b'},"
                 " {id: 8, kind: 'device', source: 'proxmox', fingerprint: 'c'}],"
                 " 'device', 'unifi', {7: {system: 'Renamed'}, 3: {open: true}})")
    assert out == {"send": [{"id": 3, "fingerprint": "a"}], "skipped": 1}


@needs_node
def test_device_editor_renders_drafts_over_the_payload():
    prelude = ("let _cxState = {drafts: {3: {system: 'Workbench PC', open: true}}, categories: []};"
               " const INV_TYPE_ORDER = ['host', 'vm', 'network'];")
    html = run_js([(UTILS_JS, "function escapeHtml"), (CX_JS, "function cxDeviceEditorHtml")],
                  "cxDeviceEditorHtml({id: 3, payload: {device: {system: 'WORKBENCH-PC', device_type: 'host'}}})",
                  prelude=prelude)
    assert 'value="Workbench PC"' in html and "WORKBENCH-PC" not in html
    assert "<details class=\"cx-sugg-edit\" open" in html
    assert '<option value="host" selected>' in html


# ── Task 8: port map ────────────────────────────────────────────────────────

CX_PANELS.append("renderCxPortMaps")
TILE_PARTS = [(CX_JS, "function cxFmtSpeed"), (CX_JS, "function cxPortTile")]


@needs_node
def test_port_tiles():
    ports = ("[{name: 'Port 7', up: true, speed_mbps: 1000, poe: true, occupants: [{name: 'Pi', connection_id: 4}]},"
             " {name: 'Port 4', up: true, speed_mbps: 2500, poe: false, occupants: []},"
             " {name: 'Port 1', up: false, occupants: []},"
             " {name: 'Port 11', up: true, speed_mbps: 100, occupants: [{name: 'A', connection_id: 1}, {name: 'B', connection_id: 2}]},"
             " {name: '3', up: null, occupants: []}]")
    out = run_js(TILE_PARTS, f"{ports}.map(cxPortTile)")
    assert out[0] == {"name": "Port 7", "state": "occupied", "link": "up", "label": "Pi",
                      "title": "Port 7 · link up · 1 Gbps · PoE · Pi", "conn_id": 4}
    assert out[1]["state"] == "up" and out[1]["label"] == "?" and "2.5 Gbps" in out[1]["title"]
    assert out[2]["state"] == "down" and out[2]["link"] == "down" and out[2]["label"] == ""
    assert out[3]["label"] == "+2" and out[3]["conn_id"] == 1 and "100 Mbps" in out[3]["title"]
    assert out[4]["link"] == "unknown" and out[4]["state"] == "down"


@needs_node
def test_short_port_names():
    out = run_js([(CX_JS, "function cxShortPortName")],
                 "['Port 7', 'Port 16', 'SFP+ 1', 'eth0', '3'].map(cxShortPortName)")
    assert out == ["7", "16", "SFP+1", "eth0", "3"]


# ── Task 9: drawer ──────────────────────────────────────────────────────────

@needs_node
def test_drawer_list_shows_the_other_end_port_and_source():
    conns = ("[{id: 1, direction: 'out', parent_id: 5, parent_name: 'USW', parent_type: 'network',"
             " child_id: 9, child_name: 'Me', parent_port: 'Port 7', connection_type: 'ethernet', source: 'unifi'},"
             " {id: 2, direction: 'in', parent_id: 9, parent_name: 'Me', child_id: 6, child_name: 'VM <1>',"
             " child_type: 'vm', parent_port: null, connection_type: 'virtual', source: 'manual'}]")
    prelude = "function deviceIcon(t, s){ return '[' + t + ']'; }"
    html = run_js([(UTILS_JS, "function escapeHtml"), (CX_JS, "const CX_SOURCE_LABELS"),
                   (INV_JS, "function drawerConnectionsHtml")],
                  f"[drawerConnectionsHtml({conns}), drawerConnectionsHtml([])]", prelude=prelude)
    assert "openInventoryDrawer(5)" in html[0] and "openInventoryDrawer(6)" in html[0]
    assert "Port 7" in html[0] and "UniFi" in html[0] and "Manual" in html[0]
    assert "VM &lt;1&gt;" in html[0] and "deleteConnection(1)" in html[0]
    assert "No connections recorded yet" in html[1]


def test_old_drawer_form_is_gone_and_fan_out_is_used():
    src = open(INV_JS, encoding="utf-8").read()
    for gone in ("_connFormState", "function renderConnectionsBody", "function startAddConnection",
                 "function cancelConnection", "function onConnTargetChange", "function submitConnection"):
        assert gone not in src, gone
    assert "renderQuickAdd(" in js_part(INV_JS, "async function loadInventoryConnections")
    assert "connectionsChanged()" in js_part(INV_JS, "async function deleteConnection")
    assert "connectionsChanged()" in js_part(INV_JS, "async function submitInventory")
    assert "connectionsChanged()" in js_part(INV_JS, "async function deleteInventory")
    assert "connectionsChanged()" in js_part(INV_JS, "async function submitImport")
    css = open(os.path.join(STATIC, "main.css"), encoding="utf-8").read()
    for gone in (".conn-form", ".conn-add", ".conn-group", ".conn-icon"):
        assert gone not in css, gone
    for name in os.listdir(STATIC):
        if name.endswith(".js"):
            text = open(os.path.join(STATIC, name), encoding="utf-8").read()
            assert "startAddConnection" not in text and "submitConnection(" not in text, name


# ── Task 10: responsive + version ───────────────────────────────────────────

def test_workspace_breakpoints_are_present():
    css = open(os.path.join(STATIC, "main.css"), encoding="utf-8").read()
    for needle in (
        '@media (max-width:900px){.cx-grid{grid-template-columns:minmax(0,1fr)',
        '@media (max-width:600px){.cx-table{min-width:640px}}',
        '.cx-face-grid{grid-template-columns:repeat(9,minmax(0,1fr))',
        '.cx-face-grid{grid-template-columns:repeat(6,minmax(0,1fr))',
        '@media (max-width:380px){',
        '[data-theme="dark"] .cx-panel{',
        '[data-theme="dark"] .qa-list{',
    ):
        assert needle in css, needle


def test_version_bumped_for_new_static_assets():
    from netwatch import VERSION
    assert VERSION == "3.78"


# ── Whole-branch review fix wave (Minor findings 1, 3, 4, 6) ────────────────
# 1: a partial load failure (status OK, connections/suggestions fail) must
#    surface an honest error and retry, not silently show "no data".
# 3: the edit row's swap control shouldn't vanish once a swap settles the
#    drift that made it appear - a wrong swap needs to stay undoable.
# 4: cxSwapConnection/cxSetFilter/the search box must not silently discard an
#    open, dirty edit draft.
# 6: migration-pending must disable quick add and show a matching message in
#    the table/inbox, not just the status strip.

@needs_node
def test_needs_retry_excludes_migration_pending():
    script = (
        "var _cxState;\n"
        + js_part(CX_JS, "function cxNeedsRetry") + "\n"
        "function scenario(s){ _cxState = s; return cxNeedsRetry(); }\n"
        "const out = [\n"
        "  scenario({migrationPending: false, status: null, connections: {}, suggestions: {}}),\n"
        "  scenario({migrationPending: false, status: {}, connections: null, suggestions: {}}),\n"
        "  scenario({migrationPending: false, status: {}, connections: {}, suggestions: null}),\n"
        "  scenario({migrationPending: false, status: {}, connections: {}, suggestions: {}}),\n"
        "  scenario({migrationPending: true, status: null, connections: null, suggestions: null}),\n"
        "];\n"
        "process.stdout.write(JSON.stringify(out));\n"
    )
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == [True, True, True, False, False]


@needs_node
def test_badge_retries_on_a_partial_load_failure_but_not_during_migration_pending():
    prelude = (
        "var _cxState;\n"
        "global.document = { getElementById: () => ({style: {}, textContent: ''}) };\n"
        "let cxRefreshCalls = 0;\n"
        "function cxRefreshAll(){ cxRefreshCalls++; }\n"
    )
    parts = [(CX_JS, "function cxNeedsRetry"), (CX_JS, "function updateConnectionsBadge")]
    expr = (
        "(function(){\n"
        "  const seq = [];\n"
        "  _cxState = {mounted: true, lastPending: null, migrationPending: false,\n"
        "              status: {}, connections: null, suggestions: {}};\n"
        "  updateConnectionsBadge(0); seq.push(cxRefreshCalls);\n"
        "  _cxState = {mounted: true, lastPending: null, migrationPending: true,\n"
        "              status: null, connections: null, suggestions: null};\n"
        "  updateConnectionsBadge(0); seq.push(cxRefreshCalls);\n"
        "  return seq;\n"
        "})()"
    )
    out = run_js(parts, expr, prelude=prelude)
    assert out == [1, 1]   # first call retries; migration-pending call adds no extra retry


@needs_node
def test_auth_hook_retries_on_a_partial_load_failure_but_not_during_migration_pending():
    prelude = (
        "global.window = {};\n"
        "function escapeHtml(s){ return String(s); }\n"
        "global.document = { getElementById: () => ({innerHTML: '', style: {}, textContent: ''}) };\n"
        "var _cxState;\n"
        "let cxRefreshCalls = 0;\n"
        "function renderCxStatus(){}\n"
        "function cxRefreshAll(){ cxRefreshCalls++; }\n"
        "let _authState = {logged_in: true, username: 'admin', admin: true};\n"
    )
    expr = (
        "(function(){\n"
        "  const seq = [];\n"
        "  _cxState = {mounted: true, lastLoggedIn: true, migrationPending: false,\n"
        "              status: {}, connections: null, suggestions: {}};\n"
        "  updateAuthUI(); seq.push(cxRefreshCalls);\n"
        "  _cxState = {mounted: true, lastLoggedIn: true, migrationPending: true,\n"
        "              status: null, connections: null, suggestions: null};\n"
        "  updateAuthUI(); seq.push(cxRefreshCalls);\n"
        "  return seq;\n"
        "})()"
    )
    out = run_js([(CX_JS, "function cxNeedsRetry"), (AUTH_JS, "function updateAuthUI")], expr, prelude=prelude)
    assert out == [1, 1]   # login-state unchanged both times; only cxNeedsRetry() drives the first retry


@needs_node
def test_table_and_inbox_show_the_honest_error_instead_of_empty_state():
    # cxRenderTableRows' empty-state text and renderCxSuggestions' "no data"
    # branch must surface _cxState.error rather than a misleading "no data
    # yet" message when a load partially failed (or migration is pending).
    table_part = js_part(CX_JS, "function cxRenderTableRows")
    assert "_cxState.error" in table_part
    sugg_part = js_part(CX_JS, "function renderCxSuggestions")
    assert "_cxState.error" in sugg_part and "escapeHtml(_cxState.error)" in sugg_part


EDIT_SWAPPED_PRELUDE = ("let _cxState = {editDraft: {parent_port: 'Port 8', connection_type: 'ethernet', notes: ''},"
                        " editPorts: [{name: 'Port 8', idx: 8, occupants: []}], swappedIds: {9: true}};")


@needs_node
def test_edit_row_keeps_swap_control_for_a_swapped_id_with_no_drift():
    html = run_js(EDIT_PARTS, f"cxEditRowHtml({EDIT_CONN}, [])", prelude=EDIT_SWAPPED_PRELUDE)
    assert "cxSwapConnection(9)" in html
    # No real drift this time - the hint about an undetermined direction
    # shouldn't reappear just because the id is remembered as swapped.
    assert "couldn't tell which end is upstream" not in html


@needs_node
def test_set_filter_confirms_before_discarding_a_dirty_edit():
    src = js_part(CX_JS, "function cxEditDraftDirty") + "\n" + js_part(CX_JS, "function cxSetFilter")
    script = (
        "let renderCalls, confirmCalls, confirmReturn, _cxState;\n"
        "function cxRenderTableRows(opts){ renderCalls++; }\n"
        "global.confirm = m => { confirmCalls.push(m); return confirmReturn; };\n"
        + src + "\n"
        "function run(draft, orig, ret){\n"
        "  renderCalls = 0; confirmCalls = []; confirmReturn = ret;\n"
        "  _cxState = {editingConn: draft ? 9 : null, editDraft: draft, editOrig: orig, filter: 'all'};\n"
        "  cxSetFilter('drift');\n"
        "  return {filter: _cxState.filter, editingConn: _cxState.editingConn,\n"
        "          renders: renderCalls, confirms: confirmCalls.length};\n"
        "}\n"
        "const clean = run({parent_port: 'a', connection_type: 'ethernet', notes: ''},\n"
        "  {parent_port: 'a', connection_type: 'ethernet', notes: ''}, false);\n"
        "const cancelled = run({parent_port: 'a', connection_type: 'ethernet', notes: 'edited'},\n"
        "  {parent_port: 'a', connection_type: 'ethernet', notes: ''}, false);\n"
        "const confirmed = run({parent_port: 'a', connection_type: 'ethernet', notes: 'edited'},\n"
        "  {parent_port: 'a', connection_type: 'ethernet', notes: ''}, true);\n"
        "process.stdout.write(JSON.stringify({clean, cancelled, confirmed}));\n"
    )
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["clean"] == {"filter": "drift", "editingConn": None, "renders": 1, "confirms": 0}
    assert out["cancelled"] == {"filter": "all", "editingConn": 9, "renders": 0, "confirms": 1}
    assert out["confirmed"] == {"filter": "drift", "editingConn": None, "renders": 1, "confirms": 1}


@needs_node
def test_swap_connection_confirms_before_discarding_edits_and_remembers_swapped_id():
    src = js_part(CX_JS, "function cxEditDraftDirty") + "\n" + js_part(CX_JS, "async function cxSwapConnection")
    script = (
        "let postCalls, renderCalls, changedCalls, confirmCalls, confirmReturn, _cxState;\n"
        "async function cxPost(url, body){ postCalls.push([url, body]); return {ok: true, body: {}}; }\n"
        "function toast(){}\n"
        "function cxRenderTableRows(opts){ renderCalls++; }\n"
        "function connectionsChanged(){ changedCalls++; }\n"
        "global.confirm = m => { confirmCalls.push(m); return confirmReturn; };\n"
        + src + "\n"
        "async function run(draft, orig, ret){\n"
        "  postCalls = []; renderCalls = 0; changedCalls = 0; confirmCalls = []; confirmReturn = ret;\n"
        "  _cxState = {editingConn: 9, editDraft: draft, editOrig: orig, swappedIds: {}};\n"
        "  await cxSwapConnection(9);\n"
        "  return {posts: postCalls, editingConn: _cxState.editingConn, swapped: _cxState.swappedIds,\n"
        "          renders: renderCalls, changed: changedCalls, confirms: confirmCalls.length};\n"
        "}\n"
        "(async () => {\n"
        "  const clean = await run(\n"
        "    {parent_port: 'a', connection_type: 'ethernet', notes: ''},\n"
        "    {parent_port: 'a', connection_type: 'ethernet', notes: ''}, false);\n"
        "  const cancelled = await run(\n"
        "    {parent_port: 'a', connection_type: 'ethernet', notes: 'edited'},\n"
        "    {parent_port: 'a', connection_type: 'ethernet', notes: ''}, false);\n"
        "  const confirmed = await run(\n"
        "    {parent_port: 'a', connection_type: 'ethernet', notes: 'edited'},\n"
        "    {parent_port: 'a', connection_type: 'ethernet', notes: ''}, true);\n"
        "  process.stdout.write(JSON.stringify({clean, cancelled, confirmed}));\n"
        "})();\n"
    )
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["clean"]["posts"] == [["/api/connections/9", {"swap": True}]]
    assert out["clean"]["editingConn"] is None and out["clean"]["swapped"] == {"9": True}
    assert out["clean"]["changed"] == 1 and out["clean"]["confirms"] == 0
    assert out["cancelled"]["posts"] == [] and out["cancelled"]["editingConn"] == 9
    assert out["cancelled"]["swapped"] == {} and out["cancelled"]["confirms"] == 1
    assert out["confirmed"]["posts"] == [["/api/connections/9", {"swap": True}]]
    assert out["confirmed"]["swapped"] == {"9": True} and out["confirmed"]["confirms"] == 1


def test_search_input_guards_against_discarding_a_dirty_edit():
    part = js_part(CX_JS, "function renderCxTable")
    assert "cxEditDraftDirty()" in part
    assert "search.value = _cxState.query" in part   # revert on cancel, keep editing


def test_quick_add_is_locked_while_migration_is_pending():
    assert "_cxQuickAdd.setLocked(pending)" in js_part(CX_JS, "async function cxRefreshAll")
    qa_part = js_part(QA_JS, "function renderQuickAdd")
    assert "setLocked:" in qa_part and "st.locked" in qa_part
