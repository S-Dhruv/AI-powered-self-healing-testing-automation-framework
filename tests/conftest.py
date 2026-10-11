from pathlib import Path

import pytest
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

from framework.browser import SeleniumPage

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def page():
    # Session-scoped: every test that uses this (always via login_page or
    # twin_forms_page below) calls page.goto() to its own fixture file first,
    # which fully replaces the document -- so sharing one browser across the
    # whole test run is safe, and avoids a real Chrome cold-start (1-3s) per
    # test, which otherwise dominates the suite's total runtime far more than
    # any actual test logic does.
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1366,900")
    driver = webdriver.Chrome(options=options)
    pg = SeleniumPage(driver)
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
