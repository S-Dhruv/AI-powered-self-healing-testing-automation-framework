"""Generic, app-agnostic action verbs. No app-specific code belongs here —
only the verb vocabulary the step grammar and executor already know about."""

from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Page

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


def accept_dialog(page: Page) -> ActionResult:
    page.once("dialog", lambda d: d.accept())
    return ActionResult(True, "registered one-time dialog accept handler")


def assert_text_visible(page: Page, text: str, timeout_ms: int) -> ActionResult:
    locator = page.get_by_text(text, exact=False)
    try:
        locator.first.wait_for(state="visible", timeout=timeout_ms)
        return ActionResult(True, f"text {text!r} is visible")
    except Exception:
        return ActionResult(False, f"text {text!r} was not visible within {timeout_ms}ms")


def assert_url_contains(page: Page, fragment: str, timeout_ms: int) -> ActionResult:
    try:
        page.wait_for_url(f"**{fragment}**", timeout=timeout_ms)
        return ActionResult(True, f"URL contains {fragment!r}")
    except Exception:
        return ActionResult(False, f"URL did not contain {fragment!r}; actual URL: {page.url}")
