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
