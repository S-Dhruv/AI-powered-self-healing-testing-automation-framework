from framework.resolver.cache import try_cache
from framework.resolver.fallback import try_fallback
from framework.resolver.fingerprint import build_fingerprint
from framework.resolver.verifier import verify_locator
from framework.store.backend import ElementStore
from framework.store.models import Strategy


def test_verify_locator_accepts_unique_visible_compatible_element(login_page):
    result = verify_locator(
        login_page, "[data-testid='login-email-input']", "type", "Email Address field"
    )
    assert result.ok, result.reason


def test_verify_locator_rejects_wrong_action_compatibility(login_page):
    result = verify_locator(
        login_page, "[data-testid='login-submit-button']", "type", "Login button"
    )
    assert not result.ok
    assert "not compatible" in result.reason


def test_verify_locator_rejects_implausible_description(login_page):
    result = verify_locator(
        login_page, "[data-testid='login-email-input']", "type", "Country dropdown selector"
    )
    assert not result.ok
    assert "not plausibly consistent" in result.reason


def test_verify_locator_rejects_multiple_matches(login_page):
    # 'input' matches both email and password fields; guessing is forbidden.
    result = verify_locator(login_page, "input", "type", "Email Address field")
    assert not result.ok
    assert "multiple different visible elements" in result.reason


def test_cache_hit_when_primary_strategy_resolves(login_page, tmp_path):
    store = ElementStore(tmp_path / "store.db")
    locator = login_page.locator("[data-testid='login-email-input']")
    fp = build_fingerprint(locator)
    store.insert(
        application="demo", environment="test", page="Login", description="Email Address field",
        strategies=[Strategy(kind="test_id", value="login-email-input")],
        fingerprint=fp, status="active", source="AI", run_id="run-1",
    )

    result = try_cache(
        store, login_page, application="demo", environment="test", target_page="Login",
        description="Email Address field", action="type", min_similarity=0.80,
    )
    assert result.hit, result.reason
    assert result.tier == "Cache"
    store.close()


def test_cache_miss_falls_through_to_fallback_on_changed_primary(login_page, tmp_path):
    store = ElementStore(tmp_path / "store.db")
    locator = login_page.locator("[data-testid='login-email-input']")
    fp = build_fingerprint(locator)
    # Primary strategy intentionally wrong (simulates element id/testid change).
    record = store.insert(
        application="demo", environment="test", page="Login", description="Email Address field",
        strategies=[
            Strategy(kind="test_id", value="no-longer-exists"),
            Strategy(kind="css", value="[data-testid='login-email-input']"),
        ],
        fingerprint=fp, status="active", source="AI", run_id="run-1",
    )

    cache_result = try_cache(
        store, login_page, application="demo", environment="test", target_page="Login",
        description="Email Address field", action="type", min_similarity=0.80,
    )
    assert not cache_result.hit

    fallback_result = try_fallback(
        store, login_page, record=cache_result.record, description="Email Address field",
        action="type", min_similarity=0.80,
    )
    assert fallback_result.hit, fallback_result.reason
    assert fallback_result.tier == "Fallback"
    store.close()


def test_zero_tokens_tiers_never_import_llm():
    # Tiers 1-2 must never reach for the LLM. Enforced structurally: these
    # modules import nothing from framework.llm.
    import framework.resolver.cache as cache_mod
    import framework.resolver.fallback as fallback_mod

    assert "framework.llm" not in cache_mod.__dict__.get("__loader__", "").__str__()
    for mod in (cache_mod, fallback_mod):
        src_path = mod.__file__
        with open(src_path, encoding="utf-8") as f:
            assert "framework.llm" not in f.read()
