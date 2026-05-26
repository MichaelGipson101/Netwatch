# Netwatch AI Assistant — Design Spec
**Date:** 2026-05-26
**Status:** Approved

## Overview

A floating chat assistant embedded in `dashboard.html` that lets the user query their homelab using natural language. The assistant is aware of the current page context (host status, inventory, incidents, topology) and communicates with the OpenRouter API using streaming responses. The feature is gated on the presence of an `openrouter_api_key` in `hosts.yaml` — if absent, nothing renders.

---

## Architecture

**Pure frontend implementation.** All AI interaction logic lives in `dashboard.html`. The only backend change to `monitor.py` is a single new endpoint:

- **`GET /api/ai-config`** — returns `{ "api_key": "...", "model": "..." }` from `hosts.yaml` settings. The key is read once on page load and held in JS memory for the session. It is never hardcoded in HTML.

No new Python dependencies are required. OpenRouter is called directly from the browser via `fetch`.

---

## Configuration

Two new optional fields in the `settings` block of `hosts.yaml`:

| Field | Default | Description |
|-------|---------|-------------|
| `openrouter_api_key` | *(none)* | Required to enable the feature. If absent, the chat bubble does not render. |
| `ai_model` | `openrouter/free` | Default model used for new sessions. Can be overridden per-session in the UI dropdown. |

Example:
```yaml
settings:
  openrouter_api_key: sk-or-v1-...
  ai_model: openrouter/free
```

---

## UI

### Chat Bubble
- Fixed-position button, bottom-right corner, matching Netwatch's frosted glass / dark mode aesthetic
- Clicking opens a panel above it: ~380px wide, ~60% viewport height
- If `openrouter_api_key` is not present in config, the bubble is not rendered at all

### Chat Panel
- **Header:** "Netwatch AI" label + model selector dropdown + close button
- **Message thread:** scrollable; user messages right-aligned, AI responses left-aligned; streaming tokens appended in real-time as they arrive
- **Input row:** text field + send button; both disabled while a response is streaming
- **Bubble button state:** subtle pulsing glow animation while a response is in-flight

### Model Selector
Default and available options (all confirmed free on OpenRouter):

| Display Name | Model ID |
|---|---|
| Auto (Free) *(default)* | `openrouter/free` |
| Llama 3.3 70B | `meta-llama/llama-3.3-70b-instruct:free` |
| DeepSeek V4 Flash | `deepseek/deepseek-v4-flash:free` |
| Gemma 4 31B | `google/gemma-4-31b-it:free` |
| Nemotron 3 Super 120B | `nvidia/nemotron-3-super-120b-a12b:free` |

---

## Context Injection

Context is collected **lazily** — only when the user submits a message, never on bubble open. This minimises token usage and avoids unnecessary processing.

Each tab injects a different context payload into the system prompt:

| Active Tab | Context Injected |
|---|---|
| **Dashboard** | All hosts: name, group, status, uptime %, latency, consecutive_down, last_seen_up, active incidents |
| **Topology** | Same as Dashboard + group-level relationships |
| **Inventory** | All hosts: name, group, specs, notes, links, configured services |
| **History** | Recent incident log as rendered on the history tab |

Context is serialized as compact JSON and embedded in the system prompt. A static preamble describes Netwatch, identifies the user as the homelab owner, and instructs the AI to be concise and direct.

Conversation history (prior turns in the session) is included in each API request so the AI has memory within the session. History is held in JS memory only — it is cleared on page refresh and is never persisted to the database.

---

## Streaming

Responses are streamed using `fetch` with `ReadableStream` and the OpenRouter SSE format. Tokens are appended to the AI message bubble incrementally as they arrive. The input field and send button are disabled for the duration of the stream.

---

## Error Handling

- If `/api/ai-config` returns no key, the bubble does not render (feature is silently disabled)
- If the OpenRouter API returns an error (non-200, rate limit, model unavailable), a user-facing error message is shown in the chat thread
- Network errors (fetch failure, timeout) surface a friendly inline error with a retry prompt

---

## What Is Out of Scope

- Persistent chat history (no DB storage)
- Server-side context building or DB queries from the AI endpoint
- Tool calls / function calling (pure chat completion only)
- Automated/scheduled AI reports (possible future feature)
- Any UI outside the chat bubble (no dedicated tab, no inline host annotations)
