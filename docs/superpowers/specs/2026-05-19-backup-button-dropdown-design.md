# Backup Button: Move to Username Dropdown

**Date:** 2026-05-19
**Status:** Approved

## Problem

The "Download backup" button lives in the nav bar alongside the username and Log out button. It's an admin-only, rarely-used action that feels too prominent for its frequency of use.

## Solution

Convert the username display in `#nav-auth` into a clickable dropdown. The backup button is removed from the nav and placed as an item inside the dropdown, alongside Log out.

## Design

### Nav bar

The username text (`admin (admin)`) becomes a `<button>` with a `▾` chevron appended. Clicking it toggles a dropdown panel positioned absolutely below it.

Non-admin users get a dropdown too — theirs contains only "Log out."

### Dropdown panel

- Appears directly below the username button, right-aligned
- Contains (admin): **Download backup**, **Log out**
- Contains (non-admin): **Log out**
- Items styled consistently with the existing `btn-ghost` aesthetic
- A thin border and slight box-shadow to lift it off the page

### Behavior

- Click username → toggle open/closed
- Click outside → close (document-level click handler, removed on close)
- "Download backup" → calls existing `downloadBackup()`
- "Log out" → calls existing `logout()`
- Dropdown closes after either action is triggered

### Scope

`dashboard.html` only — HTML structure, CSS for dropdown, JS in `updateAuthUI()`. No changes to `monitor.py` or the `/api/backup` endpoint.

## Out of scope

- No other admin tools added to the dropdown
- No keyboard navigation (arrow keys, etc.)
- No animation beyond instant show/hide
