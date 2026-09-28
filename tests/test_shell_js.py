"""shell.js status store, run in node with a stubbed DOM."""
import os
import subprocess

from js_harness import STATIC, js_part, needs_node, run_js

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
    assert out == "/lab/connections|c,c|connections|connections"


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
def test_host_param_parsing():
    drawer = os.path.join(STATIC, "drawer.js")
    parts = [(drawer, "function nwHostParam")]
    ok = run_js(parts, "[nwHostParam('?host=10.0.0.2'), nwHostParam('?host='), nwHostParam(''), nwHostParam('?x=1'), nwHostParam('?host=%3Cimg%3E')]")
    assert ok == ["10.0.0.2", None, None, None, "<img>"]


@needs_node
def test_quickadd_param_roundtrip():
    conn = os.path.join(STATIC, "connections.js")
    parts = [(conn, "function cxParseQuickAddParam"), (conn, "function cxQuickAddUrl")]
    out = run_js(parts, "[cxQuickAddUrl(7, 'Port 4'), cxParseQuickAddParam('?qa=7%3APort%204'), cxParseQuickAddParam('?qa=bad'), cxParseQuickAddParam('')]")
    assert out == ["/lab/connections?qa=7%3APort%204", {"deviceId": 7, "port": "Port 4"}, None, None]


@needs_node
def test_quickadd_param_rejects_non_integer_device_ids():
    conn = os.path.join(STATIC, "connections.js")
    parts = [(conn, "function cxParseQuickAddParam")]
    out = run_js(parts, "['?qa=1.5%3AP', '?qa=x%3AP', '?qa=%3AP', '?qa=:P', '?qa=7'].map(cxParseQuickAddParam)")
    assert out == [None, None, None, None, None]


@needs_node
def test_highlight_param_roundtrip():
    conn = os.path.join(STATIC, "connections.js")
    parts = [(conn, "function cxParseHighlightParam"), (conn, "function cxHighlightUrl")]
    out = run_js(parts, """[
      cxHighlightUrl(12), cxHighlightUrl(12, {edit: true}), cxHighlightUrl(12, {edit: true, focus: 'child_port'}),
      cxParseHighlightParam('?hc=12'), cxParseHighlightParam('?hc=12&hcedit=1'),
      cxParseHighlightParam('?hc=12&hcedit=1&hcfocus=child_port'),
      cxParseHighlightParam('?hc=12&hcfocus=%22%3E%3Cx'),
      cxParseHighlightParam(cxHighlightUrl(5, {edit: true, focus: 'child_port'}).slice(cxHighlightUrl(5).indexOf('?')))
    ]""")
    assert out == [
        "/lab/connections?hc=12", "/lab/connections?hc=12&hcedit=1",
        "/lab/connections?hc=12&hcedit=1&hcfocus=child_port",
        {"id": 12, "edit": False, "focus": None}, {"id": 12, "edit": True, "focus": None},
        {"id": 12, "edit": True, "focus": "child_port"},
        {"id": 12, "edit": False, "focus": None},
        {"id": 5, "edit": True, "focus": "child_port"},
    ]


@needs_node
def test_highlight_param_garbage_is_null():
    conn = os.path.join(STATIC, "connections.js")
    parts = [(conn, "function cxParseHighlightParam")]
    out = run_js(parts, "['', '?hc=', '?hc=abc', '?hc=1.5', '?hc=-3', '?hc=0', '?x=1', '?hc=1e3', '?hc=%3Cimg%3E'].map(cxParseHighlightParam)")
    assert out == [None] * 9


HANDOFF_PRELUDE = r"""
const log = [];
let present = {};                       // element ids currently "in the DOM"
let known = {};                         // connection ids currently loaded
const timers = []; let cleared = 0;
global.setInterval = fn => { timers.push(fn); return timers.length; };
global.clearInterval = () => { cleared++; };
global.document = { getElementById: id => present[id] ? {id} : null };
global.history = { state: {sub: 'connections'}, replaceState(s, t, url){ log.push('replace:' + url); } };
global.location = { pathname: '/lab/connections', search: '', hash: '' };
function cxFindConnection(id){ return known[id] ? {id} : null; }
function cxQuickAddAt(d, p){ log.push('qa:' + d + ':' + p); }
function cxHighlightConnection(id, o){ log.push('hc:' + id + ':' + o.edit + ':' + o.focus); }
"""


def _handoff(js):
    conn = os.path.join(STATIC, "connections.js")
    parts = [(conn, "function cxParseQuickAddParam"), (conn, "function cxParseHighlightParam"),
             (conn, "function cxRunHandoff")]
    return run_js(parts, js, prelude=HANDOFF_PRELUDE)


@needs_node
def test_handoff_no_params_does_nothing():
    out = _handoff("(cxRunHandoff(), [log, timers.length])")
    assert out == [[], 0]


@needs_node
def test_handoff_quickadd_strips_params_first_then_waits_for_the_box_then_acts_once():
    out = _handoff("""(() => {
      location.search = '?qa=7%3APort%204&keep=1';
      cxRunHandoff();
      const afterStart = log.slice();            // stripped, nothing acted yet
      timers[0]();                               // box not there yet
      const waiting = log.slice();
      present['cx-quick'] = true; timers[0]();   // now it exists
      return [afterStart, waiting, log, cleared];
    })()""")
    assert out == [["replace:/lab/connections?keep=1"],
                   ["replace:/lab/connections?keep=1"],
                   ["replace:/lab/connections?keep=1", "qa:7:Port 4"], 1]


@needs_node
def test_handoff_highlight_waits_for_the_connection_then_passes_edit_and_focus():
    out = _handoff("""(() => {
      location.search = '?hc=12&hcedit=1&hcfocus=child_port';
      cxRunHandoff();
      present['cx-table'] = true; timers[0]();          // table but no loaded connection yet
      const early = log.slice();
      known[12] = true; timers[0]();
      return [early, log];
    })()""")
    assert out == [["replace:/lab/connections"],
                   ["replace:/lab/connections", "hc:12:true:child_port"]]


@needs_node
def test_handoff_gives_up_quietly_after_bounded_retries():
    out = _handoff("""(() => {
      location.search = '?hc=99';
      cxRunHandoff();
      for(let i = 0; i < 60; i++) timers[0]();          // connection never appears
      return [log, cleared];
    })()""")
    assert out[0] == ["replace:/lab/connections"]
    assert out[1] >= 1


@needs_node
def test_home_connection_paths_all_land_on_the_lab_connections_view():
    out = run("""
      document.body.dataset = {page:'home', subviews:''};
      global.location = {pathname:'/', search:'', href:''};
      setTab('connections');
      console.log(location.href);
    """)
    assert out == "/lab/connections"


@needs_node
def test_home_port_tiles_hand_off_to_the_lab_when_the_view_is_not_on_the_page():
    conn = os.path.join(STATIC, "connections.js")
    parts = [(conn, "function cxQuickAddUrl"), (conn, "function cxHighlightUrl"),
             (conn, "function cxHighlightConnection"), (conn, "function cxQuickAddAt")]
    out = run_js(parts, """(() => {
      cxQuickAddAt(3, 'Port 9');
      cxHighlightConnection(5, {edit: true, focus: 'child_port'});
      cxHighlightConnection(6);
      return urls;
    })()""", prelude=r"""
      const urls = [];
      global.location = {pathname: '/', search: '', hash: '', set href(v){ urls.push(v); }};
      global.document = {getElementById: () => null};      // Home has no #view-connections
    """)
    assert out == ["/lab/connections?qa=3%3APort%209",
                   "/lab/connections?hc=5&hcedit=1&hcfocus=child_port",
                   "/lab/connections?hc=6"]


@needs_node
def test_navigate_to_host_drawer_goes_to_monitor_with_host_param():
    inv = os.path.join(STATIC, "inventory.js")
    out = run_js([(inv, "function navigateToHostDrawer")], """(() => {
      navigateToHostDrawer('10.0.0.2');
      return [closed, location.href];
    })()""", prelude="let closed = 0; function closeDrawer(){ closed++; }\nglobal.location = {href: ''};")
    assert out == [1, "/monitor/hosts?host=10.0.0.2"]


@needs_node
def test_page_key_matches_the_old_tab_names():
    out = run("""
      const key = (page, sub) => { document.body.dataset = {page: page}; _currentSubview = sub; return nwPageKey(); };
      console.log([key('home',''), key('monitor','hosts'), key('monitor','events'), key('monitor','briefs'),
                   key('lab','topology'), key('lab','connections'), key('lab','inventory'),
                   key('infra',''), key('links','')].join(','));
    """)
    assert out == "overview,hosts,events,briefs,topology,connections,inventory,servers,quicklinks"


@needs_node
def test_compute_summary_tolerates_missing_latency_and_uptime_fields():
    out = run("""
      const s = nwComputeSummary({hosts:[
        {is_up:true, status:'UP', always_on:true},
        {is_up:true, status:'UP', latency_ms:4, always_on:true, uptime_pct:100}]});
      console.log(JSON.stringify([s.avgLat, s.avgUpt]));
    """)
    assert out == "[4,100]"


@needs_node
def test_subview_event_fires_only_on_a_real_change_but_hooks_always_run():
    out = run("""
      document.querySelectorAll = () => [];
      document.body.dataset = {page:'lab', subviews:'topology,connections'};
      global.location = {pathname:'/lab/topology', search:''};
      global.history = {pushState(){}, replaceState(){}};
      global.CustomEvent = function(n, o){ this.detail = o.detail; };
      const fired = []; global.dispatchEvent = e => fired.push(e.detail.name);
      const ran = []; nwOnSubview('topology', () => ran.push('t'));
      nwShowSubview('topology', {push:false});      // boot: '' -> topology is a change
      nwShowSubview('topology', {push:false});      // re-click the active one: no event
      nwShowSubview('connections', {push:false});   // real change: exactly one event
      console.log(fired.join(',') + '|' + ran.join(','));
    """)
    assert out == "topology,connections|t,t"


def test_mira_send_locks_input_before_hydrating_and_always_unlocks():
    """Source-order check only (ai-panel.js has no node harness): the streaming lock must be
    taken before the hydrate await, the send must bail if the conversation was cleared during
    it, and _setStreaming(false) must live in a finally."""
    src = open(os.path.join(STATIC, "ai-panel.js"), encoding="utf-8").read()
    body = src[src.index("async function _sendMessage"):src.index("function _truncateModelName")]
    assert body.index("_setStreaming(true)") < body.index("await _hydrateContext")
    assert body.index("const gen = _convGen") < body.index("await _hydrateContext")
    assert "if(gen !== _convGen) return;" in body
    assert body.count("_setStreaming(false)") == 1
    assert body.index("}finally{") < body.index("_setStreaming(false)")
