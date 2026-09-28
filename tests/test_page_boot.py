import pytest

from boot_fixtures import default_fixtures
from boot_smoke import needs_chromium, render
from test_pages import PAGES
from netwatch.pages import PAGES as PAGE_TABLE

THEMES = ["dark", "light"]

# Temporary: cross-page runtime problems (KPI summary, power card, Home topology preview,
# missing-id guards) are fixed in Task 7; remove this flag and the xfail marks in Task 7 Step 6.
# Per-test on purpose (never a file-wide pytestmark).
PENDING_TASK_7 = True


def _cases():
    """(page, sub-view) for every page; a page without sub-views yields one case with ''."""
    for p in PAGES:
        subs = [s for s, _ in PAGE_TABLE[p.name].subviews] or [""]
        for s in subs:
            yield pytest.param(p, s, id=f"{p.name}/{s}" if s else p.name)


def _pathname(page, sub):
    return PAGE_TABLE[page.name].path + ("/" + sub if sub else "")


@needs_chromium
@pytest.mark.xfail(PENDING_TASK_7, reason="Task 7", strict=False)
@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("page,sub", list(_cases()))
def test_page_boots_without_errors(page, sub, theme, tmp_path):
    r = render(page.html, default_fixtures(tmp_path), theme=theme, pathname=_pathname(page, sub))
    assert r.errors == [], f"{page.name}/{sub} ({theme}) boot errors: {r.errors}"


@needs_chromium
@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("page,sub", list(_cases()))
@pytest.mark.parametrize("width", [320, 390])
def test_page_has_no_horizontal_overflow(page, sub, theme, width, tmp_path):
    r = render(page.html, default_fixtures(tmp_path), width=width, height=800, theme=theme,
               pathname=_pathname(page, sub))
    assert r.inner_width == width, (
        f"{page.name}/{sub} ({theme}): viewport was {r.inner_width}px, not {width}px; "
        "the overflow check would be vacuous")
    assert r.overflow <= 0, f"{page.name}/{sub} ({theme}) overflows by {r.overflow}px at {width}px"
