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
window.addEventListener('error', function (e) { window.__nwErrors.push(String(e.message)); });
window.addEventListener('unhandledrejection', function (e) { window.__nwErrors.push('rejection: ' + String(e.reason)); });
(function () { var ce = console.error; console.error = function () {
  window.__nwErrors.push('console.error: ' + Array.prototype.map.call(arguments, String).join(' '));
  ce.apply(console, arguments); }; })();
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
}, 2500); });
</script>"""


@dataclass
class BootResult:
    errors: list
    overflow: int
    dom: str


def render(html, fixtures, width=1280, height=900, theme="dark", url_path="", pathname="/"):
    """Boot `html` (a fully assembled page; {{VERSION}} already substituted or replaced here).
    `url_path` is a query string without the leading '?' (e.g. "host=10.0.0.2"); `pathname`
    simulates the page's URL path for sub-view selection (see shell.js _subviewFromPath)."""
    html = html.replace("{{VERSION}}", "test")
    html = html.replace('"/static/', '"file://' + STATIC + '/')
    stub = _STUB % {"theme": json.dumps(theme), "fixtures": json.dumps(fixtures),
                    "pathname": json.dumps(pathname)}
    html = html.replace("<head>", "<head>" + stub, 1)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "page.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        proc = subprocess.run(
            [CHROMIUM, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--allow-file-access-from-files",
             f"--window-size={width},{height}", "--virtual-time-budget=6000", "--dump-dom",
             f"--user-data-dir={d}/profile", "file://" + path + ("?" + url_path if url_path else "")],
            capture_output=True, text=True, timeout=60)
    dom = proc.stdout
    m = re.search(r'data-nw-errors="([^"]*)"', dom)
    errors = json.loads(m.group(1).replace("&quot;", '"').replace("&amp;", "&")) if m else ["boot script never finished"]
    o = re.search(r'data-nw-overflow="(-?\d+)"', dom)
    return BootResult(errors, int(o.group(1)) if o else 10**6, dom)
