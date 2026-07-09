import curses
import time
from datetime import datetime

from netwatch import BRAND, VERSION


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

