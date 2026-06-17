#!/usr/bin/env python3
"""One-off script to fire every ntfy alert type Netwatch can send, using
its real title/message/priority/tags, so you can see how each looks on
your ntfy client. Reads ntfy_topic/ntfy_server from hosts.yaml's
settings: block (same place monitor.py reads it from).

Usage: python3 scripts/simulate_ntfy_alerts.py
"""
import os
import sys
import time

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from monitor import send_ntfy_alert  # noqa: E402

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hosts.yaml")

ALERTS = [
    dict(title="Host down: nas-01",
         message="nas-01 (192.168.6.50) failed 3 consecutive pings.\nGroup: Homelab",
         priority="high", tags="warning,red_circle"),
    dict(title="Recovered: nas-01",
         message="nas-01 (192.168.6.50) is back online.\nGroup: Homelab",
         priority="default", tags="white_check_mark,green_circle"),
    dict(title="Netwatch · NAS Alert",
         message='Pool "tank" is DEGRADED',
         priority="high", tags="warning"),
    dict(title="Netwatch · NAS Alert",
         message='Scrub on "tank" found 2 error(s)',
         priority="high", tags="warning"),
    dict(title="Netwatch · NAS Alert",
         message='Replication "tank-offsite" failed',
         priority="high", tags="warning"),
    dict(title="Netwatch · Proxmox Alert",
         message='Proxmox node "pve1" is offline',
         priority="high", tags="rotating_light"),
    dict(title="Netwatch · Proxmox Alert",
         message='VM "web-server-01" (101) stopped unexpectedly on pve1',
         priority="high", tags="rotating_light"),
    dict(title="Netwatch · Proxmox Alert",
         message='VM "web-server-01" (101) is paused on pve1',
         priority="high", tags="rotating_light"),
]


def main():
    try:
        with open(CONFIG_PATH) as f:
            config = yaml.safe_load(f) or {}
        settings = config.get("settings", {})
    except FileNotFoundError:
        settings = {}

    if not settings.get("ntfy_topic"):
        print("No ntfy_topic configured in hosts.yaml's settings: block — nothing will actually send.")
        print("Set ntfy_topic (and optionally ntfy_server) via the Settings panel first.\n")

    for i, alert in enumerate(ALERTS, 1):
        print(f"[{i}/{len(ALERTS)}] {alert['title']}: {alert['message']!r} "
              f"(priority={alert['priority']}, tags={alert['tags']})")
        ok = send_ntfy_alert(settings, alert["title"], alert["message"],
                              priority=alert["priority"], tags=alert["tags"])
        print("  -> sent" if ok else "  -> not sent")
        if i < len(ALERTS):
            time.sleep(1)


if __name__ == "__main__":
    main()
