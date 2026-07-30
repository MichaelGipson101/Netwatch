import os
import json
import time
import base64
import hashlib
import hmac
import logging
import secrets
import threading


def make_maintenance_token(secret_key, host_ip):
    """Sign a token binding a maintenance quick-start action to one host,
    valid for verify_maintenance_token's max_age_seconds window. Mirrors
    AuthManager.make_session_cookie's token.sig format."""
    issued = int(time.time())
    payload = f"{host_ip}|{issued}"
    sig = hmac.new(secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    token = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{token}.{sig}"


def verify_maintenance_token(secret_key, host_ip, token_value, max_age_seconds=48 * 3600):
    """Return True if token_value was signed by secret_key for exactly this
    host_ip and hasn't exceeded max_age_seconds since issue."""
    if not token_value or "." not in token_value:
        return False
    try:
        token, sig = token_value.rsplit(".", 1)
        padded = token + "=" * (-len(token) % 4)
        payload = base64.urlsafe_b64decode(padded.encode()).decode()
        payload_ip, issued_s = payload.split("|")
        issued = int(issued_s)
    except (ValueError, UnicodeDecodeError, base64.binascii.Error):
        return False
    if payload_ip != host_ip:
        return False
    if time.time() - issued > max_age_seconds:
        return False
    expected = hmac.new(secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


# ============================================================================
# Authentication
# ============================================================================

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

    def __init__(self, auth_path, db_path=None):
        self.path = auth_path
        self.lock = threading.Lock()
        self._failed_attempts = {}  # ip -> [timestamp, ...]
        self._attempts_db = self._open_attempts_db(db_path) if db_path else None
        self.data = self._load()
        if self._attempts_db:
            self._load_attempts()

    def _open_attempts_db(self, db_path):
        import sqlite3
        try:
            conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS login_attempts "
                "(ip TEXT NOT NULL, timestamp INTEGER NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_attempts_ip ON login_attempts(ip)"
            )
            return conn
        except Exception as e:
            logging.warning(f"AuthManager: could not open attempts DB at {db_path}: {e}; brute-force attempts will not persist")
            return None

    def _load_attempts(self):
        cutoff = time.time() - self.LOCKOUT_MINUTES * 60
        rows = self._attempts_db.execute(
            "SELECT ip, timestamp FROM login_attempts WHERE timestamp > ?", (cutoff,)
        ).fetchall()
        for ip, ts in rows:
            self._failed_attempts.setdefault(ip, []).append(ts)

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
            user["session_gen"] = int(user.get("session_gen", 0)) + 1
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
        if self._attempts_db:
            self._attempts_db.execute(
                "DELETE FROM login_attempts WHERE ip = ? AND timestamp <= ?",
                (ip, cutoff),
            )

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
            if self._attempts_db:
                self._attempts_db.execute(
                    "INSERT INTO login_attempts (ip, timestamp) VALUES (?, ?)",
                    (ip, int(now)),
                )

    def record_successful_login(self, ip):
        with self.lock:
            self._failed_attempts.pop(ip, None)
            if self._attempts_db:
                self._attempts_db.execute(
                    "DELETE FROM login_attempts WHERE ip = ?", (ip,)
                )

    def close(self):
        with self.lock:
            if self._attempts_db:
                self._attempts_db.close()
                self._attempts_db = None

    # ── Session cookie signing ──────────────────────────────────────────────

    def make_session_cookie(self, username):
        expiry = int(time.time()) + self.SESSION_DAYS * 86400
        with self.lock:
            user = self.data["users"].get(username, {})
            gen = int(user.get("session_gen", 0))
        payload = f"{username}|{expiry}|{gen}"
        secret = self.data["secret_key"].encode()
        sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
        token = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        return f"{token}.{sig}"

    def verify_session_cookie(self, cookie_value):
        """Return (username, admin_bool) if valid, or (None, False).

        The payload carries a per-user session generation; bumping it on
        password change (or deleting the user) invalidates old cookies."""
        if not cookie_value or "." not in cookie_value:
            return None, False
        try:
            token, sig = cookie_value.rsplit(".", 1)
            padded = token + "=" * (-len(token) % 4)
            payload = base64.urlsafe_b64decode(padded.encode()).decode()
            username, expiry_s, gen_s = payload.split("|")
            expiry = int(expiry_s)
            gen = int(gen_s)
        except (ValueError, UnicodeDecodeError, base64.binascii.Error):
            return None, False
        if expiry < time.time():
            return None, False
        secret = self.data["secret_key"].encode()
        expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None, False
        with self.lock:
            user = self.data["users"].get(username)
            if not user or int(user.get("session_gen", 0)) != gen:
                return None, False
            return username, bool(user.get("admin"))

    def csrf_token_for_cookie(self, cookie_value):
        """Derive a CSRF token from a session cookie value.

        Stateless by design: no separate token store. The token changes
        whenever the underlying session cookie does (new login, password
        change bumping session_gen), so it can't outlive the session it
        belongs to."""
        secret = self.data["secret_key"].encode()
        return hmac.new(secret, f"csrf:{cookie_value}".encode(), hashlib.sha256).hexdigest()


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
