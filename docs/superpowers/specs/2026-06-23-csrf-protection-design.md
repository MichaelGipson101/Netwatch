# CSRF Protection — Design Spec

**Date:** 2026-06-23
**Status:** Approved

---

## Overview

Netwatch's session cookie (`nw_session`) is `HttpOnly; SameSite=Strict`, which blocks the most common CSRF vector, but there's no explicit CSRF token on state-changing endpoints (Proxmox guest actions, WoL, settings, user management, inventory writes). This adds a synchronizer-token check so a cross-site request can't ride an authenticated session even in browsers/configurations where `SameSite=Strict` alone isn't trusted as sufficient.

Three external scripts also call the API as authenticated clients (not just the dashboard's own JS):

- `siliconboard` — reads inventory, writes `cpu_score` via what it currently sends as an HTTP `PATCH` to `/api/inventory/{system}` (the server only implements `do_GET`/`do_POST`; this call 501s today and needs fixing to `POST /api/inventory/{id}` regardless of CSRF work)
- `hearthboard` — read-only, no changes needed
- `home-scripts/netwatch_cmdb.py` — reads inventory, writes notes via `POST /api/inventory/{id}`

Both writers need to be updated to carry the new token.

---

## Architecture

Sessions are stateless: `nw_session` is a signed cookie (`username|expiry|gen` + HMAC-SHA256 over `secret_key`), with no server-side session store. The CSRF token follows the same stateless pattern instead of introducing one:

```
csrf_token = HMAC-SHA256(secret_key, "csrf:" + raw_session_cookie_value).hexdigest()
```

- No new storage, no expiry bookkeeping.
- Token is implicitly invalidated whenever the session cookie is (new login, password change bumping `session_gen`, logout).
- Token is per-session, not per-request — same value for the cookie's lifetime, which is fine since it's never exposed over the wire except in the two JSON responses below (not in headers/URLs that could leak via logs/Referer).

**Token delivery** — two existing JSON responses gain a `csrf_token` field:
- `POST /api/auth/login` success response
- `GET /api/auth/status` response (covers page reload, where the cookie already exists and no fresh login happens)

**Token validation** — `_require_auth()` is the single chokepoint already called by every protected handler. It gains a check that only runs when `self.command == "POST"`:
1. Read `X-CSRF-Token` header.
2. Recompute the expected token from the current request's `nw_session` cookie value.
3. `hmac.compare_digest` against the header value.
4. Mismatch or missing header → `403 {"error": "csrf_required"}`.

GET requests are unaffected (safe by definition). This means no per-endpoint changes — every existing `if not self._require_auth(): return` call site is covered automatically.

**Exemptions** (endpoints that don't call `_require_auth()` at all, so naturally unaffected):
- `POST /api/auth/login` — no session exists yet to derive a token from; this *is* the trust boundary.
- `POST /api/auth/setup` — same reasoning, first-run only.
- `POST /api/auth/logout` — worst case of a forced cross-site logout is an annoyance, not a security issue; adding friction here isn't worth it.

---

## Frontend changes

`static/auth.js` (or wherever the login/status flow lives) stores `csrf_token` in memory after login and after `/api/auth/status` on page load. Every mutating `fetch()` call across `static/*.js` (Proxmox actions, WoL, settings, inventory writes, user management) adds the header:

```js
headers: { "X-CSRF-Token": csrfToken }
```

A shared fetch helper is the natural place to add this once, rather than touching every call site individually — check whether one already exists in `static/utils.js`.

---

## External client updates

**`siliconboard`** (`app/netwatch.py`):
- Fix `patch_cpu_score` to `POST /api/inventory/{system}` instead of `PATCH` (pre-existing bug, unrelated to CSRF but blocking either way).
- Capture `csrf_token` from the `/api/auth/login` response in `login()`.
- Add `X-CSRF-Token` to `_auth_headers()` (or a separate header set used only for the POST call) alongside the `Cookie` header.
- Re-derive the token after the existing 401→re-login retry path, same as the cookie.

**`home-scripts/netwatch_cmdb.py`**:
- `netwatch_session()` currently returns a bare `requests.Session()` after login; capture `csrf_token` from the login JSON and store it (e.g. return a small `(session, csrf_token)` tuple or stash it as a session attribute).
- `post_note()` adds `headers={"X-CSRF-Token": csrf_token}` to its `session.post(...)` call.

**`hearthboard`**: no changes — it never calls a mutating endpoint.

---

## Testing

- Unit tests for `_require_auth()`: POST without token → 403; POST with wrong token → 403; POST with correct token → passes through; GET never checks regardless of header.
- Test that `csrf_token` appears in both `/api/auth/login` and `/api/auth/status` responses.
- Test that token changes after a password change (new `session_gen` → new cookie → new derived token) and after a fresh login (new expiry → new cookie → new token).
- Existing 130 tests must continue passing; mutating-endpoint tests in `test_netwatch.py` will need the header added to their request fixtures.

---

## Out of scope

- Rotating the token independent of the session (not needed — stateless derivation ties it to session lifetime already).
- Origin/Referer header checking (considered, rejected — weaker guarantee, and the external script clients don't reliably set `Origin` anyway, so it wouldn't even simplify their side).
