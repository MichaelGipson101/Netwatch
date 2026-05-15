#!/usr/bin/env python3
"""
netwatch patch: WoL directed broadcast (auto-detected).

Replaces the WoL implementation so the magic packet is sent to the proper
DIRECTED broadcast address for the Pi's actual subnet (e.g. 192.168.3.255
for a /22 covering 192.168.0.0/22), instead of the limited broadcast
255.255.255.255 which routes only to the primary /24 on most setups.

The new implementation:
  - Detects the Pi's outgoing interface and CIDR using `ip` commands
  - Computes the directed broadcast address from that CIDR
  - Logs the detected broadcast on startup so you can verify
  - Falls back to 255.255.255.255 if detection fails

Must be applied AFTER patch_detail_drawer.py.

Run once from ~/netwatch/:
    python3 patch_wol_subnet.py
    sudo systemctl restart netwatch

Backup saved to monitor.py.bak_wol_subnet.
Idempotent - safe to re-run.
"""

import os
import shutil
import sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_wol_subnet"
SENTINEL = "_detect_broadcast_address"  # presence means already patched


# Replacement: the entire send_wol_packet function plus a helper above it.
OLD_BLOCK = '''def send_wol_packet(mac_address):
    """Send a Wake-on-LAN magic packet to the given MAC. Returns (ok, msg)."""
    import socket
    mac = mac_address.replace(":", "").replace("-", "").lower()
    if len(mac) != 12 or not all(c in "0123456789abcdef" for c in mac):
        return False, "Invalid MAC address format"
    try:
        mac_bytes = bytes.fromhex(mac)
    except ValueError:
        return False, "Could not parse MAC address"
    # Magic packet: 6 bytes of 0xFF, followed by the MAC repeated 16 times.
    packet = b"\\xff" * 6 + mac_bytes * 16
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        # Send to broadcast on port 9 (the standard WoL discard port)
        s.sendto(packet, ("255.255.255.255", 9))
        s.close()
        logging.info(f"WoL: sent magic packet to {mac_address}")
        return True, None
    except OSError as e:
        return False, f"Network error: {e}"'''


NEW_BLOCK = '''def _detect_broadcast_address():
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
    packet = b"\\xff" * 6 + mac_bytes * 16
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
        return False, f"Network error: {e}"'''


def main():
    if not os.path.isfile(TARGET):
        print(f"ERROR: {TARGET} not found.")
        sys.exit(1)

    content = open(TARGET).read()

    if SENTINEL in content:
        print(f"NOTE: '{SENTINEL}' found - patch already applied. Exiting.")
        sys.exit(0)

    if "send_wol_packet" not in content:
        print("ERROR: This patch requires patch_detail_drawer first.")
        sys.exit(1)

    if content.count(OLD_BLOCK) != 1:
        print(f"[FAIL] send_wol_packet block match: {content.count(OLD_BLOCK)}")
        print("       The current send_wol_packet doesn't match the expected text.")
        print("       Has another patch modified it?")
        sys.exit(1)

    shutil.copy2(TARGET, BACKUP)
    print(f"[OK] Backed up {TARGET} -> {BACKUP}")

    content = content.replace(OLD_BLOCK, NEW_BLOCK, 1)
    print("[OK] Replaced send_wol_packet with subnet-aware version")

    open(TARGET, "w").write(content)

    import ast
    try:
        ast.parse(open(TARGET).read())
        print("[OK] Resulting Python is valid")
    except SyntaxError as e:
        print(f"[FAIL] Syntax error: {e}")
        shutil.copy2(BACKUP, TARGET)
        sys.exit(1)

    print()
    print("Next steps:")
    print("  1. sudo systemctl restart netwatch")
    print("  2. Watch the log to confirm the detected broadcast address:")
    print("       journalctl -u netwatch -f | grep -i wol")
    print("     You should see something like:")
    print("       WoL: directed broadcast detected as 192.168.3.255 (via eth0)")
    print("  3. Test the Wake button - magic packets now go to the right address.")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
