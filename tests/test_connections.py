"""Tests for the Netwatch 4.0 connections rework (plan 1: foundation)."""
import json
import os
import tempfile

import pytest

from netwatch.connections import (
    orient_edge, default_connection_type, infer_network_role, network_role,
    normalize_port, resolve_ports, validate_parent_port, fingerprint, type_rank,
)


def rec(id_, device_type="host", system=None, role=None, **props):
    """Build an inventory-shaped record dict for pure-function tests."""
    return {"id": id_, "system": system or f"dev{id_}", "device_type": device_type,
            "role": role, "properties": dict(props)}


# ── orientation ─────────────────────────────────────────────────────────────

def test_orient_vm_is_child_of_host_regardless_of_argument_order():
    vm, host = rec(1, "vm"), rec(2, "host")
    assert orient_edge(vm, host) == (vm, host, False)
    assert orient_edge(host, vm) == (vm, host, False)


def test_orient_host_is_child_of_network():
    host, sw = rec(1, "host"), rec(2, "network", network_role="switch")
    assert orient_edge(sw, host) == (host, sw, False)


def test_orient_power_edge_makes_ups_parent_even_of_network_gear():
    sw, ups = rec(1, "network", network_role="switch"), rec(2, "ups")
    assert orient_edge(ups, sw, "power") == (sw, ups, False)
    # Without the power type, plain rank wins: the switch outranks the UPS.
    assert orient_edge(ups, sw, "ethernet") == (ups, sw, False)


def test_orient_network_tie_broken_by_role():
    gw = rec(1, "network", network_role="gateway")
    sw = rec(2, "network", network_role="switch")
    ap = rec(3, "network", network_role="ap")
    assert orient_edge(gw, sw) == (sw, gw, False)
    assert orient_edge(sw, ap) == (ap, sw, False)


def test_orient_equal_rank_without_tiebreak_is_ambiguous_and_keeps_order():
    a, b = rec(1, "host"), rec(2, "host")
    assert orient_edge(a, b) == (a, b, True)
    n1, n2 = rec(3, "network"), rec(4, "network")  # both role "other"
    assert orient_edge(n1, n2) == (n1, n2, True)


def test_type_rank_defaults_unknown_and_missing_types_to_host():
    assert type_rank({"device_type": None}) == type_rank(rec(1, "host"))
    assert type_rank({"device_type": "toaster"}) == type_rank(rec(1, "host"))


# ── default type ────────────────────────────────────────────────────────────

def test_default_type_vm_on_host_is_virtual():
    assert default_connection_type(rec(1, "vm"), rec(2, "host")) == "virtual"


def test_default_type_client_on_ap_is_wifi_and_on_switch_is_ethernet():
    ap = rec(2, "network", network_role="ap")
    sw = rec(3, "network", network_role="switch")
    assert default_connection_type(rec(1, "phone"), ap) == "wifi"
    assert default_connection_type(rec(1, "host"), sw) == "ethernet"


# ── network roles ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("system,role,expected", [
    ("Eero Pro 6E — Gateway", "Primary Router & Gateway", "gateway"),
    ("Ubiquiti Unifi USW Pro Max 16 PoE", "Primary Network Switch", "switch"),
    ("TP Link 24-port Managed Switch", "Main Network Switch", "switch"),
    ("Eero Pro 6E — Basement AP", "Access Point", "ap"),
    ("Mystery box", None, "other"),
])
def test_infer_network_role_from_free_text(system, role, expected):
    assert infer_network_role(rec(1, "network", system=system, role=role)) == expected


def test_network_role_reads_property_and_rejects_unknown_values():
    assert network_role(rec(1, "network", network_role="Switch")) == "switch"
    assert network_role(rec(1, "network", network_role="core-router")) == "other"
    assert network_role(rec(1, "network")) == "other"


# ── ports ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (" 8 ", "8"), ("08", "8"), (8, "8"), ("SFP+ 1", "SFP+ 1"), ("", None), (None, None), ("  ", None),
])
def test_normalize_port(raw, expected):
    assert normalize_port(raw) == expected


def test_resolve_ports_from_port_count():
    ports = resolve_ports(rec(1, "network", port_count=3))
    assert [p["name"] for p in ports] == ["1", "2", "3"]
    assert set(ports[0]) >= {"name", "up", "speed_mbps", "poe"}


def test_resolve_ports_prefers_live_ports_and_copies_them():
    live = [{"name": "Port 1", "up": True, "speed_mbps": 1000, "poe": None}]
    ports = resolve_ports(rec(1, "network", port_count=16), live)
    assert ports == live
    ports[0]["name"] = "mutated"
    assert live[0]["name"] == "Port 1"


@pytest.mark.parametrize("count", [None, "", "abc", 0, -2, 100000])
def test_resolve_ports_without_usable_count_is_free_text(count):
    r = rec(1, "network") if count is None else rec(1, "network", port_count=count)
    assert resolve_ports(r) is None


def test_validate_parent_port():
    sw = rec(1, "network", system="USW", port_count=16)
    ports = resolve_ports(sw)
    assert validate_parent_port(sw, "8", ports) is None
    assert validate_parent_port(sw, " 08", ports) is None
    assert validate_parent_port(sw, None, ports) is None
    assert "not a port on USW" in validate_parent_port(sw, "etho0", ports)
    assert validate_parent_port(sw, "anything", None) is None  # free text


def test_fingerprint_is_stable_and_order_independent():
    assert fingerprint({"a": 1, "b": [1, 2]}) == fingerprint({"b": [1, 2], "a": 1})
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})
    assert len(fingerprint({})) == 16
