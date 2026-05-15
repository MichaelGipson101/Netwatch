#!/usr/bin/env python3
"""
netwatch patch: host detail drawer + specs + Wake-on-LAN.

Adds:
  - Click any host (in Topology or Hosts tab) to open a sidedrawer with full detail
  - Per-host structured specs (cpu, ram, storage, os, mac) + free-form notes
  - Specs editable through an expandable "More fields" row in the editor
  - Wake-on-LAN button on the drawer for hosts with always_on=false AND a MAC
  - New /api/wake endpoint that sends a magic packet
  - Latency sparkline showing recent ping history per host

Must be applied AFTER patch_events.py.

Run once from ~/netwatch/:
    python3 patch_detail_drawer.py
    sudo systemctl restart netwatch

Backup saved to monitor.py.bak_drawer.
Idempotent - safe to re-run.
"""

import os
import re
import shutil
import sys

TARGET = "monitor.py"
BACKUP = "monitor.py.bak_drawer"
SENTINEL = "send_wol_packet"  # presence means already patched


# ─── New dashboard HTML (full replacement) ────────────────────────────────────

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
.problem-pill{background:var(--surface);border:1px solid var(--red);border-radius:8px;padding:8px 14px;display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer}
.problem-pill:hover{filter:brightness(0.97)}
.problem-pill .name{font-weight:500}
.problem-pill .ip{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted)}
.problem-pill .dur{font-family:'DM Mono',monospace;font-size:11px;color:var(--red-text);background:var(--red-bg);padding:2px 7px;border-radius:4px}

.summary{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px}
@media(max-width:640px){.summary{grid-template-columns:repeat(2,1fr)}}
.scard{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px 18px}
.scard-label{font-size:10px;font-family:'DM Mono',monospace;letter-spacing:.07em;text-transform:uppercase;color:var(--hint);margin-bottom:8px}
.scard-val{font-size:28px;font-weight:600;letter-spacing:-.03em;line-height:1}
.scard-val sup{font-size:13px;font-weight:400;color:var(--hint);margin-left:2px}
.scard-sub{font-size:11px;color:var(--hint);margin-top:5px}

/* Topology */
.topo-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
.topo-group{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px 18px;transition:box-shadow .3s}
.topo-group.has-down{border-color:var(--red);box-shadow:0 0 0 4px var(--red-soft)}
.topo-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
.topo-name{font-size:11px;font-family:'DM Mono',monospace;letter-spacing:.09em;text-transform:uppercase;color:var(--hint);font-weight:500}
.topo-count{font-size:11px;font-family:'DM Mono',monospace;color:var(--green-text)}
.topo-count.has-down{color:var(--red-text)}
.nodes{display:flex;flex-wrap:wrap;gap:8px}
.node{display:inline-flex;align-items:center;gap:9px;background:var(--subtle);border:1px solid var(--border);border-radius:8px;padding:9px 13px;font-size:14px;cursor:pointer;transition:all .15s}
.node:hover{filter:brightness(.97);border-color:var(--hint)}
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

/* Hosts */
.hosts-toolbar{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:14px;flex-wrap:wrap}
.toolbar-right{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted)}
.compact-toggle{display:inline-flex;align-items:center;gap:6px;cursor:pointer;user-select:none;font-size:12px;color:var(--muted)}
.compact-toggle input{margin:0;cursor:pointer}

.group{margin-bottom:22px}
.group-label{font-size:10px;font-family:'DM Mono',monospace;letter-spacing:.09em;text-transform:uppercase;color:var(--hint);margin-bottom:8px;padding-left:2px;display:flex;align-items:center;gap:8px}
.group-label .down-pill{background:var(--red-bg);color:var(--red-text);font-family:'DM Mono',monospace;font-size:10px;padding:2px 7px;border-radius:4px;letter-spacing:.04em}
.table{background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden;transition:box-shadow .3s}
.table.has-down{box-shadow:0 0 0 4px var(--red-soft)}
.row{display:grid;grid-template-columns:28px 1fr 130px 72px 96px 140px 90px;align-items:center;padding:11px 16px;border-bottom:1px solid var(--border-light);gap:10px;transition:background .1s,padding .15s;cursor:pointer}
.row.hdr{cursor:default}
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

/* Events */
.events-empty{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:48px 20px;text-align:center;color:var(--hint);font-size:13px}
.events-list{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden}
.event{display:grid;grid-template-columns:6px 1fr 110px 110px 100px;gap:14px;padding:13px 18px;border-bottom:1px solid var(--border-light);align-items:center;cursor:pointer}
.event:hover{background:var(--subtle)}
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

/* Modal */
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:flex-start;justify-content:center;z-index:50;overflow-y:auto;padding:40px 16px}
.modal-overlay.open{display:flex}
.modal{background:var(--surface);border:1px solid var(--border);border-radius:14px;width:100%;max-width:840px;box-shadow:0 20px 60px rgba(0,0,0,.2);overflow:hidden}
.modal-hdr{display:flex;align-items:center;justify-content:space-between;padding:18px 22px;border-bottom:1px solid var(--border)}
.modal-title{font-size:16px;font-weight:600;letter-spacing:-.01em}
.modal-body{padding:20px 22px;max-height:65vh;overflow-y:auto}
.modal-foot{padding:14px 22px;border-top:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;gap:10px;background:var(--subtle)}
.edit-row{padding:10px 0;border-bottom:1px solid var(--border-light)}
.edit-row:last-of-type{border-bottom:none}
.edit-row.hdr{font-size:10px;font-family:'DM Mono',monospace;letter-spacing:.07em;text-transform:uppercase;color:var(--hint);padding:6px 0;border-bottom:1px solid var(--border);display:grid;grid-template-columns:1fr 1fr 1fr 70px 70px 60px 32px;gap:8px;align-items:center}
.edit-row .row-main{display:grid;grid-template-columns:1fr 1fr 1fr 70px 70px 60px 32px;gap:8px;align-items:center}
.edit-row .ao-cell{display:flex;align-items:center;justify-content:center}
.edit-row .ao-cell input{width:18px;height:18px;margin:0;cursor:pointer}
.edit-row input[type="text"],.edit-row input[type="number"]{width:100%;padding:7px 10px;border:1px solid var(--border);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:13px;background:var(--surface);color:var(--text)}
.edit-row input:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px var(--blue-bg)}
.edit-row input.invalid{border-color:var(--red);background:var(--red-bg)}
.edit-row .del-btn,.edit-row .more-btn{background:transparent;border:none;color:var(--hint);cursor:pointer;padding:6px;border-radius:4px;font-size:14px;line-height:1;transition:all .15s}
.edit-row .del-btn:hover{background:var(--red-bg);color:var(--red)}
.edit-row .more-btn:hover{background:var(--subtle);color:var(--text)}
.edit-row .more-btn.open{color:var(--text);background:var(--subtle)}
.row-extra{display:none;margin-top:10px;padding:12px;background:var(--subtle);border-radius:8px;grid-template-columns:1fr 1fr;gap:8px 12px}
.row-extra.open{display:grid}
.row-extra label{display:flex;flex-direction:column;gap:4px;font-size:11px;font-family:'DM Mono',monospace;letter-spacing:.05em;color:var(--hint);text-transform:uppercase}
.row-extra label.full{grid-column:1 / -1}
.row-extra textarea{width:100%;padding:7px 10px;border:1px solid var(--border);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:13px;background:var(--surface);color:var(--text);resize:vertical;min-height:60px}
.row-extra textarea:focus,.row-extra input:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px var(--blue-bg)}
.add-row-btn{margin-top:10px;width:100%;padding:10px;border:1px dashed var(--border);background:transparent;border-radius:8px;color:var(--muted);font-family:'DM Sans',sans-serif;font-size:13px;cursor:pointer;transition:all .15s}
.add-row-btn:hover{border-color:var(--hint);background:var(--subtle);color:var(--text)}
.save-status{font-size:12px;color:var(--hint);font-family:'DM Mono',monospace}
.save-status.error{color:var(--red)}
.save-status.success{color:var(--green)}

/* Detail drawer */
.drawer-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.35);opacity:0;pointer-events:none;transition:opacity .2s;z-index:40}
.drawer-backdrop.open{opacity:1;pointer-events:auto}
.drawer{position:fixed;top:0;right:0;bottom:0;width:440px;max-width:100vw;background:var(--surface);border-left:1px solid var(--border);box-shadow:-8px 0 24px rgba(0,0,0,.12);transform:translateX(100%);transition:transform .25s ease-out;z-index:41;display:flex;flex-direction:column}
.drawer.open{transform:translateX(0)}
.drawer-hdr{padding:18px 20px;border-bottom:1px solid var(--border);display:flex;align-items:flex-start;justify-content:space-between;gap:10px;flex-shrink:0}
.drawer-name{font-size:18px;font-weight:600;letter-spacing:-.01em;display:flex;align-items:center;gap:9px}
.drawer-name-dot{width:9px;height:9px;border-radius:50%;flex-shrink:0}
.drawer-name-dot.up{background:var(--green);box-shadow:0 0 0 3px var(--green-bg)}
.drawer-name-dot.down{background:var(--red);box-shadow:0 0 0 3px var(--red-bg)}
.drawer-name-dot.idle{background:var(--hint)}
.drawer-name-dot.wait{background:var(--amber);box-shadow:0 0 0 3px var(--amber-bg)}
.drawer-meta{display:flex;gap:8px;align-items:center;margin-top:4px;font-family:'DM Mono',monospace;font-size:12px;color:var(--muted);flex-wrap:wrap}
.drawer-close{background:transparent;border:none;color:var(--hint);cursor:pointer;font-size:16px;padding:4px 10px;border-radius:6px;line-height:1}
.drawer-close:hover{background:var(--subtle);color:var(--text)}
.drawer-body{flex:1;overflow-y:auto;padding:18px 20px;display:flex;flex-direction:column;gap:18px}
.d-statgrid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}
.d-stat{background:var(--subtle);border:1px solid var(--border-light);border-radius:8px;padding:10px 12px}
.d-stat-label{font-family:'DM Mono',monospace;font-size:10px;letter-spacing:.07em;color:var(--hint);margin-bottom:3px}
.d-stat-val{font-size:18px;font-weight:600;line-height:1.1}
.d-stat-val.green{color:var(--green-text)}
.d-stat-val.red{color:var(--red-text)}
.d-stat-val.blue{color:var(--blue)}
.d-stat-val sup{font-size:11px;color:var(--hint);font-weight:400}
.d-section{display:flex;flex-direction:column;gap:8px}
.d-section-hdr{font-family:'DM Mono',monospace;font-size:10px;letter-spacing:.09em;text-transform:uppercase;color:var(--hint);font-weight:500;display:flex;align-items:center;justify-content:space-between}
.d-spark-wrap{background:var(--subtle);border:1px solid var(--border-light);border-radius:8px;padding:14px}
.d-spark{display:flex;align-items:flex-end;gap:2px;height:46px}
.d-spark-bar{flex:1;background:var(--green);border-radius:1px;min-height:3px;opacity:.85}
.d-spark-bar.dn{background:var(--red);height:3px!important}
.d-spark-axis{display:flex;justify-content:space-between;margin-top:6px;font-family:'DM Mono',monospace;font-size:10px;color:var(--hint)}
.d-specs{background:var(--subtle);border:1px solid var(--border-light);border-radius:8px;overflow:hidden}
.d-spec-row{display:grid;grid-template-columns:90px 1fr;padding:9px 12px;border-bottom:1px solid var(--border-light);font-size:13px;align-items:center;gap:10px}
.d-spec-row:last-child{border-bottom:none}
.d-spec-key{color:var(--hint);font-family:'DM Mono',monospace;font-size:11px;letter-spacing:.04em;text-transform:uppercase}
.d-spec-val{color:var(--text);word-break:break-word}
.d-spec-val.mono{font-family:'DM Mono',monospace;font-size:12px}
.d-notes{background:var(--subtle);border:1px solid var(--border-light);border-radius:8px;padding:11px 13px;font-size:13px;color:var(--text);line-height:1.6;white-space:pre-wrap}
.d-incidents{background:var(--subtle);border:1px solid var(--border-light);border-radius:8px;overflow:hidden}
.d-incident{display:grid;grid-template-columns:6px 1fr auto;gap:10px;padding:9px 12px;border-bottom:1px solid var(--border-light);align-items:center}
.d-incident:last-child{border-bottom:none}
.d-incident-bar{width:3px;height:24px;border-radius:2px;background:var(--green);justify-self:center}
.d-incident.ongoing .d-incident-bar{background:var(--red);animation:pulse 2s ease-in-out infinite}
.d-incident-time{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted)}
.d-incident-time .dur{color:var(--text);font-weight:500;margin-left:6px}
.d-incident-bdg{font-family:'DM Mono',monospace;font-size:9px;font-weight:500;padding:2px 6px;border-radius:3px;letter-spacing:.04em}
.d-incident-bdg.resolved{background:var(--green-bg);color:var(--green-text)}
.d-incident-bdg.ongoing{background:var(--red-bg);color:var(--red-text)}
.d-empty{color:var(--hint);font-size:12px;font-style:italic;padding:8px 0}
.d-actions{display:flex;flex-direction:column;gap:7px}
.d-action-btn{display:flex;align-items:center;justify-content:space-between;background:var(--text);color:var(--surface);border:1px solid var(--text);border-radius:8px;padding:11px 14px;font-family:'DM Sans',sans-serif;font-size:13px;font-weight:500;cursor:pointer;transition:all .15s;text-align:left;width:100%}
.d-action-btn:hover{background:#000;border-color:#000}
.d-action-btn:disabled{opacity:.5;cursor:not-allowed}
.d-action-btn .arrow{color:rgba(255,255,255,.6);font-size:14px}
.d-action-hint{font-size:11px;color:var(--hint);margin-top:4px;line-height:1.5;padding:0 4px}
.d-action-status{font-family:'DM Mono',monospace;font-size:11px;margin-top:4px;padding:0 4px}
.d-action-status.success{color:var(--green-text)}
.d-action-status.error{color:var(--red-text)}
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
    <span>netwatch v3.2 - raspberry pi</span>
    <span>refreshes every 5 s</span>
  </div>
</div>

<div class="drawer-backdrop" id="drawer-backdrop" onclick="closeDrawer()"></div>
<aside class="drawer" id="drawer" role="dialog" aria-modal="true" aria-label="Host details">
  <div class="drawer-hdr">
    <div>
      <div class="drawer-name"><span class="drawer-name-dot" id="d-dot"></span><span id="d-name">-</span></div>
      <div class="drawer-meta" id="d-meta"></div>
    </div>
    <button class="drawer-close" onclick="closeDrawer()" aria-label="Close">x</button>
  </div>
  <div class="drawer-body" id="drawer-body"></div>
</aside>

<div class="modal-overlay" id="modal-overlay">
  <div class="modal">
    <div class="modal-hdr">
      <div class="modal-title">Edit hosts</div>
      <button class="btn btn-ghost" onclick="closeEditor()">X</button>
    </div>
    <div class="modal-body">
      <div class="edit-row hdr">
        <div>Name</div><div>IP address</div><div>Group</div><div>Interval</div><div>Always on</div><div></div><div></div>
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

  const initialTab = localStorage.getItem('nw-tab') || 'topology';
  setTab(initialTab);
  document.querySelectorAll('.tab').forEach(t => {
    t.addEventListener('click', () => setTab(t.dataset.tab));
  });

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
let lastData = null;
let openDrawerIp = null;

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
function lastSeenStr(seconds){
  if(seconds === null || seconds === undefined) return 'never';
  if(seconds < 60) return seconds + 's ago';
  if(seconds < 3600) return Math.floor(seconds/60) + 'm ago';
  if(seconds < 86400) return Math.floor(seconds/3600) + 'h ago';
  return Math.floor(seconds/86400) + 'd ago';
}

function sortHosts(hosts){
  return hosts.slice().sort((a,b) => {
    const aDown = !a.is_up && a.status === 'DOWN';
    const bDown = !b.is_up && b.status === 'DOWN';
    if(aDown !== bDown) return aDown ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
}

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
  const ipAttr = ' data-ip="' + escapeHtml(h.ip) + '"';
  return '<div class="row' + rowCls + '"' + ipAttr + ' onclick="openDrawer(this.dataset.ip)">'
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

function renderTopologyNode(h){
  const isIdle = h.status === 'IDLE';
  const cls = h.status === 'WAIT' ? 'wait' : h.is_up ? 'up' : (isIdle ? 'idle' : 'down');
  let lat;
  if(isIdle) lat = 'idle';
  else if(h.status === 'WAIT') lat = '...';
  else if(h.is_up && h.latency_ms !== null) lat = h.latency_ms.toFixed(1) + 'ms';
  else lat = 'offline';
  return '<div class="node ' + cls + '" data-ip="' + escapeHtml(h.ip) + '" onclick="openDrawer(this.dataset.ip)" title="Click for details">'
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
      return '<div class="problem-pill" data-ip="' + escapeHtml(h.ip) + '" onclick="openDrawer(this.dataset.ip)"><span class="name">' + escapeHtml(h.name) + '</span><span class="ip">' + escapeHtml(h.ip) + '</span>' + dur + '</div>';
    }).join('');
  } else {
    banner.classList.remove('show');
  }
}

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
    return '<div class="event ' + cls + '" data-ip="' + escapeHtml(e.host_ip) + '" onclick="openDrawer(this.dataset.ip)">'
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
    lastData = data;
    renderSummary(data);
    renderTopology(data);
    renderGroups(data);
    renderEvents(data);
    if(openDrawerIp){
      const h = data.hosts.find(x => x.ip === openDrawerIp);
      if(h) renderDrawer(h, data);
    }
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

function openDrawer(ip){
  if(!lastData) return;
  const h = lastData.hosts.find(x => x.ip === ip);
  if(!h) return;
  openDrawerIp = ip;
  renderDrawer(h, lastData);
  document.getElementById('drawer').classList.add('open');
  document.getElementById('drawer-backdrop').classList.add('open');
}
function closeDrawer(){
  openDrawerIp = null;
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('drawer-backdrop').classList.remove('open');
}

function renderDrawer(h, data){
  const dotEl = document.getElementById('d-dot');
  dotEl.className = 'drawer-name-dot ' + (h.status === 'WAIT' ? 'wait' : h.is_up ? 'up' : (h.status === 'IDLE' ? 'idle' : 'down'));
  document.getElementById('d-name').textContent = h.name;
  const badgeCls = h.status === 'WAIT' ? 'badge-wt' : h.is_up ? 'badge-up' : (h.status === 'IDLE' ? 'badge-idle' : 'badge-dn');
  document.getElementById('d-meta').innerHTML =
    '<span>' + escapeHtml(h.ip) + '</span><span>·</span><span>' + escapeHtml(h.group) + '</span>'
    + '<span class="badge ' + badgeCls + '">' + h.status + '</span>';

  // Stats
  const isIdle = h.status === 'IDLE';
  const lats = (h.history || []).filter(x => x === true).length;
  const totalPings = (h.history || []).length;
  let avgLat = null;
  if(h.latency_ms !== null) avgLat = h.latency_ms;
  const labelLat = h.is_up && h.latency_ms !== null ? h.latency_ms.toFixed(1) + ' <sup>ms</sup>' : (h.is_up ? 'up' : 'offline');
  const availLabel = h.uptime_pct !== null ? h.uptime_pct.toFixed(1) + ' <sup>%</sup>' : '-';
  const uColor = isIdle ? 'var(--hint)' : uptimeColor(h.uptime_pct);

  let statsHtml = '<div class="d-statgrid">'
    + '<div class="d-stat"><div class="d-stat-label">CURRENT</div><div class="d-stat-val ' + (h.is_up ? 'green' : (isIdle ? '' : 'red')) + '">' + labelLat + '</div></div>'
    + '<div class="d-stat"><div class="d-stat-label">CURRENT LATENCY</div><div class="d-stat-val blue">' + (h.latency_ms !== null ? h.latency_ms.toFixed(1) + ' <sup>ms</sup>' : '-') + '</div></div>'
    + '<div class="d-stat"><div class="d-stat-label">' + (isIdle ? 'AVAILABILITY' : 'UPTIME') + '</div><div class="d-stat-val" style="color:' + uColor + '">' + availLabel + '</div></div>'
    + '<div class="d-stat"><div class="d-stat-label">LAST SEEN</div><div class="d-stat-val" style="font-size:14px">' + lastSeenStr(h.last_seen_up_seconds) + '</div></div>'
    + '</div>';

  // Sparkline
  const hist = h.history || [];
  let sparkHtml = '';
  if(hist.length > 0){
    sparkHtml = '<div class="d-section"><div class="d-section-hdr"><span>Recent ping history</span><span style="color:var(--muted)">last ' + hist.length + ' pings</span></div>'
      + '<div class="d-spark-wrap"><div class="d-spark">'
      + hist.map(v => v ? '<div class="d-spark-bar" style="height:36px"></div>' : '<div class="d-spark-bar dn"></div>').join('')
      + '</div><div class="d-spark-axis"><span>oldest</span><span>now</span></div></div></div>';
  }

  // Specs
  const specs = h.specs || {};
  const specEntries = [
    ['CPU', specs.cpu],
    ['RAM', specs.ram],
    ['STORAGE', specs.storage],
    ['OS', specs.os],
    ['MAC', specs.mac, true],
  ].filter(([_,v]) => v && String(v).trim());
  let specsHtml = '';
  if(specEntries.length){
    specsHtml = '<div class="d-section"><div class="d-section-hdr"><span>Specs</span></div><div class="d-specs">'
      + specEntries.map(([k,v,mono]) => '<div class="d-spec-row"><div class="d-spec-key">' + k + '</div><div class="d-spec-val' + (mono ? ' mono' : '') + '">' + escapeHtml(v) + '</div></div>').join('')
      + '</div></div>';
  }

  // Notes
  let notesHtml = '';
  if(h.notes && String(h.notes).trim()){
    notesHtml = '<div class="d-section"><div class="d-section-hdr"><span>Notes</span></div><div class="d-notes">' + escapeHtml(h.notes) + '</div></div>';
  }

  // Recent incidents (filter to this host)
  const events = (data.events || []).filter(e => e.host_ip === h.ip);
  let incHtml = '<div class="d-section"><div class="d-section-hdr"><span>Recent incidents</span><span style="color:var(--muted)">' + events.length + ' total</span></div>';
  if(events.length === 0){
    incHtml += '<div class="d-empty">No incidents recorded for this host.</div></div>';
  } else {
    incHtml += '<div class="d-incidents">' + events.slice(0, 5).map(e => {
      const cls = e.ongoing ? 'ongoing' : '';
      const bdgCls = e.ongoing ? 'ongoing' : 'resolved';
      const bdgTxt = e.ongoing ? 'ONGOING' : 'RESOLVED';
      return '<div class="d-incident ' + cls + '">'
        + '<div class="d-incident-bar"></div>'
        + '<div class="d-incident-time">' + escapeHtml(e.started_str) + ' <span class="dur">' + durationStr(e.duration_seconds) + '</span></div>'
        + '<div><span class="d-incident-bdg ' + bdgCls + '">' + bdgTxt + '</span></div>'
        + '</div>';
    }).join('') + '</div></div>';
  }

  // Wake-on-LAN action (only for always_on=false hosts with a MAC set)
  let actionsHtml = '';
  if(h.always_on === false && specs.mac && String(specs.mac).trim()){
    actionsHtml = '<div class="d-section"><div class="d-section-hdr"><span>Actions</span></div>'
      + '<div class="d-actions">'
      + '<button class="d-action-btn" id="d-wake-btn" data-ip="' + escapeHtml(h.ip) + '"><span>Wake this device</span><span class="arrow">-></span></button>'
      + '</div>'
      + '<div class="d-action-hint">Sends a Wake-on-LAN magic packet to ' + escapeHtml(specs.mac) + ' on your local network. Requires WoL to be enabled in the host\'s BIOS/UEFI and OS.</div>'
      + '<div class="d-action-status" id="d-wake-status"></div>'
      + '</div>';
  }

  document.getElementById('drawer-body').innerHTML = statsHtml + sparkHtml + specsHtml + notesHtml + incHtml + actionsHtml;

  const wakeBtn = document.getElementById('d-wake-btn');
  if(wakeBtn) wakeBtn.addEventListener('click', () => sendWake(wakeBtn.dataset.ip));
}

async function sendWake(ip){
  const btn = document.getElementById('d-wake-btn');
  const status = document.getElementById('d-wake-status');
  btn.disabled = true;
  status.className = 'd-action-status';
  status.textContent = 'Sending magic packet...';
  try {
    const res = await fetch('/api/wake', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ip })
    });
    const data = await res.json();
    if(!res.ok){
      status.className = 'd-action-status error';
      status.textContent = data.error || 'Wake failed';
    } else {
      status.className = 'd-action-status success';
      status.textContent = 'Magic packet sent at ' + new Date().toLocaleTimeString();
    }
  } catch(e){
    status.className = 'd-action-status error';
    status.textContent = 'Network error';
  } finally {
    btn.disabled = false;
  }
}

document.addEventListener('keydown', e => {
  if(e.key === 'Escape'){
    if(document.getElementById('modal-overlay').classList.contains('open')) closeEditor();
    else if(openDrawerIp) closeDrawer();
  }
});

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
  const specs = (h && h.specs) || {};
  const notes = (h && h.notes) || '';
  const hasExtra = !!(specs.cpu || specs.ram || specs.storage || specs.os || specs.mac || notes);

  row.innerHTML =
    '<div class="row-main">'
    + '<input type="text" placeholder="My device" class="f-name" value="' + (h ? escapeHtml(h.name) : '') + '">'
    + '<input type="text" placeholder="192.168.1.1" class="f-ip" value="' + (h ? escapeHtml(h.ip) : '') + '">'
    + '<input type="text" placeholder="Network" class="f-group" value="' + (h ? escapeHtml(h.group || "General") : "General") + '">'
    + '<input type="number" min="5" placeholder="30" class="f-interval" value="' + (h && h.interval ? h.interval : '') + '">'
    + '<div class="ao-cell"><input type="checkbox" class="f-alwayson" title="Always on? Uncheck for laptops/phones/etc." ' + (alwaysOn ? 'checked' : '') + '></div>'
    + '<button class="more-btn' + (hasExtra ? ' open' : '') + '" type="button" title="More fields (specs, notes)">...</button>'
    + '<button class="del-btn" title="Remove" type="button">X</button>'
    + '</div>'
    + '<div class="row-extra' + (hasExtra ? ' open' : '') + '">'
    + '<label>CPU<input type="text" class="f-cpu" placeholder="e.g. Intel i9-12900K" value="' + escapeHtml(specs.cpu || '') + '"></label>'
    + '<label>RAM<input type="text" class="f-ram" placeholder="e.g. 64 GB DDR5" value="' + escapeHtml(specs.ram || '') + '"></label>'
    + '<label>Storage<input type="text" class="f-storage" placeholder="e.g. 2TB NVMe" value="' + escapeHtml(specs.storage || '') + '"></label>'
    + '<label>OS<input type="text" class="f-os" placeholder="e.g. Windows 11" value="' + escapeHtml(specs.os || '') + '"></label>'
    + '<label class="full">MAC address<input type="text" class="f-mac" placeholder="aa:bb:cc:dd:ee:ff (required for Wake-on-LAN)" value="' + escapeHtml(specs.mac || '') + '"></label>'
    + '<label class="full">Notes<textarea class="f-notes" placeholder="Anything else worth remembering about this device.">' + escapeHtml(notes) + '</textarea></label>'
    + '</div>';

  document.getElementById('edit-rows').appendChild(row);

  // Wire up the more/delete buttons
  row.querySelector('.more-btn').addEventListener('click', () => {
    const extra = row.querySelector('.row-extra');
    const btn = row.querySelector('.more-btn');
    extra.classList.toggle('open');
    btn.classList.toggle('open', extra.classList.contains('open'));
  });
  row.querySelector('.del-btn').addEventListener('click', () => row.remove());
}

function ipValid(ip){
  return /^(\d{1,3}\.){3}\d{1,3}$/.test(ip) && ip.split('.').every(n => parseInt(n) >= 0 && parseInt(n) <= 255);
}
function macValid(m){
  if(!m) return true;
  return /^([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}$/.test(m.trim()) || /^[0-9a-fA-F]{12}$/.test(m.trim());
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
    const macEl = row.querySelector('.f-mac');
    [nameEl, ipEl, macEl].forEach(el => el && el.classList.remove('invalid'));
    const name = nameEl.value.trim();
    const ip = ipEl.value.trim();
    const group = groupEl.value.trim() || 'General';
    const intervalRaw = intervalEl.value.trim();
    if(!name && !ip) return;
    if(!name){ nameEl.classList.add('invalid'); hasError = true; }
    if(!ipValid(ip)){ ipEl.classList.add('invalid'); hasError = true; }
    if(seenIps.has(ip)){ ipEl.classList.add('invalid'); hasError = true; }
    seenIps.add(ip);
    const mac = macEl.value.trim();
    if(!macValid(mac)){ macEl.classList.add('invalid'); hasError = true; }

    const entry = { name, ip, group };
    if(intervalRaw){
      const iv = parseInt(intervalRaw);
      if(!isNaN(iv) && iv >= 5) entry.interval = iv;
    }
    const alwaysOnEl = row.querySelector('.f-alwayson');
    entry.always_on = alwaysOnEl ? alwaysOnEl.checked : true;

    const specs = {};
    ['cpu','ram','storage','os','mac'].forEach(k => {
      const el = row.querySelector('.f-' + k);
      if(el && el.value.trim()) specs[k] = el.value.trim();
    });
    if(Object.keys(specs).length) entry.specs = specs;
    const notesEl = row.querySelector('.f-notes');
    if(notesEl && notesEl.value.trim()) entry.notes = notesEl.value.trim();

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

refresh();
setInterval(refresh, REFRESH);
setInterval(clockTick, 1000);
clockTick();
</script>
</body>
</html>"""'''


def main():
    if not os.path.isfile(TARGET):
        print(f"ERROR: {TARGET} not found.")
        sys.exit(1)

    content = open(TARGET).read()

    if SENTINEL in content:
        print(f"NOTE: '{SENTINEL}' found - patch already applied.")
        sys.exit(0)

    if "view-topology" not in content or "IncidentLog" not in content:
        print("ERROR: This patch requires patch_dashboard_v3 and patch_events first.")
        sys.exit(1)

    shutil.copy2(TARGET, BACKUP)
    print(f"[OK] Backed up {TARGET} -> {BACKUP}")

    # ── 1. Add specs/notes fields to HostState dataclass
    OLD = '''@dataclass
class HostState:
    name: str
    ip: str
    group: str
    interval: int
    always_on: bool = True
    history: deque = field(default_factory=lambda: deque(maxlen=100))'''
    NEW = '''@dataclass
class HostState:
    name: str
    ip: str
    group: str
    interval: int
    always_on: bool = True
    specs: dict = field(default_factory=dict)
    notes: str = ""
    history: deque = field(default_factory=lambda: deque(maxlen=100))'''
    if content.count(OLD) != 1:
        print(f"[FAIL] HostState dataclass match: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Added specs/notes fields to HostState")

    # ── 2. Include specs + notes in to_dict
    OLD = '''            return {
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
    NEW = '''            return {
                "name":         self.name,
                "ip":           self.ip,
                "group":        self.group,
                "interval":     self.interval,
                "always_on":    self.always_on,
                "specs":        dict(self.specs) if self.specs else {},
                "notes":        self.notes,
                "status":       self.status_str,
                "is_up":        self.is_up,
                "latency_ms":   round(self.last_latency_ms, 2) if self.last_latency_ms else None,
                "uptime_pct":   round(self.uptime_pct, 1) if self.uptime_pct is not None else None,
                "last_checked": self.checked_str,
                "last_seen_up_seconds": last_seen_secs,
                "history":      list(self.history)[-50:],
                "consecutive_down": self.consecutive_down,
            }'''
    if content.count(OLD) != 1:
        print(f"[FAIL] to_dict match: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Added specs/notes to API payload")

    # ── 3. _spawn: accept specs, notes
    OLD = '''    def _spawn(self, name, ip, group, interval, always_on=True):
        host = HostState(
            name=name, ip=ip, group=group, interval=interval,
            always_on=always_on,
            history=deque(maxlen=self.history_window),
            stop_event=threading.Event(),
        )'''
    NEW = '''    def _spawn(self, name, ip, group, interval, always_on=True, specs=None, notes=""):
        host = HostState(
            name=name, ip=ip, group=group, interval=interval,
            always_on=always_on,
            specs=specs or {},
            notes=notes or "",
            history=deque(maxlen=self.history_window),
            stop_event=threading.Event(),
        )'''
    if content.count(OLD) != 1:
        print(f"[FAIL] _spawn match: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Updated _spawn to accept specs/notes")

    # ── 4. load_initial: pass specs, notes
    OLD = '''    def load_initial(self, raw_hosts, default_interval):
        with self.lock:
            for h in raw_hosts:
                self.hosts.append(self._spawn(
                    h["name"], h["ip"], h.get("group", "General"),
                    h.get("interval", default_interval),
                    bool(h.get("always_on", True))
                ))'''
    NEW = '''    def load_initial(self, raw_hosts, default_interval):
        with self.lock:
            for h in raw_hosts:
                self.hosts.append(self._spawn(
                    h["name"], h["ip"], h.get("group", "General"),
                    h.get("interval", default_interval),
                    bool(h.get("always_on", True)),
                    h.get("specs") if isinstance(h.get("specs"), dict) else None,
                    h.get("notes", "") if isinstance(h.get("notes"), str) else ""
                ))'''
    if content.count(OLD) != 1:
        print(f"[FAIL] load_initial match: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Updated load_initial")

    # ── 5. reload_from_config: handle specs + notes
    OLD = '''            for h in new_hosts_config:
                ip = h["ip"]
                new_ips.add(ip)
                interval = h.get("interval", default_interval)
                group = h.get("group", "General")
                name = h["name"]
                always_on = bool(h.get("always_on", True))
                if ip in current_by_ip:
                    existing = current_by_ip[ip]
                    existing.name = name
                    existing.group = group
                    existing.interval = interval
                    existing.always_on = always_on
                    rebuilt.append(existing)
                else:
                    rebuilt.append(self._spawn(name, ip, group, interval, always_on))'''
    NEW = '''            for h in new_hosts_config:
                ip = h["ip"]
                new_ips.add(ip)
                interval = h.get("interval", default_interval)
                group = h.get("group", "General")
                name = h["name"]
                always_on = bool(h.get("always_on", True))
                specs = h.get("specs") if isinstance(h.get("specs"), dict) else {}
                notes = h.get("notes", "") if isinstance(h.get("notes"), str) else ""
                if ip in current_by_ip:
                    existing = current_by_ip[ip]
                    existing.name = name
                    existing.group = group
                    existing.interval = interval
                    existing.always_on = always_on
                    existing.specs = specs
                    existing.notes = notes
                    rebuilt.append(existing)
                else:
                    rebuilt.append(self._spawn(name, ip, group, interval, always_on, specs, notes))'''
    if content.count(OLD) != 1:
        print(f"[FAIL] reload_from_config match: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Updated reload_from_config")

    # ── 6. validate_hosts_config: validate specs and notes
    OLD = '''        if "always_on" in h and not isinstance(h["always_on"], bool):
            return False, f"Host '{h['name']}': always_on must be true or false."
    return True, None'''
    NEW = '''        if "always_on" in h and not isinstance(h["always_on"], bool):
            return False, f"Host '{h['name']}': always_on must be true or false."
        if "specs" in h:
            if not isinstance(h["specs"], dict):
                return False, f"Host '{h['name']}': specs must be a mapping."
            mac = h["specs"].get("mac")
            if mac:
                m = str(mac).strip()
                import re as _re
                if not _re.match(r"^([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}$", m) and not _re.match(r"^[0-9a-fA-F]{12}$", m):
                    return False, f"Host '{h['name']}': MAC address format looks invalid."
        if "notes" in h and not isinstance(h["notes"], str):
            return False, f"Host '{h['name']}': notes must be a string."
    return True, None'''
    if content.count(OLD) != 1:
        print(f"[FAIL] validate match: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Added validation for specs/notes")

    # ── 7. Add WoL helper before HTTP server section
    OLD = '''# ============================================================================
# Dashboard HTML (served inline)
# ============================================================================'''
    NEW = '''# ============================================================================
# Wake-on-LAN
# ============================================================================

def send_wol_packet(mac_address):
    """Send a Wake-on-LAN magic packet to the given MAC. Returns (ok, msg)."""
    import socket
    mac = mac_address.replace(":", "").replace("-", "").lower()
    if len(mac) != 12 or not all(c in "0123456789abcdef" for c in mac):
        return False, "Invalid MAC address format"
    try:
        mac_bytes = bytes.fromhex(mac)
    except ValueError:
        return False, "Could not parse MAC address"
    # Magic packet: 6 bytes of 0xFF, followed by the MAC repeated 16 times.
    packet = b"\\xff" * 6 + mac_bytes * 16
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        # Send to broadcast on port 9 (the standard WoL discard port)
        s.sendto(packet, ("255.255.255.255", 9))
        s.close()
        logging.info(f"WoL: sent magic packet to {mac_address}")
        return True, None
    except OSError as e:
        return False, f"Network error: {e}"


# ============================================================================
# Dashboard HTML (served inline)
# ============================================================================'''
    if content.count(OLD) != 1:
        print(f"[FAIL] WoL anchor match: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Added Wake-on-LAN helper function")

    # ── 8. Add /api/wake POST handler in the HTTP handler
    OLD = '''        def do_POST(self):
            if self.path == "/api/hosts":'''
    NEW = '''        def do_POST(self):
            if self.path == "/api/wake":
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(length).decode()
                    data = json.loads(body)
                    target_ip = data.get("ip", "").strip()
                    if not target_ip:
                        self._send_json(400, {"error": "ip is required"})
                        return
                    target_host = next((h for h in host_manager.list_hosts() if h.ip == target_ip), None)
                    if not target_host:
                        self._send_json(404, {"error": "Host not found"})
                        return
                    mac = (target_host.specs or {}).get("mac", "")
                    if not mac:
                        self._send_json(400, {"error": "Host has no MAC address configured"})
                        return
                    ok, err = send_wol_packet(mac)
                    if ok:
                        self._send_json(200, {"ok": True, "message": f"Magic packet sent to {mac}"})
                    else:
                        self._send_json(500, {"error": err or "Failed to send magic packet"})
                except json.JSONDecodeError:
                    self._send_json(400, {"error": "Invalid JSON"})
                except Exception as e:
                    logging.exception("Error in /api/wake")
                    self._send_json(500, {"error": str(e)})
                return

            if self.path == "/api/hosts":'''
    if content.count(OLD) != 1:
        print(f"[FAIL] POST anchor match: {content.count(OLD)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = content.replace(OLD, NEW, 1)
    print("[OK] Added /api/wake POST endpoint")

    # ── 9. Version bump
    if 'VERSION = "3.1"' in content:
        content = content.replace('VERSION = "3.1"', 'VERSION = "3.2"', 1)
        print("[OK] Version bumped to 3.2")

    # ── 10. Replace DASHBOARD_HTML
    pattern = re.compile(
        r'^DASHBOARD_HTML = r"""<!DOCTYPE html>.*?</html>"""',
        re.DOTALL | re.MULTILINE,
    )
    matches = pattern.findall(content)
    if len(matches) != 1:
        print(f"[FAIL] DASHBOARD_HTML matches: {len(matches)}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)
    content = pattern.sub(NEW_DASHBOARD.replace("\\", r"\\"), content, count=1)
    print("[OK] Replaced DASHBOARD_HTML block")

    open(TARGET, "w").write(content)

    import ast
    try:
        ast.parse(open(TARGET).read())
        print("[OK] Resulting Python is valid")
    except SyntaxError as e:
        print(f"[FAIL] Syntax error: {e}")
        shutil.copy2(BACKUP, TARGET); sys.exit(1)

    print()
    print("Next steps:")
    print("  1. sudo systemctl restart netwatch")
    print("  2. Refresh dashboard - click any host pill/row to open detail drawer")
    print("  3. Click 'Edit hosts' - each row has a new ... button to add specs & notes")
    print("  4. For Wake-on-LAN: set always_on=false, add a MAC address, save")
    print("     The Wake button will then appear in that host's detail drawer.")
    print()
    print(f"Rollback: cp {BACKUP} {TARGET} && sudo systemctl restart netwatch")


if __name__ == "__main__":
    main()
