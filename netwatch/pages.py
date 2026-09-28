"""Page table, URL resolution and template assembly for the multi-page dashboard.

Templates live in <base_dir>/templates/: _base.html (the shell), one fragment per page
(content, then a line `<!--@modals-->`, then that page's modals) and partials/*.html
included with {{> name}}. Everything is assembled once at startup, so template edits
need a service restart (same as the old dashboard.html).
"""
import os
import re
from dataclasses import dataclass
from typing import Optional, Tuple

# Loaded on every page, in this order (shell.js must precede scripts that call nwStatus.subscribe).
SHELL_SCRIPTS = ("utils.js", "shell.js", "auth.js", "ups.js", "settings.js", "ai-panel.js")


@dataclass(frozen=True)
class Page:
    name: str
    title: str
    path: str
    label: str
    subviews: Tuple[Tuple[str, str], ...] = ()   # (name, label); switch client-side without reload
    panels: Tuple[str, ...] = ()                 # extra URL segments the page handles itself (Infra)
    scripts: Tuple[str, ...] = ()                # page scripts, loaded after SHELL_SCRIPTS


PAGES = {
    "home": Page("home", "Netwatch", "/", "Home",
                 scripts=("overview.js", "power.js", "quicklinks.js", "connections.js", "quickadd.js")),
    "monitor": Page("monitor", "Monitor · Netwatch", "/monitor", "Monitor",
                    subviews=(("hosts", "Hosts"), ("events", "Events"), ("briefs", "Briefs")),
                    scripts=("hosts.js", "events.js", "briefs.js", "drawer.js", "hosts-editor.js")),
    "lab": Page("lab", "Lab · Netwatch", "/lab", "Lab",
                subviews=(("topology", "Topology"), ("connections", "Connections"), ("inventory", "Inventory")),
                scripts=("topology.js", "topology-cards.js", "connections.js", "inventory.js",
                         "quickadd.js", "drawer.js")),
    "infra": Page("infra", "Infrastructure · Netwatch", "/infra", "Infra",
                  panels=("proxmox", "truenas"), scripts=("proxmox.js", "nas.js")),
    "links": Page("links", "Quick Links · Netwatch", "/links", "Links", scripts=("quicklinks.js",)),
}
NAV_ORDER = ("home", "monitor", "lab", "infra")          # Links is reachable from Home, not the nav
_BADGES = {"events": "events-count", "briefs": "briefs-count", "inventory": "inv-count"}


def resolve(path: str) -> Optional[Tuple[str, str]]:
    """Map a request path to (page name, sub-view) or None. Query strings are ignored."""
    path = path.split("?", 1)[0].split("#", 1)[0]
    if path in ("/", "/index.html"):
        return ("home", "")
    parts = path.strip("/").split("/") if path else []
    if not path.startswith("/") or "" in parts or len(parts) > 2:
        return None
    page = next((p for p in PAGES.values() if p.path.strip("/") == parts[0]), None)
    if page is None or page.name == "home":
        return None
    if len(parts) == 1:
        sub = page.subviews[0][0] if page.subviews else (page.panels[0] if page.panels else "")
        return (page.name, sub)
    allowed = {n for n, _ in page.subviews} | set(page.panels)
    return (page.name, parts[1]) if parts[1] in allowed else None


def _nav_html(active: str) -> str:
    items = []
    for name in NAV_ORDER:
        p = PAGES[name]
        on = name == active
        badge = ('<span class="tab-count" id="conn-count" style="display:none">0</span>'
                 if name == "lab" else "")
        cls = "tab active" if on else "tab"
        cur = ' aria-current="page"' if on else ""
        items.append(f'<a class="{cls}" href="{p.path}"{cur}>{p.label}{badge}</a>')
    return ('<div class="tabs" role="navigation" aria-label="Pages">' + "".join(items) + "</div>")


def _subnav_html(page: Page) -> str:
    if not page.subviews:
        return ""
    links = []
    for i, (name, label) in enumerate(page.subviews):
        badge = (f'<span class="tab-count" id="{_BADGES[name]}" style="display:none">0</span>'
                 if name in _BADGES else "")
        links.append(f'<a class="servers-pill{" active" if i == 0 else ""}" role="tab" '
                     f'href="{page.path}/{name}" data-subview="{name}">{label}{badge}</a>')
    return (f'<div class="servers-pills subnav" role="tablist" aria-label="{page.label} views">'
            + "".join(links) + "</div>\n")


def _body_attrs(page: Page) -> str:
    subs = ",".join(n for n, _ in page.subviews)
    return f' data-page="{page.name}" data-subviews="{subs}"'


def _scripts_html(page: Page) -> str:
    files = list(SHELL_SCRIPTS) + [s for s in page.scripts if s not in SHELL_SCRIPTS]
    return "\n".join(f'<script src="/static/{f}?v={{{{VERSION}}}}"></script>' for f in files)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def render_all(base_dir: str, version: str) -> dict:
    tpl = os.path.join(base_dir, "templates")
    base = _read(os.path.join(tpl, "_base.html"))
    partials = {os.path.splitext(n)[0]: _read(os.path.join(tpl, "partials", n))
                for n in os.listdir(os.path.join(tpl, "partials")) if n.endswith(".html")}
    out = {}
    for name, page in PAGES.items():
        frag = _read(os.path.join(tpl, f"{name}.html"))
        frag = re.sub(r"\{\{>\s*([\w-]+)\s*\}\}", lambda m: partials[m.group(1)], frag)
        content, _, modals = frag.partition("<!--@modals-->\n")
        html = base
        html = html.replace("{{TITLE}}", page.title)
        html = html.replace("{{BODY_ATTRS}}", _body_attrs(page))
        html = html.replace("{{NAV}}", _nav_html(name))
        html = html.replace("{{CONTENT}}", _subnav_html(page) + content)
        html = html.replace("{{MODALS}}", modals)
        html = html.replace("{{SCRIPTS}}", _scripts_html(page))
        out[name] = html.replace("{{VERSION}}", version)
    return out
