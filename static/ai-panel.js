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
  const _usageBtn   = document.getElementById('ai-usage-btn');
  const _usageModal = document.getElementById('ai-usage-modal');

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
          category: i.category, role: i.role,
          cpu: i.cpu, ram_gb: i.ram_gb, gpu: i.gpu,
          architecture: i.architecture, os: i.os,
          cpu_score: i.cpu_score, tdp_watts: i.tdp_watts,
          mac: i.mac, serial: i.serial, tpm: i.tpm,
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
        name:h.name, ip:h.ip, group:h.group, device_type:h.device_type,
        status:h.status, is_up:h.is_up,
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
    if(role === 'assistant' && text) d.innerHTML = _renderMarkdown(text);
    else d.textContent = text;
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
    if(_aiHistory.length > 20) _aiHistory.splice(0, _aiHistory.length - 20);

    const ctx = _buildContext();
    const systemPrompt = _buildSystemPrompt(ctx);

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
        body:JSON.stringify({model:_model.value, messages, stream:true, stream_options:{include_usage:true}})
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
      let _buf = '';
      let _streamUsage = null;
      let _streamModel = null;

      while(true){
        const {done, value} = await reader.read();
        if(done) break;
        _buf += decoder.decode(value, {stream:true});
        const lines = _buf.split('\n');
        _buf = lines.pop();
        let _finished = false;
        for(const line of lines){
          if(!line.startsWith('data: ')) continue;
          const raw = line.slice(6).trim();
          if(raw === '[DONE]'){ _finished = true; break; }
          try{
            const parsed = JSON.parse(raw);
            const delta = parsed.choices?.[0]?.delta?.content || '';
            assistantText += delta;
            assistantDiv.innerHTML = _renderMarkdown(assistantText);
            _msgs.scrollTop = _msgs.scrollHeight;
            if(parsed.usage){ _streamUsage = parsed.usage; _streamModel = parsed.model; }
          }catch(e){}
        }
        if(_finished) break;
      }
      // Flush any partial line left in the buffer
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

      if(_streamUsage && _streamModel) _accumulateUsage(_streamModel, _streamUsage);
      if(assistantText){
        _aiHistory.push({role:'assistant', content:assistantText});
      }else{
        assistantDiv.remove();
      }
    }catch(e){
      assistantDiv.className = 'ai-msg error';
      assistantDiv.textContent = 'Network error: ' + e.message;
      _aiHistory.pop();
    }

    _setStreaming(false);
  }

  function _truncateModelName(id){
    return id.replace(/:[^/]+$/, '').replace(/-\d{8}$/, '').split('/').pop();
  }

  function _renderUsageBody(accountHtml){
    let stored = {};
    try{ stored = JSON.parse(localStorage.getItem('nw-ai-usage') || '{}'); }catch(e){}
    const entries = Object.entries(stored);
    let totalReq = 0, totalIn = 0, totalOut = 0;
    let rows = '';
    for(const [modelId, stats] of entries){
      totalReq += stats.requests || 0;
      totalIn  += stats.prompt_tokens || 0;
      totalOut += stats.completion_tokens || 0;
      rows += '<tr>'
        + '<td>' + escapeHtml(_truncateModelName(modelId)) + '</td>'
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
      const rateStr = (rl.requests && rl.interval) ? (escapeHtml(String(rl.requests)) + ' req / ' + escapeHtml(String(rl.interval))) : '—';
      const used = typeof d.usage === 'number' ? '$' + d.usage.toFixed(4) : '—';
      _renderUsageBody(tier + ' · ' + rateStr + '<br>' + used + ' credits used this period');
    }catch(e){
      _renderUsageBody('<span class="ai-usage-error">Failed to load account info</span>');
    }
  }

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

  function _buildSystemPrompt(ctx){
    const base = 'You are a helpful assistant embedded in Netwatch, a homelab network monitor. The user is the homelab owner and an experienced sysadmin. Be concise and direct.\n\n';
    if(ctx.page === 'inventory'){
      return base
        + 'You have the full hardware inventory below. Each item includes:\n'
        + '- name: device name, ip: IP address, device_type, category, role\n'
        + '- cpu: processor model, ram_gb: RAM in gigabytes, gpu: graphics card, architecture, os: operating system\n'
        + '- cpu_score: PassMark CPU score, tdp_watts: power draw in watts, tpm: TPM version\n'
        + '- mac: MAC address, serial: serial number, specs: additional details, notes: admin notes\n\n'
        + 'Use this data to answer questions about hardware specs, device counts, CPU models, OS distribution, etc.\n\n'
        + 'Current page: inventory\nLive data:\n' + JSON.stringify(ctx, null, 2);
    }
    if(ctx.page === 'events'){
      return base
        + 'You have the recent event log below. Events record host status changes (going up, going down, degraded).\n\n'
        + 'Current page: events\nLive data:\n' + JSON.stringify(ctx, null, 2);
    }
    return base
      + 'You have real-time data about every monitored host below. Each host includes:\n'
      + '- name: display name, ip: IP address, group: logical group, device_type: what kind of device it is\n'
      + '- status: UP / DOWN / DEGRADED / IDLE / WAIT, is_up: boolean\n'
      + '- uptime_pct: 30-day uptime %, latency_ms: current ping latency\n'
      + '- consecutive_down: how many consecutive failed checks, last_seen_up_seconds: seconds since last seen online\n'
      + '- specs: hardware/software details, notes: admin notes, services: monitored TCP ports and their status\n\n'
      + 'Use this data to answer questions about specific hosts, IP addresses, status, and network topology.\n\n'
      + 'Current page: ' + ctx.page + '\nLive data:\n' + JSON.stringify(ctx, null, 2);
  }

  function _clearConversation(newPage){
    _aiHistory = [];
    _msgs.innerHTML = '';
    const note = document.createElement('div');
    note.className = 'ai-msg page-switch';
    note.textContent = 'Switched to ' + newPage + ' — conversation cleared.';
    _msgs.appendChild(note);
  }

  // Clear conversation when the user switches tabs so stale context doesn't bleed across pages
  (function(){
    const _origSetTab = window.setTab;
    window.setTab = function(tab){
      const prev = localStorage.getItem('nw-tab');
      if(_origSetTab) _origSetTab.call(this, tab);
      if(prev && tab !== prev && _aiHistory.length > 0) _clearConversation(tab);
    };
  })();

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

  _initAi();
})();
