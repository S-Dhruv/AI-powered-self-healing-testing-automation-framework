"""Resolver flow on the real site with a FAKE LLM (no API credits needed):
learn once -> replay with zero tokens -> heal only when the stored element is broken."""

import re

import pytest

from healtest.browser import BrowserManager
from healtest.llm import CallContext, Identification
from healtest.resolver import ElementResolver
from healtest.security import AllowList
from healtest.settings import Settings
from healtest.store import ElementStore

URL = "https://www.automationexercise.com/login"


class FakeLLM:
    model, min_confidence = "fake", 0.7

    def __init__(self):
        self.calls = 0

    def identify(self, ctx, target, action, page, lines, reason, previous="", feedback=""):
        self.calls += 1
        words = [w for w in re.findall(r"[a-z]+", target.lower()) if w not in ("input", "button", "in", "the", "form", "to", "your")]
        best = max(range(len(lines)), key=lambda i: sum(w in lines[i].lower() for w in words))
        return Identification(ok=True, index=best, confidence=0.95, input_tokens=100, output_tokens=10)


@pytest.fixture(scope="module")
def browser():
    cfg = Settings("config").environment("automationexercise", "production")
    b = BrowserManager(AllowList(["www.automationexercise.com"]), headless=True, block_hosts=cfg["block_hosts"])
    b.start()
    b.navigate_to(URL)
    yield b
    b.close()


def test_learn_replay_heal(browser, tmp_path):
    store = ElementStore(str(tmp_path), "automationexercise", "production")
    llm = FakeLLM()
    ctx = CallContext(run_id="t")
    page = "Login [/login]"
    target = "'Login' button in the 'Login to your account' form"
    ok = lambda e: e.is_displayed()

    def resolver():
        return ElementResolver(browser.driver, store, llm, {"lookup_wait_seconds": 0}, "t")

    first = resolver().resolve(ctx, target, "click", page, ok)
    assert first.source == "AI" and first.tokens == 110 and llm.calls == 1

    second = resolver().resolve(ctx, target, "click", page, ok)
    assert second.source == "Cache" and second.tokens == 0 and llm.calls == 1

    entry = store.get(page, target)
    entry["locators"] = {"css": "#gone"}                                 # locators broken, fingerprint intact
    third = resolver().resolve(ctx, target, "click", page, ok)
    assert third.source == "Fallback" and third.tokens == 0 and llm.calls == 1

    entry["fingerprint"] = {"tag": "button", "text": "changed", "attrs": {"id": "changed"}}   # element really changed
    fourth = resolver().resolve(ctx, target, "click", page, ok)
    assert fourth.source == "Healed" and llm.calls == 2
    assert store.get(page, target)["pending"] is not None                # awaits review, old reference kept

    fifth = resolver().resolve(ctx, target, "click", page, ok)           # replay uses the pending reference, still free
    assert fifth.source == "Healed" and fifth.tokens == 0 and llm.calls == 2


def test_refuses_to_guess(browser, tmp_path):
    class Unsure(FakeLLM):
        def identify(self, *a, **k):
            return Identification(ok=True, index=0, confidence=0.3, input_tokens=50, output_tokens=5)

    store = ElementStore(str(tmp_path), "automationexercise", "production")
    res = ElementResolver(browser.driver, store, Unsure(), {"lookup_wait_seconds": 0}, "t").resolve(
        CallContext(run_id="t"), "something vague", "click", "Login [/login]", lambda e: True)
    assert res.source == "Blocked" and "refusing to guess" in res.reason and not store.data
