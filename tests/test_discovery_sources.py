"""Tests for Netwatch 4.0 connections rework, plan 4 (Proxmox + wifi inference)."""
import os
import tempfile
import types
import urllib.error

import pytest

from netwatch.pollers import ProxmoxPoller

NOW = 1_800_000_000
DAY = 86400


# ── Task 1: Proxmox poller hooks ─────────────────────────────────────────────

def make_poller(configured=True):
    data = ({"proxmox_url": "https://pve.local:8006", "proxmox_user": "root@pam",
             "proxmox_token_id": "netwatch", "proxmox_token_secret": "s3cret"}
            if configured else {})
    p = ProxmoxPoller(types.SimpleNamespace(data=data), alert_settings={})
    p._check_alerts = lambda *a: None  # no ntfy from tests
    return p


def fake_fetch(calls, fail=False):
    def fetch(url, user, token_id, token_secret, path):
        calls.append(path)
        if fail:
            raise OSError("down")
        if path == "/api2/json/nodes":
            return [{"node": "pve", "status": "online"}]
        if path.endswith("/qemu"):
            return [{"vmid": 108, "name": "haos13.2", "status": "running"}]
        if path.endswith("/lxc"):
            return []
        return {"echo": path}
    return fetch


def test_proxmox_poller_api_get_uses_its_own_credentials():
    p = make_poller()
    p._fetch = fake_fetch([])
    assert p.configured() is True
    assert p.api_get("/api2/json/cluster/status") == {"echo": "/api2/json/cluster/status"}
    idle = make_poller(configured=False)
    assert idle.configured() is False
    with pytest.raises(RuntimeError):
        idle.api_get("/api2/json/cluster/status")


def test_fresh_nodes_polls_only_when_the_cache_is_stale():
    p = make_poller()
    calls = []
    p._fetch = fake_fetch(calls)
    nodes = p.fresh_nodes()
    assert [n["name"] for n in nodes] == ["pve"]
    assert nodes[0]["guests"][0]["vmid"] == 108
    polled = len(calls)
    p.fresh_nodes()
    assert len(calls) == polled          # still fresh: no second poll
    p._last_ok_at -= 301
    p.fresh_nodes()
    assert len(calls) > polled           # stale: polled again


def test_fresh_nodes_is_none_when_polling_fails():
    p = make_poller()
    p._fetch = fake_fetch([], fail=True)
    assert p.fresh_nodes() is None
