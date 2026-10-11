from framework.config.allowlist import AllowListGuard
from framework.excel.reader import TestCaseRow
from framework.executor import execute_test_case
from framework.reporting.console import ConsoleNarrator
from framework.store.backend import ElementStore
from framework.store.review import ReviewQueue
from tests.fakes import FakeLlmClient, ScriptedResponse


def _row(test_step, test_data="", expected_result="", execute=True, tc_id="TC_001"):
    return TestCaseRow(
        row_index=2, test_case_id=tc_id, title="t", test_step=test_step,
        test_data=test_data, expected_result=expected_result, execute=execute,
    )


def _run(row, llm, login_page, tmp_path):
    store = ElementStore(tmp_path / "store.db")
    queue = ReviewQueue(store)
    guard = AllowListGuard(["localhost"])  # file:// fixture pages have no host; navigate not used here
    result = execute_test_case(
        row, page=login_page, store=store, review_queue=queue, llm_client=llm, allow_guard=guard,
        application="demo", environment="test", page_name="Login", base_url="https://example.test",
        run_id="run-1", user_id="qa",
        min_similarity=0.80, min_confidence=0.85, healing_auto_approve=True,
        action_timeout_ms=5000, navigation_timeout_ms=5000, console=ConsoleNarrator(enabled=False),
    )
    store.close()
    return result


def test_navigate_to_unqualified_target_uses_configured_base_url():
    """Regression for the bug found in the real TC_002 run against the live
    site: 'Navigate to home page' has no literal URL in the step text, so it
    must resolve against base_url -- not get passed through as a literal
    non-URL string ('to home page') and rejected by the allow-list guard."""
    from framework.excel.step_parser import parse_test_steps
    from framework.executor import resolve_navigate_target

    sub = parse_test_steps("1. Navigate to home page")[0]
    assert resolve_navigate_target(sub.target, "https://www.automationexercise.com/") == (
        "https://www.automationexercise.com/"
    )

    sub2 = parse_test_steps("1. Navigate to https://www.automationexercise.com")[0]
    assert resolve_navigate_target(sub2.target, "https://www.automationexercise.com/") == (
        "https://www.automationexercise.com"
    )


def test_split_multi_field_targets_handles_post_verb_stripped_text():
    """Regression: the step parser already strips the leading verb ('Fill') off
    the target before this function ever sees it, so split must key off the
    comma-separated shape, not a 'Fill ' prefix that is never actually present."""
    from framework.executor import split_multi_field_targets

    assert split_multi_field_targets("Title, Password, DOB, Mobile") == [
        "Title", "Password", "DOB", "Mobile",
    ]
    assert split_multi_field_targets("Email Address field") == ["Email Address field"]


def test_split_multi_field_targets_splits_on_and_not_just_comma():
    """Regression: 'registered email and correct password' and 'name and new
    email' have no comma, so they were being resolved as ONE element and had
    one value typed into it -- sometimes the wrong value, since both words
    could match either Test Data label. Splitting on "and" too fixes this."""
    from framework.executor import split_multi_field_targets

    assert split_multi_field_targets("registered email and correct password") == [
        "registered email", "correct password",
    ]
    assert split_multi_field_targets("name and new email") == ["name", "new email"]
    assert split_multi_field_targets("Email Address field") == ["Email Address field"]


def test_extract_typed_value_isolates_email_and_password_after_split():
    """Proves the actual bug scenario end-to-end at the matching layer: once
    split, each field independently matches its own Test Data label and never
    the other field's value."""
    from framework.executor import _extract_typed_value, split_multi_field_targets

    test_data = "Email: user@test.com\nPassword: Wrong@999"
    targets = split_multi_field_targets("registered email and correct password")
    values = {t: _extract_typed_value(t, test_data)[0] for t in targets}
    assert values["registered email"] == "user@test.com"
    assert values["correct password"] == "Wrong@999"


def test_generated_email_is_deterministic_per_run():
    from framework.executor import _extract_typed_value

    value, detail = _extract_typed_value(
        "new email", "Email: ${GENERATED_EMAIL}", run_id="run-123"
    )
    assert value.startswith("qa.tester+")
    assert value.endswith("@test.com")
    assert detail.startswith("matched Test Data label")


def test_type_target_removes_value_instruction():
    from framework.executor import _normalise_type_target

    assert _normalise_type_target("quantity to 4") == "quantity"
    assert _normalise_type_target("an invalid keyword") == "keyword"


def test_signup_composite_fields_use_control_specific_actions():
    from framework.executor import _expand_field_target, _extract_typed_value, _field_action

    data = "DOB: 10-May-1995\nZip: 110001\nCountry: India"
    assert _field_action("DOB day") == "select"
    assert _field_action("Country") == "select"
    assert _field_action("Title") == "check"
    assert _field_action("Zipcode") == "type"
    assert _expand_field_target("DOB", data) == [
        ("DOB day", "10"),
        ("DOB month", "5"),
        ("DOB year", "1995"),
    ]
    assert _extract_typed_value("Zipcode", data)[0] == "110001"


def test_workbook_email_placeholders_use_same_run_scoped_email():
    from framework.executor import _extract_typed_value

    values = [
        _extract_typed_value("new email", "Email: qa.tester+<timestamp>@test.com", run_id="run-123")[0],
        _extract_typed_value("registered email", "Email: <registered email>", run_id="run-123")[0],
        _extract_typed_value(
            "previously registered email",
            "Email: <previously registered email>",
            run_id="run-123",
        )[0],
    ]
    assert values[0] == values[1] == values[2]


def test_type_and_click_resolve_and_pass(login_page, tmp_path):
    row = _row(
        "1. Enter email in Email Address field\n2. Click 'Login'",
        test_data="Email: qa@test.com",
        expected_result="",
    )
    llm = FakeLlmClient([
        ScriptedResponse(selector="[data-testid='login-email-input']", strategy="test_id", confidence=0.95),
        ScriptedResponse(selector="[data-testid='login-submit-button']", strategy="test_id", confidence=0.95),
    ])
    result = _run(row, llm, login_page, tmp_path)
    assert result.status == "Pass"
    assert result.element_source == "AI"


def test_wrong_expected_result_is_fail_never_heal_or_pass(login_page, tmp_path):
    """Spec R5 / verification integrity: a deliberately wrong Expected Result
    must produce Fail, never trigger a heal and never be converted to a pass."""
    row = _row(
        "1. Enter email in Email Address field",
        test_data="Email: qa@test.com",
        expected_result="'This text will never appear on the login fixture page' is displayed",
    )
    llm = FakeLlmClient([
        ScriptedResponse(selector="[data-testid='login-email-input']", strategy="test_id", confidence=0.95),
    ])
    result = _run(row, llm, login_page, tmp_path)
    assert result.status == "Fail"
    assert "expected check failed" in result.actual_result
    assert llm.calls == 1  # only the element lookup call -- expected result never sent to the LLM


def test_unparseable_step_blocks_and_stops_remaining_steps(login_page, tmp_path):
    row = _row("1. Try something weird\n2. Click 'Login'")
    llm = FakeLlmClient([])  # must not be called at all
    result = _run(row, llm, login_page, tmp_path)
    assert result.status == "Blocked"
    assert "could not parse step grammar" in result.actual_result
    assert "remaining sub-steps skipped" in result.actual_result
    assert llm.calls == 0


def test_low_confidence_blocks_never_clicks_real_element(login_page, tmp_path):
    row = _row("1. Click 'Login'")
    llm = FakeLlmClient([
        ScriptedResponse(selector="[data-testid='login-submit-button']", strategy="test_id", confidence=0.10),
        ScriptedResponse(selector="[data-testid='login-submit-button']", strategy="test_id", confidence=0.10),
    ])
    result = _run(row, llm, login_page, tmp_path)
    assert result.status == "Blocked"
