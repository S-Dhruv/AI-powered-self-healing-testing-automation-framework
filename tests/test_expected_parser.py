from framework.excel.expected_parser import parse_expected_result


def test_text_visible_clause():
    checks = parse_expected_result("'ACCOUNT CREATED!' message is displayed")
    assert checks[0].kind == "text_visible"
    assert checks[0].value == "ACCOUNT CREATED!"


def test_multiple_clauses_split_on_semicolon():
    checks = parse_expected_result(
        "Error 'Email Address already exist!' is displayed; user stays on signup page and no account is created."
    )
    assert checks[0].kind == "text_visible"
    assert checks[0].value == "Email Address already exist!"
    # Second clause has no quoted/displayed pattern -> unverifiable, never silently passed.
    assert checks[1].kind == "unverifiable"


def test_header_shows_clause():
    checks = parse_expected_result("header shows 'Logged in as QA Tester'")
    assert checks[0].kind == "text_visible"
    assert checks[0].value == "Logged in as QA Tester"


def test_unmatched_clause_is_unverifiable_never_guessed():
    checks = parse_expected_result("Home page loads within acceptable time")
    assert checks[0].kind == "unverifiable"
