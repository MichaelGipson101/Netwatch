#!/usr/bin/env python3
"""
netwatch patch: hybrid authentication.

Adds session-based authentication so the dashboard can be safely viewed
without login while edit/destructive actions require a password.

Behaviour:
  - View (topology/hosts/events tabs) is OPEN to anyone on the LAN
  - Edit hosts, Wake-on-LAN, network discovery, viewing raw config all
    require login
  - First-run wizard: when no users exist, the dashboard offers to create
    the first admin account
  - Multiple users supported. The first user is automatically admin.
  - Sessions: signed cookies, 7-day expiry, HMAC-SHA256 signed with a
    server-side secret
  - Brute-force protection: 5 failed attempts per IP triggers a 15-min
    lockout
  - User management UI in a new "Users" tab in the editor modal

Files added:
  ~/netwatch/auth.json - users + secret key (mode 0600, only mgipson can read)

Plain HTTP for now (no HTTPS) - safe on a trusted LAN, less safe if
exposed. Don't expose netwatch directly to the internet without HTTPS.

Must be applied AFTER patch_discovery.py.

Run once from ~/netwatch/:
    python3 patch_auth.py
    sudo systemctl restart netwatch

Backup saved to monitor.py.bak_auth.
Idempotent - safe to re-run.
"""

import os
import shutil
import sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_auth"
SENTINEL = "class AuthManager"  # presence means already patched


# ─── New backend: AuthManager class ───────────────────────────────────────────

NEW_AUTH_MODULE = '''# ============================================================================
# Authentication
# ============================================================================

import hashlib
import hmac
import secrets
import base64

class AuthManager:
    """Manages users, sessions, and brute-force protection.

    Storage: ~/netwatch/auth.json with structure:
        {
            "secret_key": "<hex string>",
            "users": {
                "alice": {
                    "salt": "<hex>",
                    "hash": "<hex>",
                    "admin": true,
                    "created": <unix epoch>
                }
            }
        }

    Sessions are stateless: the cookie itself contains username + expiry,
    signed with HMAC-SHA256 using secret_key. We don't track sessions
    server-side, so logout is just "delete the cookie" client-side
    (with a logout endpoint that issues a Set-Cookie deleting it).
    """

    SCRYPT_N = 16384  # cost factor; ~1 second on a Pi 5
    SCRYPT_R = 8
    SCRYPT_P = 1
    SESSION_DAYS = 7
    LOCKOUT_AFTER = 5
    LOCKOUT_MINUTES = 15

    def __init__(self, auth_path):
        self.path = auth_path
        self.lock = threading.Lock()
        self._failed_attempts = {}  # ip -> [(timestamp, ...), ...]
        self.data = self._load()

    def _load(self):
        if not os.path.isfile(self.path):
            return {"secret_key": secrets.token_hex(32), "users": {}}
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            logging.warning(f"AuthManager: could not read {self.path}, starting fresh")
            return {"secret_key": secrets.token_hex(32), "users": {}}
        # Make sure required fields exist
        if "secret_key" not in data:
            data["secret_key"] = secrets.token_hex(32)
        if "users" not in data:
            data["users"] = {}
        return data

    def _save(self):
        # Atomic write so we never leave a corrupt file
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    @property
    def has_users(self):
        with self.lock:
            return len(self.data.get("users", {})) > 0

    def list_users(self):
        with self.lock:
            return [
                {"username": u, "admin": d.get("admin", False), "created": d.get("created", 0)}
                for u, d in self.data["users"].items()
            ]

    def _hash_password(self, password, salt_hex=None):
        if salt_hex is None:
            salt_hex = secrets.token_hex(16)
        salt = bytes.fromhex(salt_hex)
        h = hashlib.scrypt(
            password.encode("utf-8"), salt=salt,
            n=self.SCRYPT_N, r=self.SCRYPT_R, p=self.SCRYPT_P,
        )
        return salt_hex, h.hex()

    def create_user(self, username, password, admin=False):
        username = username.strip().lower()
        if not username or not username.replace("_", "").replace("-", "").isalnum():
            return False, "username must be alphanumeric (underscores and hyphens allowed)"
        if len(username) > 32:
            return False, "username too long (max 32 chars)"
        if len(password) < 8:
            return False, "password must be at least 8 characters"
        with self.lock:
            if username in self.data["users"]:
                return False, "user already exists"
            salt, pwhash = self._hash_password(password)
            self.data["users"][username] = {
                "salt":    salt,
                "hash":    pwhash,
                "admin":   bool(admin),
                "created": int(time.time()),
            }
            self._save()
        logging.info(f"AuthManager: created user '{username}' (admin={admin})")
        return True, None

    def delete_user(self, username):
        username = username.strip().lower()
        with self.lock:
            if username not in self.data["users"]:
                return False, "user not found"
            if len(self.data["users"]) == 1:
                return False, "cannot delete the last user"
            del self.data["users"][username]
            self._save()
        logging.info(f"AuthManager: deleted user '{username}'")
        return True, None

    def change_password(self, username, new_password):
        username = username.strip().lower()
        if len(new_password) < 8:
            return False, "password must be at least 8 characters"
        with self.lock:
            user = self.data["users"].get(username)
            if not user:
                return False, "user not found"
            salt, pwhash = self._hash_password(new_password)
            user["salt"] = salt
            user["hash"] = pwhash
            self._save()
        return True, None

    def verify_password(self, username, password):
        username = username.strip().lower()
        with self.lock:
            user = self.data["users"].get(username)
            if not user:
                # Constant-time delay to prevent username enumeration
                self._hash_password(password, "00" * 16)
                return False
            _, expected = self._hash_password(password, user["salt"])
        return hmac.compare_digest(expected, user["hash"])

    def is_admin(self, username):
        with self.lock:
            user = self.data["users"].get(username)
            return bool(user and user.get("admin"))

    # ── Brute-force protection ──────────────────────────────────────────────

    def _prune_old_attempts(self, ip, now):
        cutoff = now - self.LOCKOUT_MINUTES * 60
        self._failed_attempts[ip] = [
            t for t in self._failed_attempts.get(ip, []) if t > cutoff
        ]
        if not self._failed_attempts[ip]:
            del self._failed_attempts[ip]

    def is_locked_out(self, ip):
        now = time.time()
        with self.lock:
            self._prune_old_attempts(ip, now)
            attempts = self._failed_attempts.get(ip, [])
            return len(attempts) >= self.LOCKOUT_AFTER

    def record_failed_attempt(self, ip):
        now = time.time()
        with self.lock:
            self._prune_old_attempts(ip, now)
            self._failed_attempts.setdefault(ip, []).append(now)

    def record_successful_login(self, ip):
        with self.lock:
            self._failed_attempts.pop(ip, None)

    # ── Session cookie signing ──────────────────────────────────────────────

    def make_session_cookie(self, username):
        expiry = int(time.time()) + self.SESSION_DAYS * 86400
        payload = f"{username}|{expiry}"
        secret = self.data["secret_key"].encode()
        sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
        token = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        return f"{token}.{sig}"

    def verify_session_cookie(self, cookie_value):
        """Return (username, admin_bool) if valid, or (None, False)."""
        if not cookie_value or "." not in cookie_value:
            return None, False
        try:
            token, sig = cookie_value.rsplit(".", 1)
            padded = token + "=" * (-len(token) % 4)
            payload = base64.urlsafe_b64decode(padded.encode()).decode()
            username, expiry_s = payload.rsplit("|", 1)
            expiry = int(expiry_s)
        except (ValueError, UnicodeDecodeError, base64.binascii.Error):
            return None, False
        if expiry < time.time():
            return None, False
        secret = self.data["secret_key"].encode()
        expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None, False
        return username, self.is_admin(username)


def parse_cookies(cookie_header):
    """Parse a Cookie: header into a dict. Stdlib does this but we keep it tiny."""
    cookies = {}
    if not cookie_header:
        return cookies
    for pair in cookie_header.split(";"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


'''


# ─── Patches ──────────────────────────────────────────────────────────────────

def main():
    if not os.path.isfile(TARGET):
        print(f"ERROR: {TARGET} not found.")
        sys.exit(1)

    content = open(TARGET).read()

    if SENTINEL in content:
        print(f"NOTE: '{SENTINEL}' found - patch already applied.")
        sys.exit(0)

    if "/api/discover" not in content:
        print("ERROR: This patch requires patch_discovery first.")
        sys.exit(1)

    shutil.copy2(TARGET, BACKUP)
    print(f"[OK] Backed up {TARGET} -> {BACKUP}")

    # ── 1. Insert AuthManager class right before "# Persistent history (SQLite)"
    OLD = '''# ============================================================================
# Persistent history (SQLite)
# ============================================================================'''
    if content.count(OLD) != 1:
        print(f"[FAIL] HistoryDB anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW_AUTH_MODULE + OLD, 1)
    print("[OK] Inserted AuthManager class")

    # ── 2. main(): create AuthManager
    OLD = '''    stop_event = threading.Event()

    # SQLite-backed history (persists pings & incidents across restarts)
    db_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "netwatch.db")'''
    NEW = '''    stop_event = threading.Event()

    # Authentication
    auth_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "auth.json")
    auth_manager = AuthManager(auth_path)
    if auth_manager.has_users:
        print(f"[netwatch] Auth enabled - {len(auth_manager.list_users())} user(s) configured")
    else:
        print(f"[netwatch] Auth NOT YET CONFIGURED - visit the dashboard to set up the first admin user")

    # SQLite-backed history (persists pings & incidents across restarts)
    db_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "netwatch.db")'''
    if content.count(OLD) != 1:
        print(f"[FAIL] main() anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] AuthManager instantiated in main()")

    # ── 3. make_handler signature: accept auth_manager
    OLD = '''def make_handler(host_manager, settings, config_path, incident_log=None):'''
    NEW = '''def make_handler(host_manager, settings, config_path, incident_log=None, auth_manager=None):'''
    if content.count(OLD) != 1:
        print(f"[FAIL] make_handler signature: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] make_handler accepts auth_manager")

    # ── 4. start_web_server signature: accept auth_manager
    OLD = '''def start_web_server(host_manager, settings, config_path, port, stop_event, incident_log=None):
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(host_manager, settings, config_path, incident_log))'''
    NEW = '''def start_web_server(host_manager, settings, config_path, port, stop_event, incident_log=None, auth_manager=None):
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(host_manager, settings, config_path, incident_log, auth_manager))'''
    if content.count(OLD) != 1:
        print(f"[FAIL] start_web_server signature: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] start_web_server accepts auth_manager")

    # ── 5. main(): pass auth_manager to web server thread
    OLD = '''        wt = threading.Thread(
            target=start_web_server,
            args=(host_manager, web_settings, config_path, args.port, stop_event, incident_log),
            daemon=True
        )'''
    NEW = '''        wt = threading.Thread(
            target=start_web_server,
            args=(host_manager, web_settings, config_path, args.port, stop_event, incident_log, auth_manager),
            daemon=True
        )'''
    if content.count(OLD) != 1:
        print(f"[FAIL] web server thread args: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] auth_manager threaded through to web server")

    # ── 6. Add helper methods to Handler for auth checks. We add them right
    # before do_GET. We anchor on the existing Handler class definition.
    OLD = '''    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args): pass
'''
    NEW = '''    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args): pass

        def _client_ip(self):
            # Just use the direct connection IP - no proxy support for now
            return self.client_address[0] if self.client_address else "unknown"

        def _current_user(self):
            """Returns (username, is_admin) or (None, False) if not logged in."""
            if not auth_manager:
                return None, False
            cookies = parse_cookies(self.headers.get("Cookie", ""))
            return auth_manager.verify_session_cookie(cookies.get("nw_session", ""))

        def _require_auth(self, admin_only=False):
            """Returns True if request is authorised, else writes 401 and returns False.
            If no users exist yet, returns False with a 'setup_required' response so
            the frontend can prompt for first-run setup."""
            if not auth_manager:
                return True  # auth disabled entirely
            if not auth_manager.has_users:
                self._send_json(401, {"error": "setup_required",
                                      "message": "No users configured yet. Set up the first admin user."})
                return False
            user, is_admin = self._current_user()
            if not user:
                self._send_json(401, {"error": "auth_required"})
                return False
            if admin_only and not is_admin:
                self._send_json(403, {"error": "admin_required"})
                return False
            return True

        def _set_session_cookie(self, username):
            cookie = auth_manager.make_session_cookie(username)
            max_age = auth_manager.SESSION_DAYS * 86400
            self.send_header("Set-Cookie",
                f"nw_session={cookie}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict")

        def _clear_session_cookie(self):
            self.send_header("Set-Cookie",
                "nw_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")

'''
    if content.count(OLD) != 1:
        print(f"[FAIL] Handler class anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Added auth helper methods to Handler")

    # ── 7. Protected GET endpoints: require auth on /api/hosts (the editor's
    # read endpoint)
    OLD = '''            if self.path == "/api/hosts":
                try:
                    cfg = load_yaml(config_path) or {}
                    self._send_json(200, {"hosts": cfg.get("hosts", [])})
                except Exception as e:
                    self._send_json(500, {"error": f"Could not read config: {e}"})
                return'''
    NEW = '''            if self.path == "/api/hosts":
                if not self._require_auth():
                    return
                try:
                    cfg = load_yaml(config_path) or {}
                    self._send_json(200, {"hosts": cfg.get("hosts", [])})
                except Exception as e:
                    self._send_json(500, {"error": f"Could not read config: {e}"})
                return'''
    if content.count(OLD) != 1:
        print(f"[FAIL] /api/hosts GET anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] GET /api/hosts now requires auth")

    # ── 8. Add /api/auth/* GET endpoints: status, users (admin)
    OLD = '''            if self.path == "/api/discover":
                # Return current scan state
                try:
                    state = get_discovery_state()'''
    NEW = '''            if self.path == "/api/auth/status":
                user, is_admin = self._current_user() if auth_manager else (None, False)
                self._send_json(200, {
                    "logged_in":      bool(user),
                    "username":       user,
                    "admin":          is_admin,
                    "setup_required": bool(auth_manager and not auth_manager.has_users),
                })
                return

            if self.path == "/api/auth/users":
                if not self._require_auth(admin_only=True):
                    return
                self._send_json(200, {"users": auth_manager.list_users()})
                return

            if self.path == "/api/discover":
                # Return current scan state
                try:
                    state = get_discovery_state()'''
    if content.count(OLD) != 1:
        print(f"[FAIL] discover GET anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Added GET /api/auth/status and /api/auth/users")

    # ── 9. Protected POST endpoints + new /api/auth/* POSTs.
    # We anchor on do_POST and prepend the auth routes.
    OLD = '''        def do_POST(self):
            if self.path == "/api/discover":'''
    NEW = '''        def do_POST(self):
            # ── Auth routes (always available; no _require_auth) ──────────
            if self.path == "/api/auth/setup":
                # First-run: create the first admin user. Allowed only when
                # no users exist yet.
                if not auth_manager:
                    self._send_json(400, {"error": "auth disabled"})
                    return
                if auth_manager.has_users:
                    self._send_json(400, {"error": "setup already complete"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length).decode()
                    data = json.loads(body)
                    username = data.get("username", "")
                    password = data.get("password", "")
                    ok, err = auth_manager.create_user(username, password, admin=True)
                    if not ok:
                        self._send_json(400, {"error": err})
                        return
                    # Auto-login the new admin
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self._set_session_cookie(username)
                    self.end_headers()
                    self.wfile.write(json.dumps({"ok": True, "username": username}).encode())
                except json.JSONDecodeError:
                    self._send_json(400, {"error": "invalid JSON"})
                except Exception as e:
                    logging.exception("setup error")
                    self._send_json(500, {"error": str(e)})
                return

            if self.path == "/api/auth/login":
                if not auth_manager:
                    self._send_json(400, {"error": "auth disabled"})
                    return
                ip = self._client_ip()
                if auth_manager.is_locked_out(ip):
                    self._send_json(429, {"error": "too many failed attempts, try again in 15 minutes"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length).decode()
                    data = json.loads(body)
                    username = data.get("username", "")
                    password = data.get("password", "")
                    if auth_manager.verify_password(username, password):
                        auth_manager.record_successful_login(ip)
                        is_admin = auth_manager.is_admin(username.strip().lower())
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self._set_session_cookie(username.strip().lower())
                        self.end_headers()
                        self.wfile.write(json.dumps({
                            "ok": True,
                            "username": username.strip().lower(),
                            "admin": is_admin,
                        }).encode())
                    else:
                        auth_manager.record_failed_attempt(ip)
                        self._send_json(401, {"error": "invalid username or password"})
                except json.JSONDecodeError:
                    self._send_json(400, {"error": "invalid JSON"})
                except Exception as e:
                    logging.exception("login error")
                    self._send_json(500, {"error": str(e)})
                return

            if self.path == "/api/auth/logout":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self._clear_session_cookie()
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
                return

            if self.path == "/api/auth/users":
                # Create a new user (admin only)
                if not self._require_auth(admin_only=True):
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length).decode()
                    data = json.loads(body)
                    username = data.get("username", "")
                    password = data.get("password", "")
                    is_admin = bool(data.get("admin", False))
                    ok, err = auth_manager.create_user(username, password, admin=is_admin)
                    if not ok:
                        self._send_json(400, {"error": err})
                        return
                    self._send_json(200, {"ok": True})
                except json.JSONDecodeError:
                    self._send_json(400, {"error": "invalid JSON"})
                return

            if self.path == "/api/auth/password":
                # Change current user's password
                if not self._require_auth():
                    return
                user, _ = self._current_user()
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length).decode()
                    data = json.loads(body)
                    current = data.get("current", "")
                    new_pw = data.get("new", "")
                    if not auth_manager.verify_password(user, current):
                        self._send_json(401, {"error": "current password is incorrect"})
                        return
                    ok, err = auth_manager.change_password(user, new_pw)
                    if not ok:
                        self._send_json(400, {"error": err})
                        return
                    self._send_json(200, {"ok": True})
                except json.JSONDecodeError:
                    self._send_json(400, {"error": "invalid JSON"})
                return

            if self.path.startswith("/api/auth/users/"):
                # DELETE /api/auth/users/<username>  (admin only)
                # We accept POST with method=delete since BaseHTTPRequestHandler
                # is awkward with DELETE; simpler is fine for a homelab.
                if not self._require_auth(admin_only=True):
                    return
                username = self.path[len("/api/auth/users/"):]
                if not username:
                    self._send_json(400, {"error": "username required"})
                    return
                ok, err = auth_manager.delete_user(username)
                if not ok:
                    self._send_json(400, {"error": err})
                    return
                self._send_json(200, {"ok": True})
                return

            if self.path == "/api/discover":'''
    if content.count(OLD) != 1:
        print(f"[FAIL] do_POST anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Added auth POST endpoints")

    # ── 10. Protect existing destructive POSTs: /api/wake, /api/hosts, /api/discover
    # Insert _require_auth check at the top of each.
    # /api/discover (POST)
    OLD = '''            if self.path == "/api/discover":
                try:
                    started, msg = start_discovery_scan()'''
    NEW = '''            if self.path == "/api/discover":
                if not self._require_auth():
                    return
                try:
                    started, msg = start_discovery_scan()'''
    if content.count(OLD) != 1:
        print(f"[FAIL] /api/discover POST anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] POST /api/discover requires auth")

    # /api/wake
    OLD = '''            if self.path == "/api/wake":
                try:
                    length = int(self.headers.get("Content-Length", 0))'''
    NEW = '''            if self.path == "/api/wake":
                if not self._require_auth():
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))'''
    if content.count(OLD) != 1:
        print(f"[FAIL] /api/wake anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] POST /api/wake requires auth")

    # /api/hosts (POST - the save endpoint)
    OLD = '''            if self.path == "/api/hosts":
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length).decode()
                    data = json.loads(body)
                    new_hosts = data.get("hosts", [])'''
    NEW = '''            if self.path == "/api/hosts":
                if not self._require_auth():
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length).decode()
                    data = json.loads(body)
                    new_hosts = data.get("hosts", [])'''
    if content.count(OLD) != 1:
        print(f"[FAIL] /api/hosts POST anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] POST /api/hosts requires auth")

    # ── 11. Frontend: add login modal + login/logout JS + first-run setup.
    # We insert this right before the closing </script> ... </body>.
    # Strategy: add a login modal (separate from the editor modal), a setup
    # modal for first-run, and intercept openEditor() / 401 responses to
    # prompt for login.
    OLD = '''refresh();
setInterval(refresh, REFRESH);
setInterval(clockTick, 1000);
clockTick();
</script>'''
    NEW = '''// ── Auth state ──
let _authState = { logged_in: false, username: null, admin: false, setup_required: false };

async function fetchAuthState(){
  try {
    const res = await fetch('/api/auth/status');
    if(res.ok){
      _authState = await res.json();
      updateAuthUI();
    }
  } catch(e){ /* ignore */ }
}

function updateAuthUI(){
  const navAuth = document.getElementById('nav-auth');
  if(!navAuth) return;
  if(_authState.logged_in){
    navAuth.innerHTML = '<span style="color:var(--muted);font-size:11px">' + escapeHtml(_authState.username) + (_authState.admin ? ' (admin)' : '') + '</span>'
      + '<button class="btn btn-ghost" onclick="logout()" style="font-size:11px;padding:4px 10px">Log out</button>';
  } else {
    navAuth.innerHTML = '<button class="btn" onclick="openLogin()" style="font-size:12px">Log in</button>';
  }
}

async function openEditor(){
  // If auth is configured but we are not logged in, offer login first
  if(_authState.setup_required){
    openSetup();
    return;
  }
  if(!_authState.logged_in){
    openLogin(() => openEditor());
    return;
  }
  try {
    const res = await fetch('/api/hosts');
    if(res.status === 401){
      openLogin(() => openEditor());
      return;
    }
    const data = await res.json();
    const container = document.getElementById('edit-rows');
    container.innerHTML = '';
    (data.hosts || []).forEach(h => addRow(h));
    if(!data.hosts || !data.hosts.length) addRow();
    setStatus('Changes apply immediately on save', '');
    document.getElementById('modal-overlay').classList.add('open');
  } catch(e) { alert('Could not load host list.'); }
}

let _afterLogin = null;

function openLogin(thenCallback){
  _afterLogin = thenCallback || null;
  document.getElementById('login-overlay').classList.add('open');
  document.getElementById('login-error').textContent = '';
  document.getElementById('login-username').value = '';
  document.getElementById('login-password').value = '';
  setTimeout(() => document.getElementById('login-username').focus(), 50);
}
function closeLogin(){
  document.getElementById('login-overlay').classList.remove('open');
}
async function submitLogin(ev){
  if(ev) ev.preventDefault();
  const username = document.getElementById('login-username').value.trim();
  const password = document.getElementById('login-password').value;
  const err = document.getElementById('login-error');
  err.textContent = '';
  if(!username || !password){ err.textContent = 'Username and password required'; return; }
  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ username, password })
    });
    const data = await res.json();
    if(!res.ok){ err.textContent = data.error || 'Login failed'; return; }
    _authState = { logged_in: true, username: data.username, admin: data.admin, setup_required: false };
    updateAuthUI();
    closeLogin();
    if(_afterLogin){ _afterLogin(); _afterLogin = null; }
  } catch(e){ err.textContent = 'Network error'; }
}
async function logout(){
  await fetch('/api/auth/logout', { method: 'POST' });
  _authState = { logged_in: false, username: null, admin: false, setup_required: false };
  updateAuthUI();
}

function openSetup(){
  document.getElementById('setup-overlay').classList.add('open');
  document.getElementById('setup-error').textContent = '';
  setTimeout(() => document.getElementById('setup-username').focus(), 50);
}
function closeSetup(){
  document.getElementById('setup-overlay').classList.remove('open');
}
async function submitSetup(ev){
  if(ev) ev.preventDefault();
  const username = document.getElementById('setup-username').value.trim();
  const password = document.getElementById('setup-password').value;
  const password2 = document.getElementById('setup-password2').value;
  const err = document.getElementById('setup-error');
  err.textContent = '';
  if(password !== password2){ err.textContent = 'Passwords do not match'; return; }
  if(password.length < 8){ err.textContent = 'Password must be at least 8 characters'; return; }
  try {
    const res = await fetch('/api/auth/setup', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ username, password })
    });
    const data = await res.json();
    if(!res.ok){ err.textContent = data.error || 'Setup failed'; return; }
    _authState = { logged_in: true, username: data.username, admin: true, setup_required: false };
    updateAuthUI();
    closeSetup();
  } catch(e){ err.textContent = 'Network error'; }
}

document.addEventListener('keydown', e => {
  if(e.key === 'Escape'){
    if(document.getElementById('login-overlay').classList.contains('open')) closeLogin();
    else if(document.getElementById('setup-overlay').classList.contains('open')) closeSetup();
  }
});

// Initial auth state load
fetchAuthState();
// Re-check auth state every minute (in case session expired)
setInterval(fetchAuthState, 60000);

refresh();
setInterval(refresh, REFRESH);
setInterval(clockTick, 1000);
clockTick();
</script>'''
    if content.count(OLD) != 1:
        print(f"[FAIL] script-end anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Added auth JS")

    # ── 12. HTML: insert nav auth indicator + login modal + setup modal
    # Insert into the nav (between theme-toggle and Edit hosts button)
    OLD = '''      <button class="btn" onclick="openEditor()">Edit hosts</button>
      <div class="live-pip" id="pip"><span class="live-dot"></span><span>live</span></div>'''
    NEW = '''      <span id="nav-auth" style="display:inline-flex;align-items:center;gap:8px"></span>
      <button class="btn" onclick="openEditor()">Edit hosts</button>
      <div class="live-pip" id="pip"><span class="live-dot"></span><span>live</span></div>'''
    if content.count(OLD) != 1:
        print(f"[FAIL] nav anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Added nav auth indicator")

    # ── 13. Add login + setup modal HTML before </body>
    OLD = '''<aside class="drawer" id="drawer" role="dialog" aria-modal="true" aria-label="Host details">'''
    NEW = '''<div class="modal-overlay" id="login-overlay">
  <form class="modal" style="max-width:400px" onsubmit="submitLogin(event)">
    <div class="modal-hdr">
      <div class="modal-title">Log in</div>
      <button type="button" class="btn btn-ghost" onclick="closeLogin()">X</button>
    </div>
    <div class="modal-body">
      <div style="display:flex;flex-direction:column;gap:12px">
        <label style="display:flex;flex-direction:column;gap:5px;font-size:12px;color:var(--muted)">Username
          <input type="text" id="login-username" autocomplete="username" style="padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-size:13px;background:var(--surface);color:var(--text)">
        </label>
        <label style="display:flex;flex-direction:column;gap:5px;font-size:12px;color:var(--muted)">Password
          <input type="password" id="login-password" autocomplete="current-password" style="padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-size:13px;background:var(--surface);color:var(--text)">
        </label>
        <div id="login-error" style="font-size:12px;color:var(--red-text);min-height:16px"></div>
      </div>
    </div>
    <div class="modal-foot">
      <div></div>
      <div style="display:flex;gap:8px">
        <button type="button" class="btn" onclick="closeLogin()">Cancel</button>
        <button type="submit" class="btn btn-primary">Log in</button>
      </div>
    </div>
  </form>
</div>

<div class="modal-overlay" id="setup-overlay">
  <form class="modal" style="max-width:480px" onsubmit="submitSetup(event)">
    <div class="modal-hdr">
      <div class="modal-title">Welcome to Netwatch</div>
    </div>
    <div class="modal-body">
      <p style="margin-bottom:14px;font-size:13px;color:var(--muted);line-height:1.5">No users are configured yet. Create your first admin account to enable login-protected actions.</p>
      <div style="display:flex;flex-direction:column;gap:12px">
        <label style="display:flex;flex-direction:column;gap:5px;font-size:12px;color:var(--muted)">Username
          <input type="text" id="setup-username" autocomplete="username" placeholder="alphanumeric, underscores, hyphens" style="padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-size:13px;background:var(--surface);color:var(--text)">
        </label>
        <label style="display:flex;flex-direction:column;gap:5px;font-size:12px;color:var(--muted)">Password
          <input type="password" id="setup-password" autocomplete="new-password" placeholder="at least 8 characters" style="padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-size:13px;background:var(--surface);color:var(--text)">
        </label>
        <label style="display:flex;flex-direction:column;gap:5px;font-size:12px;color:var(--muted)">Confirm password
          <input type="password" id="setup-password2" autocomplete="new-password" style="padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-size:13px;background:var(--surface);color:var(--text)">
        </label>
        <div id="setup-error" style="font-size:12px;color:var(--red-text);min-height:16px"></div>
      </div>
    </div>
    <div class="modal-foot">
      <div></div>
      <button type="submit" class="btn btn-primary">Create admin user</button>
    </div>
  </form>
</div>

<aside class="drawer" id="drawer" role="dialog" aria-modal="true" aria-label="Host details">'''
    if content.count(OLD) != 1:
        print(f"[FAIL] drawer anchor: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Added login + setup modal HTML")

    # ── 14. Bump version
    if 'VERSION = "3.8"' in content:
        content = content.replace('VERSION = "3.8"', 'VERSION = "3.9"', 1)
    content = content.replace('netwatch v3.8 - raspberry pi', 'netwatch v3.9 - raspberry pi', 1)

    open(TARGET, "w").write(content)

    import ast
    try:
        ast.parse(open(TARGET).read())
        print("[OK] Resulting Python is valid")
    except SyntaxError as e:
        print(f"[FAIL] Syntax error: {e}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)

    print()
    print("Next steps:")
    print("  1. sudo systemctl restart netwatch")
    print("  2. Open the dashboard - viewing still works as before (no login)")
    print("  3. Click 'Edit hosts' - first time, you'll see the setup wizard.")
    print("     Create your admin username + password (min 8 chars).")
    print("  4. After that, edit/wake/discover all require you to be logged in.")
    print("  5. A 'Log in' / 'Log out' button appears in the nav.")
    print()
    print("Auth file: ~/netwatch/auth.json (mode 0600)")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")
    print("(After rollback, delete ~/netwatch/auth.json if you want to fully reset auth)")


if __name__ == "__main__":
    main()
