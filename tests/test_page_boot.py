import os

import pytest

from boot_fixtures import default_fixtures
from boot_smoke import needs_chromium, render
from page_analysis import REPO, read
from test_pages import PAGES

THEMES = ["dark", "light"]


@needs_chromium
@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("page", PAGES, ids=[p.name for p in PAGES])
def test_page_boots_without_errors(page, theme, tmp_path):
    r = render(page.html, default_fixtures(tmp_path), theme=theme)
    assert r.errors == [], f"{page.name} ({theme}) boot errors: {r.errors}"


@needs_chromium
@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("page", PAGES, ids=[p.name for p in PAGES])
@pytest.mark.parametrize("width", [320, 390])
def test_page_has_no_horizontal_overflow(page, theme, width, tmp_path):
    r = render(page.html, default_fixtures(tmp_path), width=width, height=800, theme=theme)
    assert r.overflow <= 0, f"{page.name} ({theme}) overflows by {r.overflow}px at {width}px"
