#!/usr/bin/env python3
"""
netwatch security fixes - addresses Findings 1, 2, 3, and 4 from the
v3.36 surface-level security audit.

WHAT THIS PATCH DOES:

  Finding 1 (HIGH): require auth on previously-open read endpoints.
  Now /api/inventory, /api/topology, /api/connections, /api/inventory/<id>,
  /api/inventory/<id>/connections, /api/discover, and /api/pi-health all
  require a valid session. Tailnet members can no longer enumerate the
  entire homelab inventory without logging in.

  /api/auth/status REMAINS open - the frontend needs this to decide whether
  to show the login modal vs the main app.

  Finding 2 (MEDIUM): bind /api/auth/setup to localhost-only. The first-
  run admin-claim endpoint can now only be called from 127.0.0.1 (or ::1).
  Future reinstalls require SSH'ing to the Pi and running setup via curl
  localhost - a one-time inconvenience that prevents anyone-on-the-network
  from claiming admin before you do.

  Finding 3 (MEDIUM): validate IP/hostname format in hosts.yaml entries.
  Adds _validate_ip_or_hostname helper that rejects strings starting with
  '-' (argument injection guard), accepts valid IPv4/IPv6 addresses, and
  accepts DNS-safe hostnames per RFC 1123. Also adds '--' separator to
  the ping command line as belt-and-suspenders.

  Finding 4 (LOW): adds a _read_json_body helper with a 1MB cap by default.
  The infrastructure is in place; existing handlers can adopt it
  incrementally. Not auto-wired into every handler to keep this patch
  focused.

THREAT MODEL ASSUMPTIONS:

  Tailscale-exposed homelab tool, single trusted user, runs as non-root
  on Pi 5. Goal is defense-in-depth.

After this patch, opening the dashboard requires a login before ANY data
loads. Bookmarked tabs that were previously usable without login will now
prompt for credentials on next refresh - this is intended.

Must be applied AFTER patch_final_polish.py.

Run once from ~/netwatch/:
    python3 patch_security_fixes.py
    sudo systemctl restart netwatch

Backup of monitor.py saved to monitor.py.bak_security.
Idempotent - safe to re-run.
"""

import os, shutil, sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_security"
SENTINEL = "_validate_ip_or_hostname"


PATCHES = [
    # ──────────────────────────────────────────────────────────────────────
    # 1. Add the _validate_ip_or_hostname helper. Anchor right before
    # _validate_url which is its semantic neighbor.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''def _validate_url(s):''',
        '''def _validate_ip_or_hostname(s):
    """Returns True if s is a valid IPv4/IPv6 address or DNS-safe hostname.

    Used to reject argument-injection attempts via the 'ip' field in
    hosts.yaml entries. The ping subprocess takes the IP as a positional
    argument; without this, a leading '-' could be interpreted as a flag
    by some ping implementations. We pass '--' separator in ping_host
    too as belt-and-suspenders.
    """
    if not s or not isinstance(s, str):
        return False
    s = s.strip()
    if not s or len(s) > 253:
        return False
    # Reject anything starting with '-' immediately (argument injection guard)
    if s.startswith('-'):
        return False
    # Try as IP first
    try:
        import ipaddress
        ipaddress.ip_address(s)
        return True
    except (ValueError, ImportError):
        pass
    # Otherwise validate as hostname (RFC 1123-ish)
    import re as _re
    label_re = _re.compile(r'^[a-zA-Z0-9]([a-zA-Z0-9\\-]{0,61}[a-zA-Z0-9])?$')
    labels = s.split('.')
    return all(label_re.match(label) for label in labels)


def _validate_url(s):'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 2. Wire the validator into validate_hosts_config.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''        ip = str(h["ip"]).strip()
        if ip in seen:
            return False, f"Duplicate IP: {ip}"
        seen.add(ip)''',
        '''        ip = str(h["ip"]).strip()
        if not _validate_ip_or_hostname(ip):
            return False, f"Host '{h.get('name','?')}': '{ip}' is not a valid IP address or hostname"
        if ip in seen:
            return False, f"Duplicate IP: {ip}"
        seen.add(ip)'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 3. Belt-and-suspenders: pass '--' separator to ping so even if
    # a future bug allows '-something' through validation, ping treats
    # it as a positional argument.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), ip],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout + 1
        )''',
        '''        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), "--", ip],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout + 1
        )'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 4. Localhost-only setup endpoint.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''            if self.path == "/api/auth/setup":
                # First-run: create the first admin user. Allowed only when
                # no users exist yet.
                if not auth_manager:
                    self._send_json(400, {"error": "auth disabled"})
                    return
                if auth_manager.has_users:
                    self._send_json(400, {"error": "setup already complete"})
                    return''',
        '''            if self.path == "/api/auth/setup":
                # First-run: create the first admin user. Allowed only when
                # no users exist yet AND the request comes from localhost.
                # The localhost requirement prevents anyone-on-the-network
                # from claiming admin before you do.
                if not auth_manager:
                    self._send_json(400, {"error": "auth disabled"})
                    return
                client_ip = self._client_ip()
                if client_ip not in ("127.0.0.1", "::1", "localhost"):
                    self._send_json(403, {
                        "error": "setup must be performed from localhost",
                        "message": "First-run admin setup is restricted to localhost. SSH to the Pi and run: curl -X POST http://localhost:8080/api/auth/setup -H 'Content-Type: application/json' -d '{\\"username\\":\\"admin\\",\\"password\\":\\"...\\"}'"
                    })
                    return
                if auth_manager.has_users:
                    self._send_json(400, {"error": "setup already complete"})
                    return'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 5. Lock down /api/pi-health
    # ──────────────────────────────────────────────────────────────────────
    (
        '''            if self.path == "/api/pi-health":
                try:
                    self._send_json(200, read_pi_health())
                except Exception as e:
                    logging.exception("Error reading Pi health")
                    self._send_json(500, {"error": str(e)})
                return''',
        '''            if self.path == "/api/pi-health":
                if not self._require_auth():
                    return
                try:
                    self._send_json(200, read_pi_health())
                except Exception as e:
                    logging.exception("Error reading Pi health")
                    self._send_json(500, {"error": str(e)})
                return'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 6. Lock down /api/inventory (GET)
    # ──────────────────────────────────────────────────────────────────────
    (
        '''            if self.path == "/api/inventory":
                # Inventory list is OPEN to viewers (matches the dashboard\'s view-mode)
                try:
                    items = inventory_db.list_all() if inventory_db else []''',
        '''            if self.path == "/api/inventory":
                # Inventory list now requires auth (was open prior to v3.37)
                if not self._require_auth():
                    return
                try:
                    items = inventory_db.list_all() if inventory_db else []'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 7. Lock down /api/topology
    # ──────────────────────────────────────────────────────────────────────
    (
        '''            if self.path == "/api/topology":
                # Bundled inventory + connections + linked-host status,
                # for the topology web view. Open to viewers (matches
                # the regular inventory list endpoint).
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"})
                    return''',
        '''            if self.path == "/api/topology":
                # Bundled inventory + connections + linked-host status,
                # for the topology web view. Now requires auth.
                if not self._require_auth():
                    return
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"})
                    return'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 8. Lock down /api/connections
    # ──────────────────────────────────────────────────────────────────────
    (
        '''            if self.path == "/api/connections":
                # All-connections list (used by the topology viz)
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"})
                    return
                try:
                    self._send_json(200, {"items": inventory_db.list_all_connections()})''',
        '''            if self.path == "/api/connections":
                # All-connections list (used by the topology viz). Auth required.
                if not self._require_auth():
                    return
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"})
                    return
                try:
                    self._send_json(200, {"items": inventory_db.list_all_connections()})'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 9. Lock down /api/inventory/<id>/connections
    # ──────────────────────────────────────────────────────────────────────
    (
        '''            if (self.path.startswith("/api/inventory/")
                    and self.path.endswith("/connections")):
                # /api/inventory/<id>/connections - per-device
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"})
                    return''',
        '''            if (self.path.startswith("/api/inventory/")
                    and self.path.endswith("/connections")):
                # /api/inventory/<id>/connections - per-device. Auth required.
                if not self._require_auth():
                    return
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"})
                    return'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 10. Lock down /api/inventory/<id>
    # ──────────────────────────────────────────────────────────────────────
    (
        '''            if self.path.startswith("/api/inventory/") and self.path != "/api/inventory/":
                try:
                    inv_id = int(self.path.split("/")[-1])
                except ValueError:
                    self._send_json(400, {"error": "invalid id"})
                    return
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"})
                    return''',
        '''            if self.path.startswith("/api/inventory/") and self.path != "/api/inventory/":
                # Single inventory record. Auth required.
                if not self._require_auth():
                    return
                try:
                    inv_id = int(self.path.split("/")[-1])
                except ValueError:
                    self._send_json(400, {"error": "invalid id"})
                    return
                if not inventory_db:
                    self._send_json(500, {"error": "inventory not available"})
                    return'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 11. Lock down /api/discover (GET)
    # ──────────────────────────────────────────────────────────────────────
    (
        '''            if self.path == "/api/discover":
                # Return current scan state
                try:
                    state = get_discovery_state()''',
        '''            if self.path == "/api/discover":
                # Return current scan state. Auth required.
                if not self._require_auth():
                    return
                try:
                    state = get_discovery_state()'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 12. Body size cap helper. Available for handlers to use.
    # ──────────────────────────────────────────────────────────────────────
    (
        '''        def _send_json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)''',
        '''        def _read_json_body(self, max_bytes=1024 * 1024):
            """Read a JSON request body with size cap. Returns (data, error_response).

            On success: (parsed_dict, None).
            On error: (None, True) and an HTTP error response has been written.
            """
            length = int(self.headers.get("Content-Length", 0))
            if length > max_bytes:
                self._send_json(413, {"error": f"request body too large (max {max_bytes} bytes)"})
                return None, True
            try:
                body = self.rfile.read(length).decode()
                return json.loads(body), None
            except json.JSONDecodeError:
                self._send_json(400, {"error": "invalid JSON"})
                return None, True
            except (UnicodeDecodeError, ValueError):
                self._send_json(400, {"error": "invalid request body"})
                return None, True

        def _send_json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)'''
    ),

    # ──────────────────────────────────────────────────────────────────────
    # 13. Bump version
    # ──────────────────────────────────────────────────────────────────────
    (
        'netwatch v3.36 - raspberry pi',
        'netwatch v3.37 - raspberry pi'
    ),
]


def main():
    if not os.path.isfile(TARGET):
        print(f"ERROR: {TARGET} not found.")
        sys.exit(1)

    content = open(TARGET).read()

    if SENTINEL in content:
        print(f"NOTE: '{SENTINEL}' found - patch already applied.")
        sys.exit(0)

    if "modal-close-btn" not in content:
        print("ERROR: This patch requires patch_final_polish first.")
        sys.exit(1)

    shutil.copy2(TARGET, BACKUP)
    print(f"[OK] Backed up {TARGET} -> {BACKUP}")

    applied = 0
    for i, (old, new) in enumerate(PATCHES, 1):
        count = content.count(old)
        if count == 0:
            print(f"[FAIL] Patch #{i}: target not found")
            shutil.copy2(BACKUP, TARGET); sys.exit(1)
        if count > 1:
            print(f"[FAIL] Patch #{i}: matches {count}x")
            shutil.copy2(BACKUP, TARGET); sys.exit(1)
        content = content.replace(old, new, 1)
        applied += 1

    if 'VERSION = "3.36"' in content:
        content = content.replace('VERSION = "3.36"', 'VERSION = "3.37"', 1)

    open(TARGET, "w").write(content)

    import ast
    try:
        ast.parse(open(TARGET).read())
        print("[OK] Resulting Python is valid")
    except SyntaxError as e:
        print(f"[FAIL] Syntax error: {e}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)

    print(f"[OK] Applied {applied} patches")
    print()
    print("Security fixes deployed:")
    print("  - Read endpoints now require auth (Finding 1)")
    print("  - Setup endpoint locked to localhost (Finding 2)")
    print("  - hosts.yaml IP/hostname validation (Finding 3)")
    print("  - JSON body size helper available (Finding 4)")
    print()
    print("If you ever rebuild from scratch:")
    print("  1. SSH to the Pi after netwatch starts")
    print("  2. curl -X POST http://localhost:8080/api/auth/setup \\\\")
    print("       -H 'Content-Type: application/json' \\\\")
    print("       -d '{\"username\":\"admin\",\"password\":\"...\"}'")
    print("  3. Then log in via browser as normal")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
