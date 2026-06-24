# Per-Instance Alert Acknowledge — Design Spec

**Date:** 2026-06-24
**Status:** Approved

---

## Overview

The TrueNAS Alerts card currently has one dismiss action: **Dismiss**, which permanently ignores an entire alert *category* (`klass`) for this netwatch deployment via `truenas_ignored_alert_klasses`. The user wants a lighter second action that hides just the *current occurrence* of an alert without silencing future alerts of that category.

TrueNAS already has exactly this mechanism built in: `/api/v2.0/alert/dismiss`. Confirmed live (against the real TrueNAS instance) that calling it with an alert's ID removes that alert entirely from `/api/v2.0/alert/list` on the next fetch — not flagged-but-still-returned, genuinely gone. This means netwatch needs no new persisted state at all for this feature; it's a thin proxy plus a re-poll.

---

## Architecture

A new admin-only endpoint, `POST /api/nas/acknowledge-alert` (body `{"id": "<alert-id>"}`), calls TrueNAS's `/api/v2.0/alert/dismiss` using the already-stored TrueNAS credentials, then immediately triggers `nas_poller._poll()` (reusing the `force`-poll mechanism added for "Refresh now") so the change reflects on the dashboard right away rather than waiting up to 15 minutes for the next background poll.

No changes are needed to `_filter_alerts`, `_check_alerts`, or any persisted setting: once TrueNAS's own list no longer contains the alert, the existing pipeline already does the right thing — it won't appear in the next filtered `alerts` list, and `_check_alerts`'s existing "clear any `truenas_alert_*` cid no longer present" logic already clears any pending ntfy state for that alert ID.

**Naming:** the button is labeled **"Acknowledge"**, not "Snooze" — there's no timer; TrueNAS doesn't bring the alert back after some delay. It only reappears if the underlying condition recurs and TrueNAS raises a fresh alert for it (same `klass`, likely a new `id` depending on whether the recurrence is treated as a new alert or a continuation of an existing key — either way, "acknowledge" accurately describes "I've seen this specific instance," not "remind me later").

---

## UI

Both actions live in the same alert row, in this order: **Acknowledge** (the lighter, more common action) then **Dismiss** (whole-category, the existing button). Both remain admin-gated identically to the existing Dismiss button (`_authState.admin`), and both call `apiFetch` so the CSRF token is attached.

On a successful Acknowledge, the row is removed from the DOM immediately (same UX as the existing Dismiss success path) with a success toast; on failure, an error toast and the button re-enabled.

---

## Error handling

- If the TrueNAS dismiss call itself fails (network error, TrueNAS API error, invalid/already-resolved alert ID), the endpoint returns an error to the frontend rather than silently succeeding — the row stays in place and the user sees an error toast, consistent with how the existing Dismiss button's failure path works.
- The forced re-poll after a successful dismiss reuses the existing `_poll()` method, which already has its own error handling (a poll failure just logs a warning and marks `reachable: False`, same as any other poll failure) — no special-casing needed here.

---

## Testing

- `_h_post_nas_acknowledge_alert(data, nas_poller) -> tuple`: request-shape tests (missing `id` → 400, poller `None` → 503, successful call triggers both the TrueNAS dismiss HTTP call and a forced re-poll — mocked, matching the existing test style for `_h_post_nas_ignore_alert`).
- No automated test for the actual TrueNAS HTTP call's real-world behavior (consistent with how no other `_fetch` call in `NASPoller` is integration-tested) — already verified manually against the live instance during design.
- Frontend: no automated test (consistent with `dismissNasAlert`'s existing lack of one) — verified manually.

---

## Out of scope

- Any UI to "re-show" an acknowledged alert before TrueNAS itself re-raises it — there's nothing to undo from netwatch's side since TrueNAS owns this state entirely.
- Bulk acknowledge (acknowledge-all) — not requested, can be added later if it turns out to matter.
