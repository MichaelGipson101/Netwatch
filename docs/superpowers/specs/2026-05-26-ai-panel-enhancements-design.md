# AI Panel Enhancements — Design Spec
**Date:** 2026-05-26
**Status:** Approved

## Overview

Three enhancements to the existing AI chat bubble in `dashboard.html`:

1. **Usage modal** — per-model session token stats + live OpenRouter account info
2. **Inline markdown renderer** — assistant messages rendered as HTML, not plain text
3. **Mobile optimization** — panel layout, input zoom fix, tap delay fix, button repositioning

All changes are confined to `dashboard.html` (single-file deployment). No backend changes.

---

## 1. Usage Tracking & Modal

### Data Source

- **Session stats**: Captured from the `usage` field in the final SSE chunk of each stream response. OpenRouter includes this on the last `data:` event before `[DONE]` as a top-level field alongside `model` (the resolved model ID, e.g. `google/gemma-4-31b-it-20260402:free`).
- **Account info**: Fetched live from `https://openrouter.ai/api/v1/auth/key` with the Bearer token on every modal open. Not cached — always fresh.

### localStorage Schema

Key: `nw-ai-usage`

```json
{
  "google/gemma-4-31b-it-20260402:free": {
    "requests": 3,
    "prompt_tokens": 412,
    "completion_tokens": 87
  },
  "nvidia/nemotron-3-super-120b-a12b:free": {
    "requests": 1,
    "prompt_tokens": 198,
    "completion_tokens": 44
  }
}
```

Keyed by the resolved model ID from the response (not the selector value) so auto-routed models are tracked accurately. Accumulated additively on each completed stream.

### Stream Capture

Inside the stream loop, track usage whenever it appears on any parsed chunk (it arrives on the chunk with `finish_reason: stop`, not necessarily the final network chunk):

```js
let _streamUsage = null;
let _streamModel = null;
// inside the for(line of lines) loop:
if (parsed.usage) { _streamUsage = parsed.usage; _streamModel = parsed.model; }
```

After the loop, if `_streamUsage` was set, call `_accumulateUsage(_streamModel, _streamUsage)`.

`_accumulateUsage(modelId, usage)` reads `nw-ai-usage` from localStorage, adds `prompt_tokens`, `completion_tokens`, and increments `requests` by 1, then writes back.

### Modal Layout

```
┌─────────────────────────────────────┐
│ AI Usage                        [×] │
├─────────────────────────────────────┤
│ ACCOUNT                             │
│ Free tier · 50 req / 10s           │
│ 12 requests used this period        │
├─────────────────────────────────────┤
│ SESSION                             │
│ Model              Req  In    Out   │
│ ─────────────────────────────────── │
│ gemma-4-31b        3    412   87    │
│ nemotron-120b      1    198   44    │
│ ─────────────────────────────────── │
│ Total              4    610   131   │
├─────────────────────────────────────┤
│ [↺ Refresh]          [Clear data]   │
└─────────────────────────────────────┘
```

- Model names truncated for display: strip trailing `:<variant>` (e.g. `:free`, `:nitro`) and trailing `-YYYYMMDD` date suffixes (e.g. `-20260402`), then take the part after the last `/`. Example: `google/gemma-4-31b-it-20260402:free` → `gemma-4-31b-it`
- "Refresh" re-fetches `/api/v1/auth/key` and redraws the account section
- "Clear data" deletes `nw-ai-usage` from localStorage and resets the session table to empty
- "×" closes the modal and returns to the chat view

### Modal Trigger

A bar-chart icon button (`⬛` SVG) in the AI panel header, between the model selector and the close button. The modal is a `<div>` overlay rendered inside `#ai-panel` (not a full-page overlay), inheriting the panel's border-radius and shadow.

### OpenRouter Key API Response Shape

```json
{
  "data": {
    "label": "...",
    "usage": 0.0,
    "limit": null,
    "is_free_tier": true,
    "rate_limit": {
      "requests": 50,
      "interval": "10s"
    }
  }
}
```

Display `rate_limit.requests` / `rate_limit.interval` for the rate limit line. Display `usage` (request count this window) for the "used this period" line. If `is_free_tier` is true, show "Free tier"; otherwise show "Paid".

---

## 2. Inline Markdown Renderer

### Function

`_renderMarkdown(text)` — takes raw assistant text, returns an HTML string safe for `innerHTML` assignment.

### Processing Order

All steps applied in sequence. HTML escaping happens first to prevent XSS before any pattern introduces angle brackets.

| Step | Pattern | Output |
|------|---------|--------|
| 1 | Escape `&`, `<`, `>` | Safe HTML entities |
| 2 | Fenced code blocks ` ```lang\n...\n``` ` | `<pre><code>...</code></pre>` |
| 3 | Inline code `` `code` `` | `<code>code</code>` |
| 4 | `**bold**` | `<strong>bold</strong>` |
| 5 | `*italic*` or `_italic_` | `<em>italic</em>` |
| 6 | `### Heading` / `#### Heading` | `<h4>` / `<h5>` (h1–h2 too large for panel) |
| 7 | Unordered list lines (`- ` or `* `) | Grouped into `<ul><li>` |
| 8 | Ordered list lines (`1. `) | Grouped into `<ol><li>` |
| 9 | Double newline | `<p>` paragraph break |
| 10 | Bare URLs | `<a href="..." target="_blank" rel="noopener noreferrer">` |

### Streaming Behavior

`assistantDiv.innerHTML = _renderMarkdown(assistantText)` replaces the previous `textContent` assignment. Partial markdown mid-stream (e.g. unclosed `**`) may flicker briefly but resolves as tokens arrive. No special partial-render handling required.

### Scope

Applied only to assistant messages. User messages continue to use `textContent` (no markdown rendering for user input).

---

## 3. Mobile Optimization

### Problems

| Issue | Root Cause |
|-------|-----------|
| Panel overflows on narrow screens | Fixed `width:380px` wider than e.g. 375px iPhone |
| iOS auto-zoom on input focus | `#ai-input` font-size is 13px (iOS zooms inputs < 16px) |
| 300ms tap delay on bubble button | No `touch-action: manipulation` |
| Panel obscured by keyboard | Fixed `bottom:146px` doesn't account for virtual keyboard |

### Fixes

**AI bubble button:**
- Add `touch-action: manipulation` to remove tap delay
- On `≤480px`: move to `bottom:24px; left:24px` so it doesn't overlap the FAB (which stays `bottom:28px; right:28px`)

**AI panel:**
- On `≤480px`: `left:8px; right:8px; width:auto; bottom:8px` — full-width, pinned near the bottom of the viewport above browser chrome, clear of both buttons
- `max-height` stays `60vh`

**AI input:**
- On `≤480px`: `font-size:16px` — prevents iOS auto-zoom on focus

---

## Non-Goals

- No persistent server-side usage logging
- No historical usage across browser clears (localStorage only)
- No paid-tier credit balance display (free tier only, dollar amounts omitted)
- No push notifications for rate limit approaching
