# AI Assistant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a floating AI chat bubble to the Netwatch dashboard that lets the user query their homelab using natural language, with page-aware context injected lazily on each message send.

**Architecture:** Pure frontend — the browser calls the OpenRouter API directly. `monitor.py` adds a single new `/api/ai-config` GET endpoint that serves `openrouter_api_key` and `ai_model` from `hosts.yaml` settings. All chat logic, context building, and streaming live in `dashboard.html`. If `openrouter_api_key` is absent from `hosts.yaml`, the bubble does not render.

**Tech Stack:** Python (stdlib only, no new deps), vanilla JS with `fetch` + `ReadableStream` for SSE streaming, CSS custom properties from existing Netwatch palette.

---

## File Map

| File | Change |
|---|---|
| `monitor.py` | Add `GET /api/ai-config` in `do_GET` block (after line 2781) |
| `tests/test_netwatch.py` | Add two HTTP-level tests for `/api/ai-config` |
| `dashboard.html` | Add AI bubble CSS (before `</style>` at line 1159), HTML (before `</body>`), JS (before `</script>`) |

---

## Task 1: Test and implement `/api/ai-config` in `monitor.py`

**Files:**
- Modify: `tests/test_netwatch.py`
- Modify: `monitor.py` (after line 2781)

- [ ] **Step 1: Add `make_handler` to imports in test file**

In `tests/test_netwatch.py`, find the line:
```python
from monitor import HistoryDB, InventoryDB, build_api_payload
```
Replace with:
```python
from monitor import HistoryDB, InventoryDB, build_api_payload, make_handler
```

- [ ] **Step 2: Write the two failing tests**

Add to the bottom of `tests/test_netwatch.py`:

```python
import json as _json
import threading as _threading
import urllib.request as _urlreq
import urllib.error as _urlerr
from http.server import ThreadingHTTPServer as _THTS


def _ai_config_server(settings):
    """Spin up a single-request test server for /api/ai-config. Returns (server, port)."""
    hm = _FakeHostManager([])
    handler = make_handler(hm, settings, "/dev/null", auth_manager=None)
    server = _THTS(("127.0.0.1", 0), handler)
    return server, server.server_address[1]


def test_ai_config_returns_key_and_model():
    settings = {"openrouter_api_key": "sk-or-test-123", "ai_model": "deepseek/deepseek-v4-flash:free"}
    server, port = _ai_config_server(settings)
    t = _threading.Thread(target=server.handle_request)
    t.start()
    try:
        with _urlreq.urlopen(f"http://127.0.0.1:{port}/api/ai-config") as r:
            data = _json.loads(r.read())
        assert data["api_key"] == "sk-or-test-123"
        assert data["model"] == "deepseek/deepseek-v4-flash:free"
    finally:
        server.server_close()
        t.join()


def test_ai_config_missing_key_returns_404():
    server, port = _ai_config_server({})
    t = _threading.Thread(target=server.handle_request)
    t.start()
    try:
        try:
            _urlreq.urlopen(f"http://127.0.0.1:{port}/api/ai-config")
            assert False, "Expected 404"
        except _urlerr.HTTPError as e:
            assert e.code == 404
    finally:
        server.server_close()
        t.join()
```

- [ ] **Step 3: Run the tests to confirm they fail**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py::test_ai_config_returns_key_and_model tests/test_netwatch.py::test_ai_config_missing_key_returns_404 -v
```
Expected: both FAIL with `404` / `KeyError` or similar — the endpoint doesn't exist yet.

- [ ] **Step 4: Implement the endpoint in `monitor.py`**

In `monitor.py`, find this block (around line 2780):
```python
            if self.path == "/api/status":
                if not self._require_auth():
                    return
                self._send_json(200, build_api_payload(host_manager, settings, incident_log, inventory_db))
                return
```

Add the new endpoint immediately after it (after the `return` on the last line of that block):
```python
            if self.path == "/api/ai-config":
                if not self._require_auth():
                    return
                api_key = settings.get("openrouter_api_key", "")
                if not api_key:
                    self._send_json(404, {"error": "ai_not_configured"})
                    return
                self._send_json(200, {
                    "api_key": api_key,
                    "model": settings.get("ai_model", "openrouter/free"),
                })
                return
```

- [ ] **Step 5: Run the tests to confirm they pass**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/test_netwatch.py::test_ai_config_returns_key_and_model tests/test_netwatch.py::test_ai_config_missing_key_returns_404 -v
```
Expected: both PASS.

- [ ] **Step 6: Run the full test suite to check for regressions**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/ -v
```
Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git -C /home/mgipson/netwatch add monitor.py tests/test_netwatch.py
git -C /home/mgipson/netwatch commit -m "feat: add /api/ai-config endpoint for AI chat bubble"
```

---

## Task 2: Add AI bubble CSS to `dashboard.html`

**Files:**
- Modify: `dashboard.html` (before `</style>` at line 1159)

- [ ] **Step 1: Insert the CSS block**

Find the line in `dashboard.html`:
```css
[data-theme="dark"] .row-extra{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.07);border-radius:8px}
@media(prefers-color-scheme:dark){[data-theme="auto"] .row-extra{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.07);border-radius:8px}}
</style>
```

Replace with:
```css
[data-theme="dark"] .row-extra{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.07);border-radius:8px}
@media(prefers-color-scheme:dark){[data-theme="auto"] .row-extra{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.07);border-radius:8px}}
/* ── AI Chat Bubble ─────────────────────────────────────────────────────── */
#ai-bubble-btn{position:fixed;bottom:24px;right:24px;z-index:8000;width:48px;height:48px;border-radius:50%;background:var(--text);color:var(--surface);border:none;cursor:pointer;display:flex;align-items:center;justify-content:center;box-shadow:0 2px 12px rgba(0,0,0,.18);transition:transform .15s,box-shadow .15s}
#ai-bubble-btn:hover{transform:scale(1.07);box-shadow:0 4px 20px rgba(0,0,0,.28)}
#ai-bubble-btn.streaming{animation:ai-pulse 1.2s ease-in-out infinite}
@keyframes ai-pulse{0%,100%{box-shadow:0 2px 12px rgba(0,0,0,.18)}50%{box-shadow:0 0 0 7px rgba(34,197,94,.22),0 2px 12px rgba(0,0,0,.18)}}
#ai-panel{position:fixed;bottom:82px;right:24px;z-index:8000;width:380px;max-height:60vh;display:flex;flex-direction:column;border-radius:14px;overflow:hidden;border:1px solid var(--border);background:var(--surface);box-shadow:0 8px 32px rgba(0,0,0,.18);transition:opacity .15s,transform .15s}
#ai-panel.hidden{display:none}
[data-theme="dark"] #ai-panel{background:rgba(20,22,26,.88);backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);border-color:rgba(255,255,255,.10)}
@media(prefers-color-scheme:dark){[data-theme="auto"] #ai-panel{background:rgba(20,22,26,.88);backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);border-color:rgba(255,255,255,.10)}}
.ai-panel-header{display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid var(--border);flex-shrink:0}
.ai-panel-title{font-size:13px;font-weight:600;flex:1;color:var(--text)}
.ai-model-select{font-size:11px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);padding:3px 6px;cursor:pointer;font-family:'DM Mono',monospace;max-width:130px}
.ai-close-btn{background:none;border:none;color:var(--muted);cursor:pointer;padding:2px 4px;display:flex;align-items:center;justify-content:center;border-radius:4px;transition:color .12s}
.ai-close-btn:hover{color:var(--text)}
.ai-messages{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:10px;min-height:80px}
.ai-msg{max-width:88%;padding:8px 11px;border-radius:10px;font-size:13px;line-height:1.55;word-break:break-word;white-space:pre-wrap}
.ai-msg.user{align-self:flex-end;background:var(--text);color:var(--surface);border-radius:10px 10px 2px 10px}
.ai-msg.assistant{align-self:flex-start;background:var(--bg);border:1px solid var(--border);border-radius:10px 10px 10px 2px}
[data-theme="dark"] .ai-msg.assistant{background:rgba(255,255,255,.06)}
@media(prefers-color-scheme:dark){[data-theme="auto"] .ai-msg.assistant{background:rgba(255,255,255,.06)}}
.ai-msg.error{align-self:flex-start;background:var(--red-bg);color:var(--red-text);border:1px solid rgba(220,38,38,.2);border-radius:10px 10px 10px 2px}
.ai-input-row{display:flex;gap:6px;padding:8px 10px;border-top:1px solid var(--border);flex-shrink:0;align-items:flex-end}
#ai-input{flex:1;border:1px solid var(--border);border-radius:7px;padding:7px 10px;font-size:13px;background:var(--bg);color:var(--text);outline:none;resize:none;font-family:'DM Sans',sans-serif;line-height:1.4;max-height:100px;overflow-y:auto}
#ai-input:focus{border-color:var(--text)}
#ai-input:disabled,#ai-send:disabled{opacity:.5;cursor:not-allowed}
</style>
```

- [ ] **Step 2: Verify syntax (no parse errors)**

```bash
python3 -c "
import re
with open('/home/mgipson/netwatch/dashboard.html') as f:
    content = f.read()
assert content.count('#ai-bubble-btn') >= 1, 'CSS not inserted'
print('CSS block present')
"
```
Expected output: `CSS block present`

---

## Task 3: Add AI bubble HTML to `dashboard.html`

**Files:**
- Modify: `dashboard.html` (before `</body>`)

- [ ] **Step 1: Insert the HTML**

Find the exact string in `dashboard.html`:
```html
</script>
</body>
</html>
```

Replace with:
```html
</script>
<!-- AI Chat Bubble -->
<button id="ai-bubble-btn" title="Netwatch AI" aria-label="Open AI assistant" style="display:none">
  <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
</button>
<div id="ai-panel" class="hidden" role="dialog" aria-label="Netwatch AI assistant">
  <div class="ai-panel-header">
    <span class="ai-panel-title">Netwatch AI</span>
    <select id="ai-model-select" class="ai-model-select" title="Model">
      <option value="openrouter/free">Auto (Free)</option>
      <option value="meta-llama/llama-3.3-70b-instruct:free">Llama 3.3 70B</option>
      <option value="deepseek/deepseek-v4-flash:free">DeepSeek V4 Flash</option>
      <option value="google/gemma-4-31b-it:free">Gemma 4 31B</option>
      <option value="nvidia/nemotron-3-super-120b-a12b:free">Nemotron 3 Super 120B</option>
    </select>
    <button class="ai-close-btn" id="ai-close-btn" title="Close" aria-label="Close">
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    </button>
  </div>
  <div class="ai-messages" id="ai-messages"></div>
  <div class="ai-input-row">
    <textarea id="ai-input" rows="1" placeholder="Ask about your homelab…"></textarea>
    <button class="btn btn-primary" id="ai-send">Send</button>
  </div>
</div>
</body>
</html>
```

- [ ] **Step 2: Verify HTML inserted**

```bash
python3 -c "
with open('/home/mgipson/netwatch/dashboard.html') as f:
    content = f.read()
assert 'id=\"ai-bubble-btn\"' in content, 'bubble button missing'
assert 'id=\"ai-panel\"' in content, 'panel missing'
assert 'id=\"ai-messages\"' in content, 'messages div missing'
assert 'id=\"ai-input\"' in content, 'input missing'
print('HTML block present')
"
```
Expected output: `HTML block present`

---

## Task 4: Add AI JavaScript to `dashboard.html`

**Files:**
- Modify: `dashboard.html` (before `</script>` near end of file)

- [ ] **Step 1: Insert the JS block**

Find the exact string near the end of `dashboard.html`:
```javascript
// fetchAuthState() hides the landing page and calls refresh() once authenticated
fetchAuthState();
setInterval(fetchAuthState, 60000);
setInterval(refresh, REFRESH);
setInterval(clockTick, 1000);
clockTick();
</script>
```

Replace with:
```javascript
// fetchAuthState() hides the landing page and calls refresh() once authenticated
fetchAuthState();
setInterval(fetchAuthState, 60000);
setInterval(refresh, REFRESH);
setInterval(clockTick, 1000);
clockTick();

// ── AI Chat Bubble ─────────────────────────────────────────────────────────
(function(){
  let _aiKey = null;
  let _aiStreaming = false;
  let _aiHistory = [];

  const _btn    = document.getElementById('ai-bubble-btn');
  const _panel  = document.getElementById('ai-panel');
  const _close  = document.getElementById('ai-close-btn');
  const _msgs   = document.getElementById('ai-messages');
  const _input  = document.getElementById('ai-input');
  const _send   = document.getElementById('ai-send');
  const _model  = document.getElementById('ai-model-select');

  async function _initAi(){
    try{
      const r = await fetch('/api/ai-config');
      if(!r.ok) return;
      const cfg = await r.json();
      _aiKey = cfg.api_key;
      if(cfg.model && _model.querySelector(`option[value="${cfg.model}"]`)){
        _model.value = cfg.model;
      }
      _btn.style.display = 'flex';
    }catch(e){}
  }

  function _buildContext(){
    const tab = localStorage.getItem('nw-tab') || 'topology';
    if(tab === 'inventory'){
      return {
        page: 'inventory',
        items: (typeof _inventoryData !== 'undefined' ? _inventoryData : []).map(i => ({
          name: i.system, ip: i.ip, device_type: i.device_type,
          specs: i.specs, notes: i.notes
        }))
      };
    }
    if(tab === 'events'){
      return {page:'events', events:(lastData && lastData.events)||[]};
    }
    const hosts = (lastData && lastData.hosts)||[];
    return {
      page: tab,
      summary: lastData && lastData.summary,
      hosts: hosts.map(h => ({
        name:h.name, group:h.group, status:h.status, is_up:h.is_up,
        uptime_pct:h.uptime_pct, latency_ms:h.latency_ms,
        consecutive_down:h.consecutive_down,
        last_seen_up_seconds:h.last_seen_up_seconds,
        specs:h.specs, notes:h.notes, services:h.services
      }))
    };
  }

  function _appendMsg(role, text){
    const d = document.createElement('div');
    d.className = 'ai-msg ' + role;
    d.textContent = text;
    _msgs.appendChild(d);
    _msgs.scrollTop = _msgs.scrollHeight;
    return d;
  }

  function _setStreaming(on){
    _aiStreaming = on;
    _btn.classList.toggle('streaming', on);
    _send.disabled = on;
    _input.disabled = on;
  }

  async function _sendMessage(){
    const text = _input.value.trim();
    if(!text || _aiStreaming || !_aiKey) return;
    _input.value = '';

    _appendMsg('user', text);
    _aiHistory.push({role:'user', content:text});

    const ctx = _buildContext();
    const systemPrompt = `You are a homelab assistant embedded in Netwatch, a network monitor. The user is the homelab owner and an experienced sysadmin. Be concise and direct.\n\nCurrent page context (${ctx.page}):\n${JSON.stringify(ctx)}`;

    const messages = [{role:'system', content:systemPrompt}, ..._aiHistory];
    const assistantDiv = _appendMsg('assistant', '');
    _setStreaming(true);

    try{
      const resp = await fetch('https://openrouter.ai/api/v1/chat/completions',{
        method:'POST',
        headers:{
          'Authorization':'Bearer '+_aiKey,
          'Content-Type':'application/json',
          'HTTP-Referer':window.location.origin,
          'X-Title':'Netwatch AI'
        },
        body:JSON.stringify({model:_model.value, messages, stream:true})
      });

      if(!resp.ok){
        let errMsg = `Error ${resp.status}`;
        try{ const e = await resp.json(); errMsg = e.error?.message || errMsg; }catch(e){}
        assistantDiv.className = 'ai-msg error';
        assistantDiv.textContent = errMsg;
        _aiHistory.pop();
        _setStreaming(false);
        return;
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let assistantText = '';

      while(true){
        const {done, value} = await reader.read();
        if(done) break;
        const chunk = decoder.decode(value, {stream:true});
        for(const line of chunk.split('\n')){
          if(!line.startsWith('data: ')) continue;
          const raw = line.slice(6).trim();
          if(raw === '[DONE]') break;
          try{
            const parsed = JSON.parse(raw);
            const delta = parsed.choices?.[0]?.delta?.content || '';
            assistantText += delta;
            assistantDiv.textContent = assistantText;
            _msgs.scrollTop = _msgs.scrollHeight;
          }catch(e){}
        }
      }

      _aiHistory.push({role:'assistant', content:assistantText});
    }catch(e){
      assistantDiv.className = 'ai-msg error';
      assistantDiv.textContent = 'Network error: ' + e.message;
      _aiHistory.pop();
    }

    _setStreaming(false);
  }

  _btn.addEventListener('click', ()=>{
    _panel.classList.toggle('hidden');
    if(!_panel.classList.contains('hidden')) _input.focus();
  });
  _close.addEventListener('click', ()=> _panel.classList.add('hidden'));
  _send.addEventListener('click', _sendMessage);
  _input.addEventListener('keydown', e=>{
    if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); _sendMessage(); }
  });

  _initAi();
})();
</script>
```

- [ ] **Step 2: Verify JS inserted**

```bash
python3 -c "
with open('/home/mgipson/netwatch/dashboard.html') as f:
    content = f.read()
assert '_initAi' in content, 'AI JS missing'
assert '_buildContext' in content, 'context builder missing'
assert '_sendMessage' in content, 'sendMessage missing'
print('JS block present')
"
```
Expected output: `JS block present`

- [ ] **Step 3: Run the full test suite one more time**

```bash
cd /home/mgipson/netwatch && python -m pytest tests/ -v
```
Expected: all tests PASS.

- [ ] **Step 4: Commit**

```bash
git -C /home/mgipson/netwatch add dashboard.html
git -C /home/mgipson/netwatch commit -m "feat: add AI chat bubble to dashboard"
```

---

## Task 5: Wire `hosts.yaml` and smoke-test

**Files:**
- Modify: `hosts.yaml` (add `openrouter_api_key` and `ai_model` to `settings`)

- [ ] **Step 1: Add config to `hosts.yaml`**

Open `/home/mgipson/netwatch/hosts.yaml`. Under the `settings:` block, add:
```yaml
  openrouter_api_key: sk-or-v1-REDACTED-LEAKED-KEY
  ai_model: openrouter/free
```

- [ ] **Step 2: Restart netwatch**

```bash
sudo systemctl restart netwatch
```

Or if running manually:
```bash
cd /home/mgipson/netwatch && python monitor.py --no-tui
```

- [ ] **Step 3: Verify `/api/ai-config` is live**

```bash
curl -s -b "nw_session=$(cat /home/mgipson/netwatch/auth.json | python3 -c 'import sys,json; d=json.load(sys.stdin); print(list(d.get("sessions",{}).keys())[0])' 2>/dev/null || echo '')" http://localhost:8080/api/ai-config
```

Expected: JSON with `api_key` and `model` fields. If auth is needed, log in via the browser first and use the session cookie.

- [ ] **Step 4: Manual browser test**

Open `http://<pi-ip>:8080` in a browser. Confirm:
1. The chat bubble button is visible in the bottom-right corner
2. Clicking it opens the frosted glass panel with model selector
3. Typing a message and pressing Send or Enter sends it
4. Tokens stream in real time
5. Switching tabs and sending a message reflects the correct page context (check that inventory questions get inventory data, etc.)
6. Closing the panel and re-opening preserves chat history for the session

- [ ] **Step 5: Commit `hosts.yaml`**

> **Note:** `hosts.yaml` contains your live API key — confirm you are comfortable committing it (it's a local private repo). If not, skip this step and manage the key manually.

```bash
git -C /home/mgipson/netwatch add hosts.yaml
git -C /home/mgipson/netwatch commit -m "config: add openrouter AI settings to hosts.yaml"
```
