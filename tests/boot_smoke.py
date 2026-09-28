"""Render an assembled dashboard page in headless Chromium from file:// with stubbed fetch."""
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

import pytest

from page_analysis import STATIC

CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")
needs_chromium = pytest.mark.skipif(CHROMIUM is None, reason="chromium not available")

_STUB = """<script>
try { localStorage.setItem('nw-theme', %(theme)s); } catch (e) {}
window.__nwPath = %(pathname)s;
window.__nwErrors = [];
// a blocking alert() would hang headless Chromium; record it as an error instead
window.alert = function (m) { window.__nwErrors.push('alert: ' + String(m)); };
window.addEventListener('error', function (e) {
  var t = e.target;
  if (t && t !== window && (t.src || t.href)) { window.__nwErrors.push('resource failed: ' + (t.src || t.href)); }
  else { window.__nwErrors.push(String(e.message)); }
}, true);
window.addEventListener('unhandledrejection', function (e) { window.__nwErrors.push('rejection: ' + String(e.reason)); });
(function () { var ce = console.error; console.error = function () {
  window.__nwErrors.push('console.error: ' + Array.prototype.map.call(arguments, String).join(' '));
  ce.apply(console, arguments); }; })();
// JS-created scripts (e.g. topology.js lazy-loads /static/d3.v7.min.js) bypass the server-side
// /static/ -> file:// rewrite done on the HTML, so redirect them here.
(function () {
  var d = Object.getOwnPropertyDescriptor(HTMLScriptElement.prototype, 'src');
  Object.defineProperty(HTMLScriptElement.prototype, 'src', {
    get: d.get,
    set: function (v) { d.set.call(this, String(v).indexOf('/static/') === 0 ? %(static)s + v.slice(8) : v); }
  });
})();
var __fx = %(fixtures)s;
window.fetch = function (url) {
  var path = String(url).split('?')[0];
  var body = __fx[path];
  if (body === undefined) {
    var k = Object.keys(__fx).find(function (p) { return p.slice(-1) === '/' && path.indexOf(p) === 0; });
    body = k ? __fx[k] : {};
  }
  var status = (path === '/api/status' && __fx.__status401) ? 401 : 200;
  return Promise.resolve(new Response(JSON.stringify(body), {status: status, headers: {'Content-Type': 'application/json'}}));
};
window.addEventListener('load', function () { setTimeout(function () {
  var d = document.documentElement;
  d.setAttribute('data-nw-errors', JSON.stringify(window.__nwErrors));
  d.setAttribute('data-nw-overflow', String(d.scrollWidth - d.clientWidth));
  d.setAttribute('data-nw-inner-width', String(window.innerWidth));
}, 2500); });
</script>"""


# Headless Chromium clamps --window-size to a ~500px minimum, so narrower viewports are
# rendered inside an <iframe> of the target width (the iframe's own innerWidth is honoured).
_MIN_WINDOW = 500

_WRAPPER = """<html><body style="margin:0">
<iframe id="f" src="%(src)s" style="width:%(width)dpx;height:%(height)dpx;border:0"></iframe>
<script>
var f = document.getElementById('f');
f.addEventListener('load', function () { setTimeout(function () {
  var d = document.documentElement;
  try {
    var id = f.contentDocument.documentElement;
    var errs = id.getAttribute('data-nw-errors'), ov = id.getAttribute('data-nw-overflow');
    if (errs !== null) d.setAttribute('data-nw-errors', errs);
    if (ov !== null) d.setAttribute('data-nw-overflow', ov);
    d.setAttribute('data-nw-inner-width', String(f.contentWindow.innerWidth));
  } catch (e) {
    d.setAttribute('data-nw-errors', JSON.stringify(['wrapper: ' + e]));
  }
}, 3200); });
</script></body></html>"""


@dataclass
class BootResult:
    errors: list
    overflow: int
    dom: str
    inner_width: int = 0


def render(html, fixtures, width=1280, height=900, theme="dark", url_path="", pathname="/"):
    """Boot `html` (a fully assembled page; {{VERSION}} already substituted or replaced here).
    `url_path` is a query string without the leading '?' (e.g. "host=10.0.0.2"); `pathname`
    simulates the page's URL path for sub-view selection (see shell.js _subviewFromPath)."""
    html = html.replace("{{VERSION}}", "test")
    html = html.replace('"/static/', '"file://' + STATIC + '/')
    stub = _STUB % {"theme": json.dumps(theme), "fixtures": json.dumps(fixtures),
                    "pathname": json.dumps(pathname),
                    "static": json.dumps("file://" + STATIC + "/")}
    html = html.replace("<head>", "<head>" + stub, 1)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "page.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        target = "file://" + path + ("?" + url_path if url_path else "")
        win_w = width
        if width < _MIN_WINDOW:
            wrapper = os.path.join(d, "wrapper.html")
            with open(wrapper, "w", encoding="utf-8") as f:
                f.write(_WRAPPER % {"src": target, "width": width, "height": height})
            target = "file://" + wrapper
            win_w = _MIN_WINDOW
        proc = subprocess.run(
            [CHROMIUM, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
             "--allow-file-access-from-files",
             f"--window-size={win_w},{height}", "--virtual-time-budget=9000", "--dump-dom",
             f"--user-data-dir={d}/profile", target],
            capture_output=True, text=True, timeout=60)
    dom = proc.stdout
    m = re.search(r'data-nw-errors="([^"]*)"', dom)
    if m:
        errors = json.loads(m.group(1).replace("&quot;", '"').replace("&amp;", "&"))
    else:
        errors = ["boot script never finished: " + proc.stderr[-500:]]
    o = re.search(r'data-nw-overflow="(-?\d+)"', dom)
    w = re.search(r'data-nw-inner-width="(\d+)"', dom)
    return BootResult(errors, int(o.group(1)) if o else 10**6, dom, int(w.group(1)) if w else 0)
