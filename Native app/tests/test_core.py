"""Offline tests: no network, browser or LLM needed."""

import pytest

from healtest.executor import DataBag, Executor
from healtest.page import similarity
from healtest.security import AllowList, Masker, PaymentGuard, SecurityError
from healtest.store import ElementStore


def test_allow_list_refuses_other_hosts():
    allow = AllowList(["www.example.com", "*.test.org"])
    allow.check("https://www.example.com/x")
    allow.check("https://a.test.org")
    with pytest.raises(SecurityError):
        allow.check("https://evil.com")
    with pytest.raises(SecurityError):
        allow.check("file:///etc/passwd")


def test_masker_hides_secrets_and_personal_data():
    m = Masker()
    m.add_secret("Test@1234")
    out = m.mask("pw Test@1234 mail a.b@c.com card 4111 1111 1111 1111")
    assert "Test@1234" not in out and "a.b@c.com" not in out and "4111" not in out


def test_payment_guard():
    PaymentGuard(True, ["4111111111111111"]).check("type", "card number input", "4111111111111111")
    with pytest.raises(SecurityError):
        PaymentGuard(True, ["4111111111111111"]).check("type", "card number input", "5500000000000004")
    with pytest.raises(SecurityError):
        PaymentGuard(False, []).check("click", "'Pay and Confirm Order' button")
    PaymentGuard(False, []).check("click", "'Login' button")


def test_databag_placeholders():
    bag = DataBag("Name: QA Tester\nDOB: 10-May-1995\nExpiry: 12/2028\nEmail: a+<timestamp>@x.com\nPass: (blank)", "99", "acct@x.com")
    assert bag.resolve("{{Name.first}} {{Name.last}}") == "QA Tester"
    assert bag.resolve("{{DOB.day}}-{{DOB.month}}-{{DOB.year}}") == "10-May-1995"
    assert bag.resolve("{{Expiry.month}}/{{Expiry.year}}") == "12/2028"
    assert bag.resolve("{{Email}}") == "a+99@x.com"
    assert bag.resolve("[{{Pass}}]") == "[]"
    with pytest.raises(KeyError):
        bag.resolve("{{Missing}}")


def test_expected_phrases_are_deterministic():
    assert Executor.expected_phrases("'ORDER PLACED!' shown; Total = Rs. 2000") == ["ORDER PLACED!", "Rs. 2000"]
    assert Executor.expected_phrases("menu (Home, Products, Cart) visible") == ["Home", "Products", "Cart"]


def test_similarity():
    fp = {"tag": "button", "text": "Login", "attrs": {"id": "go"}}
    assert similarity(fp, dict(fp)) == 1.0
    assert similarity(fp, {"tag": "a", "text": "Login", "attrs": {"id": "go"}}) == 0.0
    assert similarity(fp, {"tag": "button", "text": "Other", "attrs": {"id": "x"}}) < 0.5


def test_store_versioning_and_review(tmp_path):
    store = ElementStore(str(tmp_path), "app", "prod", auto_approve=False)
    loc, fp = {"css": "#a"}, {"tag": "a", "text": "A", "attrs": {}}
    e = store.learn("Home [/]", "A link", loc, fp, "r1", "m")
    store.propose_heal(e["key"], {"css": "#b"}, {"tag": "a", "text": "B", "attrs": {}}, "r2", "m", "test")
    assert store.get("Home [/]", "A link")["locators"] == {"css": "#a"}      # old stays active until reviewed
    assert store.approve(e["key"])
    entry = store.get("Home [/]", "A link")
    assert entry["locators"] == {"css": "#b"} and entry["version"] == 2 and len(entry["history"]) == 1
    assert store.invalidate_page("home") == 1 and not store.data


def test_store_is_separated_by_environment(tmp_path):
    a = ElementStore(str(tmp_path), "app", "prod")
    b = ElementStore(str(tmp_path), "app", "staging")
    a.learn("P", "T", {}, {"tag": "a"}, "r", "m")
    assert b.get("P", "T") is None
