from framework.reporting.console import ConsoleNarrator
from framework.resolver.resolve import resolve_element
from framework.store.backend import ElementStore
from framework.store.review import ReviewQueue
from tests.fakes import FakeLlmClient, ScriptedResponse


def _resolve(store, queue, llm, page, description="Email Address field", action="type", run_id="run-1"):
    return resolve_element(
        store=store, review_queue=queue, llm_client=llm, page=page,
        application="demo", environment="test", target_page="Login", description=description,
        action=action, run_id=run_id, test_case_id="TC_001", step_no=1, user_id="qa",
        min_similarity=0.80, min_confidence=0.85, healing_auto_approve=True,
        console=ConsoleNarrator(enabled=False),
    )


def test_first_learning_calls_llm_and_stores_active(login_page, tmp_path):
    store = ElementStore(tmp_path / "store.db")
    queue = ReviewQueue(store)
    llm = FakeLlmClient([ScriptedResponse(
        selector="[data-testid='login-email-input']", strategy="test_id", confidence=0.95,
    )])

    outcome = _resolve(store, queue, llm, login_page)

    assert outcome.status == "Resolved"
    assert outcome.element_source == "AI"
    assert outcome.tokens_used == 160
    assert llm.calls == 1
    record = store.get_active("demo", "test", "Login", "Email Address field")
    assert record is not None and record.status == "active"
    store.close()


def test_replay_run_uses_zero_tokens(login_page, tmp_path):
    """Spec R3: a repeat run on an unchanged app must use 0 LLM tokens."""
    store = ElementStore(tmp_path / "store.db")
    queue = ReviewQueue(store)
    llm = FakeLlmClient([ScriptedResponse(
        selector="[data-testid='login-email-input']", strategy="test_id", confidence=0.95,
    )])

    run1 = _resolve(store, queue, llm, login_page, run_id="run-1")
    assert run1.tokens_used == 160

    run2 = _resolve(store, queue, llm, login_page, run_id="run-2")
    assert run2.tokens_used == 0
    assert run2.element_source == "Cache"
    assert llm.calls == 1  # LLM never called again
    store.close()


def test_low_confidence_blocks_never_clicks(login_page, tmp_path):
    store = ElementStore(tmp_path / "store.db")
    queue = ReviewQueue(store)
    # Retry logic gives the LLM one corrective retry; scripted to fail both times
    # so the test still proves persistent low confidence ends in Blocked, never a guess.
    llm = FakeLlmClient([
        ScriptedResponse(selector="[data-testid='login-email-input']", strategy="test_id", confidence=0.40),
        ScriptedResponse(selector="[data-testid='login-email-input']", strategy="test_id", confidence=0.40),
    ])

    outcome = _resolve(store, queue, llm, login_page)

    assert outcome.status == "Blocked"
    assert llm.calls == 2  # original attempt + one retry with corrective feedback
    assert store.get_active("demo", "test", "Login", "Email Address field") is None


def test_ambiguous_llm_selector_is_blocked(login_page, tmp_path):
    # LLM returns "input" which matches 2 elements; the resolver retries and
    # then blocks rather than guessing.
    store = ElementStore(tmp_path / "store.db")
    queue = ReviewQueue(store)
    llm = FakeLlmClient([
        ScriptedResponse(selector="input", strategy="css", confidence=0.95),
        ScriptedResponse(selector="input", strategy="css", confidence=0.95),
    ])

    outcome = _resolve(store, queue, llm, login_page)

    assert outcome.status == "Blocked"
    assert llm.calls == 2
    assert store.get_active("demo", "test", "Login", "Email Address field") is None


def test_retry_succeeds_after_first_attempt_fails_verification(login_page, tmp_path):
    """Proves the retry is a legitimate second attempt that still goes through
    full verification -- not a bypass. First proposal is an incompatible <h2>-like
    bad selector; the retry proposes the correct, verifiable input."""
    store = ElementStore(tmp_path / "store.db")
    queue = ReviewQueue(store)
    llm = FakeLlmClient([
        ScriptedResponse(selector="form#login-form", strategy="css", confidence=0.90),  # wrong: not typeable
        ScriptedResponse(selector="[data-testid='login-email-input']", strategy="test_id", confidence=0.95),
    ])

    outcome = _resolve(store, queue, llm, login_page)

    assert outcome.status == "Resolved"
    assert llm.calls == 2
    assert store.get_active("demo", "test", "Login", "Email Address field") is not None


def test_heal_auto_approve_supersedes_old_version(login_page, tmp_path):
    store = ElementStore(tmp_path / "store.db")
    queue = ReviewQueue(store)

    llm1 = FakeLlmClient([ScriptedResponse(
        selector="[data-testid='login-email-input']", strategy="test_id", confidence=0.95,
    )])
    first = _resolve(store, queue, llm1, login_page, run_id="run-1")
    assert first.element_source == "AI"
    old_active = store.get_active("demo", "test", "Login", "Email Address field")

    # Simulate the element changing: invalidate so cache/fallback miss, forcing a heal.
    store.invalidate(application="demo", page="Login", description="Email Address field")

    llm2 = FakeLlmClient([ScriptedResponse(
        selector="[data-testid='login-email-input']", strategy="test_id", confidence=0.95,
    )])
    healed = _resolve(store, queue, llm2, login_page, run_id="run-2")

    assert healed.element_source == "Healed"
    new_active = store.get_active("demo", "test", "Login", "Email Address field")
    assert new_active.status == "active"
    assert new_active.version == 2
    assert store.get_by_id(old_active.id).status == "invalidated"  # untouched by heal, already invalidated
