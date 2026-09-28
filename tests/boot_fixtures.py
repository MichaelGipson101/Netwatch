"""Canned API responses for boot smoke tests. Unknown paths return {} (200)."""
import os
from collections import deque
from datetime import datetime

from netwatch.hosts import HostState
from netwatch.http_handlers import build_api_payload


class _HM:
    def __init__(self, hosts):
        self._hosts = hosts

    def list_hosts(self):
        return self._hosts


def _host(name, ip, group, up=True, always_on=True, latency=1.4):
    h = HostState(name=name, ip=ip, group=group, interval=15, always_on=always_on)
    h.history = deque([True] * 9 + [up], maxlen=100)
    h.last_latency_ms = latency if up else None
    h.last_checked = datetime.now()
    h.last_seen_up = datetime.now() if up else None
    return h


def status_payload():
    hosts = [
        _host("pve", "10.0.0.2", "Homelab"),
        _host("nas", "10.0.0.3", "Homelab"),
        _host("jellyfin", "10.0.0.4", "Virtual Machines", up=False),
        _host("laptop", "10.0.0.5", "Computers", up=False, always_on=False),
    ]
    payload = build_api_payload(_HM(hosts), {"default_interval": 15})
    payload["events"] = [{
        "host_name": "jellyfin", "host_ip": "10.0.0.4", "host_group": "Virtual Machines",
        "started_str": "12:04:00", "started_ts": int(datetime.now().timestamp()) - 720,
        "ended_str": None, "duration_seconds": None, "duration_str": "12m", "ongoing": True,
    }]
    payload["suggestions_pending"] = 2
    return payload


def default_fixtures(tmpdir=None, logged_in=True):
    return {
        "/api/status": status_payload(),
        "/api/auth/status": {"logged_in": logged_in, "admin": logged_in, "setup_required": False,
                             "auth_required": True, "username": "admin" if logged_in else None,
                             "csrf_token": "t"},
        "/api/power": {"configured": False},
        "/api/ups": {"configured": False},
        "/api/proxmox": {"configured": False, "reachable": False, "nodes": []},
        "/api/nas": {"configured": False, "reachable": False, "pools": []},
        "/api/pbs": {"error": "PBS not configured"},
        "/api/inventory": {"items": []},
        "/api/topology": {"nodes": [], "edges": [], "suggested_edges": []},
        "/api/brief": {"briefs": []},
        "/api/quicklinks": {"links": [
            {"id": 1, "label": "Proxmox VE", "url": "https://pve.lan:8006", "icon": "\U0001F5A5", "sort_order": 0},
            {"id": 2, "label": "Grafana", "url": "http://grafana.lan:3000", "icon": "\U0001F4C8", "sort_order": 1},
        ]},
        "/api/suggestions": {"suggestions": [], "counts": {}},
        "/api/connections": {"connections": []},
        "/api/discovery/status": {"configured": False, "port_maps": []},
        "/api/ai-config": {"enabled": False},
    }
