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
