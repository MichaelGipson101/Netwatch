# Premium UI Uplift Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved premium UI uplift spec (`docs/superpowers/specs/2026-06-11-premium-ui-uplift-design.md`): self-hosted assets, single-source theming with first-class light mode, mobile rebuild, per-view fixes, perf hygiene, premium finish, keyboard/ARIA access.

**Architecture:** Plain HTML/CSS/JS served by `monitor.py` (no build step). All work on branch `ui-premium-uplift`. Python changes are TDD'd with pytest; UI changes are verified with a sandboxed screenshot harness (Task 1) that runs a patched copy of the working tree on port 8089.

**Tech Stack:** Python 3.13 stdlib http.server, vanilla JS, CSS custom properties, D3 v7 (vendored), pytest, chromium headless, PIL.

**Conventions for every commit:** conventional-commit message ending with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. Run commands from `/home/mgipson/netwatch` unless stated.

---

### Task 1: Screenshot verification harness (dev tooling, NOT committed)

**Files:**
- Create: `/tmp/nw-uplift/shotgen.sh` (outside repo, used by later tasks' verify steps)

- [x] **Step 1: Write the harness script**

```bash
mkdir -p /tmp/nw-uplift && cat > /tmp/nw-uplift/shotgen.sh <<'HARNESS'
#!/bin/bash
# Usage: shotgen.sh sync          -> copy working tree into sandbox + patch auth/alerts
#        shotgen.sh start|stop    -> run/kill sandbox on :8089
#        shotgen.sh shot THEME TAB TOPOVIEW OUT WxH   -> one screenshot
#        shotgen.sh matrix TAG    -> standard matrix into shots/TAG/
set -e
SB=/tmp/nw-uplift/sandbox; SHOTS=/tmp/nw-uplift/shots; SRC=/home/mgipson/netwatch
sync_tree(){
  mkdir -p $SB/static $SHOTS
  cp $SRC/monitor.py $SRC/dashboard.html $SB/
  cp $SRC/static/* $SB/static/
  [ -f $SB/netwatch.db ] || cp $SRC/netwatch.db $SB/ 2>/dev/null || true
  grep -v "ntfy_topic\|openrouter_api_key" $SRC/hosts.yaml > $SB/hosts.yaml
  python3 - <<'EOF'
p='/tmp/nw-uplift/sandbox/monitor.py'; s=open(p).read()
s=s.replace("        def _require_auth(self, admin_only=False):",
            "        def _require_auth(self, admin_only=False):\n            return True  # SANDBOX")
s=s.replace("def _h_get_auth_status(auth_manager, current_user_fn) -> tuple:",
            "def _h_get_auth_status(auth_manager, current_user_fn) -> tuple:\n    return 200, {'logged_in': True, 'username': 'audit', 'admin': True, 'setup_required': False}  # SANDBOX")
open(p,'w').write(s); print('patched auth')
EOF
}
start(){ cd $SB && nohup python3 monitor.py --no-tui --port 8089 --log $SB/m.log >$SB/out.log 2>&1 & sleep 5; curl -sf http://localhost:8089/api/auth/status >/dev/null && echo started; }
stop(){ pkill -f "monitor.py --no-tui --port 8089" 2>/dev/null || true; sleep 1; echo stopped; }
shot(){ # $1 theme $2 tab $3 topoview $4 outfile $5 WxH
  sed -i "s/localStorage.getItem('nw-theme') || '[a-z]*'/localStorage.getItem('nw-theme') || '$1'/g" $SB/static/core.js $SB/dashboard.html 2>/dev/null || true
  sed -i "s/localStorage.getItem('nw-tab') || '[a-z]*'/localStorage.getItem('nw-tab') || '$2'/" $SB/static/core.js
  sed -i "s/localStorage.getItem('nw-topo-view') || '[a-z]*'/localStorage.getItem('nw-topo-view') || '$3'/" $SB/static/topology.js
  sed -i "s/if(localStorage.getItem('nw-tab') === 'inventory') fetchInventory();/fetchInventory();/" $SB/static/inventory.js 2>/dev/null || true
  chromium --headless=new --disable-gpu --hide-scrollbars --no-first-run \
    --user-data-dir=/tmp/nw-uplift/chrome-$$ --window-size=$5 \
    --virtual-time-budget=15000 --screenshot=$4 http://localhost:8089/ 2>/dev/null
  rm -rf /tmp/nw-uplift/chrome-$$; echo "shot: $4"
}
matrix(){ TAG=$1; D=$SHOTS/$TAG; mkdir -p $D
  shot dark  topology web    $D/dark-topo-web.png    1440,900
  shot dark  topology cards  $D/dark-topo-cards.png  1440,900
  shot light topology web    $D/light-topo-web.png   1440,900
  shot light topology cards  $D/light-topo-cards.png 1440,900
  shot dark  hosts    web    $D/dark-hosts.png       1440,900
  shot light hosts    web    $D/light-hosts.png      1440,900
  shot dark  events   web    $D/dark-events.png      1440,900
  shot dark  inventory web   $D/dark-inventory.png   1440,1600
  shot light inventory web   $D/light-inventory.png  1440,1600
  shot dark  hosts    web    $D/m-dark-hosts.png     390,1100
  shot dark  topology web    $D/m-dark-topo.png      390,844
  shot light inventory web   $D/m-light-inventory.png 390,1400
}
case "$1" in
  sync) sync_tree;; start) start;; stop) stop;;
  shot) shot "$2" "$3" "$4" "$5" "$6";;
  matrix) sync_tree; stop; start; matrix "$2"; stop;;
  *) echo "usage: sync|start|stop|shot|matrix";;
esac
HARNESS
chmod +x /tmp/nw-uplift/shotgen.sh
```

- [x] **Step 2: Verify harness against the unmodified tree**

Run: `/tmp/nw-uplift/shotgen.sh matrix baseline`
Expected: 12 PNGs in `/tmp/nw-uplift/shots/baseline/`, "started"/"stopped" printed, no curl failure. These are the BEFORE images for later comparison. Do not commit anything.

---

### Task 2: Vendor D3 + self-hosted fonts

**Files:**
- Create: `static/d3.v7.min.js`, `static/fonts.css`, `static/dmsans-300.woff2`, `static/dmsans-400.woff2`, `static/dmsans-500.woff2`, `static/dmsans-600.woff2`, `static/dmmono-400.woff2`, `static/dmmono-500.woff2`
- Modify: `monitor.py:2870-2878` (`_STATIC_FILES`), `dashboard.html:7-8`, `static/topology.js:32-44`
- Test: `tests/test_netwatch.py` (append)

- [x] **Step 1: Write the failing test**

Append to `tests/test_netwatch.py`:

```python
# ── static asset self-hosting ────────────────────────────────────────────

def test_static_whitelist_includes_vendored_assets():
    from monitor import _STATIC_FILES
    expected = {
        'd3.v7.min.js': 'application/javascript',
        'fonts.css': 'text/css',
        'dmsans-300.woff2': 'font/woff2', 'dmsans-400.woff2': 'font/woff2',
        'dmsans-500.woff2': 'font/woff2', 'dmsans-600.woff2': 'font/woff2',
        'dmmono-400.woff2': 'font/woff2', 'dmmono-500.woff2': 'font/woff2',
    }
    for fname, mime in expected.items():
        assert fname in _STATIC_FILES, fname
        assert _STATIC_FILES[fname].startswith(mime), fname

def test_vendored_asset_files_exist_on_disk():
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'static'))
    for fname in ['d3.v7.min.js', 'fonts.css', 'dmsans-400.woff2', 'dmmono-400.woff2']:
        assert os.path.exists(os.path.join(base, fname)), fname
```

First check the test file's imports (`grep -n "^import\|^from" tests/test_netwatch.py | head`) and add `import os, re, time` to the top if any are missing (Task 3's tests need `re` and `time` too).

- [x] **Step 2: Run tests, verify they fail**

Run: `python3 -m pytest tests/test_netwatch.py -k "vendored or whitelist_includes" -v`
Expected: 2 FAILED (KeyError/assert on `d3.v7.min.js`).

- [x] **Step 3: Download D3 and fonts**

```bash
cd /home/mgipson/netwatch
curl -sfL https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js -o static/d3.v7.min.js
head -c 60 static/d3.v7.min.js   # expect "// https://d3js.org v7.8.5 ..."
python3 - <<'EOF'
import re, urllib.request
UA={'User-Agent':'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36'}
def grab(css_url, fam, weights):
    css=urllib.request.urlopen(urllib.request.Request(css_url,headers=UA)).read().decode()
    # latin block = the LAST unicode-range block per weight in css2 output
    blocks=re.findall(r'/\* latin \*/\s*@font-face\s*{(.*?)}', css, re.S)
    for b in blocks:
        w=re.search(r'font-weight:\s*(\d+)',b).group(1)
        u=re.search(r'url\((https://[^)]+\.woff2)\)',b).group(1)
        if int(w) in weights:
            out=f"static/{fam}-{w}.woff2"
            urllib.request.urlretrieve(u,out); print('saved',out)
grab('https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600&display=swap','dmsans',[300,400,500,600])
grab('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&display=swap','dmmono',[400,500])
EOF
ls -la static/*.woff2   # expect 6 files, each > 10KB
```

- [x] **Step 4: Create `static/fonts.css`**

```css
/* Self-hosted DM Sans + DM Mono (latin subset). No WAN dependency. */
@font-face{font-family:'DM Sans';font-style:normal;font-weight:300;font-display:swap;src:url(/static/dmsans-300.woff2) format('woff2');unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}
@font-face{font-family:'DM Sans';font-style:normal;font-weight:400;font-display:swap;src:url(/static/dmsans-400.woff2) format('woff2');unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}
@font-face{font-family:'DM Sans';font-style:normal;font-weight:500;font-display:swap;src:url(/static/dmsans-500.woff2) format('woff2');unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}
@font-face{font-family:'DM Sans';font-style:normal;font-weight:600;font-display:swap;src:url(/static/dmsans-600.woff2) format('woff2');unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}
@font-face{font-family:'DM Mono';font-style:normal;font-weight:400;font-display:swap;src:url(/static/dmmono-400.woff2) format('woff2');unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}
@font-face{font-family:'DM Mono';font-style:normal;font-weight:500;font-display:swap;src:url(/static/dmmono-500.woff2) format('woff2');unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}
```

- [x] **Step 5: Update `_STATIC_FILES` in monitor.py**

Replace the dict at `monitor.py:2870` with:

```python
_STATIC_FILES = {
    'main.css':    'text/css; charset=utf-8',
    'fonts.css':   'text/css; charset=utf-8',
    'utils.js':    'application/javascript; charset=utf-8',
    'core.js':     'application/javascript; charset=utf-8',
    'topology.js': 'application/javascript; charset=utf-8',
    'inventory.js':'application/javascript; charset=utf-8',
    'auth.js':     'application/javascript; charset=utf-8',
    'ai-panel.js': 'application/javascript; charset=utf-8',
    'd3.v7.min.js':'application/javascript; charset=utf-8',
    'dmsans-300.woff2': 'font/woff2', 'dmsans-400.woff2': 'font/woff2',
    'dmsans-500.woff2': 'font/woff2', 'dmsans-600.woff2': 'font/woff2',
    'dmmono-400.woff2': 'font/woff2', 'dmmono-500.woff2': 'font/woff2',
}
```

- [x] **Step 6: Swap the font link in dashboard.html**

Replace lines 7-8 (`<link rel="preconnect"...>` and the Google `<link href=...>`) with:

```html
<link rel="stylesheet" href="/static/fonts.css?v={{VERSION}}">
```

- [x] **Step 7: Point ensureD3 at the local file**

In `static/topology.js` replace `s.src = 'https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js';` with:

```js
    // Vendored copy — no {{VERSION}} templating inside JS files, and the
    // file content is immutable for this filename, so a bare URL is safe.
    s.src = '/static/d3.v7.min.js';
```

Also change both user-facing error strings `'Could not load D3 from CDN: '` → `'Could not load the graph library: '` and `'Failed to load D3 from CDN'` → `'failed to load /static/d3.v7.min.js'`.

- [x] **Step 8: Run tests + smoke**

Run: `python3 -m pytest tests/test_netwatch.py -v` → all PASS.
Run: `/tmp/nw-uplift/shotgen.sh matrix t2 && grep -c "fonts.googleapis\|cdnjs" /tmp/nw-uplift/sandbox/dashboard.html /tmp/nw-uplift/sandbox/static/topology.js`
Expected: matrix renders identically to baseline (fonts/graph still working — eyeball `t2/dark-topo-web.png`), grep prints `0` for both files.

- [x] **Step 9: Commit**

```bash
git add static/ monitor.py dashboard.html tests/test_netwatch.py
git commit -m "feat: vendor D3 and self-host DM Sans/Mono fonts (zero-WAN UI)"
```

---

### Task 3: Events payload — `started_ts` + date-aware `started_str` (TDD)

**Files:**
- Modify: `monitor.py:1264-1277` (`HistoryDB.list_incidents`)
- Test: `tests/test_netwatch.py` (append)

- [x] **Step 1: Write the failing test**

```python
# ── incident timestamp payload ───────────────────────────────────────────

def _insert_incident(hdb, started, ended=None, dur=None):
    with hdb.lock:
        hdb.conn.execute(
            "INSERT INTO incidents (host_ip, host_name, host_group, started, ended, duration_seconds) "
            "VALUES (?,?,?,?,?,?)",
            ("10.0.0.9", "TestHost", "G", started, ended, dur))

def test_list_incidents_includes_epoch_ts(tmp_path):
    from monitor import HistoryDB
    hdb = HistoryDB(str(tmp_path / "t.db"))
    now = int(time.time())
    _insert_incident(hdb, now - 30, now, 30)
    inc = hdb.list_incidents()[0]
    assert inc["started_ts"] == now - 30

def test_started_str_time_only_for_today(tmp_path):
    from monitor import HistoryDB
    hdb = HistoryDB(str(tmp_path / "t.db"))
    now = int(time.time())
    _insert_incident(hdb, now - 60, now, 60)
    inc = hdb.list_incidents()[0]
    assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", inc["started_str"])

def test_started_str_includes_date_for_older_events(tmp_path):
    from monitor import HistoryDB
    hdb = HistoryDB(str(tmp_path / "t.db"))
    old = int(time.time()) - 3 * 86400
    _insert_incident(hdb, old, old + 60, 60)
    inc = hdb.list_incidents()[0]
    # e.g. "Jun 08 17:39" — month abbrev + day + HH:MM
    assert re.fullmatch(r"[A-Z][a-z]{2} \d{2} \d{2}:\d{2}", inc["started_str"])
```

Add `import re, time` at the top of the test file if not already imported (check first: `grep -n "^import\|^from" tests/test_netwatch.py | head`).

- [x] **Step 2: Run tests, verify failure**

Run: `python3 -m pytest tests/test_netwatch.py -k "incident" -v`
Expected: `test_list_incidents_includes_epoch_ts` FAILS with KeyError `'started_ts'`; the older-events test FAILS on format.

- [x] **Step 3: Implement in `list_incidents`**

Replace the `result.append({...})` block (`monitor.py:1268-1277`) with:

```python
        for inc_id, host_ip, host_name, host_group, started, ended, duration in rows:
            ongoing = ended is None
            dur = (now - started) if ongoing else (duration or 0)
            st = _dt.fromtimestamp(started)
            # Time-only for today's events; month+day prefix once a midnight
            # has passed so the list never shows ambiguous bare times.
            fmt = "%H:%M:%S" if st.date() == _dt.now().date() else "%b %d %H:%M"
            result.append({
                "host_ip":          host_ip,
                "host_name":        host_name,
                "host_group":       host_group,
                "started_ts":       started,
                "started_str":      st.strftime(fmt),
                "started_iso":      st.isoformat(),
                "ended_iso":        _dt.fromtimestamp(ended).isoformat() if ended else None,
                "duration_seconds": dur,
                "ongoing":          ongoing,
            })
```

- [x] **Step 4: Run the full suite**

Run: `python3 -m pytest tests/test_netwatch.py -v`
Expected: all PASS (existing tests unaffected — additive field, same `started_str` shape for today's events).

- [x] **Step 5: Commit**

```bash
git add monitor.py tests/test_netwatch.py
git commit -m "feat: add started_ts to incidents payload, date-aware started_str"
```

---

### Task 4: Theme resolution in JS + CSS de-duplication + color-scheme

**Files:**
- Modify: `dashboard.html` (head), `static/core.js:1-17`, `static/main.css` (delete ~45 dup blocks; add color-scheme + scrollbars)

- [x] **Step 1: Add the inline resolver to dashboard.html**

Insert directly BEFORE the `<link rel="stylesheet" href="/static/fonts.css...">` line:

```html
<script>
(function(){
  var mq = window.matchMedia('(prefers-color-scheme: dark)');
  function apply(){
    var pref = localStorage.getItem('nw-theme') || 'auto';
    var resolved = pref === 'auto' ? (mq.matches ? 'dark' : 'light') : pref;
    document.documentElement.setAttribute('data-theme', resolved);
  }
  apply();
  mq.addEventListener('change', apply);
  window.nwApplyTheme = apply;   // setTheme() re-runs this after pref changes
})();
</script>
```

Note: keep the literal `localStorage.getItem('nw-theme') || 'auto'` — the screenshot harness seds that exact pattern.

- [x] **Step 2: Rewrite the theme code at the top of core.js**

Replace `core.js:1-11` (the `initTheme` IIFE + `setTheme`) with:

```js
// Theme: the inline <head> script resolves auto -> light|dark before first
// paint and exposes window.nwApplyTheme. Here we only store the preference
// and refresh the resolved attribute + button states.
function setTheme(mode){
  localStorage.setItem('nw-theme', mode);
  if(window.nwApplyTheme) window.nwApplyTheme();
  document.querySelectorAll('#theme-toggle button').forEach(b => {
    const active = b.dataset.themeBtn === mode;
    b.classList.toggle('active', active);
    b.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}
```

In the DOMContentLoaded handler just below, keep the existing button wiring but add `b.setAttribute('aria-pressed', b.dataset.themeBtn === current ? 'true' : 'false');` next to the `classList.toggle('active', ...)` line.

- [x] **Step 3: Delete the duplicated auto-theme CSS**

```bash
python3 - <<'EOF'
p='static/main.css'; lines=open(p).read().split('\n')
out=[]; i=0; removed=0
while i < len(lines):
    l=lines[i]
    # single-line duplicate: @media(prefers-color-scheme:dark){[data-theme="auto"] ... }}
    if l.startswith('@media(prefers-color-scheme:dark){[data-theme="auto"]') and l.endswith('}}'):
        removed+=1; i+=1; continue
    # multi-line media block (with or without spaces): consume until braces
    # balance, drop the whole block only if it targets [data-theme="auto"]
    if l.replace(' ','').startswith('@media(prefers-color-scheme:dark){') and l.count('{') > l.count('}'):
        depth=l.count('{')-l.count('}'); block=[l]; j=i+1
        while j < len(lines) and depth>0:
            depth+=lines[j].count('{')-lines[j].count('}'); block.append(lines[j]); j+=1
        if 'data-theme="auto"' in '\n'.join(block): removed+=1; i=j; continue
        out.extend(block); i=j; continue
    out.append(l); i+=1
open(p,'w').write('\n'.join(out)); print('removed', removed, 'auto blocks')
EOF
grep -c 'data-theme="auto"' static/main.css
```

Expected: script prints `removed 47 auto blocks` (±2 is fine — count them first with `grep -c '"auto"' static/main.css` if you want certainty), then grep prints `0`.

Expected output: `0`. If not 0, inspect the survivors with `grep -n 'data-theme="auto"' static/main.css` and delete those blocks by hand (they are all duplicates of an adjacent `[data-theme="dark"]` rule — never delete the dark twin).

- [x] **Step 4: Add color-scheme + scrollbar styling**

In `:root{...}` (main.css:2) add `color-scheme:light;` as the first declaration. In `[data-theme="dark"]{...}` add `color-scheme:dark;`. Then append after the `.landing-setup-desc` rule at the end of the file:

```css
/* ── Native widget + scrollbar polish ── */
.drawer-body,.modal-body,.ai-messages{scrollbar-width:thin;scrollbar-color:var(--hint) transparent}
.drawer-body::-webkit-scrollbar,.modal-body::-webkit-scrollbar,.ai-messages::-webkit-scrollbar{width:8px}
.drawer-body::-webkit-scrollbar-thumb,.modal-body::-webkit-scrollbar-thumb,.ai-messages::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px}
.drawer-body::-webkit-scrollbar-thumb:hover,.modal-body::-webkit-scrollbar-thumb:hover,.ai-messages::-webkit-scrollbar-thumb:hover{background:var(--hint)}
```

- [x] **Step 5: Verify both themes via harness**

Run: `/tmp/nw-uplift/shotgen.sh matrix t4`
Expected: `t4/dark-*` identical in look to baseline darks (frost intact — proves dedup deleted only duplicates); `t4/light-*` identical to baseline lights; dark checkboxes now render dark ("Include unconnected" box in `dark-topo-web.png`). View the four topo shots to confirm.

- [x] **Step 6: Commit**

```bash
git add dashboard.html static/core.js static/main.css
git commit -m "refactor: resolve theme in JS, single-source dark CSS, add color-scheme"
```

---

### Task 5: Design tokens, light-mode elevation, contrast, numeric polish

**Files:**
- Modify: `static/main.css` (`:root`, `[data-theme="dark"]`, scards/tables/err-banner/fab, append utilities)

- [x] **Step 1: Extend the variable blocks**

In `:root{}` adjust/add (keep existing vars, change `--hint`, add new ones):

```css
  --hint:#757a84;
  --shadow-1:0 1px 2px rgba(24,24,20,.05);
  --shadow-2:0 1px 2px rgba(24,24,20,.05),0 8px 24px rgba(24,24,20,.06);
  --shadow-3:0 2px 6px rgba(24,24,20,.07),0 16px 40px rgba(24,24,20,.10);
  --green-glow:rgba(22,163,74,.40);
  --amber-border:#f3d489;
```

In `[data-theme="dark"]{}` adjust/add:

```css
  --hint:#76736b;
  --shadow-1:0 1px 2px rgba(0,0,0,.3);
  --shadow-2:0 2px 8px rgba(0,0,0,.35);
  --shadow-3:0 8px 28px rgba(0,0,0,.5);
  --green-glow:rgba(34,197,94,.45);
  --amber-border:rgba(245,158,11,.35);
```

- [x] **Step 2: Apply tokens**

- `.err-banner` (main.css:90): `border:1px solid #fde68a` → `border:1px solid var(--amber-border)`.
- `.fab` (main.css:581): both `box-shadow` colors `rgba(93,187,141,...)` → `var(--green-glow)`; in `.fab:hover`/`.fab:active` likewise.
- `.scard`: add `box-shadow:var(--shadow-1);transition:box-shadow .2s,transform .2s`.
- `.table`, `.topo-group`, `.events-list`, `.events-empty`, `.inv-table-wrap`: add `box-shadow:var(--shadow-1)`.
- `.modal`: change hardcoded `box-shadow:0 20px 60px rgba(0,0,0,.2)` → `box-shadow:var(--shadow-3)`.
- Light ambient wash — append:

```css
/* Light theme ambient wash (mirrors dark's radial blobs) */
[data-theme="light"] body{background-image:radial-gradient(ellipse 720px 460px at 30% -10%,rgba(22,163,74,.045) 0%,transparent 65%),radial-gradient(ellipse 460px 340px at 85% 100%,rgba(22,163,74,.03) 0%,transparent 65%);background-attachment:fixed}
```

- [x] **Step 3: Tabular numerals**

Append:

```css
#clock,.scard-val,.d-stat-val,.lat,.uptime-pct,.topo-overlay-num,.inv-metric-val,.event-dur,.d-pi-val{font-variant-numeric:tabular-nums}
```

- [x] **Step 4: Verify + commit**

Run: `/tmp/nw-uplift/shotgen.sh matrix t5` — check `light-hosts.png` (COMPUTERS label readable, cards have depth), `dark-hosts.png` (unchanged feel, err-banner not applicable).

```bash
git add static/main.css
git commit -m "feat: design tokens, light-mode elevation + contrast, tabular numerals"
```

---

### Task 6: Topology light glass + theme-aware canvas internals

**Files:**
- Modify: `static/main.css` (topo furniture rules), `static/topology.js` (vignette defs), `dashboard.html` (legend swatch classes)

- [x] **Step 1: Scoped glass variables**

Append to main.css (after the topo section is fine — variables cascade):

```css
/* ── Topo canvas glass: light defaults, dark overrides ── */
.topo-web{
  --glass-bg:rgba(255,255,255,.78);--glass-bg-strong:rgba(255,255,255,.92);
  --glass-border:rgba(15,18,24,.10);--glass-text:#1a1a1a;--glass-muted:#5d6470;
  --canvas-dot:#d8d6cf;--edge-dead:#9aa0ab;
  --edge-eth:#3f6fe0;--edge-wifi:#1ba8a0;--edge-fiber:#8b53c0;--edge-power:#d18a14;--edge-virtual:#9a5fc4;--edge-usb:#3d9c6f;--edge-console:#7a7a7a;
}
[data-theme="dark"] .topo-web{
  --glass-bg:rgba(20,22,28,.65);--glass-bg-strong:rgba(15,14,13,.88);
  --glass-border:rgba(255,255,255,.10);--glass-text:#e8e6e0;--glass-muted:#9b998f;
  --canvas-dot:#232220;--edge-dead:#9b998f;
  --edge-eth:#5b8eff;--edge-wifi:#3dc7c0;--edge-fiber:#a872d6;--edge-power:#f0a93b;--edge-virtual:#b07cd6;--edge-usb:#5dbb8d;--edge-console:#888;
}
```

- [x] **Step 2: Re-point the furniture at the variables**

Rewrite these existing rules (find each by selector) so colors come from the vars; structure/spacing unchanged:

```css
.topo-web-overlay{background:var(--glass-bg);border:1px solid var(--glass-border);box-shadow:var(--shadow-2)}
.topo-overlay-num{color:var(--glass-text)}
.topo-overlay-lbl{color:var(--glass-muted)}
.topo-overlay-divider{background:var(--glass-border)}
.topo-legend-btn{background:var(--glass-bg);border:1px solid var(--glass-border);color:var(--glass-muted);box-shadow:var(--shadow-1)}
.topo-legend-btn:hover{color:var(--glass-text);box-shadow:var(--shadow-2)}
.topo-legend{background:var(--glass-bg-strong);border:1px solid var(--glass-border);color:var(--glass-text)}
.topo-legend-row{color:var(--glass-text)}
body.topo-fullscreen-active .topo-fs-close,
body.topo-fullscreen-active .topo-fs-fit{background:var(--glass-bg);border:1px solid var(--glass-border);color:var(--glass-muted)}
body.topo-fullscreen-active .topo-fs-close:hover,
body.topo-fullscreen-active .topo-fs-fit:hover{color:var(--glass-text)}
.topo-grid-dot{fill:var(--canvas-dot)}
.topo-edge-dead .topo-edge-line{stroke:var(--edge-dead) !important}
.topo-edge-ethernet .topo-edge-line{stroke:var(--edge-eth)}   .topo-edge-ethernet .topo-edge-flow{fill:var(--edge-eth)}
.topo-edge-wifi .topo-edge-line{stroke:var(--edge-wifi)}      .topo-edge-wifi .topo-edge-flow{fill:var(--edge-wifi)}
.topo-edge-fiber .topo-edge-line{stroke:var(--edge-fiber)}    .topo-edge-fiber .topo-edge-flow{fill:var(--edge-fiber)}
.topo-edge-power .topo-edge-line{stroke:var(--edge-power)}    .topo-edge-power .topo-edge-flow{fill:var(--edge-power)}
.topo-edge-virtual .topo-edge-line{stroke:var(--edge-virtual)}.topo-edge-virtual .topo-edge-flow{fill:var(--edge-virtual)}
.topo-edge-usb .topo-edge-line{stroke:var(--edge-usb)}        .topo-edge-usb .topo-edge-flow{fill:var(--edge-usb)}
.topo-edge-console .topo-edge-line{stroke:var(--edge-console)}.topo-edge-console .topo-edge-flow{fill:var(--edge-console)}
```

Keep each rule's non-color declarations (dash patterns, opacities, blur) exactly as they are — edit color values only. Delete the now-redundant `[data-theme="dark"] .topo-web-overlay` and `[data-theme="dark"] .topo-legend` Deep Frost rules.

- [x] **Step 3: Dual vignette gradients**

In `topology.js`, replace the single vignette gradient block (the `defs.append('radialGradient')...` lines) with:

```js
  // Two vignettes; CSS picks the right one per theme.
  [['topo-vignette-dark','rgba(0,0,0,0.4)'],['topo-vignette-light','rgba(15,18,24,0.07)']].forEach(([id,edge]) => {
    const g = defs.append('radialGradient').attr('id', id)
      .attr('cx','50%').attr('cy','50%').attr('r','70%');
    g.append('stop').attr('offset','60%').attr('stop-color','transparent');
    g.append('stop').attr('offset','100%').attr('stop-color', edge);
  });
```

And remove the `.attr('fill', 'url(#topo-vignette)')` from the vignette rect (keep `class="topo-vignette-rect"`). Add to main.css:

```css
.topo-vignette-rect{fill:url(#topo-vignette-light)}
[data-theme="dark"] .topo-vignette-rect{fill:url(#topo-vignette-dark)}
```

- [x] **Step 4: Legend swatch lines follow the palette**

In `dashboard.html` legend rows (lines ~318-323), replace each hardcoded `stroke="#5b8eff"` etc. with `class="leg-eth"` (and `leg-wifi`, `leg-fiber`, `leg-power`, `leg-virtual`, `leg-dead`) on the `<line>` elements, then add:

```css
.topo-legend-svg-line .leg-eth{stroke:var(--edge-eth)}
.topo-legend-svg-line .leg-wifi{stroke:var(--edge-wifi)}
.topo-legend-svg-line .leg-fiber{stroke:var(--edge-fiber)}
.topo-legend-svg-line .leg-power{stroke:var(--edge-power)}
.topo-legend-svg-line .leg-virtual{stroke:var(--edge-virtual)}
.topo-legend-svg-line .leg-dead{stroke:var(--edge-dead)}
```

- [x] **Step 5: Verify + commit**

Run: `/tmp/nw-uplift/shotgen.sh matrix t6`. Check `light-topo-web.png`: overlays readable (dark text on white glass), no smudge-ring vignette, edges visible on white, legend `?` visible bottom-left. Check `dark-topo-web.png` unchanged vs baseline. Crop overlays at full res if needed (PIL crop as in audit).

```bash
git add static/main.css static/topology.js dashboard.html
git commit -m "feat: first-class light mode for topology canvas (glass, vignette, edge palette)"
```

---

### Task 7: Topology behavior — auto-fit, simulation rest, rAF flow dots

**Files:**
- Modify: `static/topology.js`

- [x] **Step 1: Interaction tracking + auto-fit**

Add module state near the top (after `_topoD3Loading`):

```js
let _topoUserAdjusted = false;   // true once the user pans/zooms/drags
let _flowRaf = null;             // requestAnimationFrame id for flow dots
const _reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
```

In `renderTopologyWeb()`: set `_topoUserAdjusted = false;` right before `container.innerHTML = '';`. In the zoom handler change to:

```js
  _topoZoom = d3.zoom()
    .scaleExtent([0.1, 4])
    .on('zoom', (ev) => {
      if(ev.sourceEvent) _topoUserAdjusted = true;   // ignore programmatic fits
      zoomG.attr('transform', ev.transform);
    });
```

In `dragStart` add `_topoUserAdjusted = true;`.

- [x] **Step 2: Let the simulation actually stop**

Replace the cooldown lines

```js
  sim.alpha(1).restart();
  setTimeout(() => sim.alphaTarget(0.02).restart(), 4000);
```

with:

```js
  sim.alpha(1).restart();
  setTimeout(() => {
    sim.alphaTarget(0);              // decay below alphaMin -> tick loop stops
    if(!_topoUserAdjusted) fitTopologyToView();
  }, 4000);
```

In `dragEnd` change `sim.alphaTarget(0.02)` → `sim.alphaTarget(0)`. In the ResizeObserver callback change `_topoSimulation.alphaTarget(0.05).restart()` cool-down line `alphaTarget(0.02)` → `alphaTarget(0)`.

- [x] **Step 3: Flow dots on rAF with cached lengths**

In the tick handler, delete the entire `edgeSel.each(function(d){ ... })` dot-positioning block and replace with length caching:

```js
    edgeSel.each(function(){
      const path = this.querySelector('path.topo-edge-line');
      this._flowLen = path ? path.getTotalLength() : 0;   // cache while geometry changes
    });
```

After the `sim.on('tick', ...)` block, add the standalone loop + lifecycle:

```js
  // Flow dots: time-based rAF loop, independent of the (now-resting)
  // simulation. Uses cached path lengths; pauses when hidden; disabled
  // under prefers-reduced-motion.
  const SPEEDS = {ethernet:.10, fiber:.16, wifi:.06, virtual:.07, power:.05, usb:.13, console:.065, other:.10};
  if(_flowRaf) cancelAnimationFrame(_flowRaf);
  function flowFrame(ts){
    edgeSel.each(function(d){
      const len = this._flowLen;
      if(!len) return;
      const path = this.querySelector('path.topo-edge-line');
      const speed = SPEEDS[d.connection_type] || .10;
      const t = (ts / 1000 * speed) % 1;
      const fwd = this.querySelector('.topo-edge-flow-fwd');
      const rev = this.querySelector('.topo-edge-flow-rev');
      if(fwd){ const p = path.getPointAtLength(t * len); fwd.setAttribute('cx', p.x); fwd.setAttribute('cy', p.y); }
      if(rev){ const p = path.getPointAtLength((1 - (t + .5) % 1) * len); rev.setAttribute('cx', p.x); rev.setAttribute('cy', p.y); }
    });
    _flowRaf = requestAnimationFrame(flowFrame);
  }
  if(!_reducedMotion.matches) _flowRaf = requestAnimationFrame(flowFrame);
```

Append once at module level (bottom of file):

```js
// Pause the flow loop when the tab is hidden or we leave web view.
document.addEventListener('visibilitychange', () => {
  if(document.hidden){
    if(_flowRaf){ cancelAnimationFrame(_flowRaf); _flowRaf = null; }
  } else if(_topoView === 'web' && _topoSvg){
    renderTopologyWeb();   // re-render restarts sim warmup + flow loop cleanly
  }
});
```

And in `setTopoView()`, when leaving web mode add: `if(_flowRaf){ cancelAnimationFrame(_flowRaf); _flowRaf = null; }`.

- [x] **Step 4: Two-step reset (drop confirm())**

Replace `topologyResetPositions()`:

```js
let _resetArmTimer = null;
function topologyResetPositions(){
  const btn = document.querySelector('.topo-web-controls .topo-view-btn-ghost:last-child')
    || document.querySelector('[onclick="topologyResetPositions()"]');
  if(!btn) return;
  if(btn.dataset.armed !== '1'){
    btn.dataset.armed = '1';
    btn.dataset.label = btn.textContent;
    btn.textContent = 'Confirm reset?';
    btn.style.color = 'var(--red-text)';
    _resetArmTimer = setTimeout(() => disarmReset(btn), 3000);
    return;
  }
  clearTimeout(_resetArmTimer);
  disarmReset(btn);
  clearTopoPositions();
  fetchAndRenderTopologyWeb();
}
function disarmReset(btn){
  btn.dataset.armed = '';
  if(btn.dataset.label) btn.textContent = btn.dataset.label;
  btn.style.color = '';
}
```

- [x] **Step 5: Reduced-motion CSS for remaining canvas animation**

Extend the existing `@media (prefers-reduced-motion: reduce)` block in main.css to:

```css
@media (prefers-reduced-motion: reduce){
  .topo-status-up .topo-node-icon,
  .topo-status-down .topo-node-icon{animation:none}
  .topo-grid-bg{animation:none}
  .live-dot{animation:none}
  .event-bar,.d-incident-bar{animation:none !important}
  #ai-bubble-btn.streaming{animation:none}
  .topo-edge-flow{display:none}
}
```

- [x] **Step 6: Verify + commit**

Run: `/tmp/nw-uplift/shotgen.sh matrix t7` — `dark-topo-web.png` should now show the full graph framed (auto-fit; nothing clipped at edges). Manually: `top -bn1 -p $(pgrep -f 'port 8089')` while a real chromium kiosk is open is optional; primary check is visual.

```bash
git add static/topology.js static/main.css
git commit -m "perf: rest force simulation, rAF flow dots, auto-fit, two-step reset"
```

---

### Task 8: Boot wiring + dead code removal

**Files:**
- Modify: `static/core.js`, `static/inventory.js:1378-1391`, `dashboard.html:12-53`, `static/main.css`

- [x] **Step 1: Move boot into core.js**

In core.js's existing `DOMContentLoaded` listener (after the compact-mode wiring), append:

```js
  // App boot: auth gate, polling loops. Lives here (not inventory.js) so a
  // failure in any later-loaded file can't kill the heartbeat.
  fetchAuthState();
  setInterval(fetchAuthState, 60000);
  setInterval(refresh, REFRESH);
  setInterval(clockTick, 1000);
  clockTick();
```

(`fetchAuthState` is defined in auth.js — loaded before DOMContentLoaded fires, since all scripts are classic scripts before `</body>`.)

- [x] **Step 2: Inventory loads via setTab**

In `setTab(tab)` add as last line: `if(tab === 'inventory' && typeof fetchInventory === 'function') fetchInventory();`
Delete from inventory.js: the tab click-hook (`document.addEventListener('click', e => { const tab = e.target.closest('.tab[data-tab="inventory"]'); ... })`), the `if(localStorage.getItem('nw-tab') === 'inventory') fetchInventory();` line, and the four boot lines (`fetchAuthState(); setInterval(...) x3`).

- [x] **Step 3: Delete dead code**

- `dashboard.html:12-53`: remove the entire first `<svg>` sprite block (symbols `icon-host` … `icon-peripheral`). Verify nothing references them: `grep -rn '"#icon-' static/ dashboard.html` → no output.
- main.css: delete `.topo-legend-shape` rules (7 rules, lines ~415-421), `.topo-legend-line` + its 6 `.topo-leg-*` variants (~427, 439-444), and `@keyframes topo-hover-ring`.
- Verify: `grep -n "topo-legend-shape\|topo-legend-line\|topo-hover-ring" static/main.css dashboard.html static/*.js` → no output.

- [x] **Step 4: Verify + commit**

Run: `/tmp/nw-uplift/shotgen.sh matrix t8` — every tab still renders + refreshes (clock shows, summary populated, inventory table loads). Note: the harness's inventory sed becomes a no-op after this task — restored-tab loading now works natively; confirm `dark-inventory.png` shows data.

```bash
git add static/core.js static/inventory.js dashboard.html static/main.css
git commit -m "refactor: boot wiring in core.js, inventory load via setTab, remove dead code"
```

---

### Task 9: Mobile rebuild

**Files:**
- Modify: `static/main.css` (the `@media (max-width: 768px)` block + new ≤480px rules)

- [x] **Step 1: Replace the dead-selector rules**

Inside the existing `@media (max-width: 768px){...}` block, DELETE these rules: `.topbar{...}`, `.topbar .live-pip{...}`, `.topbar .clock{...}`, `.theme-toggle .theme-label{...}`, `.group-card{...}`, `.row-hdr{...}`, `.events-hdr{...}`, `.event .col-started,.event .col-ended{...}`, `.col-group` (from the `.col-ip,.col-ping,.col-group` list — keep `.col-ip,.col-ping`). ADD in their place:

```css
  /* Nav: single compact row */
  nav{padding:8px 12px;margin-left:-12px;margin-right:-12px;gap:8px}
  .nw-logo{height:22px}
  #pip{display:none}
  .nav-right{gap:8px}

  /* Hosts table: header hidden, rows become a deliberate 2-line grid */
  .row.hdr{display:none}
  .row{grid-template-columns:20px 1fr 76px;grid-template-rows:auto auto;gap:4px 8px;padding:10px 12px}
  .row > div:nth-child(1){grid-row:1;grid-column:1;align-self:center}
  .row > div:nth-child(2){grid-row:1;grid-column:2}              /* name + ip sub */
  .row > div:nth-child(4){grid-row:1;grid-column:3;justify-self:end}  /* badge */
  .row > .lat{grid-row:2;grid-column:2;font-size:11px}
  .row > .uptime-cell{grid-row:2;grid-column:3}
  .col-ip,.col-ping{display:none}
  .compact .row{grid-template-columns:20px 1fr 76px}

  /* Events: bar / host / duration / badge */
  .event{grid-template-columns:6px 1fr auto auto;gap:8px;padding:11px 12px}
  .event-time{display:none}
```

- [x] **Step 2: Stack the hosts + topo toolbars**

Add inside the same ≤768px block:

```css
  .hosts-toolbar{flex-direction:column;align-items:stretch}
  .hosts-filter-input{max-width:none;width:100%}
  .hosts-toolbar > div{display:flex;flex-wrap:wrap;gap:6px}
  .toolbar-right{justify-content:flex-end}

  .topo-web-controls{flex-wrap:wrap;row-gap:8px}
  .topo-web-overlay{padding:6px 10px}
  .topo-overlay-num{font-size:14px}
  .topo-overlay-lbl{font-size:8px}
  .topo-web-overlay-tl{top:10px;left:10px}
  .topo-web-overlay-tr{top:10px;right:10px;max-width:46%}
```

- [x] **Step 3: Inventory toolbar + chips + robust cards**

Still inside ≤768px — replace the existing `.inv-row{...}` card block (and its `td:nth-child` rules) entirely with:

```css
  .inv-toolbar-actions{flex-wrap:wrap}
  .inv-toolbar-actions .btn{flex:1 1 auto}
  .inv-export-split{flex:1 1 auto;display:flex}
  .inv-export-split .inv-export-main{flex:1}
  .inv-chip{max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

  /* Cards built on first/last cells so 6- and 7-column tables both work */
  .inv-row{background:var(--surface);border:1px solid var(--border-light);border-radius:10px;
           padding:12px 14px;display:flex;flex-wrap:wrap;gap:4px 10px;align-items:baseline}
  .inv-row td{padding:0;border:none;display:inline-block;font-size:11px;color:var(--muted)}
  .inv-row td:first-child{flex:1 1 100%;font-size:13px;color:var(--text);order:-2}
  .inv-row td:last-child{order:-1;margin-left:auto;flex:0 0 auto;align-self:center}
  .inv-row td:not(:first-child):not(:last-child){margin-right:8px}
```

- [x] **Step 4: ≤480px refinements**

Append after the 768px block:

```css
@media (max-width: 480px){
  #clock{display:none}
  .tabs{width:100%}
  .scard{padding:12px 14px}
  .scard-val{font-size:22px}
}
```

- [x] **Step 5: Verify + commit**

Run: `/tmp/nw-uplift/shotgen.sh matrix t9`. Check `m-dark-hosts.png` (no header garbage, clean 2-line rows, no chip clipping, single-row nav), `m-dark-topo.png` (toolbar wraps, overlays inside canvas), `m-light-inventory.png` (export button visible, chips ellipsized, status pill on cards).

```bash
git add static/main.css
git commit -m "fix: rebuild mobile layout on real selectors (nav, hosts, events, toolbars, inventory cards)"
```

---

### Task 10: Hosts & drawer view fixes

**Files:**
- Modify: `static/utils.js`, `static/core.js`, `static/main.css`

- [x] **Step 1: Icon size + light-mode lift**

In `utils.js deviceIcon()` add a class and bump default size handling:

```js
function deviceIcon(type, size){
  return '<svg class="dev-icon" width="' + size + '" height="' + size + '" viewBox="0 0 32 32"'
    + ' style="vertical-align:middle;flex-shrink:0" aria-hidden="true">'
    + '<use href="#topo-icon-' + (type || 'host') + '"/></svg>';
}
```

Change every 18px call site to 22: `grep -rn "deviceIcon(" static/*.js` and update each `, 18)` → `, 22)` (expected: `core.js renderHost`, `inventory.js` type-heading + drawer/table call sites; drawer 32px stays). Add CSS:

```css
[data-theme="light"] .dev-icon{filter:brightness(1.30) saturate(1.12) contrast(1.05)}
```

(Tune the brightness value 1.2-1.4 by eyeballing the Task-10 screenshot; pick what makes chassis edges readable on white.)

- [x] **Step 2: IP once per breakpoint**

main.css, outside media queries: add `@media (min-width: 769px){ .host-ip-sub{display:none} }` — desktop uses the IP column, phones keep the subtitle (mobile already hides `.col-ip`).

- [x] **Step 3: Hosts empty state**

In `core.js renderGroups(data)`, before building HTML add:

```js
  if(!data.hosts.length){
    document.getElementById('groups').innerHTML =
      '<div class="events-empty"><div class="events-empty-icon" style="background:var(--subtle);color:var(--hint)">⊘</div>'
      + '<div class="events-empty-title">No hosts match</div>'
      + '<div class="events-empty-sub">Try clearing the filter or status chips.</div></div>';
    return;
  }
```

- [x] **Step 4: Latency thresholds + arrow**

utils.js `fmtLatency`: `ms < 20` → `ms < 50`, `ms < 100` → `ms < 150`. core.js wake button: `<span class="arrow">-></span>` → `<span class="arrow">→</span>`. core.js `renderTopologyNode`: remove ` title="Click for details"`.

- [x] **Step 5: Drawer STATUS stat (both render paths)**

In `renderDrawer` replace the first stat cell:

```js
  const statusColor = h.status === 'WAIT' || h.status === 'DEGRADED' ? 'var(--amber-text)'
    : h.is_up ? 'var(--green-text)' : (isIdle ? 'var(--hint)' : 'var(--red-text)');
  let statsHtml = '<div class="d-statgrid">'
    + '<div class="d-stat"><div class="d-stat-label">STATUS</div><div class="d-stat-val" style="color:' + statusColor + '">' + h.status + '</div></div>'
    + '<div class="d-stat"><div class="d-stat-label">LATENCY</div><div class="d-stat-val blue">' + (h.latency_ms !== null ? h.latency_ms.toFixed(1) + ' <sup>ms</sup>' : '-') + '</div></div>'
```

(uptime + last-seen cells unchanged). Mirror in `updateDrawerStats`:

```js
    stats[0].className = 'd-stat-val';
    stats[0].style.color = statusColor;   // compute statusColor identically above
    stats[0].textContent = h.status;
    stats[1].innerHTML = (h.latency_ms !== null ? h.latency_ms.toFixed(1) + ' <sup>ms</sup>' : '-');
```

(declare the same `statusColor` const at the top of `updateDrawerStats`; remove the old `labelLat` usage there).

- [x] **Step 6: Verify + commit**

Run: `/tmp/nw-uplift/shotgen.sh matrix t10` — `light-hosts.png`: icons readable at 22px, single IP per row, latency colors calmer.

```bash
git add static/utils.js static/core.js static/main.css
git commit -m "feat: hosts/drawer polish — 22px icons with light lift, IP dedup, empty state, calmer latency thresholds, STATUS stat"
```

---

### Task 11: Events day grouping

**Files:**
- Modify: `static/core.js` (`renderEvents`), `static/main.css`

- [ ] **Step 1: Group by day in renderEvents**

Replace the `list.innerHTML = events.map(e => {...}).join('');` statement with:

```js
  const dayLabel = ts => {
    const d = new Date(ts * 1000), now = new Date();
    const sameDay = (a,b) => a.toDateString() === b.toDateString();
    if(sameDay(d, now)) return 'Today';
    const y = new Date(now); y.setDate(now.getDate() - 1);
    if(sameDay(d, y)) return 'Yesterday';
    return d.toLocaleDateString(undefined, {month:'short', day:'numeric'});
  };
  let lastDay = null, html = '';
  events.forEach(e => {
    const day = e.started_ts ? dayLabel(e.started_ts) : '';
    if(day && day !== lastDay){
      html += '<div class="events-day-hdr">' + day + '</div>';
      lastDay = day;
    }
    const cls = e.ongoing ? 'ongoing' : 'resolved';
    const badgeCls = e.ongoing ? 'badge-dn' : 'badge-up';
    const badgeTxt = e.ongoing ? 'ONGOING' : 'RESOLVED';
    const dur = durationStr(e.duration_seconds || 0);
    html += '<div class="event ' + cls + '" data-ip="' + escapeHtml(e.host_ip) + '" onclick="openDrawer(this.dataset.ip)">'
      + '<div class="event-bar"></div>'
      + '<div class="event-host">' + escapeHtml(e.host_name) + ' <span class="ip">' + escapeHtml(e.host_ip) + '</span></div>'
      + '<div class="event-time">' + escapeHtml(e.started_str) + '</div>'
      + '<div class="event-dur">' + dur + '</div>'
      + '<div class="event-status"><span class="badge ' + badgeCls + '">' + badgeTxt + '</span></div>'
      + '</div>';
  });
  list.innerHTML = html;
```

- [ ] **Step 2: Header style**

```css
.events-day-hdr{font-family:'DM Mono',monospace;font-size:10px;letter-spacing:.09em;text-transform:uppercase;color:var(--hint);padding:12px 18px 6px;border-bottom:1px solid var(--border-light);background:var(--subtle)}
```

- [ ] **Step 3: Verify + commit**

Run: `/tmp/nw-uplift/shotgen.sh matrix t11` — `dark-events.png` shows "TODAY" (and date headers if older events exist in the sandbox DB copy).

```bash
git add static/core.js static/main.css
git commit -m "feat: group events under Today/Yesterday/date headers"
```

---

### Task 12: Inventory chips — labels, buttons, top-8 categories

**Files:**
- Modify: `static/inventory.js` (`renderInventoryChips`, `renderInventoryMetrics`, labels), `static/main.css`

- [ ] **Step 1: Singular labels**

Add next to `INV_TYPE_LABELS`:

```js
const INV_TYPE_SINGULAR = {
  host: 'host', vm: 'vm', network: 'network', ups: 'ups',
  disk: 'disk', peripheral: 'other', tablet: 'tablet', phone: 'phone', printer: 'printer'
};
```

In `renderInventoryMetrics` change the breakdown map line to:

```js
  const breakdownParts = INV_TYPE_ORDER
    .filter(t => typeCounts[t])
    .map(t => typeCounts[t] + ' ' + (typeCounts[t] === 1
      ? (INV_TYPE_SINGULAR[t] || t)
      : (breakdownLabels[t] || t).toLowerCase()));
```

- [ ] **Step 2: Rewrite renderInventoryChips**

Replace the whole function with (key changes: `<button type="button">` elements, row labels, top-8 categories + expander, state var):

```js
let _invCatsExpanded = false;
const INV_CAT_LIMIT = 8;

function renderInventoryChips(){
  let searchFiltered = _inventoryData;
  if(_inventoryFilter.search){
    const q = _inventoryFilter.search.toLowerCase();
    searchFiltered = _inventoryData.filter(i =>
      (i.system || '').toLowerCase().includes(q)
      || (i.role || '').toLowerCase().includes(q)
      || (i.os || '').toLowerCase().includes(q)
      || (i.cpu || '').toLowerCase().includes(q)
      || (i.gpu || '').toLowerCase().includes(q)
      || JSON.stringify(i.properties || {}).toLowerCase().includes(q));
  }
  const chipBtn = (label, count, active, onclick) =>
    '<button type="button" class="inv-chip' + (active ? ' active' : '') + '" onclick="' + onclick + '">'
    + escapeHtml(label) + (count !== null ? '<span class="inv-chip-count">' + count + '</span>' : '') + '</button>';

  const typeCounts = {};
  searchFiltered.forEach(i => { const t = i.device_type || 'host'; typeCounts[t] = (typeCounts[t] || 0) + 1; });
  const typeChips = [chipBtn('All', searchFiltered.length, _inventoryFilter.deviceType === null, 'setInvTypeFilter(null)')];
  INV_TYPE_ORDER.forEach(t => {
    if(!typeCounts[t]) return;
    const safeArg = JSON.stringify(t).replace(/"/g, '&quot;');
    typeChips.push(chipBtn(INV_TYPE_LABELS[t] || t, typeCounts[t], _inventoryFilter.deviceType === t, 'setInvTypeFilter(' + safeArg + ')'));
  });

  let scope = searchFiltered;
  if(_inventoryFilter.deviceType !== null) scope = scope.filter(i => (i.device_type || 'host') === _inventoryFilter.deviceType);
  const cats = {};
  scope.forEach(i => { const c = i.category || '(uncategorized)'; cats[c] = (cats[c] || 0) + 1; });
  const sortedCats = Object.entries(cats).sort((a,b) => b[1] - a[1]);
  const visibleCats = _invCatsExpanded ? sortedCats : sortedCats.slice(0, INV_CAT_LIMIT);
  const catChips = [chipBtn('All', scope.length, _inventoryFilter.category === null, 'setInvCategoryFilter(null)')];
  visibleCats.forEach(([cat, count]) => {
    const safeArg = JSON.stringify(cat).replace(/"/g, '&quot;');
    catChips.push(chipBtn(cat, count, _inventoryFilter.category === cat, 'setInvCategoryFilter(' + safeArg + ')'));
  });
  // Keep an active-but-hidden category visible so its chip never vanishes
  if(!_invCatsExpanded && _inventoryFilter.category !== null
     && !visibleCats.some(([c]) => c === _inventoryFilter.category) && cats[_inventoryFilter.category]){
    const safeArg = JSON.stringify(_inventoryFilter.category).replace(/"/g, '&quot;');
    catChips.push(chipBtn(_inventoryFilter.category, cats[_inventoryFilter.category], true, 'setInvCategoryFilter(' + safeArg + ')'));
  }
  if(sortedCats.length > INV_CAT_LIMIT){
    catChips.push('<button type="button" class="inv-chip inv-chip-more" onclick="toggleInvCats()">'
      + (_invCatsExpanded ? 'Show less' : '+' + (sortedCats.length - INV_CAT_LIMIT) + ' more') + '</button>');
  }

  const STATUS_OPTS = [
    {val: null, label: 'All'}, {val: 'up', label: 'Up'},
    {val: 'down', label: 'Down'}, {val: 'unlinked', label: 'Unlinked'},
  ];
  const statusChips = STATUS_OPTS.map(o =>
    chipBtn(o.label, null, _inventoryFilter.status === o.val,
      'setInvStatusFilter(' + (o.val === null ? 'null' : JSON.stringify(o.val)) + ')'));

  const rowFn = (label, chips) =>
    '<div class="inv-chip-row"><span class="inv-chip-row-label">' + label + '</span>' + chips.join('') + '</div>';
  document.getElementById('inv-chips').innerHTML =
    rowFn('Status', statusChips)
    + rowFn('Type', typeChips)
    + (catChips.length > 1 ? rowFn('Category', catChips) : '');
}

function toggleInvCats(){
  _invCatsExpanded = !_invCatsExpanded;
  renderInventoryChips();
}
```

(The old `inv-chip-row-types`/`inv-chip-row-cats` class styling keys off classes we no longer emit — delete those two rules from main.css.)

- [ ] **Step 3: Chip CSS**

```css
.inv-chip-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.inv-chip-row-label{font-family:'DM Mono',monospace;font-size:9px;letter-spacing:.09em;text-transform:uppercase;color:var(--hint);min-width:62px}
.inv-chip{max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:'DM Sans',sans-serif}
.inv-chip-more{border-style:dashed;color:var(--hint)}
```

- [ ] **Step 4: Verify + commit**

Run: `/tmp/nw-uplift/shotgen.sh matrix t12` — `dark-inventory.png`: three labeled rows, category row capped with "+N more". Breakdown line reads "1 printer".

```bash
git add static/inventory.js static/main.css
git commit -m "feat: inventory chips — row labels, button semantics, top-8 categories with expander"
```

---

### Task 13: Toasts, Escape-everywhere, z-order, form fields, footer

**Files:**
- Modify: `static/utils.js`, `static/core.js`, `static/auth.js`, `static/main.css`, `dashboard.html`

- [ ] **Step 1: Toast component (utils.js, append)**

```js
function toast(msg, kind){
  let wrap = document.getElementById('nw-toasts');
  if(!wrap){
    wrap = document.createElement('div');
    wrap.id = 'nw-toasts';
    wrap.setAttribute('aria-live', 'polite');
    document.body.appendChild(wrap);
  }
  const t = document.createElement('div');
  t.className = 'nw-toast ' + (kind || 'info');
  t.textContent = msg;
  t.onclick = () => t.remove();
  wrap.appendChild(t);
  setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 250); }, 4000);
}
```

CSS:

```css
#nw-toasts{position:fixed;top:60px;right:16px;z-index:9500;display:flex;flex-direction:column;gap:8px;max-width:340px}
.nw-toast{background:var(--surface);border:1px solid var(--border);border-left:3px solid var(--blue);border-radius:8px;padding:10px 14px;font-size:13px;color:var(--text);box-shadow:var(--shadow-2);cursor:pointer;animation:toast-in .2s ease-out}
.nw-toast.error{border-left-color:var(--red)}
.nw-toast.success{border-left-color:var(--green)}
.nw-toast.out{opacity:0;transform:translateX(8px);transition:all .25s}
@keyframes toast-in{from{opacity:0;transform:translateX(12px)}to{opacity:1;transform:none}}
```

- [ ] **Step 2: Replace every native alert**

`grep -n "alert(" static/*.js` and replace each: core.js `detectMac` two `alert(...)` → `toast(..., 'error')` (and the "Set the IP first" one → `toast('Set the IP first, then try Detect.', 'info')`); auth.js `openEditor` catch → `toast('Could not load host list.', 'error')`; auth.js `downloadBackup` two alerts → `toast(msg, 'error')`. Verify `grep -c "alert(" static/*.js` → 0.

- [ ] **Step 3: Escape chain + AI z-order**

Replace the core.js Escape handler with:

```js
document.addEventListener('keydown', e => {
  if(e.key !== 'Escape') return;
  const open = id => { const el = document.getElementById(id); return el && el.classList.contains('open'); };
  const aiUsage = document.getElementById('ai-usage-modal');
  const aiPanel = document.getElementById('ai-panel');
  if(aiUsage && !aiUsage.classList.contains('hidden')) aiUsage.classList.add('hidden');
  else if(aiPanel && !aiPanel.classList.contains('hidden')) aiPanel.classList.add('hidden');
  else if(open('discover-overlay')) closeDiscover();
  else if(open('import-overlay')) closeImportModal();
  else if(open('inv-edit-overlay')) closeInventoryEditor();
  else if(open('add-host-overlay')) closeAddHostModal();
  else if(open('modal-overlay')) closeEditor();
  else if(openDrawerIp) closeDrawer();
});
```

CSS: `#ai-bubble-btn{... z-index:8000}` → `z-index:36` and `#ai-panel{... z-index:8000}` → `z-index:37` (below drawer 41 and modal 50, above FAB 35).

- [ ] **Step 4: Shared form-field classes**

Add CSS:

```css
.form-field{display:flex;flex-direction:column;gap:5px;font-size:12px;color:var(--muted)}
.form-field.mono-label{font-size:11px;font-family:'DM Mono',monospace;letter-spacing:.05em;color:var(--hint);text-transform:uppercase}
.form-field input,.form-field textarea{padding:8px 10px;border:1px solid var(--border);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:13px;background:var(--surface);color:var(--text);text-transform:none;letter-spacing:0}
.form-field textarea{resize:vertical;min-height:56px;width:100%;box-sizing:border-box}
.form-field input:focus,.form-field textarea:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px var(--blue-bg)}
[data-theme="dark"] .form-field input,[data-theme="dark"] .form-field textarea{background:rgba(255,255,255,.05);border-color:rgba(255,255,255,.10)}
.form-error{font-size:12px;color:var(--red-text);min-height:16px}
.form-stack{display:flex;flex-direction:column;gap:12px}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
@media (max-width:600px){.form-grid{grid-template-columns:1fr}}
```

In dashboard.html rewrite, removing ALL inline `style="..."` from these fields:
- Login modal: `<label class="form-field">Username<input type="text" id="login-username" autocomplete="username"></label>` (same pattern for password; error div → `<div id="login-error" class="form-error"></div>`; the wrapping `<div style="display:flex;flex-direction:column;gap:12px">` → `<div class="form-stack">` with CSS `.form-stack{display:flex;flex-direction:column;gap:12px}`).
- Setup modal: same treatment for its three fields + error div.
- Add-host modal: each `<label style="...">` → `<label class="form-field mono-label">`; inputs/textarea lose inline styles; the two grid wrapper divs keep their inline grid styles or move to `.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}` — use the class. Error div → `class="form-error"`.

- [ ] **Step 5: Footer version + refresh cadence**

dashboard.html footer → `<span>netwatch v{{VERSION}} · raspberry pi</span><span id="footer-refresh">refreshes every 5 s</span>`. In core.js DOMContentLoaded: `document.getElementById('footer-refresh').textContent = 'refreshes every ' + (REFRESH/1000) + ' s';`

- [ ] **Step 6: Verify + commit**

Run: `/tmp/nw-uplift/shotgen.sh matrix t13` — login/setup/add-host modals can't be screenshotted directly; verify footer shows `v3.38` in any shot, and `grep -c 'style="' dashboard.html` dropped substantially (record before/after counts).

```bash
git add static/ dashboard.html
git commit -m "feat: toast system, full Escape chain, AI z-order, shared form fields, templated footer"
```

---

### Task 14: Keyboard access + ARIA

**Files:**
- Modify: `static/utils.js`, `static/core.js`, `static/main.css`, `dashboard.html`

- [ ] **Step 1: Delegated Enter/Space activation (utils.js, append)**

```js
// Keyboard activation for clickable non-button rows/cards/pills.
document.addEventListener('keydown', e => {
  if(e.key !== 'Enter' && e.key !== ' ') return;
  const el = e.target.closest('.row[data-ip], .node[data-ip], .event[data-ip], .problem-pill[data-ip], .inv-row[data-inv-id]');
  if(!el || el === document.body) return;
  e.preventDefault();
  el.click();
});
```

- [ ] **Step 2: Make the rows focusable**

Add `tabindex="0" role="button"` into the opening tags generated in: core.js `renderHost` (`<div class="row...` — NOT the `.row.hdr`), `renderTopologyNode` (`<div class="node...`), `renderEvents` event div, `renderTopology` problem-pill div; inventory.js `renderTypeTable` row `<tr class="inv-row" ...` gets `tabindex="0"` (tr can't be role=button; use `role="link"`... keep `role="button"` off and rely on tabindex + delegated handler; screen-reader depth is out of scope). Confirm inv rows carry `data-inv-id` (check `grep -n 'inv-row' static/inventory.js`; if the attribute is `data-id`, use that name in the Step-1 selector instead).

- [ ] **Step 3: Tab + toggle ARIA**

core.js `setTab`: inside the tabs forEach add `t.setAttribute('aria-selected', t.dataset.tab === tab ? 'true' : 'false');`. (aria-pressed on theme buttons was done in Task 4.) dashboard.html: hosts filter input gets `aria-label="Filter hosts"`; `.tabs` div gets `aria-label="Views"`.

- [ ] **Step 4: Focus ring + focus management**

CSS:

```css
:focus-visible{outline:2px solid var(--blue);outline-offset:2px;border-radius:4px}
.row:focus-visible,.node:focus-visible,.event:focus-visible,.inv-row:focus-visible{outline-offset:-2px}
```

core.js: add module var `let _drawerOpener = null;` — in `openDrawer` before opening: `_drawerOpener = document.activeElement;` and after `.add('open')`: `document.querySelector('.drawer-close').focus();`. In `closeDrawer` append: `if(_drawerOpener && _drawerOpener.focus){ _drawerOpener.focus(); } _drawerOpener = null;`

Focus trap helper (utils.js):

```js
function trapFocus(container, e){
  const focusables = container.querySelectorAll('button,[href],input,select,textarea,[tabindex]:not([tabindex="-1"])');
  if(!focusables.length) return;
  const first = focusables[0], last = focusables[focusables.length - 1];
  if(e.shiftKey && document.activeElement === first){ e.preventDefault(); last.focus(); }
  else if(!e.shiftKey && document.activeElement === last){ e.preventDefault(); first.focus(); }
}
document.addEventListener('keydown', e => {
  if(e.key !== 'Tab') return;
  const drawer = document.getElementById('drawer');
  if(drawer && drawer.classList.contains('open')) return trapFocus(drawer, e);
  const overlay = document.querySelector('.modal-overlay.open .modal');
  if(overlay) return trapFocus(overlay, e);
});
```

- [ ] **Step 5: Verify + commit**

No screenshot value — verify by code inspection + console check: `/tmp/nw-uplift/shotgen.sh matrix t14` and confirm no JS errors in `/tmp/nw-uplift/sandbox/out.log`, all tabs render.

```bash
git add static/ dashboard.html
git commit -m "feat: keyboard access — focusable rows, Enter/Space activation, ARIA states, focus ring + trap"
```

---

### Task 15: Identity assets — favicon, theme-color, PWA manifest

**Files:**
- Create: `static/favicon.svg`, `static/favicon-alert.svg`, `static/manifest.json`, `static/icon-192.png`, `static/icon-512.png`, `static/apple-touch-icon.png`
- Modify: `dashboard.html` (head), `monitor.py` (`_STATIC_FILES`), `static/core.js` (`refresh`), `tests/test_netwatch.py`

- [ ] **Step 1: Extend the whitelist test (failing first)**

Append to the Task-2 test's `expected` dict:

```python
        'favicon.svg': 'image/svg+xml', 'favicon-alert.svg': 'image/svg+xml',
        'manifest.json': 'application/manifest+json',
        'icon-192.png': 'image/png', 'icon-512.png': 'image/png',
        'apple-touch-icon.png': 'image/png',
```

Run `python3 -m pytest tests/test_netwatch.py -k whitelist -v` → FAIL.

- [ ] **Step 2: Create the SVGs**

`static/favicon.svg`:

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="14" fill="#0f0e0d"/>
  <polyline points="8,32 24,32 30,18 38,46 44,32 56,32" fill="none" stroke="#22c55e" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
```

`static/favicon-alert.svg`: same plus `<circle cx="50" cy="14" r="9" fill="#ef4444" stroke="#0f0e0d" stroke-width="3"/>` before `</svg>`.

- [ ] **Step 3: Generate PNGs with PIL**

```bash
python3 - <<'EOF'
from PIL import Image, ImageDraw
def icon(size):
    s = size / 64
    img = Image.new('RGBA', (size, size), (0,0,0,0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0,0,size-1,size-1], radius=int(14*s), fill=(15,14,13,255))
    pts = [(8,32),(24,32),(30,18),(38,46),(44,32),(56,32)]
    d.line([(x*s,y*s) for x,y in pts], fill=(34,197,94,255), width=max(2,int(5*s)), joint='curve')
    return img
icon(512).save('static/icon-512.png')
icon(192).save('static/icon-192.png')
icon(180).save('static/apple-touch-icon.png')
print('icons written')
EOF
```

- [ ] **Step 4: manifest.json**

```json
{
  "name": "Netwatch",
  "short_name": "Netwatch",
  "description": "Homelab network monitor",
  "start_url": "/",
  "display": "standalone",
  "background_color": "#0f0e0d",
  "theme_color": "#0f0e0d",
  "icons": [
    {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
    {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"}
  ]
}
```

- [ ] **Step 5: Wire up head, whitelist, dynamic swaps**

monitor.py `_STATIC_FILES`: add the six entries matching the test. dashboard.html head, after the fonts link:

```html
<link rel="icon" id="favicon-link" type="image/svg+xml" href="/static/favicon.svg">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<link rel="manifest" href="/static/manifest.json">
<meta name="theme-color" id="theme-color-meta" content="#0f0e0d">
```

Inline theme script — inside `apply()` after setting the attribute add:

```js
    var m = document.getElementById('theme-color-meta');
    if(m) m.setAttribute('content', resolved === 'dark' ? '#0f0e0d' : '#f5f4f1');
```

core.js `refresh()` success path (right after `lastData = data;`):

```js
    const down = data.hosts.filter(h => !h.is_up && h.status === 'DOWN').length;
    const fav = document.getElementById('favicon-link');
    if(fav){
      const want = down > 0 ? '/static/favicon-alert.svg' : '/static/favicon.svg';
      if(!fav.href.endsWith(want)) fav.href = want;
    }
```

- [ ] **Step 6: Tests + smoke + commit**

`python3 -m pytest tests/test_netwatch.py -v` → PASS. `curl -sI http://localhost:8089/static/favicon.svg | head -3` after `shotgen.sh sync; shotgen.sh start` → `200` + `image/svg+xml` (then `shotgen.sh stop`).

```bash
git add static/ monitor.py dashboard.html tests/test_netwatch.py
git commit -m "feat: favicon with down-alert variant, theme-color, PWA manifest + icons"
```

---

### Task 16: Load stagger + hover micro-interactions

**Files:**
- Modify: `static/main.css`, `static/core.js`

- [ ] **Step 1: Stagger CSS (JS-gated so no-JS never hides content)**

```css
/* One orchestrated load reveal. body.nw-anim is added by JS before first
   data render and removed after; without JS everything is simply visible. */
body.nw-anim .scard,body.nw-anim .topo-group,body.nw-anim .table{animation:nw-rise .38s cubic-bezier(.2,.7,.3,1) backwards}
body.nw-anim .scard:nth-child(1){animation-delay:.02s}
body.nw-anim .scard:nth-child(2){animation-delay:.07s}
body.nw-anim .scard:nth-child(3){animation-delay:.12s}
body.nw-anim .scard:nth-child(4){animation-delay:.17s}
body.nw-anim .topo-group:nth-child(1),body.nw-anim .group:nth-child(1) .table{animation-delay:.10s}
body.nw-anim .topo-group:nth-child(2),body.nw-anim .group:nth-child(2) .table{animation-delay:.16s}
body.nw-anim .topo-group:nth-child(3),body.nw-anim .group:nth-child(3) .table{animation-delay:.22s}
@keyframes nw-rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.scard:hover{transform:translateY(-1px);box-shadow:var(--shadow-2)}
.topo-group{transition:box-shadow .2s,transform .2s}
.topo-group:hover{box-shadow:var(--shadow-2)}
@media (prefers-reduced-motion: reduce){
  body.nw-anim .scard,body.nw-anim .topo-group,body.nw-anim .table{animation:none}
  .scard:hover{transform:none}
}
```

- [ ] **Step 2: JS gate**

core.js — module scope: `let _firstRender = true;`. In `refresh()` success path, right before `renderSummary(data)`:

```js
    if(_firstRender){
      _firstRender = false;
      if(!window.matchMedia('(prefers-reduced-motion: reduce)').matches){
        document.body.classList.add('nw-anim');
        setTimeout(() => document.body.classList.remove('nw-anim'), 900);
      }
    }
```

- [ ] **Step 3: Verify + commit**

Run: `/tmp/nw-uplift/shotgen.sh matrix t16` — static shots must look IDENTICAL to t13-t15 output (animation completes within the 15s virtual budget; `backwards` fill + class removal guarantee final state). Spot-check `dark-topo-cards.png` for missing/transparent cards (would indicate the gate failed).

```bash
git add static/main.css static/core.js
git commit -m "feat: orchestrated load stagger + card hover lift (reduced-motion safe)"
```

---

### Task 17: Final verification sweep

**Files:** none (verification + stragglers only)

- [ ] **Step 1: Full test suite** — `python3 -m pytest tests/test_netwatch.py -v` → all PASS.
- [ ] **Step 2: Full matrix** — `/tmp/nw-uplift/shotgen.sh matrix final`; review all 12 against `/tmp/nw-uplift/shots/baseline/` (themes, tabs, mobile). Every audit bug must be visibly gone; dark mode must look unchanged-or-better.
- [ ] **Step 3: Console sweep** — `grep -iE "uncaught|referenceerror|typeerror" /tmp/nw-uplift/sandbox/out.log /tmp/nw-uplift/sandbox/m.log` → no output; plus one chromium run with `--enable-logging=stderr` grepping `CONSOLE` for errors.
- [ ] **Step 4: Reduced-motion spot check** — one shot with `--force-prefers-reduced-motion` added to the chromium flags; page renders fully (no invisible cards).
- [ ] **Step 5: Grep hygiene** — each must return nothing:
```bash
grep -rn "cdnjs\|fonts.googleapis" dashboard.html static/
grep -rn "alert(" static/
grep -n "v3.37" dashboard.html
grep -n 'data-theme="auto"' static/main.css
```
- [ ] **Step 6: Fix anything found, re-run, commit stragglers** — `git add -A && git commit -m "fix: final verification sweep fixes"` (only if changes exist).
- [ ] **Step 7: Invoke superpowers:verification-before-completion, then superpowers:finishing-a-development-branch** to decide merge of `ui-premium-uplift`.
