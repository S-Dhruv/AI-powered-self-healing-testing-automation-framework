from framework.excel.step_parser import parse_test_steps


def test_parses_launch_navigate_click_sequence():
    cell = "1. Launch browser\n2. Navigate to https://www.automationexercise.com\n3. Observe the page"
    steps = parse_test_steps(cell)
    assert [s.action for s in steps] == ["noop", "navigate", "assert_visible"]
    assert steps[1].target == "https://www.automationexercise.com"


def test_navigate_with_no_literal_url_has_no_url_in_target():
    """Regression: 'Navigate to home page' has no http(s) token. The parser must
    not fabricate one -- it returns the free-text phrase, and it is the
    executor's job (not this parser) to resolve that against the configured
    base_url rather than ever treating free text as a navigable URL itself."""
    steps = parse_test_steps("1. Navigate to home page")
    assert steps[0].action == "navigate"
    assert steps[0].target == "to home page"


def test_parses_click_with_quoted_target():
    steps = parse_test_steps("1. Click 'Signup / Login'")
    assert steps[0].action == "click"
    assert steps[0].target == "Signup / Login"


def test_parses_enter_field_under_quoted_section_prefers_field_over_section_name():
    # Regression: a real run showed the old "quoted always wins" rule extracting
    # the section heading ('New User Signup!') as the target instead of the
    # actual fields to fill ('name and new email'), which made the LLM hunt for
    # a non-interactive heading and block every registration/login test case.
    # The connector ("under"/"in") now wins when real text precedes it.
    steps = parse_test_steps("3. Enter name and new email under 'New User Signup!'")
    assert steps[0].action == "type"
    assert steps[0].target == "name and new email"
    assert steps[0].context == "New User Signup!"


def test_parses_click_quoted_target_with_no_connector_still_uses_quote():
    steps = parse_test_steps("1. Click 'Login to your account'")
    assert steps[0].action == "click"
    assert steps[0].target == "Login to your account"


def test_parses_enter_field_with_connector_when_no_quotes():
    steps = parse_test_steps("1. Enter email in search box")
    assert steps[0].action == "type"
    assert steps[0].target == "search box"


def test_parses_search_field_after_value_connector():
    steps = parse_test_steps("2. Enter keyword in search box")
    assert steps[0].target == "search box"


def test_hover_and_click_splits_into_a_real_hover_then_a_click():
    # "Hover X and click 'Y'" is two actions, not one: a real pointer move
    # onto X (revealing any hover-only overlay underneath it) followed by a
    # separate click on Y. Confirmed live (automationexercise.com): some
    # targets -- a product's "View Product" link, unlike its "Add to cart"
    # button -- have no always-visible copy at all, so folding "hover" into
    # a no-op prefix of Y's own resolution (the previous behavior) left them
    # structurally unreachable no matter how the step was worded.
    steps = parse_test_steps("2. Hover first product and click 'Add to cart'")
    assert steps[0].action == "hover"
    assert steps[0].target == "first product"
    assert steps[1].action == "click"
    assert steps[1].target == "Add to cart"


def test_bare_hover_is_its_own_action_not_a_click_synonym():
    steps = parse_test_steps("2. Hover over the menu icon")
    assert steps[0].action == "hover"


def test_change_maps_to_type():
    steps = parse_test_steps("2. Change quantity to 4")
    assert steps[0].action == "type"


def test_leave_maps_to_noop():
    steps = parse_test_steps("2. Leave Email and Password empty")
    assert steps[0].action == "noop"


def test_scroll_maps_to_assert_visible_checkpoint():
    steps = parse_test_steps("1. Scroll to footer on home page")
    assert steps[0].action == "assert_visible"


def test_add_maps_to_click():
    steps = parse_test_steps("1. Add a product to cart")
    assert steps[0].action == "click"


def test_bare_login_is_auto_login_not_unparseable():
    # A bare "Login" precondition names a real, performable action (run the
    # app's declarative login_steps with a cached/configured test account) --
    # not just something to wave through as unresolvable.
    steps = parse_test_steps("1. Login with valid credentials")
    assert steps[0].action == "auto_login"


def test_bare_logout_precondition_is_unparseable_with_explanatory_reason():
    # "logout" has no analogous auto-action (there's nothing to log out of
    # without already being logged in via a preceding auto_login row/step),
    # so it keeps the original explanatory-Blocked behavior.
    steps = parse_test_steps("1. Logout")
    assert steps[0].action == "unparseable"
    assert "cross-test-case session precondition" in steps[0].target


def test_login_with_quoted_target_does_not_get_precondition_message():
    # "login" is still not a mapped verb (it's never a resolvable UI action on
    # its own), but when a quoted target IS present this isn't the ambiguous
    # bare-precondition case, so it should not get that specific explanation.
    steps = parse_test_steps("1. Login 'Submit' button")
    assert steps[0].action == "unparseable"
    assert "cross-test-case session precondition" not in steps[0].target


def test_unrecognized_verb_is_unparseable_not_guessed():
    steps = parse_test_steps("4. Frobnicate the widget")
    assert steps[0].action == "unparseable"


def test_try_logging_in_again_is_attempt_login_not_a_precondition():
    # Distinct from a bare "Login" precondition: the point of this step IS
    # the (expected-to-fail) login attempt, judged by the row's own Expected
    # Result text, not by whether the login itself succeeded.
    steps = parse_test_steps("4. Try logging in again with same credentials")
    assert steps[0].action == "attempt_login"


def test_repeat_with_value_extracts_literal_and_action():
    steps = parse_test_steps("4. Repeat with invalid email 'abc'")
    assert steps[0].action == "repeat_with_value"
    assert steps[0].literal_value == "abc"


def test_blank_lines_ignored():
    steps = parse_test_steps("1. Click 'A'\n\n2. Click 'B'")
    assert len(steps) == 2
