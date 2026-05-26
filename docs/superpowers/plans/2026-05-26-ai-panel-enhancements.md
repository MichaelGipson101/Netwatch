# AI Panel Enhancements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add usage tracking modal, inline markdown rendering, and mobile optimizations to the AI chat bubble in `dashboard.html`.

**Architecture:** All changes are confined to `dashboard.html` (single-file app). CSS additions go in the AI Chat Bubble `<style>` block (~line 1159). HTML additions go inside `#ai-panel`. JS additions go inside the `(function(){ ... })()` IIFE at the bottom of the file (~line 5721). No backend changes.

**Tech Stack:** Vanilla JS, CSS, single-file HTML. No external dependencies.

---

## File Map

| File | What changes |
|------|-------------|
| `dashboard.html` CSS block (~line 1159) | Mobile media queries, markdown element styles, usage modal styles, `white-space` fix for assistant bubbles |
| `dashboard.html` HTML (~line 1878) | Usage icon button in panel header, usage modal `<div>` inside `#ai-panel` |
| `dashboard.html` JS IIFE (~line 5721) | `_renderMarkdown()`, `_accumulateUsage()`, `_truncateModelName()`, `_renderUsageBody()`, `_openUsageModal()`, stream loop updates, markdown wiring |

---

## Task 1: Mobile CSS optimizations

**Files:**
- Modify: `dashboard.html` CSS block (AI Chat Bubble section, ~line 1159)

- [ ] **Step 1: Fix `white-space` conflict with future HTML rendering**

The base `.ai-msg` rule has `white-space:pre-wrap` which will break HTML rendered via `innerHTML`. Move it to `.ai-msg.user` only.

Find this line (~1174):
```css
.ai-msg{max-width:88%;padding:8px 11px;border-radius:10px;font-size:13px;line-height:1.55;word-break:break-word;white-space:pre-wrap}
```
Replace with:
```css
.ai-msg{max-width:88%;padding:8px 11px;border-radius:10px;font-size:13px;line-height:1.55;word-break:break-word}
.ai-msg.user{white-space:pre-wrap}
```

- [ ] **Step 2: Add `touch-action` and mobile media queries**

After the last AI CSS rule (`.ai-input-row` and `#ai-input` block, just before `</style>`), append:

```css
#ai-bubble-btn{touch-action:manipulation}
@media(max-width:480px){
  #ai-bubble-btn{bottom:24px;left:24px;right:auto}
  #ai-panel{left:8px;right:8px;width:auto;bottom:8px}
  #ai-input{font-size:16px}
}
```

- [ ] **Step 3: Verify visually**

Open the dashboard in a browser. In DevTools, toggle device toolbar to iPhone SE (375×667). Confirm:
- AI bubble button appears bottom-left, FAB stays bottom-right — no overlap
- Opening the panel shows it spanning nearly full width with 8px margins each side
- Tapping the textarea does not zoom the page

- [ ] **Step 4: Commit**

```bash
git add dashboard.html
git commit -m "fix: mobile AI panel layout, input zoom, and tap delay"
```

---

## Task 2: Markdown renderer

**Files:**
- Modify: `dashboard.html` CSS block (~line 1159) — markdown element styles
- Modify: `dashboard.html` JS IIFE — add `_renderMarkdown()`, wire into message rendering

- [ ] **Step 1: Add CSS for markdown elements inside assistant bubbles**

Append to the AI Chat Bubble CSS block (just before `</style>`, after the mobile media query from Task 1):

```css
.ai-msg.assistant code{font-family:'DM Mono',monospace;font-size:11.5px;background:var(--subtle);padding:1px 4px;border-radius:3px}
.ai-msg.assistant pre{background:var(--subtle);padding:8px 10px;border-radius:6px;overflow-x:auto;margin:4px 0;white-space:pre}
.ai-msg.assistant pre code{background:none;padding:0;font-size:11.5px}
.ai-msg.assistant ul,.ai-msg.assistant ol{padding-left:18px;margin:4px 0}
.ai-msg.assistant li{margin-bottom:1px}
.ai-msg.assistant h4{margin:6px 0 2px;font-size:13px;font-weight:600}
.ai-msg.assistant h5{margin:4px 0 2px;font-size:12px;font-weight:600}
.ai-msg.assistant a{color:var(--blue);text-decoration:underline}
.ai-msg.assistant p{margin:0 0 6px}
.ai-msg.assistant p:last-child{margin-bottom:0}
```

- [ ] **Step 2: Add `_renderMarkdown()` to the JS IIFE**

Add this function inside the IIFE, just before `_buildSystemPrompt` (around line 5889):

```javascript
  function _renderMarkdown(text){
    // 1. Escape HTML first to prevent XSS
    let s = text
      .replace(/&/g,'&amp;')
      .replace(/</g,'&lt;')
      .replace(/>/g,'&gt;');

    // 2. Protect fenced code blocks from further processing
    const _blocks = [];
    s = s.replace(/```[\w]*\n([\s\S]*?)```/g, (_, code) => {
      _blocks.push('<pre><code>' + code.trimEnd() + '</code></pre>');
      return '\x00' + (_blocks.length - 1) + '\x00';
    });

    // 3. Inline code
    s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');

    // 4. Bold
    s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');

    // 5. Italic
    s = s.replace(/\*([^*\n]+)\*/g, '<em>$1</em>');
    s = s.replace(/_([^_\n]+)_/g, '<em>$1</em>');

    // 6. Headings (#### before ### to avoid partial match)
    s = s.replace(/^#### (.+)$/gm, '<h5>$1</h5>');
    s = s.replace(/^### (.+)$/gm, '<h4>$1</h4>');

    // 7. Unordered lists (group consecutive - or * lines)
    s = s.replace(/((?:^[ \t]*[-*] .+$\n?)+)/gm, m => {
      const items = m.trim().split('\n')
        .map(l => '<li>' + l.replace(/^[ \t]*[-*] /, '').trim() + '</li>').join('');
      return '<ul>' + items + '</ul>\n';
    });

    // 8. Ordered lists
    s = s.replace(/((?:^[ \t]*\d+\. .+$\n?)+)/gm, m => {
      const items = m.trim().split('\n')
        .map(l => '<li>' + l.replace(/^[ \t]*\d+\. /, '').trim() + '</li>').join('');
      return '<ol>' + items + '</ol>\n';
    });

    // 9. Newlines → breaks (double newline = paragraph gap)
    s = s.replace(/\n{2,}/g, '<br><br>');
    s = s.replace(/\n/g, '<br>');

    // 10. Bare URLs → links
    s = s.replace(/(?<!['"=])(https?:\/\/[^\s<>"']+)/g,
      '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>');

    // Restore protected code blocks
    s = s.replace(/\x00(\d+)\x00/g, (_, i) => _blocks[+i]);

    return s;
  }
```

- [ ] **Step 3: Wire `_renderMarkdown` into assistant message rendering**

Find `_appendMsg` (around line 5782):
```javascript
  function _appendMsg(role, text){
    const d = document.createElement('div');
    d.className = 'ai-msg ' + role;
    d.textContent = text;
```
Replace with:
```javascript
  function _appendMsg(role, text){
    const d = document.createElement('div');
    d.className = 'ai-msg ' + role;
    if(role === 'assistant' && text) d.innerHTML = _renderMarkdown(text);
    else d.textContent = text;
```

- [ ] **Step 4: Wire `_renderMarkdown` into streaming updates**

In `_sendMessage`, find the streaming delta assignment (inside the `for(const line of lines)` loop):
```javascript
            assistantText += delta;
            assistantDiv.textContent = assistantText;
```
Replace with:
```javascript
            assistantText += delta;
            assistantDiv.innerHTML = _renderMarkdown(assistantText);
```

Also find the `_buf` flush block after the while loop:
```javascript
          if(delta){ assistantText += delta; assistantDiv.textContent = assistantText; }
```
Replace with:
```javascript
          if(delta){ assistantText += delta; assistantDiv.innerHTML = _renderMarkdown(assistantText); }
```

- [ ] **Step 5: Verify rendering**

Open the dashboard AI panel and send: `Can you show me an example with **bold**, *italic*, \`inline code\`, and a list?`

Confirm the response renders formatted HTML, not raw asterisks and backticks.

- [ ] **Step 6: Commit**

```bash
git add dashboard.html
git commit -m "feat: inline markdown renderer for AI assistant messages"
```

---

## Task 3: Usage tracking in stream

**Files:**
- Modify: `dashboard.html` JS IIFE — `_accumulateUsage()` function + stream loop capture

- [ ] **Step 1: Add `_accumulateUsage()` function**

Inside the IIFE, add this function just before `_buildSystemPrompt`:

```javascript
  function _accumulateUsage(modelId, usage){
    if(!modelId || !usage) return;
    try{
      const stored = JSON.parse(localStorage.getItem('nw-ai-usage') || '{}');
      const entry = stored[modelId] || {requests:0, prompt_tokens:0, completion_tokens:0};
      entry.requests += 1;
      entry.prompt_tokens += usage.prompt_tokens || 0;
      entry.completion_tokens += usage.completion_tokens || 0;
      stored[modelId] = entry;
      localStorage.setItem('nw-ai-usage', JSON.stringify(stored));
    }catch(e){}
  }
```

- [ ] **Step 2: Capture usage in the stream loop**

In `_sendMessage`, find the lines just before the `while(true)` loop:
```javascript
      let assistantText = '';
      let _buf = '';
```
Add two tracking variables:
```javascript
      let assistantText = '';
      let _buf = '';
      let _streamUsage = null;
      let _streamModel = null;
```

- [ ] **Step 3: Capture usage inside the `for(const line of lines)` loop**

Find the `try` block inside the for loop that parses each SSE line. It currently ends after the `innerHTML` update. Add usage capture inside the same `try` block:

```javascript
          try{
            const parsed = JSON.parse(raw);
            const delta = parsed.choices?.[0]?.delta?.content || '';
            assistantText += delta;
            assistantDiv.innerHTML = _renderMarkdown(assistantText);
            _msgs.scrollTop = _msgs.scrollHeight;
            if(parsed.usage){ _streamUsage = parsed.usage; _streamModel = parsed.model; }
          }catch(e){}
```

- [ ] **Step 4: Also capture usage in the `_buf` flush block**

Find the `_buf` flush block after the while loop:
```javascript
      if(_buf.startsWith('data: ')){
        const raw = _buf.slice(6).trim();
        if(raw && raw !== '[DONE]'){
          try{
            const parsed = JSON.parse(raw);
            const delta = parsed.choices?.[0]?.delta?.content || '';
            if(delta){ assistantText += delta; assistantDiv.innerHTML = _renderMarkdown(assistantText); }
          }catch(e){}
        }
      }
```
Replace with:
```javascript
      if(_buf.startsWith('data: ')){
        const raw = _buf.slice(6).trim();
        if(raw && raw !== '[DONE]'){
          try{
            const parsed = JSON.parse(raw);
            const delta = parsed.choices?.[0]?.delta?.content || '';
            if(delta){ assistantText += delta; assistantDiv.innerHTML = _renderMarkdown(assistantText); }
            if(parsed.usage){ _streamUsage = parsed.usage; _streamModel = parsed.model; }
          }catch(e){}
        }
      }
```

- [ ] **Step 5: Call `_accumulateUsage` after the flush**

Immediately after the `_buf` flush block and before the `if(assistantText){` check, add:

```javascript
      if(_streamUsage && _streamModel) _accumulateUsage(_streamModel, _streamUsage);
```

- [ ] **Step 6: Verify localStorage accumulation**

Send two messages. Open DevTools → Application → Local Storage → find key `nw-ai-usage`. Confirm an object appears with the resolved model ID as key and `requests`, `prompt_tokens`, `completion_tokens` fields populated.

- [ ] **Step 7: Commit**

```bash
git add dashboard.html
git commit -m "feat: accumulate per-model token usage in localStorage from stream responses"
```

---

## Task 4: Usage modal HTML + CSS

**Files:**
- Modify: `dashboard.html` CSS block (~line 1159) — usage modal styles
- Modify: `dashboard.html` HTML (~line 1878) — usage button + modal div

- [ ] **Step 1: Add usage modal CSS**

Append to the AI Chat Bubble CSS block (after the mobile media query):

```css
.ai-usage-btn{background:none;border:none;color:var(--muted);cursor:pointer;padding:2px 4px;display:flex;align-items:center;justify-content:center;border-radius:4px;transition:color .12s;touch-action:manipulation}
.ai-usage-btn:hover{color:var(--text)}
#ai-usage-modal{position:absolute;inset:0;background:var(--surface);z-index:10;display:flex;flex-direction:column;border-radius:14px}
[data-theme="dark"] #ai-usage-modal{background:rgba(20,22,26,.97)}
@media(prefers-color-scheme:dark){[data-theme="auto"] #ai-usage-modal{background:rgba(20,22,26,.97)}}
#ai-usage-modal.hidden{display:none}
.ai-usage-modal-header{display:flex;align-items:center;padding:10px 12px;border-bottom:1px solid var(--border);flex-shrink:0}
.ai-usage-modal-title{font-size:13px;font-weight:600;flex:1;color:var(--text)}
.ai-usage-modal-body{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:12px}
.ai-usage-section-label{font-size:10px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
.ai-usage-account{font-size:12px;color:var(--text);line-height:1.7}
.ai-usage-table{width:100%;border-collapse:collapse;font-size:12px;font-family:'DM Mono',monospace}
.ai-usage-table th{text-align:left;color:var(--muted);font-weight:500;padding-bottom:5px;border-bottom:1px solid var(--border)}
.ai-usage-table th:not(:first-child),.ai-usage-table td:not(:first-child){text-align:right}
.ai-usage-table td{padding:3px 0;color:var(--text)}
.ai-usage-table tr.ai-total-row td{border-top:1px solid var(--border);padding-top:5px;font-weight:600}
.ai-usage-modal-footer{display:flex;justify-content:space-between;align-items:center;padding:8px 12px;border-top:1px solid var(--border);flex-shrink:0;gap:8px}
.ai-usage-empty{color:var(--muted);font-size:12px;text-align:center;padding:16px 0}
.ai-usage-error{color:var(--red-text);font-size:12px}
```

- [ ] **Step 2: Add usage button to the AI panel header**

Find the AI panel header HTML (~line 1878):
```html
  <div class="ai-panel-header">
    <span class="ai-panel-title">Netwatch AI</span>
    <select id="ai-model-select" class="ai-model-select" title="Model">
```
Add the usage button between the model select and the close button:
```html
  <div class="ai-panel-header">
    <span class="ai-panel-title">Netwatch AI</span>
    <select id="ai-model-select" class="ai-model-select" title="Model">
      <option value="openrouter/free">Auto (Free)</option>
      <option value="meta-llama/llama-3.3-70b-instruct:free">Llama 3.3 70B</option>
      <option value="deepseek/deepseek-v4-flash:free">DeepSeek V4 Flash</option>
      <option value="google/gemma-4-31b-it:free">Gemma 4 31B</option>
      <option value="nvidia/nemotron-3-super-120b-a12b:free">Nemotron 3 Super 120B</option>
    </select>
    <button class="ai-usage-btn" id="ai-usage-btn" title="Usage">
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>
    </button>
    <button class="ai-close-btn" id="ai-close-btn" title="Close" aria-label="Close">
```

- [ ] **Step 3: Add usage modal div inside `#ai-panel`**

Find the closing `</div>` of `#ai-panel` (after the `ai-input-row` div, ~line 1895):
```html
  </div>
</div>
```
Insert the usage modal before the closing `</div>` of `#ai-panel`:
```html
  </div>
  <div id="ai-usage-modal" class="hidden" role="dialog" aria-label="AI usage">
    <div class="ai-usage-modal-header">
      <span class="ai-usage-modal-title">AI Usage</span>
      <button class="ai-close-btn" id="ai-usage-close-btn" title="Close" aria-label="Close">
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="ai-usage-modal-body" id="ai-usage-body"></div>
    <div class="ai-usage-modal-footer">
      <button class="btn" id="ai-usage-refresh-btn">↺ Refresh</button>
      <button class="btn" id="ai-usage-clear-btn">Clear data</button>
    </div>
  </div>
</div>
```

- [ ] **Step 4: Verify HTML structure**

Open the dashboard. Open DevTools → Elements. Confirm `#ai-usage-modal` is a direct child of `#ai-panel`, and `#ai-usage-btn` appears in `.ai-panel-header`. The usage modal should be invisible (`.hidden` class).

- [ ] **Step 5: Commit**

```bash
git add dashboard.html
git commit -m "feat: add usage modal HTML and CSS to AI panel"
```

---

## Task 5: Usage modal JS

**Files:**
- Modify: `dashboard.html` JS IIFE — `_truncateModelName()`, `_renderUsageBody()`, `_openUsageModal()`, event wiring

- [ ] **Step 1: Add element refs at the top of the IIFE**

Find the existing element refs block at the top of the IIFE (~line 5727):
```javascript
  const _btn    = document.getElementById('ai-bubble-btn');
  const _panel  = document.getElementById('ai-panel');
  const _close  = document.getElementById('ai-close-btn');
  const _msgs   = document.getElementById('ai-messages');
  const _input  = document.getElementById('ai-input');
  const _send   = document.getElementById('ai-send');
  const _model  = document.getElementById('ai-model-select');
```
Add usage modal refs after:
```javascript
  const _btn    = document.getElementById('ai-bubble-btn');
  const _panel  = document.getElementById('ai-panel');
  const _close  = document.getElementById('ai-close-btn');
  const _msgs   = document.getElementById('ai-messages');
  const _input  = document.getElementById('ai-input');
  const _send   = document.getElementById('ai-send');
  const _model  = document.getElementById('ai-model-select');
  const _usageBtn   = document.getElementById('ai-usage-btn');
  const _usageModal = document.getElementById('ai-usage-modal');
```

- [ ] **Step 2: Add `_truncateModelName()` function**

Add inside the IIFE, just before `_accumulateUsage`:

```javascript
  function _truncateModelName(id){
    // Strip :variant suffix (e.g. :free), strip -YYYYMMDD date suffix, take part after last /
    return id.replace(/:[^/]+$/, '').replace(/-\d{8}$/, '').split('/').pop();
  }
```

Verify mentally: `google/gemma-4-31b-it-20260402:free`
→ strip `:free` → `google/gemma-4-31b-it-20260402`
→ strip `-20260402` → `google/gemma-4-31b-it`
→ after last `/` → `gemma-4-31b-it` ✓

- [ ] **Step 3: Add `_renderUsageBody()` function**

Add inside the IIFE, after `_truncateModelName`:

```javascript
  function _renderUsageBody(accountHtml){
    const stored = JSON.parse(localStorage.getItem('nw-ai-usage') || '{}');
    const entries = Object.entries(stored);
    let totalReq = 0, totalIn = 0, totalOut = 0;
    let rows = '';
    for(const [modelId, stats] of entries){
      totalReq += stats.requests || 0;
      totalIn  += stats.prompt_tokens || 0;
      totalOut += stats.completion_tokens || 0;
      rows += '<tr>'
        + '<td>' + _truncateModelName(modelId) + '</td>'
        + '<td>' + (stats.requests || 0) + '</td>'
        + '<td>' + (stats.prompt_tokens || 0).toLocaleString() + '</td>'
        + '<td>' + (stats.completion_tokens || 0).toLocaleString() + '</td>'
        + '</tr>';
    }
    const sessionHtml = entries.length === 0
      ? '<div class="ai-usage-empty">No requests this session</div>'
      : '<table class="ai-usage-table">'
        + '<thead><tr><th>Model</th><th>Req</th><th>In</th><th>Out</th></tr></thead>'
        + '<tbody>' + rows
        + '<tr class="ai-total-row">'
        + '<td>Total</td><td>' + totalReq + '</td>'
        + '<td>' + totalIn.toLocaleString() + '</td>'
        + '<td>' + totalOut.toLocaleString() + '</td>'
        + '</tr></tbody></table>';
    document.getElementById('ai-usage-body').innerHTML =
      '<div><div class="ai-usage-section-label">Account</div>'
      + '<div class="ai-usage-account">' + accountHtml + '</div></div>'
      + '<div><div class="ai-usage-section-label">Session</div>' + sessionHtml + '</div>';
  }
```

- [ ] **Step 4: Add `_openUsageModal()` function**

Add inside the IIFE, after `_renderUsageBody`:

```javascript
  async function _openUsageModal(){
    _usageModal.classList.remove('hidden');
    _renderUsageBody('Loading…');
    try{
      const resp = await fetch('https://openrouter.ai/api/v1/auth/key', {
        headers:{'Authorization':'Bearer ' + _aiKey}
      });
      const json = await resp.json();
      const d = json.data || {};
      const tier = d.is_free_tier ? 'Free tier' : 'Paid';
      const rl = d.rate_limit || {};
      const rateStr = rl.requests ? (rl.requests + ' req / ' + rl.interval) : '—';
      const used = typeof d.usage === 'number' ? Math.round(d.usage) : '—';
      _renderUsageBody(tier + ' · ' + rateStr + '<br>' + used + ' requests used this period');
    }catch(e){
      _renderUsageBody('<span class="ai-usage-error">Failed to load account info</span>');
    }
  }
```

- [ ] **Step 5: Wire event listeners**

Find the event listener block near the end of the IIFE (just before `_initAi()`):
```javascript
  _btn.addEventListener('click', ()=>{
    _panel.classList.toggle('hidden');
    if(!_panel.classList.contains('hidden')) _input.focus();
  });
  _close.addEventListener('click', ()=> _panel.classList.add('hidden'));
  _send.addEventListener('click', _sendMessage);
  _input.addEventListener('keydown', e=>{
    if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); _sendMessage(); }
  });
```
Add usage modal listeners after:
```javascript
  _btn.addEventListener('click', ()=>{
    _panel.classList.toggle('hidden');
    if(!_panel.classList.contains('hidden')) _input.focus();
  });
  _close.addEventListener('click', ()=> _panel.classList.add('hidden'));
  _send.addEventListener('click', _sendMessage);
  _input.addEventListener('keydown', e=>{
    if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); _sendMessage(); }
  });
  _usageBtn.addEventListener('click', _openUsageModal);
  document.getElementById('ai-usage-close-btn').addEventListener('click', ()=> _usageModal.classList.add('hidden'));
  document.getElementById('ai-usage-refresh-btn').addEventListener('click', _openUsageModal);
  document.getElementById('ai-usage-clear-btn').addEventListener('click', ()=>{
    localStorage.removeItem('nw-ai-usage');
    _renderUsageBody(document.querySelector('.ai-usage-account')?.innerHTML || 'Loading…');
  });
```

- [ ] **Step 6: Verify full flow**

1. Send 2–3 messages using different models (switch models between sends)
2. Click the bar-chart icon in the AI panel header
3. Confirm the usage modal opens over the chat
4. Confirm the Account section shows tier + rate limit + requests used
5. Confirm the Session table lists each resolved model with Req / In / Out counts
6. Confirm the Total row sums correctly
7. Click ↺ Refresh — account section reloads, session data stays
8. Click Clear data — session table resets to "No requests this session"
9. Click × — modal closes, chat is visible again

- [ ] **Step 7: Restart and final verify**

```bash
sudo systemctl restart netwatch
```

Open the dashboard fresh. Confirm AI button appears, usage modal works end-to-end, markdown renders in responses, mobile layout is correct at 375px width.

- [ ] **Step 8: Commit and push**

```bash
git add dashboard.html
git commit -m "feat: usage modal with per-model token stats and live OpenRouter account info"
git push
```
