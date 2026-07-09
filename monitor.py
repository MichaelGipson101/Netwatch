#!/usr/bin/env python3
"""
netwatch - Homelab ping monitor with btop-style TUI, web dashboard,
and web-based hosts.yaml editor. (Version: see VERSION below.)

Usage:
    python monitor.py                  # TUI + web server
    python monitor.py --no-tui         # Headless + web server (for systemd)
    python monitor.py --no-web         # TUI only, no web
    python monitor.py --port 8080      # Custom port

Config: hosts.yaml in the same directory.
Dashboard: http://<pi-ip>:8080
"""

import os, sys, time, json, re, shutil, subprocess, threading, curses, argparse, logging
from datetime import datetime, timezone
from collections import deque
from dataclasses import dataclass, field
from typing import Optional
from netwatch import BRAND, VERSION
from netwatch.auth import AuthManager
from netwatch.storage import (
    _column_exists, HistoryDB, InventoryDB, _flush_loop, _prune_loop,
    export_inventory_to_xlsx, import_inventory_from_xlsx,
    BACKUP_MANIFEST_VERSION, create_backup_tarball, restore_backup,
)
from netwatch.network import (
    _normalise_mac, _save_detected_mac, _get_dashboard_url,
    send_ntfy_alert, _send_alert_async, _detect_broadcast_address, _get_wol_broadcast,
    _get_pi_local_ips, _is_local_ip,
    _check_nmap_available, _detect_subnet_cidr, _run_nmap_scan,
    _ARP_SAVED_THIS_SESSION, NTFY_DOWN_THRESHOLD,
)
from netwatch.hosts import (
    HostState, check_tcp_service, ping_host, _should_log_transition, poll_host,
    HostManager, IncidentLog, load_yaml, _validate_ip_or_hostname,
)
from netwatch.pollers import NASPoller, ProxmoxPoller, PBSPoller, HAPoller
from netwatch.server import make_handler, start_web_server


# ============================================================================
# Dashboard HTML (loaded from dashboard.html at startup)
# ============================================================================

def _load_dashboard_html(base_dir):
    path = os.path.join(base_dir, "dashboard.html")
    with open(path, encoding="utf-8") as f:
        # {{VERSION}} markers cache-bust the /static/ asset URLs on upgrades
        return f.read().replace("{{VERSION}}", VERSION)



# ============================================================================
# TUI
# ============================================================================

def safe_addstr(win, y, x, text, attr=0):
    try:
        max_y, max_x = win.getmaxyx()
        if y < 0 or y >= max_y or x < 0 or x >= max_x: return
        allowed = max_x - x - 1
        if allowed <= 0: return
        win.addstr(y, x, text[:allowed], attr)
    except curses.error:
        pass


def clamp(text, width):
    if len(text) >= width:
        return text[:width - 1] + "\u2026"
    return text


def draw_hline(win, y, x, width, char="\u2500", attr=0):
    safe_addstr(win, y, x, char * width, attr)


def draw_box(win, y, x, h, w, attr=0):
    if w < 2 or h < 2: return
    safe_addstr(win, y,         x,         "\u256d", attr)
    safe_addstr(win, y,         x + w - 1, "\u256e", attr)
    safe_addstr(win, y + h - 1, x,         "\u2570", attr)
    safe_addstr(win, y + h - 1, x + w - 1, "\u256f", attr)
    for i in range(1, w - 1):
        safe_addstr(win, y,         x + i, "\u2500", attr)
        safe_addstr(win, y + h - 1, x + i, "\u2500", attr)
    for i in range(1, h - 1):
        safe_addstr(win, y + i, x,         "\u2502", attr)
        safe_addstr(win, y + i, x + w - 1, "\u2502", attr)


def uptime_bar(pct, width=10):
    if pct is None:
        return "\u2500" * width
    filled = int(round(pct / 100 * width))
    return "\u2588" * filled + "\u2591" * (width - filled)


def _init_tui_colors():
    """Initialize curses color pairs for the TUI, with graceful fallback.

    Tries the full 256-color palette first; falls back to an 8-color-safe
    palette if the terminal can't do 256 colors; falls back further to an
    all-default (monochrome) attribute set if even basic color pairs fail.
    Never raises - draw_tui can always render *something* in this terminal.
    """
    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, 82,  -1)
        curses.init_pair(2, 196, -1)
        curses.init_pair(3, 214, -1)
        curses.init_pair(4, 39,  -1)
        curses.init_pair(5, 243, -1)
        curses.init_pair(6, 255, -1)
        curses.init_pair(7, 208, -1)
        curses.init_pair(8, 51,  -1)
        return {
            "C_UP":    curses.color_pair(1) | curses.A_BOLD,
            "C_DOWN":  curses.color_pair(2) | curses.A_BOLD,
            "C_WAIT":  curses.color_pair(3) | curses.A_BOLD,
            "C_HDR":   curses.color_pair(4),
            "C_HDRB":  curses.color_pair(4) | curses.A_BOLD,
            "C_MUTED": curses.color_pair(5),
            "C_TEXT":  curses.color_pair(6),
            "C_LATC":  curses.color_pair(7),
            "C_SPARK": curses.color_pair(8),
        }
    except (curses.error, ValueError):
        pass

    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN,  -1)
        curses.init_pair(2, curses.COLOR_RED,    -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_CYAN,   -1)
        curses.init_pair(5, curses.COLOR_WHITE,  -1)
        return {
            "C_UP":    curses.color_pair(1) | curses.A_BOLD,
            "C_DOWN":  curses.color_pair(2) | curses.A_BOLD,
            "C_WAIT":  curses.color_pair(3) | curses.A_BOLD,
            "C_HDR":   curses.color_pair(4),
            "C_HDRB":  curses.color_pair(4) | curses.A_BOLD,
            "C_MUTED": curses.color_pair(5),
            "C_TEXT":  0,
            "C_LATC":  curses.color_pair(3),
            "C_SPARK": curses.color_pair(4),
        }
    except (curses.error, ValueError):
        pass

    return {
        "C_UP": 0, "C_DOWN": 0, "C_WAIT": 0, "C_HDR": 0,
        "C_HDRB": curses.A_BOLD, "C_MUTED": 0, "C_TEXT": 0,
        "C_LATC": 0, "C_SPARK": 0,
    }


def _tui_status_role(pend, is_up, status):
    """Pure decision logic for which color role applies to a host's TUI row.

    Decoupled from curses attr objects so this is testable without a real
    screen. status == "DEGRADED" implies is_up is True (see Host.status_str),
    so this must be checked before the generic "up" branch - that ordering
    is the actual bug fix this function exists for: a naive is_up check
    alone misclassifies a degraded host as healthy.
    """
    if pend:
        return "wait"
    if status == "DEGRADED":
        return "degraded"
    if is_up:
        return "up"
    if status == "IDLE":
        return "idle"
    return "down"


def draw_tui(stdscr, host_manager, refresh_rate, port, stop_event):
    colors  = _init_tui_colors()
    C_UP    = colors["C_UP"]
    C_DOWN  = colors["C_DOWN"]
    C_WAIT  = colors["C_WAIT"]
    C_HDR   = colors["C_HDR"]
    C_HDRB  = colors["C_HDRB"]
    C_MUTED = colors["C_MUTED"]
    C_TEXT  = colors["C_TEXT"]
    C_LATC  = colors["C_LATC"]
    C_SPARK = colors["C_SPARK"]
    ROLE_ATTRS = {
        "wait":     (C_WAIT,  C_WAIT,  C_TEXT),
        "degraded": (C_WAIT,  C_WAIT,  C_TEXT),
        "up":       (C_UP,    C_UP,    C_TEXT),
        "idle":     (C_MUTED, C_MUTED, C_MUTED),
        "down":     (C_DOWN,  C_DOWN,  C_DOWN),
    }
    curses.curs_set(0)
    stdscr.nodelay(True)
    COL_IND, COL_NAME, COL_IP, COL_STAT, COL_LAT, COL_BAR, COL_PCT, COL_SPK, COL_CHK = 1, 4, 24, 42, 50, 62, 74, 82, 105

    while not stop_event.is_set():
        stdscr.erase()
        max_y, max_x = stdscr.getmaxyx()
        now_str = datetime.now().strftime("%a %Y-%m-%d  %H:%M:%S")
        hosts = host_manager.list_hosts()
        up_hosts = [h for h in hosts if h.is_up]
        dn_hosts = [h for h in hosts if not h.is_up and h.last_checked]
        latencies = [h.last_latency_ms for h in up_hosts if h.last_latency_ms]
        avg_lat = (sum(latencies) / len(latencies)) if latencies else None
        uptimes = [h.uptime_pct for h in hosts if h.uptime_pct is not None]
        avg_up = (sum(uptimes) / len(uptimes)) if uptimes else None

        row = 0
        safe_addstr(stdscr, row, 1, BRAND, C_HDRB)
        safe_addstr(stdscr, row, 1 + len(BRAND), f"  v{VERSION} \u00b7 homelab monitor", C_MUTED)
        url_str = f"http://0.0.0.0:{port}"
        safe_addstr(stdscr, row, max(0, max_x - len(now_str) - len(url_str) - 4), url_str, C_HDR)
        safe_addstr(stdscr, row, max(0, max_x - len(now_str) - 1), now_str, C_MUTED)
        row += 1
        draw_hline(stdscr, row, 0, max_x, "\u2500", C_MUTED)
        row += 1

        if max_x >= 60:
            num_cards = 5
            card_w = max_x // num_cards
            cards = [
                ("hosts up",    f"{len(up_hosts)} / {len(hosts)}",          C_UP),
                ("hosts down",  str(len(dn_hosts)),                          C_DOWN if dn_hosts else C_MUTED),
                ("avg latency", f"{avg_lat:.1f} ms" if avg_lat else "- ms",  C_LATC),
                ("avg uptime",  f"{avg_up:.1f}%" if avg_up else "-%",        C_UP if avg_up and avg_up >= 90 else C_WAIT),
                ("dashboard",   f":{port}",                                   C_HDR),
            ]
            for i, (label, val, color) in enumerate(cards):
                cx = i * card_w
                bw = card_w if i < num_cards - 1 else max_x - cx
                draw_box(stdscr, row, cx, 4, bw, C_MUTED)
                safe_addstr(stdscr, row + 1, cx + 2, label.upper(), C_MUTED)
                safe_addstr(stdscr, row + 2, cx + 2, val, color)
            row += 4

        draw_hline(stdscr, row, 0, max_x, "\u2500", C_MUTED)
        row += 1
        safe_addstr(stdscr, row, COL_IND,  "\u25cf",         C_MUTED)
        safe_addstr(stdscr, row, COL_NAME, "HOST",           C_HDR)
        safe_addstr(stdscr, row, COL_IP,   "IP ADDRESS",     C_HDR)
        safe_addstr(stdscr, row, COL_STAT, "STATUS",         C_HDR)
        safe_addstr(stdscr, row, COL_LAT,  "LATENCY",        C_HDR)
        safe_addstr(stdscr, row, COL_BAR,  "UPTIME",         C_HDR)
        safe_addstr(stdscr, row, COL_PCT,  "%",              C_HDR)
        if max_x > COL_SPK + 10:
            safe_addstr(stdscr, row, COL_SPK, "HISTORY",     C_HDR)
        if max_x > COL_CHK + 8:
            safe_addstr(stdscr, row, COL_CHK, "LAST PING",   C_HDR)
        row += 1
        draw_hline(stdscr, row, 0, max_x, "\u2500", C_MUTED)
        row += 1

        groups = {}
        for h in hosts:
            groups.setdefault(h.group, []).append(h)

        for group_name, group_hosts in groups.items():
            if row >= max_y - 2: break
            prefix = "\u2500\u2500 "
            suffix_len = max(0, max_x - len(prefix) - len(group_name) - 4)
            safe_addstr(stdscr, row, 0, prefix, C_MUTED)
            safe_addstr(stdscr, row, len(prefix), f" {group_name.upper()} ", C_HDRB)
            safe_addstr(stdscr, row, len(prefix) + len(group_name) + 2, " " + "\u2500" * suffix_len, C_MUTED)
            row += 1
            for host in group_hosts:
                if row >= max_y - 2: break
                with host.lock:
                    is_up   = host.is_up
                    pend    = host.last_checked is None
                    name    = clamp(host.name, 18)
                    ip      = host.ip
                    status  = host.status_str
                    latency = host.latency_str
                    upct    = host.uptime_pct
                    uptime  = host.uptime_str
                    spark   = host.spark_str(20)
                    checked = host.checked_str
                role = _tui_status_role(pend, is_up, status)
                ind_attr, st_attr, nm_attr = ROLE_ATTRS[role]
                bar_str = uptime_bar(upct, 10)
                if upct is None:
                    bar_attr = C_MUTED
                elif upct >= 95:
                    bar_attr = C_UP
                elif upct >= 80:
                    bar_attr = C_WAIT
                else:
                    bar_attr = C_DOWN
                safe_addstr(stdscr, row, COL_IND,  "\u25cf",         ind_attr)
                safe_addstr(stdscr, row, COL_NAME, f"{name:<18}",    nm_attr)
                safe_addstr(stdscr, row, COL_IP,   f"{ip:<16}",      C_MUTED)
                safe_addstr(stdscr, row, COL_STAT, f"{status:<9}",   st_attr)
                safe_addstr(stdscr, row, COL_LAT,  f" {latency:<10}", C_LATC if is_up else C_MUTED)
                safe_addstr(stdscr, row, COL_BAR,  bar_str,          bar_attr)
                safe_addstr(stdscr, row, COL_PCT,  f" {uptime:<6}",  bar_attr)
                if max_x > COL_SPK + 10:
                    for i, ch in enumerate(spark):
                        if COL_SPK + i >= max_x - 1: break
                        sp_color = C_SPARK if ch == "\u2588" else (C_DOWN if ch == "\u2581" else C_MUTED)
                        safe_addstr(stdscr, row, COL_SPK + i, ch, sp_color)
                if max_x > COL_CHK + 8:
                    safe_addstr(stdscr, row, COL_CHK, checked, C_MUTED)
                row += 1

        if max_y >= 3:
            draw_hline(stdscr, max_y - 2, 0, max_x, "\u2500", C_MUTED)
            keys = " [q] quit  [r] refresh "
            info = f" dashboard: http://0.0.0.0:{port}  \u00b7  {len(hosts)} hosts  \u00b7  {refresh_rate}s interval "
            safe_addstr(stdscr, max_y - 1, 0, keys, C_MUTED)
            safe_addstr(stdscr, max_y - 1, max(0, max_x - len(info) - 1), info, C_MUTED)

        stdscr.refresh()
        for _ in range(refresh_rate * 10):
            key = stdscr.getch()
            if key in (ord('q'), ord('Q')):
                stop_event.set()
                return
            if key in (ord('r'), ord('R')):
                break
            time.sleep(0.1)


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
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.config)
        ok, message = restore_backup(args.restore, config_path, force=args.force)
        print(message)
        sys.exit(0 if ok else 1)

    from logging.handlers import RotatingFileHandler
    _log_handler = RotatingFileHandler(args.log, maxBytes=10 * 1024 * 1024, backupCount=3)
    _log_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logging.basicConfig(level=logging.INFO, handlers=[_log_handler])

    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.config)
    base_dir = os.path.dirname(os.path.abspath(__file__))
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

    if not args.no_web:
        wt = threading.Thread(
            target=start_web_server,
            args=(host_manager, settings, config_path, args.port, stop_event, incident_log, auth_manager, inventory_db, dashboard_html, history_db),
            kwargs={"nas_poller": nas_poller, "proxmox_poller": proxmox_poller, "ha_poller": ha_poller, "pbs_poller": pbs_poller},
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
