from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    pg = browser.new_page()
    yield pg
    pg.close()


@pytest.fixture
def login_page(page):
    url = (FIXTURES_DIR / "login_page.html").resolve().as_uri()
    page.goto(url)
    return page


@pytest.fixture
def twin_forms_page(page):
    url = (FIXTURES_DIR / "twin_forms_page.html").resolve().as_uri()
    page.goto(url)
    return page
