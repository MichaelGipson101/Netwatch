"""Static-analysis helpers for the page-split safety net (see tests/test_pages.py)."""
import os
import re
from dataclasses import dataclass

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(REPO, "static")

JS_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "function", "return", "typeof", "new", "await",
    "async", "else", "do", "void", "delete", "in", "of", "case", "throw", "try", "with",
}
BUILTINS = {
    "alert", "confirm", "prompt", "parseInt", "parseFloat", "isNaN", "String", "Number", "Boolean",
    "Array", "Object", "JSON", "Math", "Date", "encodeURIComponent", "decodeURIComponent",
    "setTimeout", "setInterval", "clearTimeout", "clearInterval", "fetch", "event", "stopPropagation",
    "preventDefault", "getElementById", "querySelector", "querySelectorAll", "add", "remove",
    "toggle", "contains", "focus", "blur", "click", "select", "stopImmediatePropagation",
    "closest", "getAttribute", "setAttribute", "requestAnimationFrame", "open", "print", "Set", "Map",
}

HANDLER_ATTR = re.compile(
    r"""\bon(?:click|input|change|keydown|keyup|submit|blur|focus)\s*=\s*\\?(["'])(.*?)\\?\1""", re.S)
CALL = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(")
DECL = re.compile(
    r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"
    r"|^(?:const|let|var)\s+([A-Za-z_$][\w$]*)"
    r"|^window\.([A-Za-z_$][\w$]*)\s*=", re.M)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def script_srcs(html):
    """Ordered /static/*.js filenames referenced by <script src>."""
    return re.findall(r'<script[^>]*\bsrc="/static/([^"?]+\.js)', html)


def declared_names(js_sources):
    """Top-level function/const/let/var names and window.x assignments across sources."""
    names = set()
    for src in js_sources:
        for m in DECL.finditer(src):
            names.add(next(g for g in m.groups() if g))
        for m in re.finditer(r"window\.([A-Za-z_$][\w$]*)\s*=", src):
            names.add(m.group(1))
    return names


def handler_calls(text):
    """Function names invoked from inline handler attributes in `text` (HTML or JS-built HTML)."""
    out = set()
    for m in HANDLER_ATTR.finditer(text):
        body = re.sub(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"", "''", m.group(2))
        for c in CALL.finditer(body):
            n = c.group(1)
            if n not in JS_KEYWORDS and n not in BUILTINS:
                out.add(n)
    return out


def lit_ids(js):
    return set(re.findall(r"getElementById\(\s*['\"]([\w-]+)['\"]\s*\)", js))


def html_ids(html):
    return set(re.findall(r'\bid="([\w-]+)"', html))


def js_created_ids(js):
    """ids that JS writes into markup it builds (id="x" inside string literals)."""
    return set(re.findall(r"""\bid=\\?["']([\w-]+)""", js))


def api_literals(js):
    return set(re.findall(r"""['"`](/api/[\w\-/]+)""", js))


@dataclass
class PageSource:
    name: str
    html: str
    scripts: list

    @property
    def js(self):
        return {s: read(os.path.join(STATIC, s)) for s in self.scripts}

    @property
    def inline_js(self):
        return "\n".join(re.findall(r"<script>(.*?)</script>", self.html, re.S))
