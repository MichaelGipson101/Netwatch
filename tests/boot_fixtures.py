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
        "/api/attention": {"generated": "", "items": [],
                           "verdict": {"level": "ok", "headline": "Everything looks good.",
                                       "counts": {"hosts_total": 4, "hosts_up": 2, "hosts_down": 0,
                                                  "affected": 0, "maintenance": 0, "dismissed": 0}}},
        "/api/heartbeat": {"generated": "", "bucket_seconds": 1800, "start": 0, "hosts": {}},
    }


def home_fixtures(tmpdir=None, logged_in=True):
    """default_fixtures plus rich data for every Home section."""
    fx = default_fixtures(tmpdir, logged_in)
    now = int(datetime.now().timestamp())
    fx.update({
        "/api/attention": {
            "generated": "", "verdict": {
                "level": "down", "headline": "2 problems need attention. jellyfin is down.",
                "counts": {"hosts_total": 4, "hosts_up": 2, "hosts_down": 1, "affected": 0,
                           "maintenance": 0, "dismissed": 1}},
            "items": [
                {"id": "host_down:10.0.0.4", "kind": "host_down", "severity": "critical",
                 "title": "jellyfin is down", "detail": "Down 12 min", "since": now - 720,
                 "affected": ["10.0.0.7", "10.0.0.8"], "root_ip": "10.0.0.4",
                 "link": {"page": "monitor", "subview": "hosts", "params": {"host": "10.0.0.4"}}},
                {"id": "alert:pool_health_tank", "kind": "poller_condition", "severity": "warning",
                 "title": "TrueNAS pool tank is DEGRADED", "detail": "TrueNAS · 1 h", "since": now - 3600,
                 "affected": [], "root_ip": None, "link": {"page": "infra", "subview": "truenas", "params": {}}},
                {"id": "connection_suggestions", "kind": "connection_suggestions", "severity": "info",
                 "title": "2 connection suggestions", "detail": "Review in Lab", "since": None,
                 "affected": [], "root_ip": None, "link": {"page": "lab", "subview": "connections", "params": {}}},
            ]},
        "/api/heartbeat": {"generated": "", "bucket_seconds": 1800, "start": 0, "hosts": {
            "10.0.0.2": [1] * 48, "10.0.0.3": [1] * 47 + [2],
            "10.0.0.4": [1] * 40 + [0] * 8, "10.0.0.5": [None] * 30 + [1] * 18}},
        "/api/power": {"configured": True, "live": {"watts": 178.4},
                       "history": [{"watts": 170}, {"watts": 180}, {"watts": 184}, {"watts": None}]},
        "/api/proxmox": {"configured": True, "reachable": True,
                         "nodes": [{"name": "pve", "cpu_percent": 12.0, "status": "online", "guests": []}]},
        "/api/nas": {"configured": True, "reachable": True, "pools": [
            {"name": "tank", "status": "ONLINE", "capacity_used_bytes": 61, "capacity_total_bytes": 100}]},
        "/api/ups": {"configured": True, "live": {"status": "OL CHRG", "charge_percent": 100}},
        "/api/brief": {"briefs": [{"created_ts": now - 7200, "subject": "Quiet night, one slow backup",
                                   "narrative": "All hosts held above 99.9% overnight."}]},
        "/api/inventory": {"items": [{"device_type": "host"}, {"device_type": "vm"}, {"device_type": "vm"}]},
        "/api/discovery/status": {"configured": True, "port_maps": [{"device_id": 1, "name": "USW"}]},
        # the stub resolves any /api/ports/<id> through this prefix key
        "/api/ports/": {"live": True, "ports": [{"name": "Port 1", "up": True, "occupants": []},
                                                {"name": "Port 2", "up": False, "occupants": []}]},
    })
    return fx
