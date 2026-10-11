"""Small Selenium browser facade used by the framework runtime.

The facade keeps browser operations explicit while presenting the resolver and
action layers with the operations they need: CSS/XPath lookup, visibility,
attributes, interaction, and JavaScript execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    TimeoutException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import Select, WebDriverWait


def _selector_to_by(selector: str) -> tuple[str, str]:
    selector = selector.strip()
    if selector.startswith("xpath="):
        return By.XPATH, selector[6:]
    if selector.startswith("text="):
        text = selector[5:].strip("'\"")
        return By.XPATH, f"//*[contains(normalize-space(.), {xpath_literal(text)})]"
    if selector.startswith("role="):
        role, _, name = selector[5:].partition("[name=")
        role = role.strip()
        if name:
            name = name.rstrip("]").strip("'\"")
            return By.XPATH, (
                f"//*[@role={xpath_literal(role)} and "
                f"contains(normalize-space(.), {xpath_literal(name)})]"
            )
        return By.XPATH, f"//*[@role={xpath_literal(role)}]"
    if ":has-text(" in selector or ":text(" in selector:
        marker = ":has-text(" if ":has-text(" in selector else ":text("
        tag, _, suffix = selector.partition(marker)
        text = suffix.rstrip(")").strip("'\"")
        return By.XPATH, f"//{tag or '*'}[contains(normalize-space(.), {xpath_literal(text)})]"
    if " >> " in selector:
        selector = selector.split(" >> ", 1)[0]
    if selector.startswith("label:has-text("):
        text = selector.split("(", 1)[1].rstrip(")").strip("'\"")
        return By.XPATH, f"//label[contains(normalize-space(.), {xpath_literal(text)})]"
    return By.CSS_SELECTOR, selector


def xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    return "concat(" + ", \"'\", ".join(f"'{part}'" for part in parts) + ")"


@dataclass
class SeleniumLocator:
    page: "SeleniumPage"
    selector: str

    def _elements(self) -> list[WebElement]:
        by, value = _selector_to_by(self.selector)
        return self.page.driver.find_elements(by, value)

    def _element(self) -> WebElement:
        elements = self._elements()
        if not elements:
            raise NoSuchElementException(self.selector)
        # Prefer a visible match over elements[0]: hover-reveal UI (a common,
        # legitimate pattern -- e.g. a product listing with one "Add to cart"
        # button duplicated for a hidden vs. revealed-on-hover state, both
        # sharing identical attributes) can put a hidden twin earlier in DOM
        # order than the one actually on screen. Interacting with whichever
        # one happens to be visible is correct; interacting with elements[0]
        # regardless would silently target the wrong twin.
        for element in elements:
            if element.is_displayed():
                return element
        return elements[0]

    def count(self) -> int:
        return len(self._elements())

    def visible_count(self) -> int:
        return sum(1 for el in self._elements() if el.is_displayed())

    def visible_locators(self) -> list["SeleniumElementLocator"]:
        """One locator per currently-visible match, for comparing whether
        several visible matches are genuinely different elements or just
        repeated copies of the same one (e.g. a product card repeated in a
        main grid and a recommended-items carousel)."""
        return [
            SeleniumElementLocator(self.page, self.selector, element=el)
            for el in self._elements()
            if el.is_displayed()
        ]

    @property
    def first(self) -> "SeleniumLocator":
        return SeleniumLocator(self.page, self.selector)

    def nth(self, index: int) -> "SeleniumElementLocator":
        elements = self._elements()
        if index >= len(elements):
            raise NoSuchElementException(f"{self.selector}[{index}]")
        return SeleniumElementLocator(self.page, elements[index])

    def is_visible(self) -> bool:
        try:
            return self._element().is_displayed()
        except NoSuchElementException:
            return False

    def is_enabled(self) -> bool:
        return self._element().is_enabled()

    def click(self, timeout: int = 0) -> None:
        element = self._element()
        if timeout:
            WebDriverWait(self.page.driver, timeout / 1000).until(lambda _: element.is_displayed())
        # Two click mechanisms are available, chosen per-app via
        # AppConfig.click_strategy (see its own docstring for why this is a
        # per-app setting, not a single framework default): "native" is
        # WebElement.click(), long-proven safe on automationexercise.com.
        # "action_chains" is a real pointer move-then-click via the W3C
        # Actions API -- confirmed live, nine consecutive full login cycles
        # plus every cart/menu/checkout control that native click had been
        # intermittently silent on, all with zero failures on
        # saucedemo.com. Its pointer movement also triggers CSS :hover
        # states along the way, which is exactly what regressed
        # automationexercise.com's hover-reveal product-grid overlays when
        # this was tried as an unconditional global default -- so it is
        # opt-in per app, never assumed.
        primary_click = (
            (lambda: ActionChains(self.page.driver).move_to_element(element).click().perform())
            if self.page.click_strategy == "action_chains"
            else element.click
        )
        try:
            self._click_and_verify_effect(element, primary_click)
        except ElementClickInterceptedException:
            # A page overlay (commonly a third-party ad iframe) is sitting on
            # top of the correct, visible element at its current scroll
            # position. Known overlay shapes are proactively dismissed before
            # any click is attempted (see clear_overlays_before_interaction),
            # but ad networks produce too many DOM shapes to enumerate
            # exhaustively. Centering the element in the viewport moves it out
            # from under overlays anchored to the old scroll position; a
            # JS-dispatched click bypasses hit-testing entirely as a last
            # resort, which is safe here because the element was already
            # independently resolved and deterministically verified -- this
            # only changes *how* the already-approved element is clicked.
            self.page.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', inline: 'center'})", element
            )
            try:
                self._click_and_verify_effect(element, primary_click)
            except ElementClickInterceptedException:
                self.page.driver.execute_script("arguments[0].click()", element)

    def _click_and_verify_effect(self, element, primary_click) -> None:
        """Perform primary_click(), then re-fire via a JS-dispatched click if
        it demonstrably had zero effect.

        Confirmed live, repeatedly, on saucedemo.com: a click (either
        mechanism) that raises no exception at all can still silently never
        reach the page's own event handler, non-deterministically -- it
        isn't tied to one specific mechanism or control. There's no
        app-specific "did this work" check available generically, but
        "did the page's DOM change AT ALL in the next moment" is a real,
        app-agnostic signal a totally inert click fails -- a click that
        causes any mutation anywhere (even one unrelated to the intended
        effect) is left alone, so this never double-fires a click that
        actually landed; only a click that changed literally nothing gets
        one JS-dispatched retry.

        One refinement, confirmed live on automationexercise.com: a
        body-wide mutation observer can be satisfied by something entirely
        unrelated to this click (an ad refreshing, an analytics beacon) at
        the exact moment a genuinely-inert click happened to fire, masking
        the failure. Bootstrap's collapse widget (`data-toggle="collapse"`,
        pointing at its target via `href="#id"` or `data-target`) is an
        extremely common, standardized accordion/dropdown pattern -- not
        this one site's own markup -- whose target element gets an `in`
        (Bootstrap 3) or `show` (4/5) class exactly when expanded. When the
        clicked element is one of these, that class is checked directly,
        which is strictly more precise for this specific, common control
        shape than "something, somewhere, changed."
        """
        driver = self.page.driver
        # Confirmed live (bbc.com): a plain aria-expanded toggle button (a
        # slide-out nav menu, no Bootstrap data-toggle at all) is just as
        # susceptible as a collapse widget to the generic mutation-observer
        # check below being satisfied by something else entirely unrelated
        # (here: an ad unit refreshing) before the menu itself finished
        # opening -- same failure mode, different widget shape, so checked
        # the same way: a real, specific state transition, not just "did
        # anything, anywhere, change".
        try:
            collapse_state_before = driver.execute_script(
                "const el = arguments[0];"
                "if (el.getAttribute('data-toggle') === 'collapse') {"
                "  const sel = el.getAttribute('data-target') || el.getAttribute('href');"
                "  if (!sel || !sel.startsWith('#')) return null;"
                "  const t = document.querySelector(sel);"
                "  if (!t) return null;"
                "  return t.classList.contains('in') || t.classList.contains('show');"
                "}"
                "if (el.hasAttribute('aria-expanded')) {"
                "  return el.getAttribute('aria-expanded') === 'true';"
                "}"
                "return null;",
                element,
            )
        except Exception:
            collapse_state_before = None
        try:
            driver.execute_script(
                "window.__shMutated = false;"
                "if (window.__shObserver) { window.__shObserver.disconnect(); }"
                "window.__shObserver = new MutationObserver(() => { window.__shMutated = true; });"
                "window.__shObserver.observe(document.body, {childList: true, subtree: true, attributes: true, characterData: true});"
            )
        except Exception:
            primary_click()
            return
        def retry_if_state_unchanged() -> None:
            if collapse_state_before is None:
                driver.execute_script("arguments[0].click()", element)
                return
            try:
                collapse_state_after = driver.execute_script(
                    "const el = arguments[0];"
                    "if (el.getAttribute('data-toggle') === 'collapse') {"
                    "  const sel = el.getAttribute('data-target') || el.getAttribute('href');"
                    "  const t = document.querySelector(sel);"
                    "  return t ? (t.classList.contains('in') || t.classList.contains('show')) : null;"
                    "}"
                    "if (el.hasAttribute('aria-expanded')) {"
                    "  return el.getAttribute('aria-expanded') === 'true';"
                    "}"
                    "return null;",
                    element,
                )
            except Exception:
                collapse_state_after = collapse_state_before
            if collapse_state_after == collapse_state_before:
                driver.execute_script("arguments[0].click()", element)

        primary_click()
        try:
            # A real navigation (a plain link click, common on
            # automationexercise.com) tears down this page -- including the
            # observer and window.__shMutated -- before this can even run;
            # that's indistinguishable here from "can't check", and a click
            # that just navigated away obviously already had an effect, so
            # any exception here (not just a timeout) is treated the same
            # as "mutation detected, nothing more to do".
            WebDriverWait(driver, 0.5).until(lambda d: d.execute_script("return window.__shMutated === true;"))
            retry_if_state_unchanged()
        except TimeoutException:
            # Confirmed live (bbc.com): this specific menu's own mutation
            # reliably lands around ~595ms -- just past the 0.5s wait above,
            # so this branch fires almost every time, not as a rare edge
            # case. It used to retry-click unconditionally here, skipping
            # the same collapse/aria-expanded check the success path just
            # above already does -- for a toggle control, an unconditional
            # retry click is actively wrong the moment the first click
            # *did* work (just slightly slower than the 0.5s budget): it
            # toggles the control right back to closed. Routed through the
            # same state check instead of assuming "timed out" means
            # "nothing happened yet".
            try:
                retry_if_state_unchanged()
            except Exception:
                pass
        except Exception:
            pass
        else:
            try:
                driver.execute_script("if (window.__shObserver) { window.__shObserver.disconnect(); }")
            except Exception:
                pass

    def hover(self, timeout: int = 0) -> None:
        """A real pointer move onto the element, nothing else -- not a click.
        This is what actually triggers a CSS :hover-revealed overlay (e.g. a
        product card's otherwise-hidden "View Product" link); a plain click
        on something still invisible at that instant would just fail
        verification, since hovering and clicking are genuinely different
        gestures, not one a mouse click can substitute for.
        """
        element = self._element()
        if timeout:
            WebDriverWait(self.page.driver, timeout / 1000).until(lambda _: element.is_displayed())
        ActionChains(self.page.driver).move_to_element(element).perform()

    def fill(self, value: str, timeout: int = 0) -> None:
        element = self._element()
        if timeout:
            WebDriverWait(self.page.driver, timeout / 1000).until(lambda _: element.is_displayed())
        element.clear()
        element.send_keys(value)
        if value and element.get_attribute("value") != value:
            # Confirmed live, twice, on saucedemo.com: a React-controlled
            # input can silently accept zero characters from send_keys
            # (no exception, field just stays empty) on any page reached by
            # a navigation other than the session's very first one -- seen
            # on both a checkout form and, intermittently, the login form
            # itself, consistent with something about a repeat/bfcache-style
            # page load leaving the framework's native keyboard-event
            # dispatch not actually wired to React's input handler. Setting
            # the value through the element's own native property setter and
            # dispatching a real 'input' event is the same mechanism React's
            # own code uses internally, so it reaches the controlled
            # component correctly regardless of how the page was reached.
            # Only used when send_keys demonstrably left the field wrong, so
            # a normally-functioning input (the common case, and the only
            # case ever seen on automationexercise.com) takes no extra action.
            self.page.driver.execute_script(
                "const el = arguments[0], v = arguments[1];"
                "const proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype"
                " : window.HTMLInputElement.prototype;"
                "const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;"
                "setter.call(el, v);"
                "el.dispatchEvent(new Event('input', { bubbles: true }));"
                "el.dispatchEvent(new Event('change', { bubbles: true }));",
                element, value,
            )

    def select_option(self, value: str, timeout: int = 0) -> None:
        select = Select(self._element())
        try:
            select.select_by_value(value)
        except Exception:
            # Test Data commonly names an option by its real attribute value
            # (e.g. a country/date dropdown where "United States" IS the
            # value), but a step can just as naturally name it by what's
            # actually on screen instead (e.g. a sort dropdown whose "Price
            # (low to high)" option has value="lohi") -- falling back to the
            # visible-text match covers that shape too, generically, for any
            # <select> on any site.
            select.select_by_visible_text(value)

    def check(self, timeout: int = 0) -> None:
        element = self._element()
        if not element.is_selected():
            element.click()

    def set_input_files(self, file_path: str, timeout: int = 0) -> None:
        self._element().send_keys(file_path)

    def wait_for(self, state: str = "visible", timeout: int = 5000) -> None:
        seconds = timeout / 1000
        if state == "visible":
            WebDriverWait(self.page.driver, seconds).until(lambda _: self.is_visible())
        elif state == "hidden":
            WebDriverWait(self.page.driver, seconds).until(lambda _: not self.is_visible())
        else:
            WebDriverWait(self.page.driver, seconds).until(lambda _: self.count() > 0)

    def evaluate(self, expression: str) -> Any:
        expression = expression.strip()
        if expression.startswith("el =>") or expression.startswith("(el) =>"):
            return self.page.driver.execute_script(
                f"return ({expression})(arguments[0])", self._element()
            )
        return self.page.driver.execute_script(
            "return arguments[0]." + expression, self._element()
        )


@dataclass
class SeleniumElementLocator(SeleniumLocator):
    element: WebElement | None = None

    def _element(self) -> WebElement:
        if self.element is None:
            return super()._element()
        return self.element

    def _elements(self) -> list[WebElement]:
        return [self._element()]


class SeleniumPage:
    def __init__(self, driver: WebDriver, click_strategy: str = "native"):
        self.driver = driver
        # Which mechanism SeleniumLocator.click() uses by default -- set
        # once per run from the app's own config (AppConfig.click_strategy),
        # never hardcoded here. See click()'s own docstring/comments for why
        # this is a per-app choice, not a single framework-wide default.
        self.click_strategy = click_strategy

    @property
    def url(self) -> str:
        return self.driver.current_url

    def native_validation_messages(self) -> list[str]:
        """Text of every currently-invalid form control's native browser
        tooltip (e.g. "Please fill out this field"). This is standard HTML5
        constraint validation (':invalid' + '.validationMessage'), present on
        any form on any site -- not a page-content check, since the tooltip
        itself is rendered by the browser's own UI, outside the DOM a normal
        text-visibility search can see at all."""
        return self.driver.execute_script(
            "return [...document.querySelectorAll(':invalid')]"
            ".map(el => el.validationMessage).filter(Boolean);"
        )

    def goto(self, url: str, timeout: int = 30000) -> None:
        self.driver.set_page_load_timeout(timeout / 1000)
        self.driver.get(url)

    def locator(self, selector: str) -> SeleniumLocator:
        return SeleniumLocator(self, selector)

    def get_by_text(self, text: str, exact: bool = False) -> SeleniumLocator:
        # Case-insensitive: Expected Result text in the workbook is free-form
        # prose (often ALL-CAPS for emphasis, e.g. "ACCOUNT CREATED!") and
        # rarely matches a real page's actual rendered casing (e.g. "Account
        # Created!") exactly -- a case-sensitive match would wrongly treat
        # that styling difference as the text being absent. XPath 1.0 has no
        # lower-case() function, so translate() is the standard idiom.
        # Real pages commonly use &nbsp; (U+00A0, non-breaking space) around
        # dynamic content for layout reasons -- it renders identically to a
        # regular space but XPath's normalize-space() does not treat it as
        # whitespace, so it survives untouched and silently breaks matches
        # against a search string that has an ordinary space there (e.g.
        # automationexercise's "Brand -\xa0Polo Products"). Fold it to a
        # regular space first so normalize-space() can then collapse it.
        _upper, _lower = "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
        nbsp_folded = "translate(., ' ', ' ')"
        normalized_node_text = (
            f"translate(normalize-space({nbsp_folded}), '{_upper}', '{_lower}')"
        )
        lowered_text = xpath_literal(text.lower())
        condition = f"{normalized_node_text}={lowered_text}" if exact else (
            f"contains({normalized_node_text}, {lowered_text})"
        )
        return SeleniumLocator(self, f"xpath=//*[{condition}]")

    def evaluate(self, script: str, arg: Any = None) -> Any:
        script = script.strip()
        if script.startswith("xpath="):
            return self.driver.execute_script(script[6:], arg)
        if script.endswith("})();"):
            return self.driver.execute_script(f"return {script}", arg)
        if "=>" in script and script.startswith(("(", "async")):
            return self.driver.execute_script(f"return ({script})(arguments[0])", arg)
        return self.driver.execute_script(script, arg)

    def wait_for_load_state(self, state: str, timeout: int = 5000) -> None:
        WebDriverWait(self.driver, timeout / 1000).until(
            lambda d: d.execute_script("return document.readyState") in {"interactive", "complete"}
        )

    def wait_for_url(self, pattern: str, timeout: int = 5000) -> None:
        fragment = pattern.replace("**", "")
        WebDriverWait(self.driver, timeout / 1000).until(lambda d: fragment in d.current_url)

    def keyboard_press(self, key: str) -> None:
        body = self.driver.find_element(By.TAG_NAME, "body")
        body.send_keys(Keys.ESCAPE if key.lower() == "escape" else key)

    def screenshot(self, path: str) -> None:
        self.driver.save_screenshot(path)

    def close(self) -> None:
        self.driver.quit()
