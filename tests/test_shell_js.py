"""shell.js status store, run in node with a stubbed DOM."""
import os
import subprocess

from js_harness import STATIC, needs_node

SHELL = os.path.join(STATIC, "shell.js")

PRELUDE = r"""
const _els = {};
function mkel(id){ return _els[id] || (_els[id] = {id, style:{}, textContent:'', href:'/static/favicon.svg',
  classList:{add(){},remove(){},toggle(){},contains(){return false}},
  querySelector(){ return {textContent:''}; }, setAttribute(){}}); }
global.document = { getElementById: mkel, addEventListener(){}, querySelectorAll(){ return []; },
  body:{classList:{add(){},remove(){},toggle(){},contains(){return false}}, dataset:{}} };
global.window = global;
global.localStorage = { getItem(){ return null; }, setItem(){} };
global.matchMedia = () => ({matches:false});
var _authState = {logged_in:true, setup_required:false};
var __calls = {landing:0, login:0};
function showLanding(){ __calls.landing++; } function openLogin(){ __calls.login++; } function updateAuthUI(){}
let __fetch = null; global.fetch = (u) => __fetch(u);
function okJson(body, status){ return Promise.resolve({status: status||200, ok: (status||200)<400, json: () => Promise.resolve(body)}); }
"""


def run(tail):
    src = PRELUDE + open(SHELL, encoding="utf-8").read() + "\n(async () => {\n" + tail + "\n})().catch(e => { console.error(e); process.exit(1); });"
    r = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@needs_node
def test_subscribers_receive_each_poll_and_late_subscribers_get_cached_data():
    out = run("""
      __fetch = () => okJson({hosts: [{is_up:true, status:'UP'}], suggestions_pending: 0});
      const seen = [];
      nwStatus.subscribe(d => seen.push('a'));
      await refresh();
      nwStatus.subscribe(d => seen.push('late'));     // cached payload delivered immediately
      await refresh();
      console.log(seen.join(','));
    """)
    assert out == "a,late,a,late"


@needs_node
def test_a_throwing_subscriber_does_not_block_others_or_flip_the_stale_banner():
    out = run("""
      __fetch = () => okJson({hosts: [], suggestions_pending: 0});
      const seen = [];
      nwStatus.subscribe(d => { throw new Error('boom'); });
      nwStatus.subscribe(d => seen.push('ok'));
      const origErr = console.error; console.error = () => {};
      await refresh();
      console.error = origErr;
      console.log(seen.join(',') + '|' + (lastOk ? 'live' : 'stale'));
    """)
    assert out == "ok|live"


@needs_node
def test_subscribe_once_fires_once():
    out = run("""
      __fetch = () => okJson({hosts: [], suggestions_pending: 0});
      let n = 0;
      nwStatus.subscribeOnce(() => n++);
      await refresh(); await refresh();
      console.log(n);
    """)
    assert out == "1"


@needs_node
def test_401_shows_landing_and_does_not_notify_subscribers():
    out = run("""
      __fetch = () => okJson({}, 401);
      _authState.logged_in = false;
      let n = 0; nwStatus.subscribe(() => n++);
      await refresh();
      console.log(n + ',' + __calls.landing);
    """)
    assert out == "0,1"


@needs_node
def test_fetch_failure_marks_stale_then_recovers():
    out = run("""
      let fail = true;
      __fetch = () => fail ? Promise.reject(new Error('net')) : okJson({hosts: [], suggestions_pending: 0});
      await refresh(); const a = lastOk;
      fail = false; await refresh();
      console.log(a + ',' + lastOk);
    """)
    assert out == "false,true"


@needs_node
def test_page_url_and_tab_map():
    out = run("""
      console.log([nwPageUrl('lab','topology'), nwPageUrl('home',''), nwPageUrl('infra',''),
                   NW_TABS.servers.join('/'), NW_TABS.quicklinks.join('/')].join('|'));
    """)
    assert out == "/lab/topology|/|/infra|infra/proxmox|links/"


@needs_node
def test_show_subview_pushes_history_once_and_runs_hooks():
    out = run("""
      const subnav = [{dataset:{subview:'topology'}, classList:{toggle(){}}, setAttribute(){}},
                      {dataset:{subview:'connections'}, classList:{toggle(){}}, setAttribute(){}}];
      document.querySelectorAll = (sel) => sel.includes('subnav') ? subnav : [];
      document.body.dataset = {page:'lab', subviews:'topology,connections,inventory'};
      global.location = {pathname:'/lab/topology', search:''};
      const pushed = [];
      global.history = {pushState(s,t,u){ pushed.push(u); global.location.pathname = u; }};
      global.CustomEvent = function(n, o){ this.type = n; this.detail = o.detail; };
      const fired = []; global.dispatchEvent = e => fired.push(e.detail.name);
      const ran = []; nwOnSubview('connections', () => ran.push('c'));
      nwShowSubview('connections', {push:true});
      nwShowSubview('connections', {push:true});           // already there: no duplicate entry
      nwShowSubview('nope', {push:true});                    // unknown name: ignored
      console.log([pushed.join(','), ran.join(','), fired.join(','), nwCurrentSubview()].join('|'));
    """)
    assert out == "/lab/connections|c,c|connections,connections|connections"


@needs_node
def test_set_tab_switches_in_page_or_navigates_across_pages():
    out = run("""
      document.querySelectorAll = () => [];
      document.body.dataset = {page:'lab', subviews:'topology,connections,inventory'};
      global.location = {pathname:'/lab/topology', search:'', href:''};
      global.history = {pushState(s,t,u){ global.location.pathname = u; }};
      global.CustomEvent = function(n, o){ this.detail = o.detail; };
      global.dispatchEvent = () => {};
      setTab('inventory');  const inPage = location.pathname;
      setTab('hosts');      const cross = location.href;
      setTab('storage');    const legacy = location.href;      // renamed alias for servers
      setTab('bogus');      const same = location.href;
      console.log([inPage, cross, legacy, same].join('|'));
    """)
    assert out == "/lab/inventory|/monitor/hosts|/infra|/infra"


@needs_node
def test_a_throwing_subview_hook_does_not_block_others():
    out = run("""
      document.querySelectorAll = () => [];
      document.body.dataset = {page:'lab', subviews:'topology,connections'};
      global.location = {pathname:'/lab', search:''};
      global.history = {pushState(){}};
      global.CustomEvent = function(n, o){ this.detail = o.detail; };
      global.dispatchEvent = () => {};
      const ran = [];
      nwOnSubview('topology', () => { throw new Error('boom'); });
      nwOnSubview('topology', () => ran.push('second'));
      const origErr = console.error; console.error = () => {};
      nwShowSubview('topology', {push:false});
      console.error = origErr;
      console.log(ran.join(','));
    """)
    assert out == "second"


@needs_node
def test_a_throwing_connections_badge_does_not_flip_the_stale_banner():
    out = run("""
      __fetch = () => okJson({hosts: [], suggestions_pending: 2});
      global.updateConnectionsBadge = () => { throw new Error('badge'); };
      const origErr = console.error; console.error = () => {};
      await refresh();
      console.error = origErr;
      console.log(lastOk ? 'live' : 'stale');
    """)
    assert out == "live"


@needs_node
def test_clicking_the_active_subview_never_pushes_a_duplicate_entry():
    out = run("""
      const subnav = [];
      document.querySelectorAll = () => subnav;
      document.body.dataset = {page:'lab', subviews:'topology,connections,inventory'};
      global.location = {pathname:'/lab', search:''};                 // bare /lab shows topology
      const log = [];
      global.history = {pushState(s,t,u){ log.push('push:' + u); global.location.pathname = u; },
                        replaceState(s,t,u){ log.push('replace:' + u); global.location.pathname = u; }};
      global.CustomEvent = function(n, o){ this.detail = o.detail; };
      global.dispatchEvent = () => {};
      nwShowSubview('topology', {push:false});     // boot: no history call
      nwShowSubview('topology', {push:true});      // click the active one on bare /lab: canonicalise in place
      nwShowSubview('topology', {push:true});      // already canonical: nothing at all
      nwShowSubview('connections', {push:true});   // a different sub-view still pushes exactly once
      console.log(log.join(','));
    """)
    assert out == "replace:/lab/topology,push:/lab/connections"


@needs_node
def test_compute_summary_matches_the_old_kpi_math_and_survives_empty_data():
    out = run("""
      const s = nwComputeSummary({hosts:[
        {is_up:true, status:'UP', latency_ms:2, always_on:true, uptime_pct:100},
        {is_up:false, status:'DOWN', latency_ms:null, always_on:true, uptime_pct:80},
        {is_up:false, status:'IDLE', latency_ms:null, always_on:false, uptime_pct:null},
        {is_up:true, status:'DEGRADED', latency_ms:4, always_on:true, uptime_pct:90},
        {is_up:false, status:'MAINTENANCE', latency_ms:null, always_on:true, uptime_pct:null}]});
      const e = nwComputeSummary({hosts:[]});
      const n = nwComputeSummary({});
      console.log(JSON.stringify([s.up, s.total, s.down, s.degraded, s.maintenance,
        s.avgLat, s.avgUpt, e.total, e.avgLat, e.avgUpt, n.total]));
    """)
    assert out == "[2,5,1,1,1,3,90,0,null,null,0]"


@needs_node
def test_edit_hosts_opens_in_place_on_monitor_and_hops_there_elsewhere():
    out = run("""
      global.location = {pathname:'/lab', search:'', href:''};
      nwEditHosts();                                   // no openEditor on this page
      const hopped = location.href;
      let opened = 0; global.openEditor = () => { opened++; };
      location.href = '';
      nwEditHosts();
      console.log(hopped + '|' + opened + '|' + location.href);
    """)
    assert out == "/monitor?edit=1|1|"
