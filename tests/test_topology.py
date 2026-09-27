"""Tests for Netwatch 4.0 connections rework, plan 5 (topology)."""
import os
import tempfile

from netwatch.http_handlers import compute_primary_parents


def e(id_, child, parent, ctype="ethernet", last_seen=None):
    return {"id": id_, "from_device_id": child, "to_device_id": parent,
            "connection_type": ctype, "last_seen": last_seen}


def ns(*ids):
    return [{"id": i} for i in ids]


# ── Task 1: primary parents ──────────────────────────────────────────────────

def test_type_priority_picks_the_primary_parent():
    parents, primary = compute_primary_parents(ns(1, 2, 3, 4, 5), [
        e(10, 1, 2, "power"), e(11, 1, 3, "wifi"), e(12, 1, 4, "ethernet"),
        e(13, 5, 2, "usb"), e(14, 5, 3, "virtual")])
    assert parents == {1: 4, 2: None, 3: None, 4: None, 5: 3}
    assert primary == {12, 14}


def test_ethernet_and_fiber_tie_on_recency_then_lowest_id():
    parents, _ = compute_primary_parents(ns(1, 2, 3), [
        e(20, 1, 2, "fiber", last_seen=100), e(21, 1, 3, "ethernet", last_seen=200)])
    assert parents[1] == 3
    parents, _ = compute_primary_parents(ns(1, 2, 3), [
        e(22, 1, 2, "ethernet"), e(21, 1, 3, "fiber")])
    assert parents[1] == 3                     # no last_seen: lowest edge id wins


def test_unknown_types_rank_as_other_and_self_loops_or_strangers_are_ignored():
    parents, primary = compute_primary_parents(ns(1, 2, 3), [
        e(30, 1, 2, "carrier-pigeon"), e(31, 1, 3, "power"), e(32, 2, 2, "ethernet"),
        e(33, 3, 99, "ethernet")])
    assert parents == {1: 2, 2: None, 3: None} and primary == {30}


def test_two_cycle_drops_the_lower_priority_edge():
    parents, primary = compute_primary_parents(ns(1, 2), [
        e(40, 1, 2, "ethernet"), e(41, 2, 1, "wifi")])
    assert parents == {1: 2, 2: None} and primary == {40}


def test_three_cycle_drops_one_edge_and_its_child_falls_back():
    parents, primary = compute_primary_parents(ns(1, 2, 3, 4), [
        e(50, 1, 2, "ethernet"), e(51, 2, 3, "ethernet"), e(52, 3, 1, "wifi"),
        e(53, 3, 4, "power")])
    # 52 (wifi) is the weakest link in the cycle; node 3 falls back to power -> 4
    assert parents == {1: 2, 2: 3, 3: 4, 4: None}
    assert primary == {50, 51, 53}


def test_cycle_breaking_is_deterministic_on_ties():
    edges = [e(61, 1, 2, "ethernet"), e(60, 2, 1, "ethernet")]
    a = compute_primary_parents(ns(1, 2), edges)
    b = compute_primary_parents(ns(2, 1), list(reversed(edges)))
    assert a == b
    # equal type and no last_seen: the higher edge id (61) is "lowest priority"
    assert a == ({1: None, 2: 1}, {60})


# ── Task 2: payload ──────────────────────────────────────────────────────────

from netwatch.http_handlers import build_topology_payload
from netwatch.storage import HistoryDB, InventoryDB

OLD_NODE_KEYS = {"id", "name", "category", "device_type", "linked_host", "status", "is_up",
                 "ip", "mac"}
OLD_EDGE_KEYS = {"id", "source", "target", "from_port", "to_port", "connection_type", "notes"}


def lab(d):
    hdb = HistoryDB(os.path.join(d, "topo.db"))
    idb = InventoryDB(hdb)

    def add(system, dtype="host", **props):
        new_id, err = idb.create({"system": system, "device_type": dtype,
                                  "properties": props or None})
        assert err is None, err
        return new_id

    gw = add("Eero", "network", network_role="gateway")
    sw = add("USW", "network", network_role="switch")
    node = add("Prodesk")
    vm = add("Minecraft", "vm")
    with idb.lock:
        for child, parent, port, ctype, source in [(sw, gw, "1", "ethernet", "manual"),
                                                   (node, sw, "Port 8", "ethernet", "unifi"),
                                                   (vm, node, None, "virtual", "proxmox"),
                                                   (node, gw, None, "power", "manual")]:
            idb.conn.execute(
                "INSERT INTO inventory_connections (from_device_id, to_device_id, to_port, "
                "connection_type, created_at, source) VALUES (?, ?, ?, ?, 0, ?)",
                (child, parent, port, ctype, source))
    return hdb, idb, {"gw": gw, "sw": sw, "node": node, "vm": vm}


def test_payload_keeps_every_old_key_and_adds_the_new_ones():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids = lab(d)
        out = build_topology_payload(idb, None)
        for n in out["nodes"]:
            assert OLD_NODE_KEYS <= set(n)
            assert {"network_role", "primary_parent_id", "children_count"} <= set(n)
        by = {n["id"]: n for n in out["nodes"]}
        assert by[ids["gw"]]["network_role"] == "gateway"
        assert by[ids["node"]]["primary_parent_id"] == ids["sw"]   # ethernet beats power
        assert by[ids["vm"]]["primary_parent_id"] == ids["node"]
        assert by[ids["gw"]]["primary_parent_id"] is None
        assert (by[ids["gw"]]["children_count"], by[ids["node"]]["children_count"]) == (1, 1)
        for edge in out["edges"]:
            assert OLD_EDGE_KEYS <= set(edge) and {"origin", "is_primary"} <= set(edge)
        power = [edge for edge in out["edges"] if edge["connection_type"] == "power"][0]
        assert (power["origin"], power["is_primary"]) == ("manual", False)
        assert (power["source"], power["target"]) == (ids["node"], ids["gw"])   # child, parent
        vm_edge = [edge for edge in out["edges"] if edge["connection_type"] == "virtual"][0]
        assert (vm_edge["origin"], vm_edge["is_primary"]) == ("proxmox", True)
        assert out["suggested_edges"] == []
        hdb.close()


def test_suggested_edges_are_pending_edge_suggestions_with_both_ends_known():
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids = lab(d)

        def upsert(kind, key, payload):
            return {"kind": kind, "source": "unifi", "subject_key": key,
                    "payload": payload, "fp": "f" * 16}

        idb.apply_discovery_changes({"upserts": [
            upsert("edge", "edge:unifi:a", {"child_id": ids["vm"], "parent_id": ids["sw"],
                                            "connection_type": "ethernet", "parent_port": "Port 3"}),
            upsert("edge", "edge:unifi:b", {"child_id": 9999, "parent_id": ids["sw"],
                                            "connection_type": "ethernet", "parent_port": None}),
            upsert("device", "device:unifi:c", {"device": {"system": "x"},
                                                "edge": {"parent_id": ids["sw"]}}),
        ]}, 1_800_000_000)
        out = build_topology_payload(idb, None)
        [g] = out["suggested_edges"]
        assert {k: g[k] for k in ("source", "target", "connection_type", "parent_port", "origin")} == {
            "source": ids["vm"], "target": ids["sw"], "connection_type": "ethernet",
            "parent_port": "Port 3", "origin": "unifi"}
        assert isinstance(g["suggestion_id"], int)
        assert all(edge.get("suggestion_id") is None for edge in out["edges"])  # never mixed in
        hdb.close()


def test_suggested_edges_unavailable_degrades_to_empty_and_logs_a_warning(monkeypatch, caplog):
    with tempfile.TemporaryDirectory() as d:
        hdb, idb, ids = lab(d)

        def boom(status="pending"):
            raise RuntimeError("suggestions table is locked")

        monkeypatch.setattr(idb.suggestions, "list", boom)
        with caplog.at_level("WARNING"):
            out = build_topology_payload(idb, None)
        assert out["suggested_edges"] == []
        assert any("topology: suggested_edges unavailable: RuntimeError" in r.message
                   for r in caplog.records)
        hdb.close()


def test_payload_without_inventory_still_carries_suggested_edges():
    # Consumers (topology.js, hearthboard) can rely on the key always existing.
    assert build_topology_payload(None, None) == {"nodes": [], "edges": [], "suggested_edges": []}
