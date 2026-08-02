"""Entry point: argument parsing, dashboard HTML loading, and process wiring.
`main()` loads hosts.yaml, starts HistoryDB/prune/flush threads, starts the
NAS/Proxmox/HA/PBS pollers conditionally, starts the web server in a thread,
then runs either the curses TUI or a headless sleep loop."""

import os
import sys
import time
import threading
import curses
import argparse
import logging

from netwatch import VERSION
from netwatch.auth import AuthManager
from netwatch.storage import HistoryDB, InventoryDB, QuickLinksDB, _flush_loop, _prune_loop, restore_backup
from netwatch.hosts import HostManager, IncidentLog, load_yaml
from netwatch.pollers import NASPoller, ProxmoxPoller, PBSPoller, HAPoller, UPSPoller
from netwatch.server import start_web_server
from netwatch.tui import draw_tui

# monitor.py, hosts.yaml, auth.json, netwatch.db, monitor.log, and
# dashboard.html all live at the repo root, one directory above this
# package. `__file__` here points inside netwatch/, so - unlike the old
# monitor.py, where `__file__` *was* the repo root - path resolution needs
# an extra `dirname()` hop to land in the right place.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ============================================================================
# Dashboard HTML (loaded from dashboard.html at startup)
# ============================================================================

def _load_dashboard_html(base_dir):
    path = os.path.join(base_dir, "dashboard.html")
    with open(path, encoding="utf-8") as f:
        # {{VERSION}} markers cache-bust the /static/ asset URLs on upgrades
        return f.read().replace("{{VERSION}}", VERSION)


# ============================================================================
# Entry point
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Netwatch - homelab ping monitor")
    parser.add_argument("--config", default="hosts.yaml")
    parser.add_argument("--no-tui", action="store_true", help="Headless / systemd mode")
    parser.add_argument("--no-web", action="store_true", help="Disable web dashboard")
    parser.add_argument("--port",   type=int, default=8080, help="Web server port")
    parser.add_argument("--log",    default="monitor.log")
    parser.add_argument("--restore", metavar="TARBALL",
                         help="Restore hosts.yaml/auth.json/netwatch.db from a backup tarball, then exit")
    parser.add_argument("--force", action="store_true",
                         help="With --restore, overwrite existing hosts.yaml/auth.json/netwatch.db")
    args = parser.parse_args()

    if args.restore:
        config_path = os.path.join(_REPO_ROOT, args.config)
        ok, message = restore_backup(args.restore, config_path, force=args.force)
        print(message)
        sys.exit(0 if ok else 1)

    from logging.handlers import RotatingFileHandler
    _log_handler = RotatingFileHandler(args.log, maxBytes=10 * 1024 * 1024, backupCount=3)
    _log_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logging.basicConfig(level=logging.INFO, handlers=[_log_handler])

    config_path = os.path.join(_REPO_ROOT, args.config)
    base_dir = _REPO_ROOT
    dashboard_html = _load_dashboard_html(base_dir)
    config   = load_yaml(config_path)
    settings = config.get("settings", {})
    default_interval = settings.get("default_interval", 30)
    ping_timeout     = settings.get("ping_timeout", 2)
    history_window   = settings.get("history_window", 100)
    refresh_rate     = settings.get("refresh_rate", 5)
    # Make sure default_interval is always present on `settings` itself - the
    # rest of this function passes this exact dict object (not a copy) to
    # HostManager, NASPoller, ProxmoxPoller, and the web server, so that
    # POST /api/settings's in-place mutations are visible to all of them
    # immediately, without a restart.
    settings["default_interval"] = default_interval

    stop_event = threading.Event()

    # Authentication
    config_dir = os.path.dirname(os.path.abspath(config_path))
    auth_path = os.path.join(config_dir, "auth.json")
    db_path = os.path.join(config_dir, "netwatch.db")
    auth_manager = AuthManager(auth_path, db_path=db_path)
    settings["secret_key"] = auth_manager.data["secret_key"]
    if auth_manager.has_users:
        print(f"[netwatch] Auth enabled - {len(auth_manager.list_users())} user(s) configured")
    else:
        print(f"[netwatch] Auth NOT YET CONFIGURED - visit the dashboard to set up the first admin user")

    # SQLite-backed history (persists pings & incidents across restarts)
    retention_days = int(settings.get("history_days", 30))
    history_db = HistoryDB(db_path, retention_days=retention_days)
    print(f"[netwatch] History DB -> {db_path} (retention {retention_days} days)")
    inventory_db = InventoryDB(history_db)
    inv_count = len(inventory_db.list_all())
    print(f"[netwatch] Inventory  -> {inv_count} record(s)")
    quicklinks_db = QuickLinksDB(history_db)

    # Daily prune task
    pt = threading.Thread(target=_prune_loop, args=(history_db, stop_event), daemon=True, name="prune")
    pt.start()

    # Ping flush task (batched inserts land every 30s)
    ft = threading.Thread(target=_flush_loop, args=(history_db, stop_event), daemon=True, name="ping-flush")
    ft.start()

    # systemd sends SIGTERM on stop; route it through the KeyboardInterrupt
    # path so buffered pings get flushed and the WAL is checkpointed.
    import signal
    def _sigterm(_sig, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _sigterm)

    incident_log = IncidentLog(history_db=history_db)
    host_manager = HostManager(
        config_path, ping_timeout, history_window, stop_event,
        incident_log, history_db,
        alert_settings=settings, alert_port=args.port,
    )
    host_manager.load_initial(config.get("hosts", []), default_interval)

    nas_poller = NASPoller(auth_manager, alert_settings=settings, alert_port=args.port)
    _nas_url, _ = nas_poller._get_config()
    if _nas_url:
        nas_poller.start(stop_event)
        print(f"[netwatch] NAS poller -> polling TrueNAS every {NASPoller.POLL_INTERVAL_SECONDS}s")

    proxmox_poller = ProxmoxPoller(auth_manager, alert_settings=settings, alert_port=args.port)
    _pve_url, _, _, _ = proxmox_poller._get_config()
    if _pve_url:
        proxmox_poller.start(stop_event)
        print(f"[netwatch] Proxmox poller -> polling every {ProxmoxPoller.POLL_INTERVAL_SECONDS}s")

    ha_poller = None
    _ha_url = (auth_manager.data if auth_manager else {}).get("ha_url", "")
    if _ha_url:
        ha_poller = HAPoller(auth_manager, history_db)
        ha_poller.start(stop_event)
        print(f"[netwatch] HA poller -> polling Home Assistant every {HAPoller.POLL_INTERVAL_SECONDS}s")

    pbs_poller = PBSPoller(auth_manager, alert_settings=settings, alert_port=args.port, proxmox_poller=proxmox_poller)
    _pbs_url, _, _ = pbs_poller._get_config()
    if _pbs_url:
        pbs_poller.start(stop_event)
        print(f"[netwatch] PBS poller -> polling every {PBSPoller.POLL_INTERVAL_SECONDS}s")

    ups_poller = UPSPoller(auth_manager, alert_settings=settings, alert_port=args.port)
    _nut_server, _, _nut_ups_name, _, _ = ups_poller._get_config()
    if _nut_server and _nut_ups_name:
        ups_poller.start(stop_event)
        print(f"[netwatch] UPS poller -> polling NUT every {UPSPoller.POLL_INTERVAL_SECONDS}s")

    if not args.no_web:
        wt = threading.Thread(
            target=start_web_server,
            args=(host_manager, settings, config_path, args.port, stop_event, incident_log, auth_manager, inventory_db, dashboard_html, history_db),
            kwargs={"nas_poller": nas_poller, "proxmox_poller": proxmox_poller, "ha_poller": ha_poller, "pbs_poller": pbs_poller, "ups_poller": ups_poller, "static_dir": os.path.join(base_dir, "static"), "quicklinks_db": quicklinks_db},
            daemon=True
        )
        wt.start()
        print(f"[netwatch] Dashboard -> http://0.0.0.0:{args.port}")

    try:
        if args.no_tui:
            print(f"[netwatch] Monitoring {len(host_manager.list_hosts())} hosts")
            print("[netwatch] Headless mode. Ctrl+C to stop.")
            try:
                while True: time.sleep(1)
            except KeyboardInterrupt:
                stop_event.set()
        else:
            try:
                curses.wrapper(draw_tui, host_manager, refresh_rate, args.port, stop_event)
            except KeyboardInterrupt:
                pass
            except (curses.error, ValueError):
                print("\n[netwatch] Terminal doesn't support the TUI's required features. Try --no-tui.")
                sys.exit(1)
            finally:
                stop_event.set()
                print("\n[netwatch] Stopped.")
    finally:
        auth_manager.close()
        history_db.close()


if __name__ == "__main__":
    main()
