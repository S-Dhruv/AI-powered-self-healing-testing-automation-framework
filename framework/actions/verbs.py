"""Generic, app-agnostic action verbs. No app-specific code belongs here —
only the verb vocabulary the step grammar and executor already know about."""

from __future__ import annotations

import re
from dataclasses import dataclass

from framework.browser import SeleniumPage as Page

from framework.config.allowlist import AllowListGuard, AllowListViolation


class ActionError(RuntimeError):
    pass


@dataclass
class ActionResult:
    ok: bool
    detail: str


def navigate(page: Page, url: str, guard: AllowListGuard, timeout_ms: int) -> ActionResult:
    try:
        guard.check(url)
    except AllowListViolation as exc:
        return ActionResult(False, str(exc))
    page.goto(url, timeout=timeout_ms)
    return ActionResult(True, f"navigated to {url}")


def click(page: Page, selector: str, timeout_ms: int) -> ActionResult:
    page.locator(selector).click(timeout=timeout_ms)
    return ActionResult(True, f"clicked {selector}")


def hover(page: Page, selector: str, timeout_ms: int) -> ActionResult:
    page.locator(selector).hover(timeout=timeout_ms)
    return ActionResult(True, f"hovered {selector}")


def type_text(page: Page, selector: str, text: str, timeout_ms: int) -> ActionResult:
    page.locator(selector).fill(text, timeout=timeout_ms)
    return ActionResult(True, f"typed into {selector}")


def select_option(page: Page, selector: str, value: str, timeout_ms: int) -> ActionResult:
    page.locator(selector).select_option(value, timeout=timeout_ms)
    return ActionResult(True, f"selected {value!r} in {selector}")


def check(page: Page, selector: str, timeout_ms: int) -> ActionResult:
    page.locator(selector).check(timeout=timeout_ms)
    return ActionResult(True, f"checked {selector}")


def upload(page: Page, selector: str, file_path: str, timeout_ms: int) -> ActionResult:
    page.locator(selector).set_input_files(file_path, timeout=timeout_ms)
    return ActionResult(True, f"uploaded {file_path} to {selector}")


def accept_dialog(page: Page, timeout_ms: int = 5000) -> ActionResult:
    """Accept a native JS alert/confirm/prompt. A step that triggers one
    (e.g. a form submit calling alert()) leaves every subsequent WebDriver
    command raising UnexpectedAlertPresentException until it's explicitly
    accepted -- there is no locator for a native browser dialog, so this
    talks to the driver's alert API directly rather than through a Locator.
    """
    from selenium.common.exceptions import NoAlertPresentException, TimeoutException
    from selenium.webdriver.support.ui import WebDriverWait

    try:
        WebDriverWait(page.driver, timeout_ms / 1000).until(lambda d: _has_alert(d))
        alert = page.driver.switch_to.alert
        text = alert.text
        alert.accept()
        return ActionResult(True, f"accepted native dialog: {text!r}")
    except (NoAlertPresentException, TimeoutException) as exc:
        return ActionResult(False, f"no native dialog appeared within {timeout_ms}ms: {exc}")


def _has_alert(driver) -> bool:
    try:
        driver.switch_to.alert
        return True
    except Exception:
        return False


def assert_text_visible(page: Page, text: str, timeout_ms: int) -> ActionResult:
    locator = page.get_by_text(text, exact=False)
    try:
        locator.first.wait_for(state="visible", timeout=timeout_ms)
        return ActionResult(True, f"text {text!r} is visible")
    except Exception:
        pass
    # Not in the DOM at all -- but native HTML5 constraint-validation tooltips
    # (e.g. "Please fill out this field") are rendered by the browser's own
    # UI, outside the DOM entirely, so no DOM-text search can ever find them.
    # The timeout above has already elapsed, so the invalid state (set
    # synchronously on a failed form submit) is long since stable; checking
    # once more here is not an extra wait.
    messages = page.native_validation_messages()
    for message in messages:
        if text.lower() in message.lower():
            return ActionResult(True, f"native validation message {message!r} matches {text!r}")
    return ActionResult(False, f"text {text!r} was not visible within {timeout_ms}ms (checked DOM text and native validation messages)")


_URL_REFERENCE_STOPWORDS = {"the", "a", "an", "page", "to", "of", "redirected"}


def assert_url_contains(page: Page, fragment: str, timeout_ms: int) -> ActionResult:
    try:
        page.wait_for_url(f"**{fragment}**", timeout=timeout_ms)
        return ActionResult(True, f"URL contains {fragment!r}")
    except Exception:
        pass
    # The fragment may name a page in natural language rather than give a
    # literal URL fragment -- "redirected to the login page" is common
    # Expected Result prose for any app's login flow, not one site's
    # wording, and "the login page" will never appear verbatim in a URL.
    # The timeout above has already elapsed, so checking the current URL
    # once more here is not an extra wait.
    words = {w.lower() for w in re.findall(r"[A-Za-z]+", fragment) if w.lower() not in _URL_REFERENCE_STOPWORDS}
    if words:
        current_url = page.url.lower()
        if any(word in current_url for word in words):
            return ActionResult(True, f"URL {page.url!r} matches page reference {fragment!r} by keyword")
    return ActionResult(False, f"URL did not contain {fragment!r}; actual URL: {page.url}")
