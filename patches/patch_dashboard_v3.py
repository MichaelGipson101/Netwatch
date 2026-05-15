#!/usr/bin/env python3
"""
netwatch patch: dashboard v3 with tabs, topology, smart sort, compact mode.

Adds:
  - Three-tab layout: Topology | Hosts | Events (Events tab is wired up here
    but doesn't show data until patch 5 is applied)
  - Topology view as the default landing tab
  - Problem hosts banner at the top of topology when anything is down
  - Smart sort on Hosts tab: down hosts float to the top of each group
  - Compact mode toggle on Hosts tab (denser rows for more on-screen at once)
  - Subtle ambient pulse on group cards with down hosts
  - Big, kiosk-friendly node sizing on the Topology view
  - Tab choice persists in localStorage

Run once from ~/netwatch/:
    python3 patch_dashboard_v3.py
    sudo systemctl restart netwatch

Backup saved to monitor.py.bak_dashboard_v3.
Idempotent - safe to re-run.
"""

import os
import re
import shutil
import sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_dashboard_v3"
SENTINEL = "nw-tabs"  # presence means already patched


# ─── New dashboard HTML (replaces existing DASHBOARD_HTML block) ──────────────

NEW_DASHBOARD = r'''DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en" data-theme="auto">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Netwatch</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#f5f4f1;--surface:#fff;--border:#e5e4e0;--border-light:#f0efe9;
  --text:#1a1a1a;--muted:#6b7280;--hint:#9ca3af;--subtle:#fafaf8;
  --green:#16a34a;--green-bg:#dcfce7;--green-text:#15803d;--green-soft:#ecfdf5;
  --red:#dc2626;--red-bg:#fee2e2;--red-text:#b91c1c;--red-soft:#fef2f2;
  --amber:#d97706;--amber-bg:#fef3c7;--amber-text:#b45309;
  --blue:#2563eb;--blue-bg:#dbeafe;
}
[data-theme="dark"]{
  --bg:#0f0e0d;--surface:#1a1917;--border:#2a2825;--border-light:#232220;
  --text:#e8e6e0;--muted:#9b998f;--hint:#6b6962;--subtle:#1f1d1b;
  --green:#22c55e;--green-bg:#0c2518;--green-text:#4ade80;--green-soft:#0a1f14;
  --red:#ef4444;--red-bg:#2a1515;--red-text:#f87171;--red-soft:#1f1010;
  --amber:#f59e0b;--amber-bg:#2a1f0a;--amber-text:#fbbf24;
  --blue:#3b82f6;--blue-bg:#0f1a2e;
}
@media (prefers-color-scheme: dark){
  [data-theme="auto"]{
    --bg:#0f0e0d;--surface:#1a1917;--border:#2a2825;--border-light:#232220;
    --text:#e8e6e0;--muted:#9b998f;--hint:#6b6962;--subtle:#1f1d1b;
    --green:#22c55e;--green-bg:#0c2518;--green-text:#4ade80;--green-soft:#0a1f14;
    --red:#ef4444;--red-bg:#2a1515;--red-text:#f87171;--red-soft:#1f1010;
    --amber:#f59e0b;--amber-bg:#2a1f0a;--amber-text:#fbbf24;
    --blue:#3b82f6;--blue-bg:#0f1a2e;
  }
}
html{background:var(--bg)}
body{font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text);font-size:14px;line-height:1.5;transition:background .2s,color .2s}
.shell{max-width:1280px;margin:0 auto;padding:28px 20px}

nav{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;flex-wrap:wrap;gap:12px}
.logo{font-family:'DM Mono',monospace;font-size:16px;font-weight:500;letter-spacing:.06em}
.logo span{color:var(--green)}
.nav-right{display:flex;align-items:center;gap:12px;font-family:'DM Mono',monospace;font-size:11px;color:var(--hint);flex-wrap:wrap}
.live-pip{display:flex;align-items:center;gap:6px}
.live-dot{width:7px;height:7px;border-radius:50%;background:var(--green);animation:pulse 2s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.stale .live-dot{background:var(--amber);animation:none}
.btn{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:7px 14px;font-family:'DM Sans',sans-serif;font-size:12px;font-weight:500;color:var(--text);cursor:pointer;transition:all .15s}
.btn:hover{background:var(--subtle);border-color:var(--hint)}
.btn:active{transform:scale(.98)}
.btn-primary{background:var(--text);color:var(--surface);border-color:var(--text)}
.btn-primary:hover{background:#000;border-color:#000}
.btn-ghost{border-color:transparent;background:transparent}
.btn-ghost:hover{background:var(--subtle)}

.theme-toggle{display:inline-flex;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:2px;gap:0}
.theme-toggle button{background:transparent;border:none;padding:4px 8px;border-radius:6px;cursor:pointer;color:var(--hint);display:inline-flex;align-items:center;justify-content:center;transition:all .15s;font-family:inherit}
.theme-toggle button:hover{color:var(--text)}
.theme-toggle button.active{background:var(--subtle);color:var(--text)}
.theme-toggle svg{width:14px;height:14px;display:block}

.tabs{display:inline-flex;gap:3px;background:var(--surface);border:1px solid var(--border);border-radius:9px;padding:3px;margin-bottom:22px}
.tab{background:transparent;border:none;padding:7px 16px;border-radius:6px;font-family:'DM Sans',sans-serif;font-size:13px;font-weight:500;color:var(--muted);cursor:pointer;transition:all .12s;display:inline-flex;align-items:center;gap:7px}
.tab:hover{color:var(--text)}
.tab.active{background:var(--text);color:var(--surface)}
.tab-count{display:inline-block;background:var(--red-bg);color:var(--red-text);border-radius:9px;padding:1px 7px;font-family:'DM Mono',monospace;font-size:10px;font-weight:500;line-height:1.4}
.tab.active .tab-count{background:rgba(255,255,255,.2);color:var(--surface)}

.view{display:none}
.view.active{display:block}

.err-banner{background:var(--amber-bg);color:var(--amber-text);border:1px solid #fde68a;border-radius:8px;padding:10px 16px;font-size:13px;margin-bottom:16px;display:none}

.problem-banner{background:var(--red-bg);border:1px solid var(--red);border-radius:12px;padding:16px 20px;margin-bottom:20px;display:none}
.problem-banner.show{display:block;animation:slideIn .25s ease-out}
@keyframes slideIn{from{transform:translateY(-8px);opacity:0}to{transform:translateY(0);opacity:1}}
.problem-banner-hdr{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.problem-banner-icon{width:24px;height:24px;border-radius:50%;background:var(--red);color:#fff;display:flex;align-items:center;justify-content:center;font-weight:600;font-size:13px;flex-shrink:0}
.problem-banner-title{font-weight:600;font-size:15px;color:var(--red-text)}
.problem-banner-list{display:flex;flex-wrap:wrap;gap:8px}
.problem-pill{background:var(--surface);border:1px solid var(--red);border-radius:8px;padding:8px 14px;display:flex;align-items:center;gap:8px;font-size:13px}
.problem-pill .name{font-weight:500}
.problem-pill .ip{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted)}
.problem-pill .dur{font-family:'DM Mono',monospace;font-size:11px;color:var(--red-text);background:var(--red-bg);padding:2px 7px;border-radius:4px}

/* Summary cards */
.summary{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px}
@media(max-width:640px){.summary{grid-template-columns:repeat(2,1fr)}}
.scard{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px 18px}
.scard-label{font-size:10px;font-family:'DM Mono',monospace;letter-spacing:.07em;text-transform:uppercase;color:var(--hint);margin-bottom:8px}
.scard-val{font-size:28px;font-weight:600;letter-spacing:-.03em;line-height:1}
.scard-val sup{font-size:13px;font-weight:400;color:var(--hint);margin-left:2px}
.scard-sub{font-size:11px;color:var(--hint);margin-top:5px}

/* ── Topology view ── */
.topo-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
.topo-group{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px 18px;transition:box-shadow .3s}
.topo-group.has-down{border-color:var(--red);box-shadow:0 0 0 4px var(--red-soft)}
.topo-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
.topo-name{font-size:11px;font-family:'DM Mono',monospace;letter-spacing:.09em;text-transform:uppercase;color:var(--hint);font-weight:500}
.topo-count{font-size:11px;font-family:'DM Mono',monospace;color:var(--green-text)}
.topo-count.has-down{color:var(--red-text)}
.nodes{display:flex;flex-wrap:wrap;gap:8px}
.node{display:inline-flex;align-items:center;gap:9px;background:var(--subtle);border:1px solid var(--border);border-radius:8px;padding:9px 13px;font-size:14px;cursor:default;transition:all .15s}
.node.up{background:var(--green-soft);border-color:var(--green-bg)}
.node.down{background:var(--red-bg);border-color:var(--red);color:var(--red-text)}
.node.idle{background:var(--subtle);border-color:var(--border);color:var(--muted)}
.node.wait{background:var(--amber-bg);border-color:var(--amber);color:var(--amber-text)}
.node-dot{width:9px;height:9px;border-radius:50%;flex-shrink:0}
.node.up .node-dot{background:var(--green);box-shadow:0 0 0 2.5px var(--green-bg)}
.node.down .node-dot{background:var(--red);box-shadow:0 0 0 2.5px var(--red-bg)}
.node.idle .node-dot{background:var(--hint)}
.node.wait .node-dot{background:var(--amber);box-shadow:0 0 0 2.5px var(--amber-bg)}
.node-name{font-weight:500}
.node-lat{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted)}
.node.down .node-lat{color:var(--red-text)}
.node.up .node-lat{color:var(--green-text)}

/* ── Hosts view ── */
.hosts-toolbar{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:14px;flex-wrap:wrap}
.toolbar-right{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted)}
.compact-toggle{display:inline-flex;align-items:center;gap:6px;cursor:pointer;user-select:none;font-size:12px;color:var(--muted)}
.compact-toggle input{margin:0;cursor:pointer}

.group{margin-bottom:22px}
.group-label{font-size:10px;font-family:'DM Mono',monospace;letter-spacing:.09em;text-transform:uppercase;color:var(--hint);margin-bottom:8px;padding-left:2px;display:flex;align-items:center;gap:8px}
.group-label .down-pill{background:var(--red-bg);color:var(--red-text);font-family:'DM Mono',monospace;font-size:10px;padding:2px 7px;border-radius:4px;letter-spacing:.04em}
.table{background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden;transition:box-shadow .3s}
.table.has-down{box-shadow:0 0 0 4px var(--red-soft)}
.row{display:grid;grid-template-columns:28px 1fr 130px 72px 96px 140px 90px;align-items:center;padding:11px 16px;border-bottom:1px solid var(--border-light);gap:10px;transition:background .1s,padding .15s}
.row:last-child{border-bottom:none}
.row.hdr{background:var(--subtle);font-size:10px;font-family:'DM Mono',monospace;letter-spacing:.07em;text-transform:uppercase;color:var(--hint);padding:9px 16px;border-bottom:1px solid var(--border)}
.row:not(.hdr):hover{background:var(--subtle)}
.row.down-row{background:var(--red-bg)}
.row.down-row:hover{background:var(--red-bg);filter:brightness(1.05)}
@media(max-width:780px){.row{grid-template-columns:24px 1fr 72px 90px 90px;gap:8px}.col-ip,.col-ping{display:none}}
.compact .row:not(.hdr){padding:6px 16px;font-size:13px}
.compact .row{grid-template-columns:24px 1fr 110px 60px 80px 110px 80px}
.compact .host-ip-sub{display:none}
.compact .host-name{font-size:13px}
.dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.dot-up{background:var(--green);box-shadow:0 0 0 3px var(--green-bg)}
.dot-dn{background:var(--red);box-shadow:0 0 0 3px var(--red-bg)}
.dot-wt{background:var(--amber);box-shadow:0 0 0 3px var(--amber-bg)}
.dot-idle{background:var(--hint);box-shadow:0 0 0 3px var(--subtle)}
.host-name{font-weight:500;font-size:13px}
.host-ip-sub{font-family:'DM Mono',monospace;font-size:11px;color:var(--hint);margin-top:1px}
.badge{font-size:10px;font-family:'DM Mono',monospace;font-weight:500;padding:3px 8px;border-radius:4px;display:inline-block;letter-spacing:.04em}
.badge-up{background:var(--green-bg);color:var(--green-text)}
.badge-dn{background:var(--red-bg);color:var(--red-text)}
.badge-wt{background:var(--amber-bg);color:var(--amber-text)}
.badge-idle{background:var(--subtle);color:var(--muted);border:1px solid var(--border)}
.lat{font-family:'DM Mono',monospace;font-size:12px}
.uptime-cell{display:flex;align-items:center;gap:8px}
.uptime-track{flex:1;height:4px;background:var(--border-light);border-radius:2px;overflow:hidden;max-width:72px}
.uptime-fill{height:100%;border-radius:2px;transition:width .4s}
.uptime-pct{font-family:'DM Mono',monospace;font-size:11px;min-width:34px;text-align:right}
.col-ip{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted)}
.col-ping{font-family:'DM Mono',monospace;font-size:11px;color:var(--hint)}

/* ── Events view ── */
.events-empty{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:48px 20px;text-align:center;color:var(--hint);font-size:13px}
.events-list{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden}
.event{display:grid;grid-template-columns:6px 1fr 110px 110px 100px;gap:14px;padding:13px 18px;border-bottom:1px solid var(--border-light);align-items:center}
.event:last-child{border-bottom:none}
.event-bar{width:3px;height:32px;border-radius:2px;background:var(--green);justify-self:center}
.event.ongoing .event-bar{background:var(--red);animation:pulse 2s ease-in-out infinite}
.event-host{font-weight:500;font-size:13px}
.event-host .ip{font-family:'DM Mono',monospace;font-size:11px;color:var(--hint);margin-left:6px;font-weight:400}
.event-time{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted)}
.event-dur{font-family:'DM Mono',monospace;font-size:12px;font-weight:500}
.event.ongoing .event-dur{color:var(--red-text)}
.event-status{justify-self:end}

.footer{margin-top:28px;display:flex;justify-content:space-between;align-items:center;font-family:'DM Mono',monospace;font-size:11px;color:var(--hint);padding-top:16px;border-top:1px solid var(--border)}

/* Modal (host editor) */
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:flex-start;justify-content:center;z-index:50;overflow-y:auto;padding:40px 16px}
.modal-overlay.open{display:flex}
.modal{background:var(--surface);border:1px solid var(--border);border-radius:14px;width:100%;max-width:780px;box-shadow:0 20px 60px rgba(0,0,0,.2);overflow:hidden}
.modal-hdr{display:flex;align-items:center;justify-content:space-between;padding:18px 22px;border-bottom:1px solid var(--border)}
.modal-title{font-size:16px;font-weight:600;letter-spacing:-.01em}
.modal-body{padding:20px 22px;max-height:60vh;overflow-y:auto}
.modal-foot{padding:14px 22px;border-top:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;gap:10px;background:var(--subtle)}
.edit-row{display:grid;grid-template-columns:1fr 1fr 1fr 70px 70px 32px;gap:8px;padding:8px 0;align-items:center;border-bottom:1px solid var(--border-light)}
.edit-row .ao-cell{display:flex;align-items:center;justify-content:center}
.edit-row .ao-cell input{width:18px;height:18px;margin:0;cursor:pointer}
.edit-row:last-of-type{border-bottom:none}
.edit-row.hdr{font-size:10px;font-family:'DM Mono',monospace;letter-spacing:.07em;text-transform:uppercase;color:var(--hint);padding:6px 0;border-bottom:1px solid var(--border)}
.edit-row input[type="text"],.edit-row input[type="number"]{width:100%;padding:7px 10px;border:1px solid var(--border);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:13px;background:var(--surface);color:var(--text)}
.edit-row input:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px var(--blue-bg)}
.edit-row input.invalid{border-color:var(--red);background:var(--red-bg)}
.edit-row .del-btn{background:transparent;border:none;color:var(--hint);cursor:pointer;padding:6px;border-radius:4px;font-size:16px;line-height:1;transition:all .15s}
.edit-row .del-btn:hover{background:var(--red-bg);color:var(--red)}
.add-row-btn{margin-top:10px;width:100%;padding:10px;border:1px dashed var(--border);background:transparent;border-radius:8px;color:var(--muted);font-family:'DM Sans',sans-serif;font-size:13px;cursor:pointer;transition:all .15s}
.add-row-btn:hover{border-color:var(--hint);background:var(--subtle);color:var(--text)}
.save-status{font-size:12px;color:var(--hint);font-family:'DM Mono',monospace}
.save-status.error{color:var(--red)}
.save-status.success{color:var(--green)}
</style>
</head>
<body>
<div class="shell">

  <nav>
    <div class="logo">net<span>watch</span></div>
    <div class="nav-right">
      <div class="theme-toggle" id="theme-toggle" role="group" aria-label="Theme">
        <button data-theme-btn="light" title="Light" aria-label="Light theme"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg></button>
        <button data-theme-btn="auto" title="Auto" aria-label="Auto theme"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 3v18" fill="currentColor"/><path d="M12 3a9 9 0 0 1 0 18" fill="currentColor" stroke="none"/></svg></button>
        <button data-theme-btn="dark" title="Dark" aria-label="Dark theme"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg></button>
      </div>
      <button class="btn" onclick="openEditor()">Edit hosts</button>
      <div class="live-pip" id="pip"><span class="live-dot"></span><span>live</span></div>
      <span id="clock">-</span>
    </div>
  </nav>

  <div class="err-banner" id="err-banner">Lost connection to netwatch - retrying...</div>

  <div class="tabs" role="tablist">
    <button class="tab" data-tab="topology" role="tab">Topology</button>
    <button class="tab" data-tab="hosts" role="tab">Hosts</button>
    <button class="tab" data-tab="events" role="tab">Events <span class="tab-count" id="events-count" style="display:none">0</span></button>
  </div>

  <div class="summary">
    <div class="scard"><div class="scard-label">Hosts up</div><div class="scard-val" id="s-up">-</div><div class="scard-sub" id="s-up-sub">loading...</div></div>
    <div class="scard"><div class="scard-label">Avg latency</div><div class="scard-val" id="s-lat">-</div><div class="scard-sub">across online hosts</div></div>
    <div class="scard"><div class="scard-label">Avg uptime</div><div class="scard-val" id="s-upt">-</div><div class="scard-sub">100-ping window</div></div>
    <div class="scard"><div class="scard-label">Monitored</div><div class="scard-val" id="s-tot">-</div><div class="scard-sub" id="s-interval">loading...</div></div>
  </div>

  <div class="view" id="view-topology">
    <div class="problem-banner" id="problem-banner">
      <div class="problem-banner-hdr">
        <div class="problem-banner-icon">!</div>
        <div class="problem-banner-title" id="problem-banner-title">Hosts offline</div>
      </div>
      <div class="problem-banner-list" id="problem-banner-list"></div>
    </div>
    <div class="topo-grid" id="topo-grid"></div>
  </div>

  <div class="view" id="view-hosts">
    <div class="hosts-toolbar">
      <div></div>
      <div class="toolbar-right">
        <label class="compact-toggle"><input type="checkbox" id="compact-mode"> Compact</label>
      </div>
    </div>
    <div id="groups"></div>
  </div>

  <div class="view" id="view-events">
    <div class="events-empty" id="events-empty">No incidents recorded yet. When a monitored host goes down, it'll appear here.</div>
    <div class="events-list" id="events-list" style="display:none"></div>
  </div>

  <div class="footer">
    <span>netwatch v3.0 - raspberry pi</span>
    <span>refreshes every 5 s</span>
  </div>
</div>

<div class="modal-overlay" id="modal-overlay">
  <div class="modal">
    <div class="modal-hdr">
      <div class="modal-title">Edit hosts</div>
      <button class="btn btn-ghost" onclick="closeEditor()">X</button>
    </div>
    <div class="modal-body">
      <div class="edit-row hdr">
        <div>Name</div><div>IP address</div><div>Group</div><div>Interval (s)</div><div>Always on</div><div></div>
      </div>
      <div id="edit-rows"></div>
      <button class="add-row-btn" onclick="addRow()">+ Add host</button>
    </div>
    <div class="modal-foot">
      <div class="save-status" id="save-status">Changes apply immediately on save</div>
      <div style="display:flex;gap:8px">
        <button class="btn" onclick="closeEditor()">Cancel</button>
        <button class="btn btn-primary" onclick="saveHosts()">Save changes</button>
      </div>
    </div>
  </div>
</div>

<script>
(function initTheme(){
  const saved = localStorage.getItem('nw-theme') || 'auto';
  document.documentElement.setAttribute('data-theme', saved);
})();
function setTheme(mode){
  document.documentElement.setAttribute('data-theme', mode);
  localStorage.setItem('nw-theme', mode);
  document.querySelectorAll('#theme-toggle button').forEach(b => {
    b.classList.toggle('active', b.dataset.themeBtn === mode);
  });
}
document.addEventListener('DOMContentLoaded', () => {
  const current = localStorage.getItem('nw-theme') || 'auto';
  document.querySelectorAll('#theme-toggle button').forEach(b => {
    b.classList.toggle('active', b.dataset.themeBtn === current);
    b.addEventListener('click', () => setTheme(b.dataset.themeBtn));
  });

  // Tabs
  const initialTab = localStorage.getItem('nw-tab') || 'topology';
  setTab(initialTab);
  document.querySelectorAll('.tab').forEach(t => {
    t.addEventListener('click', () => setTab(t.dataset.tab));
  });

  // Compact toggle
  const compactSaved = localStorage.getItem('nw-compact') === 'true';
  document.getElementById('compact-mode').checked = compactSaved;
  document.body.classList.toggle('compact', compactSaved);
  document.getElementById('compact-mode').addEventListener('change', e => {
    document.body.classList.toggle('compact', e.target.checked);
    localStorage.setItem('nw-compact', e.target.checked);
  });
});

function setTab(tab){
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + tab));
  localStorage.setItem('nw-tab', tab);
}

const REFRESH = 5000;
let lastOk = true;

function escapeHtml(s){
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmtLatency(ms){
  if(ms === null) return '<span style="color:var(--hint)">- ms</span>';
  const c = ms < 10 ? 'var(--green)' : ms < 50 ? 'var(--blue)' : 'var(--amber)';
  return '<span style="color:' + c + '">' + ms.toFixed(1) + ' ms</span>';
}
function uptimeColor(pct){
  if(pct === null) return 'var(--hint)';
  if(pct >= 95) return 'var(--green)';
  if(pct >= 80) return 'var(--amber)';
  return 'var(--red)';
}

function durationStr(seconds){
  if(seconds < 60) return seconds + 's';
  if(seconds < 3600) return Math.floor(seconds/60) + 'm ' + (seconds % 60) + 's';
  const h = Math.floor(seconds/3600);
  const m = Math.floor((seconds % 3600) / 60);
  return h + 'h ' + m + 'm';
}

// ── Sort: down hosts first, then by name ──
function sortHosts(hosts){
  return hosts.slice().sort((a,b) => {
    const aDown = !a.is_up && a.status === 'DOWN';
    const bDown = !b.is_up && b.status === 'DOWN';
    if(aDown !== bDown) return aDown ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
}

// ── Hosts table render ──
function renderHost(h){
  const isIdle = h.status === 'IDLE';
  const dotCls = h.status === 'WAIT' ? 'dot-wt' : h.is_up ? 'dot-up' : (isIdle ? 'dot-idle' : 'dot-dn');
  const badgeCls = h.status === 'WAIT' ? 'badge-wt' : h.is_up ? 'badge-up' : (isIdle ? 'badge-idle' : 'badge-dn');
  const nameStyle = h.is_up || h.status === 'WAIT' || isIdle ? '' : 'style="color:var(--red)"';
  const uPct = h.uptime_pct;
  const uColor = isIdle ? 'var(--hint)' : uptimeColor(uPct);
  const uBarColor = isIdle ? 'var(--border)' : uColor;
  const uBarW = uPct !== null ? uPct.toFixed(1) : 0;
  const uLabel = uPct !== null ? uPct.toFixed(1) + '%' : '-%';
  const rowCls = h.is_up || h.status === 'WAIT' || isIdle ? '' : ' down-row';
  return '<div class="row' + rowCls + '">'
    + '<div><span class="dot ' + dotCls + '"></span></div>'
    + '<div><div class="host-name" ' + nameStyle + '>' + escapeHtml(h.name) + '</div><div class="host-ip-sub">' + escapeHtml(h.ip) + '</div></div>'
    + '<div class="col-ip">' + escapeHtml(h.ip) + '</div>'
    + '<div><span class="badge ' + badgeCls + '">' + h.status + '</span></div>'
    + '<div class="lat">' + fmtLatency(h.latency_ms) + '</div>'
    + '<div class="uptime-cell"><div class="uptime-track"><div class="uptime-fill" style="width:' + uBarW + '%;background:' + uBarColor + '"></div></div><span class="uptime-pct" style="color:' + uColor + '">' + uLabel + '</span></div>'
    + '<div class="col-ping">' + h.last_checked + '</div>'
    + '</div>';
}

function renderGroups(data){
  const groups = {};
  data.hosts.forEach(h => {
    if(!groups[h.group]) groups[h.group] = [];
    groups[h.group].push(h);
  });
  document.getElementById('groups').innerHTML = Object.entries(groups).map(([name, hosts]) => {
    const sorted = sortHosts(hosts);
    const downCount = hosts.filter(h => h.status === 'DOWN').length;
    const labelExtras = downCount > 0 ? '<span class="down-pill">' + downCount + ' DOWN</span>' : '';
    return '<div class="group">'
      + '<div class="group-label">' + escapeHtml(name) + labelExtras + '</div>'
      + '<div class="table' + (downCount > 0 ? ' has-down' : '') + '">'
      + '<div class="row hdr"><div></div><div>Host</div><div class="col-ip">IP address</div><div>Status</div><div>Latency</div><div>Uptime</div><div class="col-ping">Last ping</div></div>'
      + sorted.map(renderHost).join('')
      + '</div></div>';
  }).join('');
}

// ── Topology render ──
function renderTopologyNode(h){
  const isIdle = h.status === 'IDLE';
  const cls = h.status === 'WAIT' ? 'wait' : h.is_up ? 'up' : (isIdle ? 'idle' : 'down');
  let lat;
  if(isIdle){
    lat = 'idle';
  } else if(h.status === 'WAIT'){
    lat = '...';
  } else if(h.is_up && h.latency_ms !== null){
    lat = h.latency_ms.toFixed(1) + 'ms';
  } else {
    lat = 'offline';
  }
  return '<div class="node ' + cls + '" title="' + escapeHtml(h.ip) + '">'
    + '<span class="node-dot"></span>'
    + '<span class="node-name">' + escapeHtml(h.name) + '</span>'
    + '<span class="node-lat">' + lat + '</span>'
    + '</div>';
}

function renderTopology(data){
  const groups = {};
  data.hosts.forEach(h => {
    if(!groups[h.group]) groups[h.group] = [];
    groups[h.group].push(h);
  });
  document.getElementById('topo-grid').innerHTML = Object.entries(groups).map(([name, hosts]) => {
    const sorted = sortHosts(hosts);
    const upCount = hosts.filter(h => h.is_up).length;
    const downCount = hosts.filter(h => h.status === 'DOWN').length;
    const totalNonIdle = hosts.filter(h => h.status !== 'IDLE').length;
    return '<div class="topo-group' + (downCount > 0 ? ' has-down' : '') + '">'
      + '<div class="topo-hdr">'
      + '<div class="topo-name">' + escapeHtml(name) + '</div>'
      + '<div class="topo-count' + (downCount > 0 ? ' has-down' : '') + '">' + upCount + ' of ' + totalNonIdle + ' up</div>'
      + '</div>'
      + '<div class="nodes">' + sorted.map(renderTopologyNode).join('') + '</div>'
      + '</div>';
  }).join('');

  // Problem banner
  const problemHosts = data.hosts.filter(h => h.status === 'DOWN');
  const banner = document.getElementById('problem-banner');
  const list = document.getElementById('problem-banner-list');
  const titleEl = document.getElementById('problem-banner-title');
  if(problemHosts.length > 0){
    banner.classList.add('show');
    titleEl.textContent = problemHosts.length + ' host' + (problemHosts.length > 1 ? 's' : '') + ' offline';
    list.innerHTML = problemHosts.map(h => {
      let dur = '';
      if(h.last_seen_up_seconds !== undefined && h.last_seen_up_seconds !== null){
        dur = '<span class="dur">down ' + durationStr(h.last_seen_up_seconds) + '</span>';
      } else {
        dur = '<span class="dur">down</span>';
      }
      return '<div class="problem-pill"><span class="name">' + escapeHtml(h.name) + '</span><span class="ip">' + escapeHtml(h.ip) + '</span>' + dur + '</div>';
    }).join('');
  } else {
    banner.classList.remove('show');
  }
}

// ── Events render (placeholder until patch 5) ──
function renderEvents(data){
  const events = data.events || [];
  const ongoing = events.filter(e => e.ongoing).length;
  const countBadge = document.getElementById('events-count');
  if(ongoing > 0){
    countBadge.textContent = ongoing;
    countBadge.style.display = '';
  } else {
    countBadge.style.display = 'none';
  }

  const empty = document.getElementById('events-empty');
  const list = document.getElementById('events-list');
  if(!events.length){
    empty.style.display = '';
    list.style.display = 'none';
    return;
  }
  empty.style.display = 'none';
  list.style.display = '';
  list.innerHTML = events.map(e => {
    const cls = e.ongoing ? 'ongoing' : 'resolved';
    const badgeCls = e.ongoing ? 'badge-dn' : 'badge-up';
    const badgeTxt = e.ongoing ? 'ONGOING' : 'RESOLVED';
    const dur = durationStr(e.duration_seconds || 0);
    return '<div class="event ' + cls + '">'
      + '<div class="event-bar"></div>'
      + '<div class="event-host">' + escapeHtml(e.host_name) + ' <span class="ip">' + escapeHtml(e.host_ip) + '</span></div>'
      + '<div class="event-time">' + escapeHtml(e.started_str) + '</div>'
      + '<div class="event-dur">' + dur + '</div>'
      + '<div class="event-status"><span class="badge ' + badgeCls + '">' + badgeTxt + '</span></div>'
      + '</div>';
  }).join('');
}

function renderSummary(data){
  const up = data.hosts.filter(h => h.is_up).length;
  const total = data.hosts.length;
  const down = data.hosts.filter(h => !h.is_up && h.status === 'DOWN').length;
  const lats = data.hosts.filter(h => h.latency_ms !== null).map(h => h.latency_ms);
  const avgLat = lats.length ? (lats.reduce((a,b)=>a+b,0)/lats.length) : null;
  const alwaysOnUpts = data.hosts.filter(h => h.always_on !== false && h.uptime_pct !== null).map(h => h.uptime_pct);
  const avgUpt = alwaysOnUpts.length ? (alwaysOnUpts.reduce((a,b)=>a+b,0)/alwaysOnUpts.length) : null;
  const upEl = document.getElementById('s-up');
  upEl.innerHTML = up + ' <sup>/ ' + total + '</sup>';
  upEl.style.color = down > 0 ? 'var(--red)' : 'var(--green)';
  document.getElementById('s-up-sub').textContent = down > 0 ? down + ' host' + (down>1?'s':'') + ' offline' : 'all hosts online';
  const latEl = document.getElementById('s-lat');
  latEl.innerHTML = avgLat !== null ? avgLat.toFixed(1) + ' <sup>ms</sup>' : '-';
  latEl.style.color = 'var(--blue)';
  const uptEl = document.getElementById('s-upt');
  uptEl.innerHTML = avgUpt !== null ? avgUpt.toFixed(1) + ' <sup>%</sup>' : '-';
  uptEl.style.color = avgUpt !== null && avgUpt >= 95 ? 'var(--green)' : 'var(--amber)';
  const totEl = document.getElementById('s-tot');
  totEl.innerHTML = total + ' <sup>hosts</sup>';
  totEl.style.color = 'var(--text)';
  document.getElementById('s-interval').textContent = data.settings.default_interval + 's poll interval';
}

async function refresh(){
  try {
    const res = await fetch('/api/status');
    if(!res.ok) throw new Error('bad');
    const data = await res.json();
    renderSummary(data);
    renderTopology(data);
    renderGroups(data);
    renderEvents(data);
    if(!lastOk){
      document.getElementById('err-banner').style.display = 'none';
      document.getElementById('pip').classList.remove('stale');
      lastOk = true;
    }
  } catch(e) {
    document.getElementById('err-banner').style.display = 'block';
    document.getElementById('pip').classList.add('stale');
    lastOk = false;
  }
}

function clockTick(){
  const d = new Date();
  const p = n => String(n).padStart(2,'0');
  document.getElementById('clock').textContent =
    d.getFullYear() + '-' + p(d.getMonth()+1) + '-' + p(d.getDate()) + '  ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

// ── Editor ──
async function openEditor(){
  try {
    const res = await fetch('/api/hosts');
    const data = await res.json();
    const container = document.getElementById('edit-rows');
    container.innerHTML = '';
    (data.hosts || []).forEach(h => addRow(h));
    if(!data.hosts || !data.hosts.length) addRow();
    setStatus('Changes apply immediately on save', '');
    document.getElementById('modal-overlay').classList.add('open');
  } catch(e) { alert('Could not load host list.'); }
}
function closeEditor(){ document.getElementById('modal-overlay').classList.remove('open'); }
function addRow(h){
  const row = document.createElement('div');
  row.className = 'edit-row';
  const alwaysOn = !h || h.always_on !== false;
  row.innerHTML =
    '<input type="text" placeholder="My device" class="f-name" value="' + (h ? escapeHtml(h.name) : '') + '">'
    + '<input type="text" placeholder="192.168.1.1" class="f-ip" value="' + (h ? escapeHtml(h.ip) : '') + '">'
    + '<input type="text" placeholder="Network" class="f-group" value="' + (h ? escapeHtml(h.group || 'General') : 'General') + '">'
    + '<input type="number" min="5" placeholder="30" class="f-interval" value="' + (h && h.interval ? h.interval : '') + '">'
    + '<div class="ao-cell"><input type="checkbox" class="f-alwayson" title="Always on? Uncheck for laptops/phones/etc." ' + (alwaysOn ? 'checked' : '') + '></div>'
    + '<button class="del-btn" title="Remove" onclick="this.parentElement.remove()">X</button>';
  document.getElementById('edit-rows').appendChild(row);
}
function ipValid(ip){
  return /^(\d{1,3}\.){3}\d{1,3}$/.test(ip) && ip.split('.').every(n => parseInt(n) >= 0 && parseInt(n) <= 255);
}
async function saveHosts(){
  const rows = document.querySelectorAll('#edit-rows .edit-row');
  const hosts = [];
  let hasError = false;
  const seenIps = new Set();
  rows.forEach(row => {
    const nameEl = row.querySelector('.f-name');
    const ipEl = row.querySelector('.f-ip');
    const groupEl = row.querySelector('.f-group');
    const intervalEl = row.querySelector('.f-interval');
    [nameEl, ipEl].forEach(el => el.classList.remove('invalid'));
    const name = nameEl.value.trim();
    const ip = ipEl.value.trim();
    const group = groupEl.value.trim() || 'General';
    const intervalRaw = intervalEl.value.trim();
    if(!name && !ip) return;
    if(!name){ nameEl.classList.add('invalid'); hasError = true; }
    if(!ipValid(ip)){ ipEl.classList.add('invalid'); hasError = true; }
    if(seenIps.has(ip)){ ipEl.classList.add('invalid'); hasError = true; }
    seenIps.add(ip);
    const entry = { name, ip, group };
    if(intervalRaw){
      const iv = parseInt(intervalRaw);
      if(!isNaN(iv) && iv >= 5) entry.interval = iv;
    }
    const alwaysOnEl = row.querySelector('.f-alwayson');
    entry.always_on = alwaysOnEl ? alwaysOnEl.checked : true;
    hosts.push(entry);
  });
  if(hasError){ setStatus('Fix the highlighted fields and try again', 'error'); return; }
  setStatus('Saving...', '');
  try {
    const res = await fetch('/api/hosts', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ hosts })
    });
    const data = await res.json();
    if(!res.ok){ setStatus(data.error || 'Save failed', 'error'); return; }
    setStatus('Saved ' + hosts.length + ' host' + (hosts.length===1?'':'s') + '.', 'success');
    setTimeout(() => { closeEditor(); refresh(); }, 700);
  } catch(e) { setStatus('Network error while saving', 'error'); }
}
function setStatus(msg, kind){
  const el = document.getElementById('save-status');
  el.textContent = msg;
  el.className = 'save-status ' + (kind || '');
}
document.addEventListener('keydown', e => { if(e.key === 'Escape') closeEditor(); });

refresh();
setInterval(refresh, REFRESH);
setInterval(clockTick, 1000);
clockTick();
</script>
</body>
</html>"""'''


# ─── Patch logic ──────────────────────────────────────────────────────────────

def main():
    if not os.path.isfile(TARGET):
        print(f"ERROR: {TARGET} not found in current directory.")
        print("Run this from your ~/netwatch/ directory.")
        sys.exit(1)

    content = open(TARGET).read()

    if SENTINEL in content:
        print(f"NOTE: '{SENTINEL}' found in {TARGET} -- dashboard v3 already applied.")
        sys.exit(0)

    shutil.copy2(TARGET, BACKUP)
    print(f"[OK] Backed up {TARGET} -> {BACKUP}")

    # ── Step 1: Add 'last_seen_up_seconds' to to_dict so the topology problem
    # banner can show "down for X minutes" without extra round-trips.
    OLD_TO_DICT = '''            return {
                "name":         self.name,
                "ip":           self.ip,
                "group":        self.group,
                "interval":     self.interval,
                "always_on":    self.always_on,
                "status":       self.status_str,
                "is_up":        self.is_up,
                "latency_ms":   round(self.last_latency_ms, 2) if self.last_latency_ms else None,
                "uptime_pct":   round(self.uptime_pct, 1) if self.uptime_pct is not None else None,
                "last_checked": self.checked_str,
                "history":      list(self.history)[-50:],
                "consecutive_down": self.consecutive_down,
            }'''

    NEW_TO_DICT = '''            last_seen_secs = None
            if self.last_seen_up:
                last_seen_secs = int((datetime.now() - self.last_seen_up).total_seconds())
            return {
                "name":         self.name,
                "ip":           self.ip,
                "group":        self.group,
                "interval":     self.interval,
                "always_on":    self.always_on,
                "status":       self.status_str,
                "is_up":        self.is_up,
                "latency_ms":   round(self.last_latency_ms, 2) if self.last_latency_ms else None,
                "uptime_pct":   round(self.uptime_pct, 1) if self.uptime_pct is not None else None,
                "last_checked": self.checked_str,
                "last_seen_up_seconds": last_seen_secs,
                "history":      list(self.history)[-50:],
                "consecutive_down": self.consecutive_down,
            }'''

    if content.count(OLD_TO_DICT) != 1:
        print(f"[FAIL] Could not locate to_dict (matches: {content.count(OLD_TO_DICT)}). Aborting.")
        shutil.copy2(BACKUP, TARGET)
        sys.exit(1)
    content = content.replace(OLD_TO_DICT, NEW_TO_DICT, 1)
    print("[OK] Added last_seen_up_seconds to API payload")

    # ── Step 2: Add events: [] to build_api_payload (placeholder for patch 5)
    OLD_PAYLOAD = '''        "summary": {
            "total":   len(hosts),
            "up":      sum(1 for h in hosts if h.is_up),
            "down":    sum(1 for h in hosts if not h.is_up and h.last_checked and h.always_on),
            "idle":    sum(1 for h in hosts if not h.is_up and h.last_checked and not h.always_on),
            "pending": sum(1 for h in hosts if not h.last_checked),
        },
        "hosts": [h.to_dict() for h in hosts],
    }'''

    NEW_PAYLOAD = '''        "summary": {
            "total":   len(hosts),
            "up":      sum(1 for h in hosts if h.is_up),
            "down":    sum(1 for h in hosts if not h.is_up and h.last_checked and h.always_on),
            "idle":    sum(1 for h in hosts if not h.is_up and h.last_checked and not h.always_on),
            "pending": sum(1 for h in hosts if not h.last_checked),
        },
        "hosts":  [h.to_dict() for h in hosts],
        "events": [],
    }'''

    if content.count(OLD_PAYLOAD) != 1:
        print(f"[FAIL] Could not locate api payload (matches: {content.count(OLD_PAYLOAD)}). Aborting.")
        shutil.copy2(BACKUP, TARGET)
        sys.exit(1)
    content = content.replace(OLD_PAYLOAD, NEW_PAYLOAD, 1)
    print("[OK] Added events placeholder to API payload")

    # ── Step 3: Bump version
    if 'VERSION = "2.4"' in content:
        content = content.replace('VERSION = "2.4"', 'VERSION = "3.0"', 1)
        print("[OK] Version bumped to 3.0")
    elif 'VERSION = "2.3"' in content:
        content = content.replace('VERSION = "2.3"', 'VERSION = "3.0"', 1)
        print("[OK] Version bumped to 3.0")
    else:
        print("[WARN] Could not find expected version string; skipping version bump")

    # ── Step 4: Replace the entire DASHBOARD_HTML block with the new one
    pattern = re.compile(
        r'^DASHBOARD_HTML = r"""<!DOCTYPE html>.*?</html>"""',
        re.DOTALL | re.MULTILINE,
    )
    matches = pattern.findall(content)
    if len(matches) != 1:
        print(f"[FAIL] Could not isolate DASHBOARD_HTML block (matches: {len(matches)}). Aborting.")
        shutil.copy2(BACKUP, TARGET)
        sys.exit(1)
    content = pattern.sub(NEW_DASHBOARD.replace("\\", r"\\"), content, count=1)
    print("[OK] Replaced DASHBOARD_HTML block")

    open(TARGET, "w").write(content)

    # Validate
    import ast
    try:
        ast.parse(open(TARGET).read())
        print("[OK] Resulting Python is valid")
    except SyntaxError as e:
        print(f"[FAIL] Resulting Python has a syntax error: {e}")
        print("Restoring backup.")
        shutil.copy2(BACKUP, TARGET)
        sys.exit(1)

    print()
    print("Next steps:")
    print("  1. sudo systemctl restart netwatch")
    print("  2. Refresh the dashboard - you'll land on the new Topology view")
    print("  3. Click 'Hosts' tab for the detailed table (with new Compact toggle)")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
