# CSRF Protection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a synchronizer-token CSRF check to every state-changing (`POST`) endpoint behind `_require_auth()`, derived statelessly from the existing signed session cookie, and propagate the change to the dashboard frontend and the two external scripts that write to the API.

**Architecture:** `AuthManager` gains `csrf_token_for_cookie(cookie_value)`, an HMAC-SHA256 over `"csrf:" + cookie_value` keyed by the same `secret_key` already used to sign sessions — no new storage. The token is handed to clients in the `/api/auth/login` and `/api/auth/status` JSON responses. `_require_auth()` validates `X-CSRF-Token` against the recomputed value for every `POST` request it gates. The dashboard frontend stores the token after login/status and attaches it via a new `apiFetch()` helper used at all mutating call sites. Two external repos (`siliconboard`, `home-scripts`) get matching client-side updates.

**Tech Stack:** Python stdlib (`hmac`, `hashlib`), no new dependencies. Plain `<script>`-tag JS (no bundler/modules) for the frontend.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-23-csrf-protection-design.md`
- No new server-side session storage — token derivation must stay stateless (HMAC over the existing signed cookie value).
- CSRF check only applies to `self.command == "POST"`; GETs are never checked.
- `/api/auth/login`, `/api/auth/setup`, `/api/auth/logout` are exempt (none call `_require_auth()` today — do not add a call).
- All 130 existing tests in `tests/test_netwatch.py` must keep passing; only 2 of them (`test_post_hosts_requires_admin`, `test_non_dict_json_body_rejected`) exercise the real HTTP layer through `_require_auth()` and need a valid token added — every other `_h_post_*` test calls the handler function directly and bypasses `_require_auth()` entirely, so it is unaffected.
- No CDN dependencies; vanilla JS only, matching existing `static/*.js` style (global functions, no ES modules).

---

## Part A — Backend (`monitor.py`)

### Task 1: `AuthManager.csrf_token_for_cookie()`

**Files:**
- Modify: `monitor.py` (add method to `AuthManager`, after `verify_session_cookie`, ~line 977)
- Test: `tests/test_netwatch.py`

**Interfaces:**
- Produces: `AuthManager.csrf_token_for_cookie(self, cookie_value: str) -> str` — hex digest, used by Tasks 2-4.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_netwatch.py`, near the other `AuthManager` session tests (after `test_legacy_two_part_cookie_rejected`, ~line 859):

```python
def test_csrf_token_for_cookie_deterministic():
    with tempfile.TemporaryDirectory() as td:
        auth = _make_auth(td)
        token1 = auth.csrf_token_for_cookie("some-cookie-value")
        token2 = auth.csrf_token_for_cookie("some-cookie-value")
        assert token1 == token2
        assert len(token1) == 64  # hex sha256 digest


def test_csrf_token_for_cookie_differs_per_cookie():
    with tempfile.TemporaryDirectory() as td:
        auth = _make_auth(td)
        token_a = auth.csrf_token_for_cookie("cookie-a")
        token_b = auth.csrf_token_for_cookie("cookie-b")
        assert token_a != token_b


def test_csrf_token_for_cookie_differs_per_secret():
    with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
        auth1 = _make_auth(td1)
        auth2 = _make_auth(td2)
        assert auth1.csrf_token_for_cookie("same-cookie") != auth2.csrf_token_for_cookie("same-cookie")
```

Check `_make_auth` exists already (it's used throughout the file, e.g. `test_legacy_two_part_cookie_rejected`) — it constructs an `AuthManager` pointed at a temp dir's `auth.json`, generating a fresh random `secret_key` each time, which is exactly why the third test proves the token is keyed by `secret_key`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_netwatch.py -k csrf_token_for_cookie -v`
Expected: FAIL with `AttributeError: 'AuthManager' object has no attribute 'csrf_token_for_cookie'`

- [ ] **Step 3: Implement**

In `monitor.py`, add this method to `AuthManager` directly after `verify_session_cookie` (after line 977, before the blank lines preceding `def parse_cookies`):

```python
    def csrf_token_for_cookie(self, cookie_value):
        """Derive a CSRF token from a session cookie value.

        Stateless by design: no separate token store. The token changes
        whenever the underlying session cookie does (new login, password
        change bumping session_gen), so it can't outlive the session it
        belongs to."""
        secret = self.data["secret_key"].encode()
        return hmac.new(secret, f"csrf:{cookie_value}".encode(), hashlib.sha256).hexdigest()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_netwatch.py -k csrf_token_for_cookie -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add monitor.py tests/test_netwatch.py
git commit -m "feat: add AuthManager.csrf_token_for_cookie for stateless CSRF tokens"
```

---

### Task 2: Add raw cookie access + thread it into `_h_get_auth_status`

**Files:**
- Modify: `monitor.py` (handler class methods ~line 4001-4039, `_h_get_auth_status` ~line 3679)
- Test: `tests/test_netwatch.py`

**Interfaces:**
- Consumes: `AuthManager.csrf_token_for_cookie` from Task 1.
- Produces: `handler._session_cookie_value(self) -> str` (raw `nw_session` cookie string, `""` if absent) — used by Task 3 (login) and Task 4 (CSRF validation in `_require_auth`). `_h_get_auth_status(auth_manager, current_user_fn, cookie_value) -> tuple` — new required third parameter.

- [ ] **Step 1: Write the failing tests**

The three existing `_h_get_auth_status` tests (lines ~443-466) call it with 2 args. Update them and add one new test for the token field. Replace the existing block:

```python
def test_h_get_auth_status_no_auth_manager():
    code, body = _h_get_auth_status(None, lambda: (None, False), "")
    assert code == 200
    assert body["logged_in"] is False
    assert body["setup_required"] is False


def test_h_get_auth_status_logged_in():
    class FakeAM:
        has_users = True
        def csrf_token_for_cookie(self, cookie_value):
            return "deadbeef"
    code, body = _h_get_auth_status(FakeAM(), lambda: ("alice", True), "some-cookie")
    assert code == 200
    assert body["logged_in"] is True
    assert body["username"] == "alice"
    assert body["admin"] is True
    assert body["csrf_token"] == "deadbeef"


def test_h_get_auth_status_setup_required():
    class FakeAM:
        has_users = False
    code, body = _h_get_auth_status(FakeAM(), lambda: (None, False), "")
    assert code == 200
    assert body["setup_required"] is True
    assert "csrf_token" not in body


def test_h_get_auth_status_logged_out_no_csrf_token():
    class FakeAM:
        has_users = True
    code, body = _h_get_auth_status(FakeAM(), lambda: (None, False), "")
    assert code == 200
    assert body["logged_in"] is False
    assert "csrf_token" not in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_netwatch.py -k h_get_auth_status -v`
Expected: FAIL — `TypeError: _h_get_auth_status() takes 2 positional arguments but 3 were given` (for the 3 updated tests), and `AttributeError` or `KeyError: 'csrf_token'` for the new one.

- [ ] **Step 3: Implement**

Update `_h_get_auth_status` (monitor.py ~line 3679):

```python
def _h_get_auth_status(auth_manager, current_user_fn, cookie_value) -> tuple:
    user, is_admin = current_user_fn() if auth_manager else (None, False)
    result = {
        "logged_in":      bool(user),
        "username":       user,
        "admin":          is_admin,
        "setup_required": bool(auth_manager and not auth_manager.has_users),
    }
    if user and auth_manager:
        result["csrf_token"] = auth_manager.csrf_token_for_cookie(cookie_value)
    return 200, result
```

Add a cookie-access helper to the handler class and use it from `_current_user`. In monitor.py, replace `_current_user` (~line 4005-4010):

```python
        def _session_cookie_value(self):
            cookies = parse_cookies(self.headers.get("Cookie", ""))
            return cookies.get("nw_session", "")

        def _current_user(self):
            """Returns (username, is_admin) or (None, False) if not logged in."""
            if not auth_manager:
                return None, False
            return auth_manager.verify_session_cookie(self._session_cookie_value())
```

Update the `/api/auth/status` call site (~line 4148-4150):

```python
            if self.path == "/api/auth/status":
                self._send_json(*_h_get_auth_status(auth_manager, self._current_user, self._session_cookie_value()))
                return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_netwatch.py -k h_get_auth_status -v`
Expected: 4 passed

- [ ] **Step 5: Run the full suite to check for regressions from the signature change**

Run: `python3 -m pytest tests/test_netwatch.py -v 2>&1 | tail -20`
Expected: all passing (no other call site uses `_h_get_auth_status` or the old `_current_user` body directly)

- [ ] **Step 6: Commit**

```bash
git add monitor.py tests/test_netwatch.py
git commit -m "feat: surface csrf_token in /api/auth/status when logged in"
```

---

### Task 3: Return `csrf_token` from `/api/auth/login`

**Files:**
- Modify: `monitor.py` (`_set_session_cookie` ~line 4031, login handler ~line 4244-4276)
- Test: `tests/test_netwatch.py`

**Interfaces:**
- Consumes: `_session_cookie_value` (Task 2, only needed conceptually — login derives the cookie value directly from `make_session_cookie`'s return value, not from the request, since the cookie doesn't exist yet at request time), `csrf_token_for_cookie` (Task 1).
- Produces: `_set_session_cookie(self, username) -> str` — now returns the raw cookie value it just set, so the login handler can derive the token from it.

- [ ] **Step 1: Write the failing test**

Add an integration test near `test_post_hosts_requires_admin` (~line 887), using the same `_auth_test_server` helper:

```python
def test_login_response_includes_csrf_token(tmp_path):
    auth = AuthManager(str(tmp_path / "auth.json"))
    auth.create_user("root", "password123", admin=True)
    server, port, t = _auth_test_server(auth)
    try:
        body = _json.dumps({"username": "root", "password": "password123"}).encode()
        req = _urlreq.Request(f"http://127.0.0.1:{port}/api/auth/login", data=body,
                              method="POST", headers={"Content-Type": "application/json"})
        resp = _urlreq.urlopen(req)
        data = _json.loads(resp.read())
        assert data["ok"] is True
        assert "csrf_token" in data
        assert len(data["csrf_token"]) == 64
    finally:
        server.server_close()
        t.join()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_netwatch.py -k test_login_response_includes_csrf_token -v`
Expected: FAIL — `KeyError: 'csrf_token'`

- [ ] **Step 3: Implement**

Update `_set_session_cookie` (monitor.py ~line 4031-4035) to return the cookie value:

```python
        def _set_session_cookie(self, username):
            cookie = auth_manager.make_session_cookie(username)
            max_age = auth_manager.SESSION_DAYS * 86400
            self.send_header("Set-Cookie",
                f"nw_session={cookie}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict")
            return cookie
```

Update the login handler (monitor.py ~line 4256-4267):

```python
                    if auth_manager.verify_password(username, password):
                        auth_manager.record_successful_login(ip)
                        is_admin = auth_manager.is_admin(username.strip().lower())
                        cookie = auth_manager.make_session_cookie(username.strip().lower())
                        csrf_token = auth_manager.csrf_token_for_cookie(cookie)
                        max_age = auth_manager.SESSION_DAYS * 86400
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Set-Cookie",
                            f"nw_session={cookie}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict")
                        self.end_headers()
                        self.wfile.write(json.dumps({
                            "ok": True,
                            "username": username.strip().lower(),
                            "admin": is_admin,
                            "csrf_token": csrf_token,
                        }).encode())
```

This inlines what `_set_session_cookie` did rather than calling it, because the handler needs the raw `cookie` value to derive `csrf_token` before writing headers — calling `_set_session_cookie` and capturing its return value works identically and is less duplication; use that instead:

```python
                    if auth_manager.verify_password(username, password):
                        auth_manager.record_successful_login(ip)
                        is_admin = auth_manager.is_admin(username.strip().lower())
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        cookie = self._set_session_cookie(username.strip().lower())
                        self.end_headers()
                        self.wfile.write(json.dumps({
                            "ok": True,
                            "username": username.strip().lower(),
                            "admin": is_admin,
                            "csrf_token": auth_manager.csrf_token_for_cookie(cookie),
                        }).encode())
```

Use this second version (it's the one that ships) — it keeps `_set_session_cookie` as the single place that builds and sends the cookie header.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_netwatch.py -k test_login_response_includes_csrf_token -v`
Expected: 1 passed

- [ ] **Step 5: Run full suite**

Run: `python3 -m pytest tests/test_netwatch.py -v 2>&1 | tail -10`
Expected: all passing

- [ ] **Step 6: Commit**

```bash
git add monitor.py tests/test_netwatch.py
git commit -m "feat: return csrf_token from /api/auth/login"
```

---

### Task 4: Enforce CSRF in `_require_auth()`

**Files:**
- Modify: `monitor.py` (`_require_auth` ~line 4012-4029)
- Test: `tests/test_netwatch.py`

**Interfaces:**
- Consumes: `_session_cookie_value` (Task 2), `csrf_token_for_cookie` (Task 1).
- Produces: `_require_auth()` now returns `False` and sends `403 {"error": "csrf_required"}` for any `POST` request with a missing/incorrect `X-CSRF-Token` header, checked after the existing auth/admin checks so error precedence stays `401 auth_required` → `403 admin_required` → `403 csrf_required` → success.

- [ ] **Step 1: Write the failing tests**

Add these tests near `test_post_hosts_requires_admin` (~line 887-901):

```python
def test_post_without_csrf_token_rejected(tmp_path):
    auth = AuthManager(str(tmp_path / "auth.json"))
    auth.create_user("root", "password123", admin=True)
    server, port, t = _auth_test_server(auth)
    try:
        cookie = auth.make_session_cookie("root")
        req = _urlreq.Request(f"http://127.0.0.1:{port}/api/hosts", data=b'{"hosts": []}',
                              method="POST", headers={"Cookie": f"nw_session={cookie}"})
        try:
            _urlreq.urlopen(req)
            assert False, "expected 403"
        except _urlerr.HTTPError as e:
            assert e.code == 403
            body = _json.loads(e.read())
            assert body["error"] == "csrf_required"
    finally:
        server.server_close()
        t.join()


def test_post_with_wrong_csrf_token_rejected(tmp_path):
    auth = AuthManager(str(tmp_path / "auth.json"))
    auth.create_user("root", "password123", admin=True)
    server, port, t = _auth_test_server(auth)
    try:
        cookie = auth.make_session_cookie("root")
        req = _urlreq.Request(f"http://127.0.0.1:{port}/api/hosts", data=b'{"hosts": []}',
                              method="POST", headers={"Cookie": f"nw_session={cookie}",
                                                       "X-CSRF-Token": "wrong-token"})
        try:
            _urlreq.urlopen(req)
            assert False, "expected 403"
        except _urlerr.HTTPError as e:
            assert e.code == 403
            body = _json.loads(e.read())
            assert body["error"] == "csrf_required"
    finally:
        server.server_close()
        t.join()


def test_post_with_correct_csrf_token_accepted(tmp_path):
    auth = AuthManager(str(tmp_path / "auth.json"))
    auth.create_user("root", "password123", admin=True)
    server, port, t = _auth_test_server(auth)
    try:
        cookie = auth.make_session_cookie("root")
        token = auth.csrf_token_for_cookie(cookie)
        req = _urlreq.Request(f"http://127.0.0.1:{port}/api/hosts", data=b'{"hosts": []}',
                              method="POST", headers={"Cookie": f"nw_session={cookie}",
                                                       "X-CSRF-Token": token,
                                                       "Content-Type": "application/json"})
        resp = _urlreq.urlopen(req)
        assert resp.status == 200
    finally:
        server.server_close()
        t.join()
```

Update the existing `test_non_dict_json_body_rejected` (~line 916-930) to include a valid token, since it now must pass the CSRF gate before reaching body validation:

```python
def test_non_dict_json_body_rejected(tmp_path):
    auth = AuthManager(str(tmp_path / "auth.json"))
    auth.create_user("root", "password123", admin=True)
    server, port, t = _auth_test_server(auth)
    try:
        cookie = auth.make_session_cookie("root")
        token = auth.csrf_token_for_cookie(cookie)
        req = _urlreq.Request(f"http://127.0.0.1:{port}/api/hosts", data=b'[1,2,3]',
                              method="POST", headers={"Cookie": f"nw_session={cookie}",
                                                       "X-CSRF-Token": token})
        try:
            _urlreq.urlopen(req)
            assert False, "expected 400"
        except _urlerr.HTTPError as e:
            assert e.code == 400
    finally:
        server.server_close()
        t.join()
```

`test_post_hosts_requires_admin` needs no change — `bob` fails the admin check before the CSRF check runs, so it still gets `403` (now for a different reason if it had a token, but it has none either way and the admin check runs first).

- [ ] **Step 2: Run new/updated tests to verify they fail correctly**

Run: `python3 -m pytest tests/test_netwatch.py -k "csrf_token_rejected or csrf_token_accepted or non_dict_json_body_rejected" -v`
Expected: `test_post_without_csrf_token_rejected` and `test_post_with_wrong_csrf_token_rejected` FAIL (request currently succeeds with 200, not 403); `test_post_with_correct_csrf_token_accepted` passes trivially today (no check exists yet) — that's fine, it'll stay green; `test_non_dict_json_body_rejected` still passes today (token is extraneous until Step 3 lands) but will break later in the suite for the *other* two new tests if the gate is in place — re-run after Step 3, not before, is the meaningful check. Run the full new set after Step 3 instead.

- [ ] **Step 3: Implement**

Update `_require_auth` (monitor.py ~line 4012-4029):

```python
        def _require_auth(self, admin_only=False):
            """Returns True if request is authorised, else writes an error
            response and returns False. If no users exist yet, returns False
            with a 'setup_required' response so the frontend can prompt for
            first-run setup. POST requests additionally require a valid
            X-CSRF-Token header matching the session cookie."""
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
            if self.command == "POST":
                expected = auth_manager.csrf_token_for_cookie(self._session_cookie_value())
                provided = self.headers.get("X-CSRF-Token", "")
                if not provided or not hmac.compare_digest(expected, provided):
                    self._send_json(403, {"error": "csrf_required"})
                    return False
            return True
```

- [ ] **Step 4: Run all CSRF-related tests**

Run: `python3 -m pytest tests/test_netwatch.py -k "csrf" -v`
Expected: all passing (this now includes Tasks 1-3's tests plus this task's 4 new/updated ones)

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest tests/test_netwatch.py -v 2>&1 | tail -15`
Expected: all passing — this confirms the assumption that no other test in the suite calls a `POST` endpoint through the real HTTP layer (everything else calls `_h_post_*` functions directly, bypassing `_require_auth()`).

- [ ] **Step 6: Commit**

```bash
git add monitor.py tests/test_netwatch.py
git commit -m "feat: enforce CSRF token on POST requests in _require_auth"
```

---

## Part B — Frontend (`static/*.js`)

### Task 5: `apiFetch()` helper + token capture in `_authState`

**Files:**
- Modify: `static/utils.js` (add helper, loads first per `dashboard.html`)
- Modify: `static/auth.js` (`_authState`, `fetchAuthState`, `submitLandingLogin`, `submitLogin`, `submitLandingSetup`, `submitSetup`)

**Interfaces:**
- Produces: `apiFetch(url, options)` — global function in `utils.js`, drop-in replacement for `fetch()` that adds `X-CSRF-Token: _authState.csrf_token` whenever `options.method` is `'POST'` (case-insensitive) and a token is known. `_authState.csrf_token` — new field, `null` until login/status provides one.

- [ ] **Step 1: Add the helper to `static/utils.js`**

Append after `macValid` (~line 113), before `setStatus`:

```js
// Drop-in replacement for fetch() that attaches the CSRF token (captured in
// _authState by auth.js) to mutating requests. _authState may not exist yet
// if this is called before auth.js loads, hence the typeof guard.
function apiFetch(url, options){
  options = options || {};
  const method = (options.method || 'GET').toUpperCase();
  if(method === 'POST' && typeof _authState !== 'undefined' && _authState.csrf_token){
    options.headers = Object.assign({}, options.headers, { 'X-CSRF-Token': _authState.csrf_token });
  }
  return fetch(url, options);
}
```

- [ ] **Step 2: Capture the token in `static/auth.js`**

Update `_authState` initializer (line 2):

```js
let _authState = { logged_in: false, username: null, admin: false, setup_required: false, csrf_token: null };
```

In `fetchAuthState` (lines 4-21), `_authState = await res.json()` already replaces the whole object, so the `csrf_token` field from `/api/auth/status` (Task 2) lands automatically — no change needed there.

In `submitLandingLogin` (~line 141), `submitLogin` (~line 200), and `submitLandingSetup`/`submitSetup` (~line 164, ~line 272), each does `_authState = { logged_in: true, username: data.username, admin: data.admin, setup_required: false };` — add the token field to each of these four object literals:

```js
    _authState = { logged_in: true, username: data.username, admin: data.admin, setup_required: false, csrf_token: data.csrf_token };
```

(`submitLandingSetup`/`submitSetup` use `admin: true` instead of `data.admin` — keep that, just add `csrf_token: data.csrf_token` alongside it. `/api/auth/setup` doesn't return `csrf_token` per the design — setup is exempt from CSRF entirely, and the resulting session still needs one for subsequent calls. Add this to Task 3's scope check.)

- [ ] **Step 3: Verify manually**

This codebase has no JS test runner — verify in-browser per the project's UI-change convention:

```bash
python3 monitor.py --no-tui --port 8080
```

Open `http://127.0.0.1:8080`, open devtools console, log in, and run:

```js
console.log(_authState.csrf_token)
```

Expected: a 64-character hex string, not `null`/`undefined`.

- [ ] **Step 4: Commit**

```bash
git add static/utils.js static/auth.js
git commit -m "feat: add apiFetch helper and capture csrf_token in auth state"
```

**Follow-up required:** Task 3 only added `csrf_token` to the `/api/auth/login` response. `/api/auth/setup` needs the same treatment so first-run setup sessions get a token too — fold this into Task 3 before starting Task 6, or add it now:

- [ ] **Step 5: Add `csrf_token` to `/api/auth/setup` response**

Find the setup handler in `monitor.py` (`if self.path == "/api/auth/setup":`, ~line 4212). It creates the first user and logs them in; locate where it calls `self._set_session_cookie(...)` and apply the same pattern as Task 3's login change — capture the return value and add `"csrf_token": auth_manager.csrf_token_for_cookie(cookie)` to the JSON response written.

- [ ] **Step 6: Add a test**

Mirror `test_login_response_includes_csrf_token` from Task 3, posting to `/api/auth/setup` against a fresh `AuthManager` with no users yet, asserting `"csrf_token" in data`.

Run: `python3 -m pytest tests/test_netwatch.py -k setup -v`
Expected: all passing including the new test.

- [ ] **Step 7: Commit**

```bash
git add monitor.py tests/test_netwatch.py
git commit -m "feat: return csrf_token from /api/auth/setup"
```

---

### Task 6: Wire `apiFetch` into every mutating call site

**Files:**
- Modify: `static/auth.js` (lines 207, 220)
- Modify: `static/core.js` (lines 830, 941, 1033, 1102, 1212, 1326)
- Modify: `static/inventory.js` (lines 802, 818, 1126, 1147, 1254)
- Modify: `static/proxmox.js` (line 218)
- Modify: `static/settings.js` (lines 66, 262)
- Modify: `static/ai-panel.js` (line 167)

**Interfaces:**
- Consumes: `apiFetch` from Task 5.

This is the same mechanical change at every call site: replace `fetch(` with `apiFetch(` for every call whose `options.method` is `'POST'`. `/api/auth/login`, `/api/auth/setup`, and `/api/auth/logout` calls in `auth.js` (lines 134, 157, 193, 207, 265) stay as plain `fetch()` — login/setup have no token yet to send, and logout is server-exempt, so there's no benefit to changing them (sending a stale/absent header would be harmless but pointless).

- [ ] **Step 1: `static/auth.js` — backup download (line 220)**

```js
    const res = await apiFetch('/api/backup', { method: 'POST' });
```

- [ ] **Step 2: `static/core.js` — 6 call sites (lines 830, 941, 1033, 1102, 1212, 1326)**

Each currently reads `const res = await fetch('/api/...', {` or `fetch('/api/discover', { method: 'POST' })` (line 1102). Change each `fetch(` to `apiFetch(` — the rest of each call (headers, body) is unchanged. Confirm with:

```bash
grep -n "fetch('/api/wake'\|fetch('/api/hosts'\|fetch('/api/detect-mac'\|fetch('/api/discover', { method: 'POST'" static/core.js
```

Expected: 6 lines, all now starting with `apiFetch(`.

- [ ] **Step 3: `static/inventory.js` — 5 call sites (lines 802, 818, 1126, 1147, 1254)**

Same mechanical change: `fetch(url, {` → `apiFetch(url, {` at line 802 (the create/update save), `fetch('/api/inventory/' + _editingInvId + '/delete', ...)` at line 818, `fetch('/api/inventory/' + deviceId + '/connections', ...)` at line 1126, `fetch('/api/connections/' + connId + '/delete', ...)` at line 1147, and `fetch('/api/wake', ...)` at line 1254.

Leave line 1362 (`/api/inventory-import`, `body: fd` where `fd` is a `FormData`) as a normal `fetch()` for now if `apiFetch`'s header-merge logic would conflict with `FormData`'s auto-set `Content-Type` — it won't, since `apiFetch` only adds `X-CSRF-Token`, not `Content-Type`. Change it too:

```js
    const res = await apiFetch('/api/inventory-import', { method: 'POST', body: fd });
```

- [ ] **Step 4: `static/proxmox.js` — line 218**

```js
    apiFetch('/api/proxmox/action', {
```

- [ ] **Step 5: `static/settings.js` — lines 66, 262**

Both `fetch('/api/settings', {` → `apiFetch('/api/settings', {`.

- [ ] **Step 6: `static/ai-panel.js` — line 167**

```js
      const resp = await apiFetch('/api/ai/chat',{
```

- [ ] **Step 7: Verify no POST call site was missed**

```bash
grep -n "fetch(" static/*.js | grep -v apiFetch
```

Inspect the output: every remaining plain `fetch(` call should be either a `GET` (no `method` specified, or none of the lines say `method: 'POST'`/`method:'POST'`) or one of the three exempt auth calls (`/api/auth/login`, `/api/auth/setup`, `/api/auth/logout`). If any other `POST` call shows up here, it was missed — fix it before continuing.

- [ ] **Step 8: Manual verification in browser**

```bash
python3 monitor.py --no-tui --port 8080
```

Log in, then exercise each changed surface and confirm no 403s in the Network tab and no functional regression:
- Settings panel: change and save a setting
- Inventory: create, edit, and delete a record; add and remove a connection
- Host editor: add/edit a host (saves via `/api/hosts`)
- Wake-on-LAN button on a host with a MAC on record
- Proxmox tab: start/stop/reboot a guest (if Proxmox is configured) — otherwise confirm the request at least reaches the server with the header attached, via devtools
- Mira chat panel: send a message
- Download backup (Settings → Download backup)
- Inventory XLSX import

- [ ] **Step 9: Run the Python test suite once more to confirm no backend regression**

Run: `python3 -m pytest tests/test_netwatch.py -v 2>&1 | tail -10`
Expected: all passing (frontend-only change, but cheap to confirm nothing else shifted)

- [ ] **Step 10: Commit**

```bash
git add static/auth.js static/core.js static/inventory.js static/proxmox.js static/settings.js static/ai-panel.js
git commit -m "feat: attach X-CSRF-Token header to all mutating dashboard requests"
```

---

## Part C — External client repos

### Task 7: Fix and update `siliconboard`

**Repo:** `MichaelGipson101/siliconboard` (clone fresh; the version inspected during planning was at `/tmp/siliconboard` but treat that as stale)

**Files:**
- Modify: `app/netwatch.py` (`NetwatchClient` class)

**Interfaces:**
- Consumes: Netwatch's `/api/auth/login` response now including `csrf_token` (Task 3), and the server's enforcement of `X-CSRF-Token` on `POST` (Task 4).

- [ ] **Step 1: Clone fresh and locate the existing tests**

```bash
rm -rf /tmp/siliconboard-work
gh repo clone MichaelGipson101/siliconboard /tmp/siliconboard-work
cd /tmp/siliconboard-work
cat app/netwatch.py
ls tests/
```

Check `tests/test_netwatch.py` in *this* repo (siliconboard's own test file, distinct from netwatch's) for how `NetwatchClient` is tested — likely with a mocked `httpx` transport. Read it before changing the class so the new behavior is tested the same way.

- [ ] **Step 2: Write/update failing tests**

In siliconboard's `tests/test_netwatch.py`, find or add a test that mocks the login response to include `csrf_token` and asserts the client stores it, plus a test that the `PATCH` call (now `POST`) includes the `X-CSRF-Token` header. The exact mock setup depends on what's already there (likely `httpx.MockTransport` or `respx`) — match the existing pattern rather than introducing a new mocking approach.

- [ ] **Step 3: Implement**

Update `NetwatchClient` in `app/netwatch.py`:

```python
class NetwatchClient:
    def __init__(self, base_url: str, username: str, password: str):
        self._base_url = base_url
        self._username = username
        self._password = password
        self._session_cookie: Optional[str] = None
        self._csrf_token: Optional[str] = None
        self._client = httpx.AsyncClient(timeout=10.0)

    async def login(self) -> None:
        resp = await self._client.post(
            f"{self._base_url}/api/auth/login",
            json={"username": self._username, "password": self._password},
        )
        resp.raise_for_status()
        cookie = resp.cookies.get("nw_session")
        if not cookie:
            raise RuntimeError("Login succeeded but no session cookie returned")
        self._session_cookie = cookie
        self._csrf_token = resp.json().get("csrf_token")
        if not self._csrf_token:
            raise RuntimeError("Login succeeded but no csrf_token returned")
        logger.info("Netwatch login successful")

    def _auth_headers(self) -> dict:
        if not self._session_cookie:
            raise RuntimeError("Not logged in — call login() first")
        return {"Cookie": f"nw_session={self._session_cookie}"}

    def _write_headers(self) -> dict:
        return {**self._auth_headers(), "X-CSRF-Token": self._csrf_token}
```

Update `_patch` to `_post` (fixing the verb-mismatch bug — the real server only implements `do_GET`/`do_POST`, so a `PATCH` request 501s today) and have it use `_write_headers()`:

```python
    async def _post(self, path: str, **kwargs) -> dict:
        resp = await self._client.post(
            f"{self._base_url}{path}",
            headers=self._write_headers(),
            **kwargs,
        )
        if resp.status_code == 401:
            logger.warning("Session expired, re-logging in")
            await self.login()
            resp = await self._client.post(
                f"{self._base_url}{path}",
                headers=self._write_headers(),
                **kwargs,
            )
        resp.raise_for_status()
        return resp.json()
```

Update `patch_cpu_score` to use it:

```python
    async def patch_cpu_score(self, system: str, score: int) -> None:
        await self._post(f"/api/inventory/{system}", json={"cpu_score": score})
```

(`_get` is unchanged — reads need no CSRF header.)

- [ ] **Step 4: Run tests**

```bash
cd /tmp/siliconboard-work
python3 -m pytest tests/ -v
```

Expected: all passing.

- [ ] **Step 5: Commit and push**

```bash
cd /tmp/siliconboard-work
git add app/netwatch.py tests/test_netwatch.py
git commit -m "fix: send CSRF token and correct HTTP verb for inventory writes

Netwatch now requires an X-CSRF-Token header on POST requests. While
fixing this, also corrected patch_cpu_score to use POST instead of PATCH —
the Netwatch server only implements do_GET/do_POST, so the PATCH call was
already failing with a 501 independent of CSRF."
git push
```

**Do not push without explicit confirmation from the user first** — this is a separate repo from netwatch and pushing changes there is a distinct action from anything in this plan's main repo.

---

### Task 8: Update `home-scripts/netwatch_cmdb.py`

**Repo:** `MichaelGipson101/home-scripts`

**Files:**
- Modify: `netwatch_cmdb.py`

**Interfaces:**
- Consumes: same login response change as Task 7.

- [ ] **Step 1: Clone fresh**

```bash
rm -rf /tmp/home-scripts-work
gh repo clone MichaelGipson101/home-scripts /tmp/home-scripts-work
cd /tmp/home-scripts-work
```

- [ ] **Step 2: Implement**

This script has no test suite for `netwatch_cmdb.py` specifically (confirm with `ls tests/ 2>/dev/null` or `grep -l netwatch_cmdb *test*`) — if none exists, this is a straightforward edit verified by a manual dry run rather than TDD. Update `netwatch_session()` and `post_note()`:

```python
def netwatch_session():
    s = requests.Session()
    r = s.post(f"{NETWATCH_URL}/api/auth/login",
               json={"username": NETWATCH_USER, "password": NETWATCH_PASS}, timeout=10)
    r.raise_for_status()
    s.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return s
```

(`requests.Session` persists default headers across requests made with that session object, so setting `s.headers["X-CSRF-Token"]` once here means every subsequent `session.get(...)`/`session.post(...)` call — including the existing `GET` calls — automatically carries it. That's harmless for GETs since the server never checks the header on non-POST requests.)

`post_note()` needs no change — it already calls `session.post(...)`, and the session-level header from the change above covers it.

- [ ] **Step 3: Verify manually**

If you have a real `.env` with `NETWATCH_URL`/`NETWATCH_USER`/`NETWATCH_PASS` pointed at a running netwatch instance with this plan's Part A already deployed:

```bash
cd /tmp/home-scripts-work
python3 netwatch_cmdb.py
```

Expected: `[CMDB] Connecting to Netwatch...` followed by normal gap-filling output, no `403`/`csrf_required` errors. If no live instance is available yet, defer this verification until after Part A is deployed.

- [ ] **Step 4: Commit and push**

```bash
cd /tmp/home-scripts-work
git add netwatch_cmdb.py
git commit -m "fix: send CSRF token on Netwatch API session

Netwatch now requires an X-CSRF-Token header on POST requests."
git push
```

**Do not push without explicit confirmation from the user first**, same as Task 7.

---

## Part D — Documentation

### Task 9: Document the CSRF requirement for API consumers

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a note to the existing "Security notes" section**

In `README.md`, find the "Security notes" bullets added by the earlier Proxmox/OpenRouter security commit (search for `**Security notes:**`). Add:

```markdown
- All `POST` requests require an `X-CSRF-Token` header matching a token
  issued in the `/api/auth/login` or `/api/auth/setup` response body
  (`csrf_token` field). The dashboard's own JS handles this automatically;
  any external script calling the API must capture `csrf_token` at login
  and send it back on every mutating request.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: document X-CSRF-Token requirement for API consumers"
```

---

## Self-Review Notes

- **Spec coverage:** token derivation (Task 1), login/status delivery (Tasks 2, 3, 5 follow-up), `_require_auth` enforcement (Task 4), frontend wiring (Tasks 5-6), `siliconboard` fix + update (Task 7), `netwatch_cmdb.py` update (Task 8), `hearthboard`/`netwatch_brief.py` correctly require no task (read-only, confirmed during brainstorming). README update (Task 9) covers the spec's implicit need for API consumers to discover this requirement.
- **Correction from the original spec scope:** the spec assumed many existing tests would need fixture updates; investigation during planning found only 2 do (`test_post_hosts_requires_admin` unaffected in practice, `test_non_dict_json_body_rejected` needs a token) because almost all `_h_post_*` tests call handler functions directly, bypassing `_require_auth()`. Task 4 reflects the corrected, smaller scope.
- **Gap found during planning:** the spec didn't mention `/api/auth/setup` needing a `csrf_token` in its response (only login and status were specified), but the first-run setup flow creates a session exactly like login does and needs the same token. Folded into Task 5 as a follow-up rather than back into Task 3, since it was discovered while wiring up the frontend.
