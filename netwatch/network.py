import os
import time
import logging
import threading
import subprocess


# ============================================================================
# ARP-based MAC detection
# ============================================================================

# Lock for serializing writes to hosts.yaml from the ping thread.
# Uses the same lock the host_manager uses for config reloads, but we need
# our own here since this module-level helper can't reach into HostManager.
_ARP_WRITE_LOCK = threading.Lock()

# Track which IPs we've already auto-saved a MAC for in this session.
# Prevents rewriting hosts.yaml on every successful ping.
_ARP_SAVED_THIS_SESSION = set()


def _detect_mac_for_ip(ip):
    """Read /proc/net/arp and return the MAC for ip, or None if not present
    or not yet resolved.

    Format is 6 fixed-position columns:
        IP address       HW type     Flags       HW address            Mask     Device

    Flags 0x2 (ATF_COM) means the entry is complete & resolved. Other values
    mean it's incomplete (no response yet) or has an error.
    """
    try:
        with open("/proc/net/arp") as f:
            lines = f.readlines()
    except OSError:
        return None
    # Skip header line
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        arp_ip, hw_type, flags, mac = parts[0], parts[1], parts[2], parts[3]
        if arp_ip != ip:
            continue
        # Only return resolved entries
        try:
            flag_int = int(flags, 16)
        except ValueError:
            continue
        if not (flag_int & 0x2):
            continue
        if mac == "00:00:00:00:00:00" or not mac:
            continue
        return mac.lower()
    return None


def _normalise_mac(s):
    """Normalise a MAC string for comparison. Returns lowercase colon form."""
    if not s:
        return ""
    clean = "".join(c for c in str(s).lower() if c in "0123456789abcdef")
    if len(clean) != 12:
        return str(s).lower().strip()
    return ":".join(clean[i:i+2] for i in range(0, 12, 2))


def _save_detected_mac(config_path, host_ip, detected_mac):
    """Write back to hosts.yaml: set specs.mac and specs.mac_auto for the
    host with the given IP. Atomic via temp-file-then-rename. Returns True
    on success.

    Race-aware: we re-read the config under our write lock so we don't
    clobber concurrent edits from POST /api/hosts. The HTTP handler also
    uses load_yaml + save under its own lock, so we coordinate through
    the file itself - last writer wins, but neither corrupts.
    """
    with _ARP_WRITE_LOCK:
        try:
            # Deferred import: netwatch/hosts.py imports several helpers from
            # this module (netwatch.network) at module level, so a
            # module-level import in this direction would be a circular
            # import. Importing lazily inside the function instead is safe -
            # this only runs long after both modules have finished loading.
            from netwatch.hosts import load_yaml
            cfg = load_yaml(config_path) or {}
        except Exception as e:
            logging.warning(f"ARP detect: could not read {config_path}: {e}")
            return False
        hosts = cfg.get("hosts", []) or []
        changed = False
        for h in hosts:
            if not isinstance(h, dict):
                continue
            if h.get("ip") != host_ip:
                continue
            specs = h.get("specs")
            if not isinstance(specs, dict):
                specs = {}
                h["specs"] = specs
            existing_mac = specs.get("mac")
            if existing_mac:
                # Don't overwrite a user-set MAC. Caller already decided
                # whether to call us; this is just defensive.
                return False
            specs["mac"] = detected_mac
            specs["mac_auto"] = True
            changed = True
            break
        if not changed:
            return False
        # Atomic write
        tmp = config_path + ".tmp"
        try:
            import yaml
            with open(tmp, "w") as f:
                yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
            os.chmod(tmp, 0o600)  # hosts.yaml carries the OpenRouter key + ntfy topic
            os.replace(tmp, config_path)
            logging.info(f"ARP detect: saved MAC {detected_mac} for {host_ip}")
            return True
        except Exception as e:
            try: os.unlink(tmp)
            except OSError: pass
            logging.warning(f"ARP detect: could not write {config_path}: {e}")
            return False


# ============================================================================
# Alerts (ntfy)
# ============================================================================

NTFY_DEFAULT_SERVER = "https://ntfy.sh"
NTFY_DOWN_THRESHOLD = 3

_DASHBOARD_URL_CACHE = None


def _get_dashboard_url(settings, port):
    """Return clickable dashboard URL for alert click actions."""
    global _DASHBOARD_URL_CACHE
    explicit = (settings or {}).get("dashboard_url")
    if explicit:
        return str(explicit).rstrip("/")
    if _DASHBOARD_URL_CACHE is not None:
        return _DASHBOARD_URL_CACHE if _DASHBOARD_URL_CACHE else None
    import subprocess
    try:
        result = subprocess.run(
            ["ip", "-o", "route", "get", "1.1.1.1"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            parts = result.stdout.split()
            if "src" in parts:
                src_ip = parts[parts.index("src") + 1]
                _DASHBOARD_URL_CACHE = f"http://{src_ip}:{port}"
                return _DASHBOARD_URL_CACHE
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        pass
    _DASHBOARD_URL_CACHE = ""
    return None


def send_ntfy_alert(settings, title, message, priority="default",
                    tags=None, click_url=None, actions=None):
    """Send a ntfy notification. Returns True on success."""
    topic = (settings or {}).get("ntfy_topic")
    if not topic:
        return False
    server = (settings or {}).get("ntfy_server") or NTFY_DEFAULT_SERVER
    server = str(server).rstrip("/")
    url = f"{server}/{topic}"

    import urllib.request
    import urllib.error
    headers = {"Title": title, "Priority": priority}
    if tags: headers["Tags"] = tags
    if click_url: headers["Click"] = click_url
    if actions: headers["Actions"] = actions

    data = (message or "").encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            ok = 200 <= resp.status < 300
            if ok:
                logging.info(f"ntfy: sent alert '{title}' to {topic}")
            else:
                logging.warning(f"ntfy: server returned {resp.status} for '{title}'")
            return ok
    except urllib.error.URLError as e:
        logging.warning(f"ntfy: could not deliver '{title}': {e}")
        return False
    except Exception as e:
        logging.warning(f"ntfy: unexpected error sending '{title}': {e}")
        return False


def _send_alert_async(settings, title, message, priority, tags, click_url,
                      on_success=None, actions=None):
    """Fire-and-forget wrapper. on_success runs only if delivery succeeds."""
    def _worker():
        ok = send_ntfy_alert(settings, title, message, priority=priority,
                             tags=tags, click_url=click_url, actions=actions)
        if ok and on_success:
            try: on_success()
            except Exception as e:
                logging.warning(f"ntfy: on_success callback failed: {e}")
    threading.Thread(target=_worker, daemon=True, name="ntfy-alert").start()


# ============================================================================
# Wake-on-LAN
# ============================================================================

def _detect_broadcast_address():
    """Detect the directed broadcast address for the Pi's primary interface.

    Works for any subnet size (/24, /22, /16, etc.) by reading the actual
    netmask from `ip -o -f inet addr show`. Falls back to 255.255.255.255
    if detection fails for any reason.

    Returns (broadcast_str, source_str). source_str is one of:
      "auto:<iface>"  - successfully detected from a real interface
      "fallback"      - using 255.255.255.255
    """
    import subprocess
    import ipaddress
    try:
        # Step 1: find which interface the Pi uses to reach the LAN.
        # `ip route get` returns something like:
        #   1.1.1.1 via 192.168.1.1 dev eth0 src 192.168.1.42 ...
        result = subprocess.run(
            ["ip", "-o", "route", "get", "1.1.1.1"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            return "255.255.255.255", "fallback"
        parts = result.stdout.split()
        # Find the "dev" token to get the interface name
        if "dev" not in parts:
            return "255.255.255.255", "fallback"
        iface = parts[parts.index("dev") + 1]

        # Step 2: get the CIDR for that interface.
        # `ip -o -f inet addr show eth0` returns lines like:
        #   3: eth0    inet 192.168.1.42/22 brd 192.168.3.255 scope global ...
        # The 'brd' token already gives us the broadcast - prefer it.
        result = subprocess.run(
            ["ip", "-o", "-f", "inet", "addr", "show", iface],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return "255.255.255.255", "fallback"

        line_parts = result.stdout.split()
        # Try the 'brd' field first (most reliable)
        if "brd" in line_parts:
            bcast = line_parts[line_parts.index("brd") + 1]
            return bcast, f"auto:{iface}"

        # Otherwise compute from the inet x.x.x.x/NN field
        if "inet" in line_parts:
            cidr = line_parts[line_parts.index("inet") + 1]
            net = ipaddress.IPv4Network(cidr, strict=False)
            return str(net.broadcast_address), f"auto:{iface}"

        return "255.255.255.255", "fallback"
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError, OSError):
        return "255.255.255.255", "fallback"


# Cache the detection result so we log once and don't re-run `ip` per packet.
_WOL_BROADCAST_CACHE = None

def _get_wol_broadcast():
    global _WOL_BROADCAST_CACHE
    if _WOL_BROADCAST_CACHE is None:
        bcast, source = _detect_broadcast_address()
        _WOL_BROADCAST_CACHE = bcast
        if source.startswith("auto:"):
            iface = source.split(":", 1)[1]
            logging.info(f"WoL: directed broadcast detected as {bcast} (via {iface})")
        else:
            logging.warning(
                f"WoL: could not detect subnet broadcast, falling back to {bcast}. "
                "Wake-on-LAN may not reach hosts on a /22 or larger subnet."
            )
    return _WOL_BROADCAST_CACHE


def send_wol_packet(mac_address):
    """Send a Wake-on-LAN magic packet to the given MAC. Returns (ok, msg).

    Uses the auto-detected directed broadcast address for the Pi's subnet,
    which works correctly for /22, /16, etc. - not just /24.
    """
    import socket
    mac = mac_address.replace(":", "").replace("-", "").lower()
    if len(mac) != 12 or not all(c in "0123456789abcdef" for c in mac):
        return False, "Invalid MAC address format"
    try:
        mac_bytes = bytes.fromhex(mac)
    except ValueError:
        return False, "Could not parse MAC address"
    # Magic packet: 6 bytes of 0xFF, followed by the MAC repeated 16 times.
    packet = b"\xff" * 6 + mac_bytes * 16
    bcast = _get_wol_broadcast()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        # Port 9 is the standard WoL discard port
        s.sendto(packet, (bcast, 9))
        s.close()
        logging.info(f"WoL: sent magic packet to {mac_address} via {bcast}")
        return True, None
    except OSError as e:
        return False, f"Network error: {e}"


# ============================================================================
# Pi self-monitoring
# ============================================================================

_PI_LOCAL_IPS_CACHE = None

def _get_pi_local_ips():
    """Return the set of local IPs this Pi has, used to identify which monitored
    host represents the Pi itself. Cached on first call."""
    global _PI_LOCAL_IPS_CACHE
    if _PI_LOCAL_IPS_CACHE is not None:
        return _PI_LOCAL_IPS_CACHE
    import subprocess
    ips = set()
    try:
        result = subprocess.run(
            ["ip", "-o", "-f", "inet", "addr"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                parts = line.split()
                if "inet" in parts:
                    cidr = parts[parts.index("inet") + 1]
                    ip = cidr.split("/")[0]
                    ips.add(ip)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    # Always include loopback so 127.0.0.1 in hosts.yaml also matches the Pi
    ips.add("127.0.0.1")
    _PI_LOCAL_IPS_CACHE = ips
    return ips


def _is_local_ip(ip):
    return ip in _get_pi_local_ips()


def read_pi_health():
    """Read current Pi system metrics. Returns a dict with whatever we could
    read; missing keys mean that metric is unavailable on this system."""
    health = {}

    # CPU temperature (Pi exposes this at /sys/class/thermal/thermal_zone0/temp
    # in millidegrees C). Fall back to None if unavailable.
    for path in ("/sys/class/thermal/thermal_zone0/temp",):
        try:
            with open(path) as f:
                health["cpu_temp_c"] = round(int(f.read().strip()) / 1000.0, 1)
                break
        except (OSError, ValueError):
            continue

    # Load average
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
            health["load_1m"]  = float(parts[0])
            health["load_5m"]  = float(parts[1])
            health["load_15m"] = float(parts[2])
    except (OSError, ValueError, IndexError):
        pass

    # CPU count for normalising load average
    try:
        health["cpu_count"] = os.cpu_count() or 1
    except Exception:
        pass

    # Memory (read /proc/meminfo)
    try:
        meminfo = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                meminfo[k.strip()] = v.strip()
        def _kb(k):
            v = meminfo.get(k, "")
            try:
                return int(v.split()[0]) * 1024
            except (ValueError, IndexError):
                return None
        total = _kb("MemTotal")
        avail = _kb("MemAvailable")
        if total and avail is not None:
            used = total - avail
            health["mem_total_bytes"] = total
            health["mem_used_bytes"]  = used
            health["mem_pct"] = round(used / total * 100, 1)
    except OSError:
        pass

    # Disk usage of /
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free  = st.f_bavail * st.f_frsize
        used  = total - free
        health["disk_total_bytes"] = total
        health["disk_used_bytes"]  = used
        health["disk_pct"] = round(used / total * 100, 1) if total else None
    except OSError:
        pass

    # System uptime
    try:
        with open("/proc/uptime") as f:
            health["uptime_seconds"] = int(float(f.read().split()[0]))
    except (OSError, ValueError):
        pass

    return health


# ============================================================================
# Network discovery (nmap-based)
# ============================================================================

# Cache the most recent scan result. Scans take a few seconds, so we run
# them in a background thread and let the client poll for results.
_DISCOVERY_LOCK = threading.Lock()
_DISCOVERY_STATE = {
    "running":   False,
    "started":   None,    # unix epoch
    "finished":  None,    # unix epoch
    "subnet":    None,
    "results":   [],
    "error":     None,
}


def _check_nmap_available():
    """Return (ok, path_or_error)."""
    try:
        r = subprocess.run(
            ["which", "nmap"], capture_output=True, text=True, timeout=2
        )
        if r.returncode == 0 and r.stdout.strip():
            return True, r.stdout.strip()
        return False, "nmap not installed (try: sudo apt-get install nmap)"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return False, f"could not check for nmap: {e}"


def _detect_subnet_cidr():
    """Detect the Pi's primary subnet in CIDR form, e.g. '192.168.0.0/22'.
    Reuses the same logic as _detect_broadcast_address."""
    import ipaddress
    try:
        # Find the outgoing interface
        result = subprocess.run(
            ["ip", "-o", "route", "get", "1.1.1.1"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            return None
        parts = result.stdout.split()
        if "dev" not in parts:
            return None
        iface = parts[parts.index("dev") + 1]

        # Get the CIDR for that interface
        result = subprocess.run(
            ["ip", "-o", "-f", "inet", "addr", "show", iface],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            return None
        line_parts = result.stdout.split()
        if "inet" not in line_parts:
            return None
        cidr = line_parts[line_parts.index("inet") + 1]
        net = ipaddress.IPv4Network(cidr, strict=False)
        return str(net)
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError, OSError):
        return None


def _run_nmap_scan(subnet):
    """Run `nmap -sn -oX -` against the subnet. Parses XML output and returns
    a list of {ip, mac, hostname, vendor} dicts. Returns (results, error)."""
    import xml.etree.ElementTree as ET

    try:
        # -sn = ping scan (no ports). -oX - = XML to stdout.
        # -T4 = aggressive timing for faster scans on local networks.
        # -n  = no DNS resolution by nmap (we'll do reverse DNS ourselves
        #       per host below for hostname enrichment).
        result = subprocess.run(
            ["nmap", "-sn", "-T4", "-n", "-oX", "-", subnet],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return [], "scan timed out (>2 minutes)"
    except FileNotFoundError:
        return [], "nmap not installed"
    except OSError as e:
        return [], f"could not run nmap: {e}"

    if result.returncode != 0:
        return [], f"nmap exited with code {result.returncode}: {result.stderr.strip()[:200]}"

    try:
        root = ET.fromstring(result.stdout)
    except ET.ParseError as e:
        return [], f"could not parse nmap output: {e}"

    hosts = []
    for host_el in root.findall("host"):
        # Skip down hosts (status="down")
        status_el = host_el.find("status")
        if status_el is None or status_el.get("state") != "up":
            continue

        ip = mac = vendor = None
        for addr in host_el.findall("address"):
            addrtype = addr.get("addrtype")
            if addrtype == "ipv4":
                ip = addr.get("addr")
            elif addrtype == "mac":
                mac = addr.get("addr", "").lower()
                vendor = addr.get("vendor")

        if not ip:
            continue

        # Reverse DNS lookup
        hostname = None
        try:
            import socket
            hostname = socket.gethostbyaddr(ip)[0]
        except (socket.herror, socket.gaierror, OSError):
            pass

        hosts.append({
            "ip":       ip,
            "mac":      mac,
            "hostname": hostname,
            "vendor":   vendor,
        })

    # Sort by IP for stable display
    def _ip_key(h):
        try:
            return tuple(int(x) for x in h["ip"].split("."))
        except (ValueError, AttributeError):
            return (999, 999, 999, 999)
    hosts.sort(key=_ip_key)
    return hosts, None


def start_discovery_scan():
    """Kick off a scan in a background thread if one isn't already running.
    Returns (started, message)."""
    with _DISCOVERY_LOCK:
        if _DISCOVERY_STATE["running"]:
            return False, "scan already in progress"

        ok, info = _check_nmap_available()
        if not ok:
            return False, info

        subnet = _detect_subnet_cidr()
        if not subnet:
            return False, "could not auto-detect subnet"

        _DISCOVERY_STATE.update({
            "running":  True,
            "started":  int(time.time()),
            "finished": None,
            "subnet":   subnet,
            "results":  [],
            "error":    None,
        })

    def _worker():
        results, error = _run_nmap_scan(subnet)
        with _DISCOVERY_LOCK:
            _DISCOVERY_STATE.update({
                "running":  False,
                "finished": int(time.time()),
                "results":  results,
                "error":    error,
            })
        if error:
            logging.warning(f"Discovery scan failed: {error}")
        else:
            logging.info(f"Discovery scan finished: {len(results)} hosts found in {subnet}")

    threading.Thread(target=_worker, daemon=True, name="discovery-scan").start()
    return True, f"started scan on {subnet}"


def get_discovery_state():
    """Return a snapshot of current scan state for the API."""
    with _DISCOVERY_LOCK:
        return dict(_DISCOVERY_STATE)
