"""Browser check of the web page (headless Chrome via Playwright).

Needs API_BASE (a running server). Screenshots go to $UI_SHOTS if set.
"""

import os
import shutil
from pathlib import Path

import pytest

API_BASE = os.getenv("API_BASE")
CHROME = os.getenv("CHROME", shutil.which("google-chrome") or shutil.which("chromium") or "")
pytestmark = pytest.mark.skipif(not API_BASE, reason="set API_BASE to run the browser check")
SHOTS = Path(os.getenv("UI_SHOTS", "")) if os.getenv("UI_SHOTS") else None


@pytest.fixture(scope="module", params=["light", "dark"])
def page(request):
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME or None, headless=True)
        pg = browser.new_page(viewport={"width": 1200, "height": 900}, color_scheme=request.param)
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.on("console", lambda m: m.type == "error" and pg.errors.append(m.text))
        pg.scheme = request.param
        yield pg
        browser.close()


def shot(page, name):
    if SHOTS:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / f"{name}-{page.scheme}.png"), full_page=True)


def test_terms_flow(page):
    page.errors.clear()
    page.goto(API_BASE + "/")
    page.wait_for_selector("#status:has-text('database connected')", timeout=90_000)
    links = [a.get_attribute("href") for a in page.locator("#api a").all()]
    assert sum(h.startswith("/dissociate/") for h in links) == 4 and {"/img", "/test_db", "/docs"} <= set(links)
    page.click("#examples button:has-text('DMN')")
    page.wait_for_selector("#sides .side", timeout=60_000)
    assert "897" in page.inner_text("#legend")  # A only, matches the oracle-tested count
    assert page.locator("#sides .side").count() == 2
    assert page.locator(".chip").count() > 0
    assert page.is_hidden("#form-locations")  # only the active tab's form is shown
    assert "NaN" not in page.inner_text("#sides")
    # each card's location table lists cells over-represented on that card's side
    for card in range(2):
        for row in page.locator(f"#sides .side >> nth={card} >> table.locs tr").all()[1:]:
            this, other = (int(c.inner_text().rstrip("%")) for c in row.locator("td").all()[1:3])
            assert this >= other, (card, row.inner_text())
    assert page.locator("ol.studies li").count() == 100  # 50 per side
    shot(page, "terms")
    # editing the form must not change what "Show more" appends to the displayed query
    page.fill("#form-terms [name=b]", "memory")
    page.click("#sides .side >> nth=0 >> button:has-text('Show more')")
    page.wait_for_function("document.querySelectorAll('#sides .side')[0].querySelectorAll('li').length === 100")
    assert "797 left" in page.inner_text("#sides .side >> nth=0 >> button")  # 897 - 100
    assert not page.errors, page.errors


def test_locations_flow_and_permalink(page):
    page.errors.clear()
    page.goto(API_BASE + "/")
    page.click("[data-tab=locations]")
    page.click("#examples button:has-text('DMN')")
    page.wait_for_selector("#sides .side", timeout=60_000)
    assert "406" in page.inner_text("#legend")
    url = page.url
    shot(page, "locations")
    page.goto("about:blank")
    page.goto(url)  # the hash restores the query
    page.wait_for_selector("#sides .side", timeout=60_000)
    assert "406" in page.inner_text("#legend")
    assert not page.errors, page.errors


def test_error_message(page):
    page.goto(API_BASE + "/")
    page.fill("#form-terms [name=a]", "amygdale")
    page.click("#form-terms button[type=submit]")
    page.wait_for_selector("#summary .error", timeout=60_000)
    text = page.inner_text("#summary .error")
    assert "amygdala" in text  # suggestion shown
    shot(page, "error")
