# TrueNAS Alerts in the NAS Panel — Design Spec

**Date:** 2026-06-24
**Status:** Approved

---

## Overview

TrueNAS's own alert system (`/api/v2.0/alert/list`) already surfaces operationally useful information netwatch doesn't show anywhere today — app update availability, replication success/failure events, and pool-level warnings (e.g. a `PoolUSBDisks` warning flagging a USB-enclosure-backed pool, which this user has confirmed is an intentional setup and wants permanently dismissed, not just hidden once). This adds a filtered, dismissible TrueNAS alert feed to the NAS panel.

---

## Architecture

`NASPoller._poll()` adds one more fetch — `/api/v2.0/alert/list` — alongside its existing pool/scrub/replication/system-info calls. A new pure function, `_filter_alerts(raw_alerts, ignored_klasses)`, keeps only alerts at `WARNING` severity or above and drops any whose `klass` is in the ignore list. The filtered result is stored in the existing poller cache (`self._cache["alerts"]`), so it's already present in whatever `/api/nas` returns — no new GET endpoint required.

`_check_alerts` gains a loop over these filtered alerts, using each TrueNAS alert's own `id` as the condition key into the existing `_fire_alert`/`_clear_alert` ntfy machinery (the same one-fire-until-cleared semantics pool health and replication alerts already use). When an alert disappears from TrueNAS's own list (resolved or newly ignored), the next poll simply won't re-fire it, and `_check_alerts` clears any previously-armed state for IDs no longer present.

---

## Ignore-list storage and endpoints

A new setting, `truenas_ignored_alert_klasses` — a comma-separated string, stored in `hosts.yaml` exactly like other string-typed settings (`SETTINGS_EDITABLE_KEYS`'s existing `str` type, no new type system needed). Two new admin-only POST endpoints, following the same shape as other mutating endpoints in `monitor.py`:

- `POST /api/nas/ignore-alert` — body `{"klass": "PoolUSBDisks"}`, appends to the list (de-duplicated) and persists via the existing `save_hosts_config` path.
- `POST /api/nas/unignore-alert` — body `{"klass": "PoolUSBDisks"}`, removes it from the list.

Filtering happens server-side in `_h_get_nas` (via `_filter_alerts`), so a non-admin viewer of the dashboard never sees a dismissed alert category at all — consistent with `/api/nas` itself only requiring login (`_require_auth()`), while the two new mutating endpoints require `admin_only=True`, matching the precedent of every other settings-mutating endpoint in the app.

---

## UI

`static/nas.js`'s `renderNas()` gains a new block, rendered above the existing per-pool sections, only when there's at least one alert left after filtering (renders nothing on a clean day, no empty-state clutter). Each entry shows:
- A severity badge (reusing the existing `nas-badge-ok`/`nas-badge-err`-style classing, mapped from TrueNAS's `level` field)
- The alert's human-readable message (TrueNAS's `formatted` field, falling back to `text`)
- A "Dismiss" button, visible only when the logged-in user is an admin (mirrors how other admin-only UI affordances already check `_authState.admin` elsewhere in the dashboard), which calls `POST /api/nas/ignore-alert` with that alert's `klass` and removes it from the rendered list immediately on success.

No "un-ignore" UI is in scope for this pass — reversing a dismissal (if ever needed) is a direct `hosts.yaml` edit for now, consistent with YAGNI; revisit if it turns out to matter in practice.

---

## Testing

- `_filter_alerts(raw_alerts, ignored_klasses) -> list`: pure function, full pytest coverage — severity threshold (INFO excluded, WARNING/ERROR/CRITICAL kept), klass-based exclusion, case where the ignore list is empty, case where every alert is filtered out (empty result).
- `_h_post_nas_ignore_alert` / `_h_post_nas_unignore_alert`: request-shape tests matching the existing `_h_post_*` pattern (missing `klass` → 400, successful add/remove, de-duplication on repeated ignore calls).
- `_check_alerts`'s new TrueNAS-alert loop: same `patch("monitor._send_alert_async")` test style already used for pool/replication alerts — fires once per alert id, clears when the alert disappears from the next poll's filtered list.
- No automated test for the actual `/api/v2.0/alert/list` HTTP call itself, consistent with how none of `NASPoller`'s other `_fetch` calls are unit-tested — verified manually against the live TrueNAS instance, same as the rest of this session's NAS work.

---

## Out of scope

- Un-ignoring a klass via the UI (direct `hosts.yaml` edit suffices for now).
- Per-deployment severity threshold configuration (hardcoded at `WARNING` and above for this pass).
- Surfacing TrueNAS alerts anywhere outside the NAS panel (e.g. the topology view or host drawer).
