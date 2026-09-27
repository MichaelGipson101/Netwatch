"""Node-based test plumbing for static/*.js pure helpers (see js_part)."""
import json
import os
import shutil
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(REPO, "static")

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def js_part(path, marker):
    """Source of the top-level `function name(...)` or `const NAME = ...` that
    starts at `marker`, found by bracket matching. Helpers tested this way
    keep brackets balanced inside string and regex literals."""
    with open(path, encoding="utf-8") as f:
        src = f.read()
    start = src.index(marker)
    opens = [i for i in (src.find("{", start), src.find("[", start)) if i != -1]
    i = min(opens)
    depth = 0
    for j in range(i, len(src)):
        if src[j] in "{[":
            depth += 1
        elif src[j] in "}]":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    if src[end:end + 1] == ";":
        end += 1
    return src[start:end]


def run_js(parts, expr, prelude=""):
    src = prelude + "\n" + "\n".join(js_part(p, m) for p, m in parts)
    script = src + f"\nprocess.stdout.write(JSON.stringify({expr}));"
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)
