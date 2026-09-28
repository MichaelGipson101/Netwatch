import os

from js_harness import STATIC, needs_node, run_js

OV = os.path.join(STATIC, "overview.js")
UT = os.path.join(STATIC, "utils.js")
CX = os.path.join(STATIC, "connections.js")


def hm(*names, extra=()):
    parts = [(UT, "function escapeHtml")] + list(extra)
    parts += [(OV, "function " + n) for n in names]
    return parts


@needs_node
def test_host_class_maps_every_status():
    out = run_js(hm("hmHostClass"),
                 "['UP','down','DEGRADED','IDLE','MAINTENANCE','WAIT',null,undefined,'weird'].map(hmHostClass)")
    assert out == ["topo-status-up", "topo-status-down", "topo-status-degraded", "topo-status-idle",
                   "topo-status-idle", "topo-status-unknown", "topo-status-unknown",
                   "topo-status-unknown", "topo-status-unknown"]


@needs_node
def test_is_not_up_lists_problems_and_idle_hosts():
    out = run_js(hm("hmIsNotUp"),
                 "['DOWN','down','DEGRADED','MAINTENANCE','UP','IDLE','WAIT',null].map(s => hmIsNotUp({status:s}))")
    assert out == [True, True, True, True, False, True, False, False]   # idle is listed; up/WAIT/null are not


@needs_node
def test_group_hosts_keeps_first_seen_order_and_counts():
    out = run_js(hm("hmGroupHosts"), """[hmGroupHosts([
        {name:'a',group:'Homelab',is_up:true},{name:'b',group:'VMs',is_up:false},
        {name:'c',group:'Homelab',is_up:false},{name:'d'}]).map(g => [g.name,g.up,g.total,g.hosts.length]),
        hmGroupHosts(null), hmGroupHosts([])]""")
    assert out == [[["Homelab", 1, 2, 2], ["VMs", 0, 1, 1], ["Other", 0, 1, 1]], [], []]


@needs_node
def test_avg_watts_ignores_non_numbers():
    out = run_js(hm("hmAvgWatts"), """[hmAvgWatts([{watts:100},{watts:200},{watts:null},{}]),
        hmAvgWatts([]), hmAvgWatts(undefined), hmAvgWatts([{watts:'x'}]), hmAvgWatts([{watts:10},{watts:11}])]""")
    assert out == [150, None, None, None, 11]


@needs_node
def test_item_href_builds_only_known_pages_and_encodes_params():
    out = run_js(hm("hmItemHref"), """[
        hmItemHref({page:'monitor',subview:'hosts',params:{host:'10.0.0.2'}}),
        hmItemHref({page:'infra',subview:'truenas',params:{}}),
        hmItemHref({page:'lab',subview:'inventory',params:{inv:5}}),
        hmItemHref({page:'lab',subview:'connections'}),
        hmItemHref({page:'monitor',subview:'hosts',params:{host:'a b&c',skip:null}}),
        hmItemHref({page:'home'}),
        hmItemHref({page:'evil',subview:'x'}), hmItemHref({page:'lab',subview:'../x'}),
        hmItemHref(null), hmItemHref(undefined), hmItemHref({})]""")
    assert out == ["/monitor/hosts?host=10.0.0.2", "/infra/truenas", "/lab/inventory?inv=5",
                   "/lab/connections", "/monitor/hosts?host=a%20b%26c", "/",
                   None, "/lab", None, None, None]


@needs_node
def test_ago_formats_seconds_minutes_hours_days():
    out = run_js(hm("hmAgo"), "[4,59,60,720,3*3600,47*3600,48*3600,-5,NaN,null].map(hmAgo)")
    assert out == ["4s", "59s", "1m", "12m", "3h", "47h", "2d", "0s", "", ""]


@needs_node
def test_stats_line_joins_known_parts_only():
    out = run_js(hm("hmStatsLine", "hmAgo"), """[
        hmStatsLine({up:21,total:29,avgLat:4.2,avgUpt:99.6}, 142.4, 4),
        hmStatsLine({up:0,total:0}, null, null),
        hmStatsLine({up:3,total:4,avgLat:null,avgUpt:null}, 0, 90),
        hmStatsLine(null, undefined, undefined)]""")
    assert out == ["21/29 up · 4.2 ms · 99.6% uptime · 142 W · checked 4s ago", "",
                   "3/4 up · 0 W · checked 1m ago", ""]


@needs_node
def test_verdict_led_class():
    out = run_js(hm("hmVerdictLevelClass"), """[hmVerdictLevelClass('ok',false),
        hmVerdictLevelClass('warn',false), hmVerdictLevelClass('down',false),
        hmVerdictLevelClass('ok',true), hmVerdictLevelClass('weird',false),
        hmVerdictLevelClass(undefined,false)]""")
    assert out == ["hm-led-ok", "hm-led-warn", "hm-led-down", "hm-led-stale", "hm-led-stale", "hm-led-stale"]


@needs_node
def test_heartbeat_background_and_label():
    out = run_js(hm("hmHeartbeatBackground", "hmHeartbeatLabel"), """[
        hmHeartbeatBackground([1,0,2,null]), hmHeartbeatBackground([1,1,1]),
        hmHeartbeatBackground([]), hmHeartbeatBackground(undefined), hmHeartbeatBackground('x'),
        hmHeartbeatLabel([1,1,0,2,null]), hmHeartbeatLabel([null,null]), hmHeartbeatLabel(undefined)]""")
    assert out == [
        "linear-gradient(90deg, var(--green) 0% 25%, var(--red) 25% 50%, var(--amber) 50% 75%, var(--border) 75% 100%)",
        "linear-gradient(90deg, var(--green) 0% 33.333%, var(--green) 33.333% 66.667%, var(--green) 66.667% 100%)",
        "none", "none", "none",
        "24h: 2 of 4 periods fully up", "24h: no data", "24h: no data"]


@needs_node
def test_attention_row_html():
    names = ("hmAttentionRowHtml", "hmItemHref")
    out = run_js(hm(*names), """(() => {
      const root = {id:'host_down:10.0.0.2', kind:'host_down', severity:'critical', title:'sw is down',
        detail:'Down 10 min', affected:['a','b'], link:{page:'monitor',subview:'hosts',params:{host:'10.0.0.2'}}};
      const cond = {id:'alert:pool_health_tank', kind:'poller_condition', severity:'warning',
        title:'Pool tank is DEGRADED', detail:'TrueNAS · 1 h', affected:[], link:null};
      const info = {id:'connection_suggestions', kind:'connection_suggestions', severity:'info',
        title:'3 connection suggestions', detail:'Review in Lab', affected:[], link:{page:'lab',subview:'connections',params:{}}};
      const evil = {id:'alert:x', kind:'poller_condition', severity:'warning',
        title:'<img src=x onerror=alert(1)>', detail:'<script>1</script>', affected:[], link:null};
      return [hmAttentionRowHtml(root, true), hmAttentionRowHtml(cond, true), hmAttentionRowHtml(cond, false),
        hmAttentionRowHtml(info, true), hmAttentionRowHtml(evil, true)];
    })()""")
    root, cond_admin, cond_user, info, evil = out
    assert 'hm-aico hm-aico-crit' in root and '<b>sw is down</b>' in root and '2 affected' in root
    assert 'href="/monitor/hosts?host=10.0.0.2"' in root and 'data-dismiss' not in root   # hosts: no dismiss
    assert 'hm-aico-warn' in cond_admin and 'data-dismiss="alert:pool_health_tank"' in cond_admin
    assert 'Open' not in cond_admin                                                        # null link
    assert 'data-dismiss' not in cond_user                                                 # non-admin
    assert 'hm-aico-info' in info and 'href="/lab/connections"' in info and 'affected' not in info
    assert '<img' not in evil and '<script' not in evil and '&lt;img' in evil


@needs_node
def test_host_tile_html():
    names = ("hmHostTileHtml", "hmHostClass", "hmHeartbeatBackground", "hmHeartbeatLabel")
    out = run_js(hm(*names), """[
      hmHostTileHtml({name:'jellyfin',ip:'10.0.0.4',status:'DOWN',device_type:'vm'}, [1,0]),
      hmHostTileHtml({name:'<b>x</b>',ip:'10.0.0.4&x=1',status:'UP',device_type:'<x>'}, undefined),
      hmHostTileHtml({name:'m',ip:'10.0.0.9',status:'UP',device_type:'foo'}, undefined),
      ['host','vm','network','ups','disk','peripheral','tablet','phone','printer'].map(
        t => hmHostTileHtml({name:'m',ip:'10.0.0.9',status:'UP',device_type:t}, undefined).includes('#topo-icon-' + t + '"'))]""")
    tile, evil, unknown, known = out
    assert '#topo-icon-host"' in unknown and 'topo-icon-foo' not in unknown     # unknown type -> host icon
    assert known == [True] * 9
    assert 'class="hm-h3 topo-status-down"' in tile and 'href="/monitor/hosts?host=10.0.0.4"' in tile
    assert '<use href="#topo-icon-vm"/>' in tile and 'aria-label="jellyfin · down"' in tile
    assert '24h: 1 of 2 periods fully up' in tile and 'linear-gradient(90deg' in tile
    assert 'class="hm-ic topo-node-icon"' in tile
    assert '<b>' not in evil and '&lt;b&gt;' in evil
    assert 'host=10.0.0.4%26x%3D1' in evil and '#topo-icon-host' in evil and '24h: no data' in evil


@needs_node
def test_group_html_lists_problem_and_idle_hosts_by_name():
    names = ("hmGroupHtml", "hmHostTileHtml", "hmHostClass", "hmHeartbeatBackground",
             "hmHeartbeatLabel", "hmIsNotUp", "hmNotUpLineHtml", "hmAgo")
    out = run_js(hm(*names), """hmGroupHtml({name:'Homelab', up:1, total:3, hosts:[
        {name:'pve',ip:'10.0.0.2',status:'UP',device_type:'host'},
        {name:'jellyfin',ip:'10.0.0.4',status:'DOWN',device_type:'vm',last_seen_up_seconds:720},
        {name:'laptop',ip:'10.0.0.5',status:'IDLE',device_type:'host'},
        {name:'pending',ip:'10.0.0.6',status:'WAIT',device_type:'host'}]}, {'10.0.0.2':[1,1]})""")
    assert '<span>Homelab</span><em>1/3</em>' in out
    assert 'hm-nu-name">jellyfin</span><span class="hm-nu-meta">down 12m' in out
    assert 'hm-nu-name">laptop</span><span class="hm-nu-meta">idle' in out       # idle hosts are listed too
    assert 'hm-nu-name">pve' not in out                                          # up hosts are not
    assert 'hm-nu-name">pending' not in out                                      # WAIT is not listed
    assert out.count('class="hm-h3 ') == 4                                       # every host has a tile


@needs_node
def test_explain_message_variants():
    out = run_js(hm("hmExplainMessage"), """[
      hmExplainMessage(200,{explanation:'It is the switch.'}), hmExplainMessage(200,{explanation:'x',stale:true}),
      hmExplainMessage(200,{}), hmExplainMessage(404,{error:'ai_not_configured'}),
      hmExplainMessage(404,{error:'other'}), hmExplainMessage(429,{}), hmExplainMessage(502,null),
      hmExplainMessage(500,undefined), hmExplainMessage(0,null)]""")
    generic = "Couldn't generate an explanation right now."
    assert out[0] == {"ok": True, "text": "It is the switch.", "note": ""}
    assert out[1] == {"ok": True, "text": "x", "note": "cached"}
    assert out[2] == {"ok": False, "text": generic}
    assert out[3] == {"ok": False, "text": "Add an OpenRouter key in Settings to enable explanations."}
    assert out[4] == {"ok": False, "text": generic}
    assert out[5] == {"ok": False, "text": "Try again in a moment."}
    assert out[6:] == [{"ok": False, "text": generic}] * 3


@needs_node
def test_ups_text_and_pool_pct():
    out = run_js(hm("hmUpsText", "hmPoolPct"), """[
      hmUpsText({status:'OL CHRG',charge_percent:100}), hmUpsText({status:'OB DISCHRG',charge_percent:64.4}),
      hmUpsText({status:'OL'}), hmUpsText(null), hmUpsText({status:'OB LB',charge_percent:9}),
      hmPoolPct({capacity_used_bytes:61,capacity_total_bytes:100}), hmPoolPct({capacity_total_bytes:0}), hmPoolPct(null)]""")
    assert out == ["on line · 100%", "on battery · 64%", "on line", "unknown", "low battery · 9%", 61, None, None]


@needs_node
def test_free_ports_counts_down_unoccupied_ports_on_live_switches():
    maps = ("[{device_id:1,name:'USW',data:{live:true,ports:[{name:'P1',up:true,occupants:[]},"
            "{name:'P2',up:false,occupants:[]},{name:'P3',up:false,occupants:[{x:1}]}]}},"
            "{device_id:2,name:'old',data:{live:false,ports:[{up:false,occupants:[]}]}}, null]")
    out = run_js(hm("hmFreePorts", extra=[(CX, "function cxLivePortMaps")]),
                 f"[hmFreePorts({maps}), hmFreePorts(null), hmFreePorts([])]")
    assert out == [{"free": 1, "total": 3}, {"free": 0, "total": 0}, {"free": 0, "total": 0}]


@needs_node
def test_safe_url_only_allows_http_and_https():
    out = run_js(hm("hmSafeUrl"), """['https://pve.lan:8006','HTTP://x','javascript:alert(1)',
        null,'//evil','  https://ok.lan ','data:text/html,x'].map(hmSafeUrl)""")
    assert out == ["https://pve.lan:8006", "HTTP://x", "#", "#", "#", "https://ok.lan", "#"]


@needs_node
def test_should_poll_gates_on_visibility_and_interval():
    out = run_js(hm("hmShouldPoll"), """[
        hmShouldPoll(20000, 0, 15000, false), hmShouldPoll(20000, 0, 15000, true),
        hmShouldPoll(15000, 0, 15000, false), hmShouldPoll(14999, 0, 15000, false),
        hmShouldPoll(15000, 0, 15000, true), hmShouldPoll(100000, 90000, 15000, false),
        hmShouldPoll(105000, 90000, 15000, false), hmShouldPoll(0, 0, 15000, false),
        hmShouldPoll(5, 0, 0, false)]""")
    assert out == [True, False, True, False, False, False, True, False, True]


@needs_node
def test_valid_attention_requires_headline_string_and_items_array():
    out = run_js(hm("hmValidAttention"), """[
        hmValidAttention({verdict:{level:'ok',headline:'All good.'},items:[]}),
        hmValidAttention({verdict:{level:'ok',headline:'x'},items:[null,{id:'a'}]}),
        hmValidAttention({verdict:{},items:[null]}), hmValidAttention({verdict:{headline:''},items:[]}),
        hmValidAttention({verdict:{headline:5},items:[]}), hmValidAttention({verdict:{headline:'x'}}),
        hmValidAttention({verdict:{headline:'x'},items:{}}), hmValidAttention({verdict:'x',items:[]}),
        hmValidAttention({}), hmValidAttention(null), hmValidAttention(undefined), hmValidAttention('x')]""")
    assert out == [True, True, False, False, False, False, False, False, False, False, False, False]
